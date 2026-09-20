"""The poll pipeline.

```
fetch -> normalize -> LOCATION + CHEAP GATES -> cluster -> detail -> score
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                      this order is load-bearing
```

Gating before collapsing is what stops dedupe discarding the US copy of a role
and keeping the Canadian one. Detail after clustering is what turns Workday's
N+1 into a handful of requests instead of one per duplicate. Scoring happens
elsewhere, on complete descriptions only.

A source that fails does not fail the run. Each board's outcome is recorded, and
partial results are kept: twelve boards polled with one 500ing is eleven boards'
worth of real data plus a flag, not a discarded run.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from winnow import dedupe, resolver, store
from winnow.dedupe import Cluster, cluster_postings, merge_cluster
from winnow.gates import apply_structured_gates
from winnow.models import Board, Posting
from winnow.sources import adapter_for


@dataclass(frozen=True)
class ScorableCluster:
    """A cluster that survived the gates and is ready to be judged."""

    cluster_id: int
    posting: Posting
    company_id: int


@dataclass(frozen=True)
class PollReport:
    """What one poll established, for the digest to report faithfully."""

    clusters: tuple[ScorableCluster, ...]
    tally: dict[str, int]
    failed_sources: tuple[str, ...]
    suppressed: int
    boards_polled: int


def run_poll(
    conn: sqlite3.Connection,
    profile,
    boards: list[Board],
    *,
    adapter_factory: Callable[[str], object] = adapter_for,
    now: datetime | None = None,
) -> PollReport:
    """Poll every board once and persist what survives.

    Args:
        conn: An open connection.
        profile: The loaded rubric.
        boards: Boards to poll.
        adapter_factory: Builds the adapter for a vendor; injected for tests.
        now: Reference time, passed to every adapter so normalisation is
            reproducible.

    Returns:
        The clusters worth scoring, the rejection tally, and which sources
        failed. A failure is reported rather than raised, because one bad board
        must not cost the run the other eleven.
    """
    now = now or datetime.now(UTC)
    tally: Counter[str] = Counter()
    failed: list[str] = []
    scorable: list[ScorableCluster] = []
    suppressed = 0

    for board in boards:
        started = now.isoformat()
        try:
            # Inside the try: a vendor recorded without an adapter — Teamtailor
            # publishes no unauthenticated JSON — is a board that cannot be
            # polled, which is a reportable outcome rather than a crash.
            adapter = adapter_factory(board.vendor)
            rows = list(adapter.fetch_list(board))
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            _record_run(conn, board, started, "failed", 0, tally={}, error=str(error))
            failed.append(board.vendor)
            continue

        survivors, rejected, board_tally = _gate(rows, adapter, board, profile, conn, now)
        tally.update(board_tally)
        _record_run(conn, board, started, "ok", len(rows), tally=board_tally)
        if board.board_id is not None:
            resolver.record_poll(conn, board.board_id, job_count=len(rows))

        _mark_departed(conn, board, rows, adapter, now)
        _withdraw_rejected(conn, rejected, now)

        for cluster in cluster_postings(survivors):
            persisted, was_suppressed = _persist(conn, cluster, board, adapter, now)
            suppressed += int(was_suppressed)
            if persisted is not None:
                scorable.append(persisted)

    return PollReport(
        clusters=tuple(scorable),
        tally=dict(tally),
        failed_sources=tuple(failed),
        suppressed=suppressed,
        boards_polled=len(boards),
    )


def _mark_departed(
    conn: sqlite3.Connection, board: Board, rows: list[dict], adapter, now: datetime
) -> int:
    """Mark the postings this board used to list and no longer does.

    Without this, three things that read ``disappeared_at`` are inert: repost
    detection, which is one of the few computable ghost-job signals; the
    retention prune; and the digest's ability to notice a role has closed.

    Only a board that answered *with content* can retire anything. A failure
    never reaches here, and a 200-with-zero-jobs is ambiguous enough that the
    resolver already tracks it as staleness — concluding from one empty
    response that a company stopped hiring would hide it entirely.

    Args:
        conn: An open connection.
        board: The board just polled.
        rows: Its raw payloads.
        adapter: The adapter, for normalising ids.
        now: Reference time.

    Returns:
        How many postings were marked.
    """
    if board.board_id is None or not rows:
        return 0

    seen = {str(adapter.normalize(raw, board, now=now).source_id) for raw in rows}
    placeholders = ", ".join("?" for _ in seen)
    cursor = conn.execute(
        "UPDATE postings SET disappeared_at = ? WHERE board_id = ? "
        f"AND disappeared_at IS NULL AND source_id NOT IN ({placeholders})",
        (now.isoformat(), board.board_id, *seen),
    )
    return int(cursor.rowcount)


def _gate(
    rows: list[dict],
    adapter,
    board: Board,
    profile,
    conn: sqlite3.Connection,
    now: datetime,
) -> tuple[list[Posting], dict[str, int]]:
    """Normalise and gate a board's payloads, before anything is collapsed."""
    comp_tier = (
        store.comp_tier(conn, board.company_id) if board.company_id is not None else "mid_market"
    )
    survivors: list[Posting] = []
    rejected: list[Posting] = []
    tally: Counter[str] = Counter()

    for raw in rows:
        posting = adapter.normalize(raw, board, now=now)
        outcome = apply_structured_gates(posting, profile, comp_tier=comp_tier)
        if outcome.passed:
            survivors.append(posting)
            continue
        rejected.append(posting)
        for finding in outcome.rejections:
            tally[finding.gate] += 1

    return survivors, rejected, dict(tally)


