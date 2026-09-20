"""What the review surface reads and writes.

Separated from the widgets so the parts that matter — the queue draining, the
structured reasons, the tunables write-back — are testable without a terminal.

The structured reasons are the point of the pass flow. Free text is for the
human reading it back in six months; the enumeration is what lets the system
notice that eight of the last ten passes were the same objection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from winnow import learning, store
from winnow.digest import format_comp
from winnow.models import Posting
from winnow.profile import Profile, update_digest_tunables
from winnow.scoring import BreakdownLine

#: The reasons a pass may carry. Kept in step with the schema's CHECK
#: constraint: offering a reason the store would reject fails at the last
#: keystroke, after the thinking is done.
PASS_REASONS = (
    "comp_too_low",
    "rto_suspected",
    "no_growth_signal",
    "boring_domain",
    "bad_culture_signal",
    "title_beneath_target",
    "already_applied",
    "timing",
    # The only reason here that names something outside the posting. Everything
    # else is a judgement about text the scorer also read, so filing outside
    # knowledge under one of those would read later as a scoring miss.
    "external_feedback",
    # The only reason that is feedback about winnow rather than about the
    # job. "Business operations" is a title anchor and also an event
    # coordinator's job, so the gate surfaces work that was never the right
    # kind. Separate from boring_domain, which is a real match he does not
    # want; this is no match at all, and it is the one signal that says the
    # anchor vocabulary needs narrowing.
    "wrong_function",
)

OUTCOMES = (
    "no_response",
    "auto_reject",
    "recruiter_screen",
    "interview",
    "offer",
    "withdrawn",
)


class NotSendable(RuntimeError):
    """This draft must not be sent, and the reason is not the operator's fault."""


@dataclass(frozen=True)
class QueueRow:
    """One cluster in a list, whichever list that is.

    Attributes:
        age_days: How long this row has been in its current state — days since
            it was marked interested, or since it was applied for. ``None`` in
            the review queue, where nothing has happened to it yet.
    """

    cluster_id: int
    score: int
    title: str
    company: str
    comp: str
    source: str
    vetoed: bool
    age_days: int | None = None


@dataclass(frozen=True)
class Detail:
    """Everything behind one score.

    A score with no visible reasoning is a number to argue with; a score with
    quoted evidence is a claim that can be checked.
    """

    cluster_id: int
    score: int
    title: str
    company: str
    board: str
    url: str
    comp: str
    remote: str
    posted: str
    reposts: int
    gates: str
    why_fits: str
    concern: str
    breakdown: tuple[BreakdownLine, ...]
    posting: Posting
    drafts_pending: int = 0
    drafts_sent: int = 0


@dataclass(frozen=True)
class TunablesView:
    """The adjustable numbers, next to the measurement that justifies changing them."""

    score_threshold: int
    max_items: int
    always_show_top: int
    cadence: str
    applications_per_week: float
    #: Passes in the window citing wrong_function — the count of times the
    #: title vocabulary surfaced work that was never the right kind.
    gate_misfires: int = 0


@dataclass(frozen=True)
class MergeCandidate:
    """A pairing similar enough to question, not similar enough to act on."""

    id: int
    left: str
    right: str
    similarity: float


@dataclass(frozen=True)
class StaleBoard:
    """A board that has answered with nothing three times running."""

    board_id: int
    company: str
    vendor: str
    identifier: str


def queue(
    conn: sqlite3.Connection,
    *,
    profile: Profile | None = None,
    include_below_threshold: bool = False,
) -> list[QueueRow]:
    """List the clusters awaiting a decision, highest score first.

    Args:
        conn: An open connection.
        profile: The rubric, whose threshold filters the list. Without one
            nothing is filtered — hiding rows on the authority of a rubric the
            caller never passed would be deciding for them.
        include_below_threshold: Show everything the scorer produced.

    Returns:
        One row per undecided cluster.

        Vetoed clusters are included and marked. The digest hides them, but a
        veto is a model reading prose rather than a gate reading a field, and a
        model misreading "3 days onsite" out of a remote posting is exactly the
        call worth being able to overrule by hand. Deterministic gates are a
        different matter: those rejections are counted and never shown, because
        they rest on stated structured evidence.

        Below-threshold clusters are excluded by default. Measured at 217
        boards: 136 rows, 49 of them under the rubric's own bar. That was
        harmless at twenty boards and useless at two hundred, and a title
        claiming 136 awaited a decision was not true in any useful sense.
    """
    rows: list[QueueRow] = []
    for cluster_id, _ in learning.clusters_awaiting_score(conn, include_scored=True):
        record = learning.latest_score(conn, cluster_id)
        posting = store.canonical_posting(conn, cluster_id)
        if record is None or posting is None:
            continue
        rows.append(
            QueueRow(
                cluster_id=cluster_id,
                score=record.score,
                title=posting.title,
                company=posting.company,
                comp=format_comp(posting),
                source=posting.source,
                vetoed=record.vetoed,
            )
        )
    rows.sort(key=lambda row: -row.score)
    if profile is not None and not include_below_threshold:
        # A role that has left its board cannot be applied to. The digest
        # already refuses to list one — "the worst thing this system can
        # produce is an evening spent on a role that has already closed" — and
        # that reasoning was never applied to the screen where the evening
        # actually gets spent.
        closed = _closed_clusters(conn)
        rows = [
            row
            for row in rows
            if row.score >= profile.score_threshold and row.cluster_id not in closed
        ]
    return rows


