"""Clustering: which postings are the same job.

The ordering rule this module exists to protect is stated once, here, and
enforced by the pipeline rather than by this module:

    **Location gating runs before cross-record collapsing.**

If collapsing ran first and kept an arbitrary survivor, it might keep
``Remote (Canada)`` and discard ``Remote (United States)`` — after which the
location gate rejects the survivor and a qualifying job has silently vanished.
Location class is part of the cluster key for the same reason: it keeps the two
copies apart instead of letting them race.

The second rule is asymmetry. A false merge hides a real job permanently and
silently; a false split costs one redundant digest line. Those are not equally
bad, so every threshold here leans toward splitting and the ambiguous band goes
to a human.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher

from winnow.location import classify
from winnow.models import (
    CompSource,
    EmploymentTypeSource,
    LocationClass,
    PostedAtPrecision,
    Posting,
    RemoteSource,
)
from winnow.normalize import normalize_title
from winnow.sources.registry import VENDORS

#: At or above this, two titles at one company are the same role.
MERGE_THRESHOLD = 0.92

#: Between this and :data:`MERGE_THRESHOLD`, the pair is a question for a human.
CONFIRM_THRESHOLD = 0.80

#: Words whose presence is the difference between two real roles. A pair that
#: differs only by one of these is split outright — nobody should be asked
#: whether Director and Senior Director are the same job.
_SENIORITY_WORDS = frozenset(
    {
        "senior",
        "junior",
        "principal",
        "staff",
        "lead",
        "associate",
        "director",
        "manager",
        "head",
        "vp",
        "chief",
        "intern",
        "1",
        "2",
        "3",
        "4",
        "5",
    }
)

_REMOTE_RANK = {
    RemoteSource.STRUCTURED: 0,
    RemoteSource.LOCATION_STRING: 1,
    RemoteSource.DESCRIPTION_TEXT: 2,
    RemoteSource.ABSENT: 3,
}

_EMPLOYMENT_RANK = {
    EmploymentTypeSource.STRUCTURED: 0,
    EmploymentTypeSource.CUSTOM_FIELD: 1,
    EmploymentTypeSource.TEXT: 2,
    EmploymentTypeSource.ABSENT: 3,
}

#: Stated beats parsed beats a modelled figure; a modelled figure still beats
#: knowing only that the employer withheld one. What stops a predicted number
#: clearing a floor is the gate reading its provenance, not this ordering.
_COMP_RANK = {
    CompSource.STATED: 0,
    CompSource.PARSED: 1,
    CompSource.PREDICTED: 2,
    CompSource.WITHHELD: 3,
    CompSource.ABSENT: 4,
}

_POSTED_RANK = {
    PostedAtPrecision.EXACT: 0,
    PostedAtPrecision.DAY: 1,
    PostedAtPrecision.RELATIVE: 2,
    PostedAtPrecision.UNKNOWN: 3,
}


@dataclass(frozen=True)
class ClusterKey:
    """What makes two postings the same job."""

    company: str
    normalized_title: str
    location_class: LocationClass


@dataclass(frozen=True)
class AmbiguousPair:
    """A pairing similar enough to question and not similar enough to act on."""

    other: ClusterKey
    similarity: float


@dataclass(frozen=True)
class Cluster:
    """One job, and every copy of it that was seen."""

    key: ClusterKey
    postings: tuple[Posting, ...]
    ambiguous_with: tuple[AmbiguousPair, ...] = ()

    @property
    def canonical(self) -> Posting:
        """The copy whose URL and identity the digest should use.

        Source proximity is worth roughly 3.5x on interview rate, so the most
        proximate copy wins: the company's own ATS, then an aggregator that
        resolves to the employer, then one offering only a redirect.
        """
        return min(self.postings, key=lambda posting: (source_rank(posting), posting.source_id))


def source_rank(posting: Posting) -> int:
    """Rank a posting by how close it is to the employer.

    Args:
        posting: The posting to rank.

    Returns:
        0 for a company ATS board, 1 for an aggregator whose URL resolves to the
        employer, 2 for one offering only a redirect. Lower is better.
    """
    if posting.source in VENDORS:
        return 0
    if any(f"{vendor}" in posting.source_url for vendor in ("greenhouse", "lever", "ashby")):
        return 1
    return 2


def cluster_key(posting: Posting) -> ClusterKey:
    """Build the cluster key for one posting.

    Args:
        posting: A normalised posting.

    Returns:
        The key. Location class is part of it, which is what keeps a Canadian
        and a US copy of one role from colliding.
    """
    return ClusterKey(
        company=posting.company.strip().lower(),
        normalized_title=normalize_title(posting.title),
        location_class=classify(posting.locations, posting.remote),
    )


def cluster_postings(postings: list[Posting]) -> list[Cluster]:
    """Group postings into clusters.

    Tier 1 is an exact match on the cluster key, which catches the large
    majority cheaply and deterministically. Tier 2 compares titles within a
    single company: above :data:`MERGE_THRESHOLD` the clusters join, and in the
    band below it the pair is recorded for confirmation in the review TUI rather
    than merged. Fuzzy matching never crosses companies.

    Args:
        postings: Normalised postings, in any order.

    Returns:
        The clusters, each carrying any ambiguous pairings for a human to settle.
    """
    grouped: dict[ClusterKey, list[Posting]] = {}
    for posting in postings:
        grouped.setdefault(cluster_key(posting), []).append(posting)

    merged = _merge_near_identical(grouped)
    ambiguities = _find_ambiguous_pairs(merged)

    return [
        Cluster(
            key=key,
            postings=tuple(members),
            ambiguous_with=tuple(ambiguities.get(key, ())),
        )
        for key, members in merged.items()
    ]


def merge_cluster(cluster: Cluster) -> Posting:
    """Collapse a cluster into one record, field by field.

    The canonical copy decides identity — URL, source, title — but each field is
    filled by whichever copy has the strongest provenance for that field. An
    aggregator's stated compensation may fill a gap the ATS left; the gate reads
    ``comp_source`` and decides for itself whether it counts.

    Args:
        cluster: The cluster to collapse.

    Returns:
        A single posting carrying the best of what the cluster knows.
    """
    canonical = cluster.canonical
    postings = list(cluster.postings)

    remote_from = min(postings, key=lambda p: _REMOTE_RANK[p.remote_source])
    employment_from = min(postings, key=lambda p: _EMPLOYMENT_RANK[p.employment_type_source])
    comp_from = min(postings, key=lambda p: _COMP_RANK[p.comp_source])
    described = max(postings, key=lambda p: (p.description_complete, len(p.description_text or "")))
    dated = min(
        postings,
        key=lambda p: (_POSTED_RANK[p.posted_at_precision], p.posted_at or datetime.max),
    )

    return replace(
        canonical,
        remote=remote_from.remote,
        remote_source=remote_from.remote_source,
        employment_type=employment_from.employment_type,
        employment_type_source=employment_from.employment_type_source,
        comp_min=comp_from.comp_min,
        comp_max=comp_from.comp_max,
        comp_currency=comp_from.comp_currency,
        comp_interval=comp_from.comp_interval,
        comp_source=comp_from.comp_source,
        description_text=described.description_text,
        description_complete=described.description_complete,
        posted_at=dated.posted_at,
        posted_at_precision=dated.posted_at_precision,
        posted_at_text=dated.posted_at_text,
    )


def persist_cluster(
    conn: sqlite3.Connection, cluster: Cluster, *, company_id: int, board_id: int | None = None
) -> int:
    """Write a cluster and its postings to the store.

    A cluster key reappearing with a new ``source_id`` after the previous
    posting disappeared is a repost, not a new job: it is linked to its
    predecessor and counted, because repost frequency is one of the few
    ghost-job signals that is computable rather than guessed. Naive dedupe would
    merge it away and hide the signal.

    Args:
        conn: An open connection.
        cluster: The cluster to persist.
        company_id: The owning company.
        board_id: The board it was polled from, when it came from one. Recorded
            so a later poll can tell which postings that board has stopped
            listing.

    Returns:
        The cluster's row id.
    """
    from winnow import store

    key = cluster.key
    conn.execute(
        "INSERT OR IGNORE INTO clusters (company_id, normalized_title, location_class) "
        "VALUES (?, ?, ?)",
        (company_id, key.normalized_title, key.location_class.value),
    )
    row = conn.execute(
        "SELECT id FROM clusters WHERE company_id = ? AND normalized_title = ? "
        "AND location_class = ?",
        (company_id, key.normalized_title, key.location_class.value),
    ).fetchone()
    cluster_id = int(row["id"])

    for posting in cluster.postings:
        known = conn.execute(
            "SELECT id FROM postings WHERE source = ? AND source_id = ?",
            (posting.source, posting.source_id),
        ).fetchone()
        previous_id = None if known else _vacated_posting(conn, cluster_id)
        posting_id = store.upsert_posting(
            conn,
            posting,
            cluster_id=cluster_id,
            company_id=company_id,
            board_id=board_id,
            previous_posting_id=previous_id,
        )
        if previous_id is not None:
            conn.execute(
                "UPDATE clusters SET repost_count = repost_count + 1 WHERE id = ?",
                (cluster_id,),
            )
        if posting is cluster.canonical:
            conn.execute(
                "UPDATE clusters SET canonical_posting_id = ?, last_seen_at = ? WHERE id = ?",
                (posting_id, posting.first_seen_at.isoformat(), cluster_id),
            )

    for pair in cluster.ambiguous_with:
        _record_ambiguity(conn, cluster_id, pair, company_id)

    return cluster_id


def is_decided(conn: sqlite3.Connection, cluster_id: int) -> bool:
    """Say whether a cluster already has a decision against it.

    Decisions key on the cluster rather than on ``(source, source_id)``, so a
    role passed on does not come back tomorrow via a second source or in six
    weeks as a repost.

    Args:
        conn: An open connection.
        cluster_id: The cluster to check.

    Returns:
        True when the cluster has been marked interested or passed. A deferral
        is not a decision.
    """
    row = conn.execute(
        "SELECT count(*) AS n FROM decisions WHERE cluster_id = ? "
        "AND decision IN ('interested', 'pass')",
        (cluster_id,),
    ).fetchone()
    return bool(row["n"])


def _vacated_posting(conn: sqlite3.Connection, cluster_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM postings WHERE cluster_id = ? AND disappeared_at IS NOT NULL "
        "ORDER BY disappeared_at DESC LIMIT 1",
        (cluster_id,),
    ).fetchone()
    return int(row["id"]) if row else None


def _record_ambiguity(
    conn: sqlite3.Connection, cluster_id: int, pair: AmbiguousPair, company_id: int
) -> None:
    other = pair.other
    other_row = conn.execute(
        "SELECT canonical_posting_id FROM clusters WHERE company_id = ? "
        "AND normalized_title = ? AND location_class = ?",
        (company_id, other.normalized_title, other.location_class.value),
    ).fetchone()
    this_row = conn.execute(
        "SELECT canonical_posting_id FROM clusters WHERE id = ?", (cluster_id,)
    ).fetchone()
    if not other_row or not this_row:
        return
    left, right = this_row["canonical_posting_id"], other_row["canonical_posting_id"]
    if left is None or right is None:
        return
    conn.execute(
        "INSERT OR IGNORE INTO merge_confirmations (posting_id, other_id, similarity) "
        "VALUES (?, ?, ?)",
        (min(left, right), max(left, right), pair.similarity),
    )


def _merge_near_identical(
    grouped: dict[ClusterKey, list[Posting]],
) -> dict[ClusterKey, list[Posting]]:
    keys = list(grouped)
    absorbed: dict[ClusterKey, ClusterKey] = {}
    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            if right in absorbed or not _comparable(left, right):
                continue
            if _title_similarity(left.normalized_title, right.normalized_title) >= MERGE_THRESHOLD:
                absorbed[right] = absorbed.get(left, left)

    merged: dict[ClusterKey, list[Posting]] = {}
    for key, members in grouped.items():
        target = absorbed.get(key, key)
        merged.setdefault(target, []).extend(members)
    return merged


def _find_ambiguous_pairs(
    grouped: dict[ClusterKey, list[Posting]],
) -> dict[ClusterKey, list[AmbiguousPair]]:
    pairs: dict[ClusterKey, list[AmbiguousPair]] = {}
    keys = list(grouped)
    for index, left in enumerate(keys):
        for right in keys[index + 1 :]:
            if not _comparable(left, right) or _differ_only_by_seniority(left, right):
                continue
            similarity = _title_similarity(left.normalized_title, right.normalized_title)
            if CONFIRM_THRESHOLD <= similarity < MERGE_THRESHOLD:
                pairs.setdefault(left, []).append(AmbiguousPair(right, similarity))
                pairs.setdefault(right, []).append(AmbiguousPair(left, similarity))
    return pairs


def _comparable(left: ClusterKey, right: ClusterKey) -> bool:
    """Fuzzy matching stays inside one company and one location class."""
    return left.company == right.company and left.location_class == right.location_class


def _differ_only_by_seniority(left: ClusterKey, right: ClusterKey) -> bool:
    difference = set(left.normalized_title.split()) ^ set(right.normalized_title.split())
    return bool(difference) and difference <= _SENIORITY_WORDS


def _title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()