def _withdraw_rejected(conn: sqlite3.Connection, rejected: list[Posting], now: datetime) -> int:
    """Retire anything already stored that has started failing a gate.

    A posting can pass today and fail tomorrow: an employer adds a salary below
    the floor, or an onsite requirement appears in a field that was empty. Gated
    postings are never stored, so nothing writes to the row that already exists
    — it keeps its old values and keeps surfacing, scored on facts that are no
    longer true.

    Found live. A role sat in the queue at 90 reading "comp not disclosed"
    through the poll that revealed it pays well under the floor; the gate
    counted the rejection and the stale row stayed where it was.

    Retiring rather than deleting: the score and any decision are the training
    data, and ``disappeared_at`` is the field the digest, the repost detector
    and the prune already read.

    Args:
        conn: An open connection.
        rejected: Postings this poll gated.
        now: Reference time.

    Returns:
        How many stored postings were retired.
    """
    retired = 0
    for posting in rejected:
        cursor = conn.execute(
            "UPDATE postings SET disappeared_at = ? "
            "WHERE source = ? AND source_id = ? AND disappeared_at IS NULL",
            (now.isoformat(), posting.source, posting.source_id),
        )
        retired += cursor.rowcount
    return retired


def _persist(
    conn: sqlite3.Connection,
    cluster: Cluster,
    board: Board,
    adapter,
    now: datetime,
) -> tuple[ScorableCluster | None, bool]:
    """Fetch detail if the cluster needs it, store it, and say if it is old news."""
    company_id = board.company_id
    if company_id is None:
        company_id = store.insert_company(conn, cluster.canonical.company)

    enriched = _with_detail(cluster, adapter, board, now)
    cluster_id = dedupe.persist_cluster(
        conn, enriched, company_id=company_id, board_id=board.board_id
    )

    if dedupe.is_decided(conn, cluster_id):
        # Already triaged. Re-surfacing it would slowly fill the digest with
        # things that were rejected weeks ago.
        return None, True

    return (
        ScorableCluster(
            cluster_id=cluster_id,
            posting=merge_cluster(enriched),
            company_id=company_id,
        ),
        False,
    )


def _with_detail(cluster: Cluster, adapter, board: Board, now: datetime) -> Cluster:
    """Fetch the detail payload once for a cluster that has no description.

    One request per cluster, not per posting, and only for clusters that already
    survived the gates. Without both conditions this is Red Hat's 148 requests
    for a handful of scorable roles.
    """
    canonical = cluster.canonical
    if canonical.description_complete:
        return cluster

    detail = adapter.fetch_detail(canonical)
    if not detail:
        return cluster

    enriched = adapter.normalize({**canonical.raw, **detail}, board, now=now)
    others = tuple(p for p in cluster.postings if p is not canonical)
    return Cluster(
        key=cluster.key,
        postings=(enriched, *others),
        ambiguous_with=cluster.ambiguous_with,
    )


def _record_run(
    conn: sqlite3.Connection,
    board: Board,
    started: str,
    outcome: str,
    postings_seen: int,
    *,
    tally: dict[str, int],
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO poll_runs (source, board_id, started_at, finished_at, outcome, "
        "postings_seen, error, rejection_tally) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            board.vendor,
            board.board_id,
            started,
            datetime.now(UTC).isoformat(),
            outcome,
            postings_seen,
            error,
            json.dumps(tally),
        ),
    )