def _closed_clusters(conn: sqlite3.Connection) -> set[int]:
    """Clusters whose canonical posting has left its board."""
    return {
        int(row["id"])
        for row in conn.execute(
            "SELECT c.id AS id FROM clusters c "
            "JOIN postings p ON p.id = c.canonical_posting_id "
            "WHERE p.disappeared_at IS NOT NULL"
        )
    }


def below_threshold_count(conn: sqlite3.Connection, profile: Profile) -> int:
    """How many scored, undecided clusters the threshold is holding back.

    Reported rather than silently dropped: a list that got shorter without
    saying why reads as a quiet day, which is the one thing this project never
    lets a number do.
    """
    withheld = queue(conn, profile=profile, include_below_threshold=True)
    return sum(1 for row in withheld if row.score < profile.score_threshold)


def working_set(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[QueueRow]:
    """List what was marked interested and has not been applied for yet.

    The queue drains, which is right: it is a list of questions and a decision
    answers one. But an answer of *yes* used to drain it the same way a *no*
    did, so the roles actually worth pursuing were the ones that vanished
    hardest. This is where they go instead, oldest first, because the failure
    mode of a working set is a role quietly ageing out of relevance.

    Args:
        conn: An open connection.
        now: Reference time, for the age of each row.

    Returns:
        One row per interested, unapplied cluster, longest-waiting first.
    """
    now = now or datetime.now(UTC)
    return _rows(conn, learning.interested_unapplied(conn), now=now)


def applied(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[QueueRow]:
    """List the applications that are waiting on somebody else.

    An application leaves this list when it is answered, including when its
    silence is recorded as an answer. What is left is the open thread: the
    things to chase, or to stop hoping about.

    Args:
        conn: An open connection.
        now: Reference time, for how long each has been quiet.

    Returns:
        One row per submitted, unanswered application, longest-waiting first.
    """
    now = now or datetime.now(UTC)
    return _rows(conn, learning.submitted_unanswered(conn), now=now)


def deferred(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[QueueRow]:
    """List what was set aside for later, longest-waiting first.

    Deferring is not an answer, so this list has to be one that can be left:
    every decision key works here, and using one moves the row out.

    Args:
        conn: An open connection.
        now: Reference time, for how long each has waited.

    Returns:
        One row per deferred cluster.
    """
    now = now or datetime.now(UTC)
    return _rows(conn, learning.deferred_clusters(conn), now=now)


def _rows(
    conn: sqlite3.Connection, pairs: list[tuple[int, str | None]], *, now: datetime
) -> list[QueueRow]:
    """Build queue rows for clusters, oldest first, skipping any without a score."""
    built: list[QueueRow] = []
    for cluster_id, since in pairs:
        record = learning.latest_score(conn, cluster_id)
        posting = store.canonical_posting(conn, cluster_id)
        if record is None or posting is None:
            continue
        built.append(
            QueueRow(
                cluster_id=cluster_id,
                score=record.score,
                title=posting.title,
                company=posting.company,
                comp=format_comp(posting),
                source=posting.source,
                vetoed=record.vetoed,
                age_days=_days_since(since, now),
            )
        )
    built.sort(key=lambda row: -(row.age_days or 0))
    return built


def _days_since(timestamp: str | None, now: datetime) -> int | None:
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((now - when).days, 0)


def detail(conn: sqlite3.Connection, cluster_id: int) -> Detail:
    """Return everything behind one cluster's score.

    Args:
        conn: An open connection.
        cluster_id: The cluster to open.

    Returns:
        The detail view.

    Raises:
        KeyError: If the cluster has no score or no canonical posting.
    """
    record = learning.latest_score(conn, cluster_id)
    posting = store.canonical_posting(conn, cluster_id)
    if record is None or posting is None:
        raise KeyError(cluster_id)

    row = conn.execute("SELECT repost_count FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    drafts = learning.drafts_for(conn, cluster_id)

    return Detail(
        cluster_id=cluster_id,
        score=record.score,
        title=posting.title,
        company=posting.company,
        board=f"{posting.source}/{posting.discovered_via}",
        url=posting.source_url,
        comp=format_comp(posting),
        remote=f"{posting.remote} ({posting.remote_source})",
        posted=_posted(posting),
        reposts=int(row["repost_count"]) if row else 0,
        gates=_gates(record),
        why_fits=record.why_fits,
        concern=record.concern,
        breakdown=record.breakdown,
        posting=posting,
        drafts_pending=sum(1 for draft in drafts if not draft.submitted_at),
        drafts_sent=sum(1 for draft in drafts if draft.submitted_at),
    )


def decide(
    conn: sqlite3.Connection,
    cluster_id: int,
    decision: str,
    *,
    reason: str | None = None,
    note: str | None = None,
) -> None:
    """Record a decision against a cluster.

    Args:
        conn: An open connection.
        cluster_id: The cluster decided on.
        decision: ``interested``, ``pass`` or ``defer``.
        reason: Required for a pass, and must be one of :data:`PASS_REASONS`.
        note: Optional free text.

    Raises:
        ValueError: If a pass carries no reason, or the reason is not one the
            store will accept.
    """
    if decision == "pass":
        if reason is None:
            raise ValueError("a pass needs a structured reason to be worth recording")
        if reason not in PASS_REASONS:
            raise ValueError(f"{reason} is not one of {', '.join(PASS_REASONS)}")
    learning.record_decision(conn, cluster_id, decision, reason=reason, note=note)


@dataclass(frozen=True)
class UndoneDecision:
    """What an undo took back, so the app can say what it did."""

    cluster_id: int
    decision: str
    company: str
    title: str


def undo_last_decision(conn: sqlite3.Connection) -> UndoneDecision | None:
    """Take back the most recent decision, whichever cluster it was about.

    Mis-keys are a design problem rather than a user error: ``d`` for defer
    sits beside ``p`` for pass and reads as *dismiss*. Undo is the general
    answer, and it is cheap here because decisions are append-only and every
    list reads the latest one — deleting it restores exactly the state before
    the press, including an earlier decision it had superseded.

    The most recent decision anywhere is the right target: after a mis-press
    the row has already left the list, so there is nothing to select.

    Args:
        conn: An open connection.

    Returns:
        What was undone, or ``None`` when there is nothing to undo.
    """
    row = conn.execute(
        """
        SELECT d.id AS id, d.cluster_id AS cluster_id, d.decision AS decision,
               p.company AS company, p.title AS title
        FROM decisions d
        JOIN clusters c ON c.id = d.cluster_id
        LEFT JOIN postings p ON p.id = c.canonical_posting_id
        ORDER BY d.id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None

    conn.execute("DELETE FROM decisions WHERE id = ?", (row["id"],))
    return UndoneDecision(
        cluster_id=int(row["cluster_id"]),
        decision=row["decision"],
        company=row["company"] or "",
        title=row["title"] or "",
    )


def record_outcome(
    conn: sqlite3.Connection,
    cluster_id: int,
    outcome: str,
    *,
    occurred_on: str | None = None,
    rejection_text: str | None = None,
) -> None:
    """Record an outcome by hand.

    Inbound mail classifies most of them, but a phone screen and a verbal
    rejection arrive through neither mail nor a board, and anything the
    classifier was unsure about lands here rather than being guessed.

    Args:
        conn: An open connection.
        cluster_id: The cluster the outcome belongs to.
        outcome: One of :data:`OUTCOMES`.
        occurred_on: ISO date, when known.
        rejection_text: The employer's own words, when there are any.

    Raises:
        ValueError: If the outcome is not one the store will accept.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"{outcome} is not one of {', '.join(OUTCOMES)}")
    learning.record_outcome(
        conn, cluster_id, outcome, occurred_on=occurred_on, rejection_text=rejection_text
    )


def tunables(
    conn: sqlite3.Connection, profile: Profile, *, now: datetime | None = None
) -> TunablesView:
    """Return the adjustable numbers and the rate that justifies changing them.

    Throughput was deliberately left unknown at intake. This is where it gets
    answered by behaviour instead of estimate, which is why the measured
    application rate sits beside the cap rather than in a separate report.

    Args:
        conn: An open connection.
        profile: The loaded rubric.
        now: Reference time.

    Returns:
        The current values, the measured weekly application rate, and the count
        of gate misfires. The last is not a tunable but the evidence for one:
        it is the number of times the title vocabulary surfaced work that was
        never the right kind, and the vocabulary is a thing to go and narrow.
    """
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=28)).isoformat()
    row = conn.execute(
        "SELECT count(*) AS n FROM decisions WHERE decision = 'interested' AND created_at >= ?",
        (since,),
    ).fetchone()
    misfires = conn.execute(
        "SELECT count(*) AS n FROM decisions WHERE reason = 'wrong_function' AND created_at >= ?",
        (since,),
    ).fetchone()
    return TunablesView(
        score_threshold=profile.score_threshold,
        max_items=profile.max_items,
        always_show_top=profile.always_show_top,
        cadence=profile.cadence,
        applications_per_week=round(int(row["n"]) / 4, 1),
        gate_misfires=int(misfires["n"]),
    )


def set_tunable(path: Path | str, name: str, value: object) -> None:
    """Write one tunable back to ``profile.yaml``.

    Args:
        path: Location of the rubric.
        name: The tunable to set.
        value: Its new value.

    Raises:
        KeyError: If the name is not a digest tunable.
    """
    update_digest_tunables(path, **{name: value})


def pending_merges(conn: sqlite3.Connection) -> list[MergeCandidate]:
    """List duplicate pairs waiting on a human.

    Args:
        conn: An open connection.

    Returns:
        One row per unresolved pairing, with both titles so the question can be
        answered without opening anything.
    """
    rows = conn.execute(
        "SELECT m.id AS id, m.similarity AS similarity, "
        "a.title AS left_title, b.title AS right_title "
        "FROM merge_confirmations m "
        "JOIN postings a ON a.id = m.posting_id "
        "JOIN postings b ON b.id = m.other_id "
        "WHERE m.status = 'pending' ORDER BY m.similarity DESC"
    ).fetchall()
    return [
        MergeCandidate(
            id=int(row["id"]),
            left=row["left_title"],
            right=row["right_title"],
            similarity=float(row["similarity"]),
        )
        for row in rows
    ]


def resolve_merge(conn: sqlite3.Connection, confirmation_id: int, *, merged: bool) -> None:
    """Settle one ambiguous pairing.

    Args:
        conn: An open connection.
        confirmation_id: The pairing.
        merged: True if they are one job, False if they are two.
    """
    conn.execute(
        "UPDATE merge_confirmations SET status = ? WHERE id = ?",
        ("merged" if merged else "split", confirmation_id),
    )


def stale_boards(conn: sqlite3.Connection) -> list[StaleBoard]:
    """List boards flagged for re-resolution.

    Args:
        conn: An open connection.

    Returns:
        One row per stale board. A board goes quiet either because the company
        is not hiring or because the slug stopped addressing it, and the
        difference is not something to guess at.
    """
    rows = conn.execute(
        "SELECT b.id AS id, b.vendor AS vendor, b.identifier AS identifier, c.name AS name "
        "FROM company_boards b JOIN companies c ON c.id = b.company_id "
        "WHERE b.status = 'stale' ORDER BY c.name"
    ).fetchall()
    return [
        StaleBoard(
            board_id=int(row["id"]),
            company=row["name"],
            vendor=row["vendor"],
            identifier=row["identifier"],
        )
        for row in rows
    ]


def _gates(record) -> str:
    if not record.vetoed:
        return "passed · no vetoes"
    return " · ".join(f'{veto.get("gate")}: "{veto.get("evidence")}"' for veto in record.vetoes)


def _posted(posting: Posting) -> str:
    if posting.posted_at_text:
        return posting.posted_at_text
    if posting.posted_at is None:
        return "posting date not stated"
    age = (datetime.now(UTC) - posting.posted_at).days
    return f"{age} days ago ({posting.posted_at_precision})"


def send_draft(
    conn: sqlite3.Connection,
    draft: learning.StoredDraft,
    profile: Profile,
    *,
    send: Callable[[object], str] | None = None,
) -> str:
    """Send one specific draft, now, because a human asked for this one.

    This is the only function in the project that puts a message on the wire,
    and it is reachable only from an interactive keystroke. The digest timer and
    the inbound listener hold no credential that could do this.

    Args:
        conn: An open connection.
        draft: The draft to send.
        profile: Supplies the address to send from.
        send: Injected transport, for tests. Defaults to the mail helper, which
            is imported here rather than at module scope so that importing the
            review layer never pulls SMTP code into a process that should not
            have it.

    Returns:
        The Message-ID of what was sent.

    Raises:
        NotSendable: If the draft has no recipient — an ATS application is a
            form to paste into, not an email — or has already been sent.
    """
    if draft.submitted_at:
        raise NotSendable(f"draft {draft.id} was already submitted at {draft.submitted_at}")
    if not draft.recipient:
        raise NotSendable(
            "this draft has no recipient: it is a cover letter to paste into the "
            "employer's application form, not an email to send"
        )

    from winnow.mailer import build_message
    from winnow.mailer import send as smtp_send

    message = build_message(
        sender=profile.mailbox,
        to=draft.recipient,
        subject=draft.subject,
        body=draft.body,
    )
    message_id = (send or smtp_send)(message)
    learning.mark_draft_sent(conn, draft.id, message_id)
    return message_id
