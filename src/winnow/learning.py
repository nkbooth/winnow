"""The learning store: scores, decisions, outcomes.

Scoring only improves if decisions are captured with the context that produced
them. A bare interested/pass is nearly useless — six months on you know forty
roles were rejected and nothing about why. So:

* **scores** are written once and hold the breakdown, not the total. Which
  weights fired, with what evidence, under which rubric, prompt and model.
* **decisions** carry a structured reason. The enum is what makes the signal
  aggregable across months; the optional note is what makes it intelligible.
* **outcomes** are the only ground truth here. Everything else, scores
  included, is opinion.

What that buys: preference drift (if eight of the last ten passes were
``rto_suspected``, the detector should run earlier and harder), threshold
calibration against real conversions, silent-filter detection, and which boards
are worth polling at all.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from winnow.drafting import Draft
from winnow.models import RemoteSource, RemoteStatus
from winnow.scoring import BreakdownLine, ScoreRecord

#: Score bands for calibration. Coarse on purpose — the question is whether the
#: threshold sits in the right place, not what each individual point does.
_BANDS = ((0, 69), (70, 79), (80, 89), (90, 100))

_INTERVIEW_OUTCOMES = ("recruiter_screen", "interview", "offer")


@dataclass(frozen=True)
class CalibrationBand:
    """How one score band actually converted."""

    low: int
    high: int
    applications: int
    interviews: int
    rejections: int

    @property
    def interview_rate(self) -> float:
        """Share of applications in this band that reached a screen or beyond."""
        return self.interviews / self.applications if self.applications else 0.0


@dataclass(frozen=True)
class StoredDraft:
    """A draft as it sits in the queue."""

    id: int
    cluster_id: int
    kind: str
    resume_variant: str | None
    recipient: str | None
    subject: str
    body: str
    built_from: dict
    sent_at: str | None
    submitted_at: str | None = None
    #: What was actually sent, where it differs from the proposal above.
    final_body: str | None = None
    final_source: str | None = None

    @property
    def tailoring_notes(self) -> tuple[str, ...]:
        """Which existing bullets to lead with, and which words to mirror."""
        return tuple(self.built_from.get("tailoring_notes") or ())

    @property
    def unverified(self) -> list[dict]:
        """Claims the drafter could not trace to the candidate's own history."""
        return list(self.built_from.get("unverified") or [])

    @property
    def needs_attention(self) -> bool:
        """True when something in this draft could not be traced."""
        return bool(self.unverified)


@dataclass(frozen=True)
class SourceQuality:
    """What one source produced, measured in outcomes rather than volume."""

    source: str
    applications: int
    interviews: int


def record_score(conn: sqlite3.Connection, cluster_id: int, record: ScoreRecord) -> int:
    """Store a score and its full breakdown.

    Scores are append-only. Re-scoring after a rubric or prompt change writes a
    new row, because the history is what calibration reads.

    Args:
        conn: An open connection.
        cluster_id: The cluster scored.
        record: The assembled score.

    Returns:
        The score's row id.
    """
    posting_row = conn.execute(
        "SELECT canonical_posting_id FROM clusters WHERE id = ?", (cluster_id,)
    ).fetchone()
    if posting_row and posting_row["canonical_posting_id"]:
        _record_vetoed_arrangement(conn, int(posting_row["canonical_posting_id"]), record)
    cursor = conn.execute(
        "INSERT INTO scores (cluster_id, posting_id, score, breakdown, vetoes, flags, "
        "why_fits, concern, rubric_version, prompt_version, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cluster_id,
            posting_row["canonical_posting_id"] if posting_row else None,
            record.score,
            json.dumps([_line_to_dict(line) for line in record.breakdown]),
            json.dumps(list(record.vetoes)),
            json.dumps(list(record.flags)),
            record.why_fits,
            record.concern,
            record.rubric_version,
            record.prompt_version,
            record.model,
        ),
    )
    return int(cursor.lastrowid)


def latest_score(conn: sqlite3.Connection, cluster_id: int) -> ScoreRecord | None:
    """Return the most recent score for a cluster.

    Args:
        conn: An open connection.
        cluster_id: The cluster to read.

    Returns:
        The score, or ``None`` if the cluster has never been scored.
    """
    row = conn.execute(
        "SELECT * FROM scores WHERE cluster_id = ? ORDER BY id DESC LIMIT 1", (cluster_id,)
    ).fetchone()
    if row is None:
        return None
    return ScoreRecord(
        score=int(row["score"]),
        breakdown=tuple(BreakdownLine(**line) for line in json.loads(row["breakdown"])),
        vetoes=tuple(json.loads(row["vetoes"])),
        flags=tuple(json.loads(row["flags"])),
        unverified=(),
        why_fits=row["why_fits"] or "",
        concern=row["concern"] or "",
        vetoed=bool(json.loads(row["vetoes"])),
        model=row["model"],
        prompt_version=row["prompt_version"],
        rubric_version=row["rubric_version"],
    )


def record_decision(
    conn: sqlite3.Connection,
    cluster_id: int,
    decision: str,
    *,
    reason: str | None = None,
    note: str | None = None,
) -> int:
    """Record what was decided about a cluster.

    Args:
        conn: An open connection.
        cluster_id: The cluster decided on.
        decision: ``interested``, ``pass`` or ``defer``.
        reason: A structured reason from the schema's enumeration. Free text
            belongs in ``note``; a reason the system cannot aggregate is not a
            reason it can learn from.
        note: Optional free text.

    Returns:
        The decision's row id.

    Raises:
        sqlite3.IntegrityError: If the decision or reason is not one the schema
            recognises.
    """
    cursor = conn.execute(
        "INSERT INTO decisions (cluster_id, decision, reason, note) VALUES (?, ?, ?, ?)",
        (cluster_id, decision, reason, note),
    )
    return int(cursor.lastrowid)


def record_outcome(
    conn: sqlite3.Connection,
    cluster_id: int,
    outcome: str,
    *,
    occurred_on: str | None = None,
    rejection_text: str | None = None,
) -> int:
    """Record what actually happened after applying.

    Args:
        conn: An open connection.
        cluster_id: The cluster the outcome belongs to.
        outcome: One of the schema's outcome values.
        occurred_on: ISO date, when known.
        rejection_text: The employer's stated reason, verbatim. This is the
            highest-value text in the system and the part a human would never
            bother to log by hand.

    Returns:
        The outcome's row id.
    """
    cursor = conn.execute(
        "INSERT INTO outcomes (cluster_id, outcome, occurred_on, rejection_text) "
        "VALUES (?, ?, ?, ?)",
        (cluster_id, outcome, occurred_on, rejection_text),
    )
    return int(cursor.lastrowid)


def clusters_awaiting_score(
    conn: sqlite3.Connection, *, include_scored: bool = False
) -> list[tuple[int, int]]:
    """List clusters that still need attention.

    Args:
        conn: An open connection.
        include_scored: Keep clusters that already have a score. The scorer
            wants only the unscored ones; the digest wants everything that has
            not been decided on.

    Returns:
        ``(cluster_id, repost_count)`` pairs, oldest cluster first. Clusters
        already decided on are always excluded — a role passed on does not come
        back because it reappeared, and a role deferred does not come back
        tomorrow, which is the whole of what deferring asked for.

        The *latest* decision is what counts, so changing your mind returns a
        cluster to wherever that new decision sends it.
    """
    unscored = (
        ""
        if include_scored
        else "AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.cluster_id = c.id) "
    )
    rows = conn.execute(
        "SELECT c.id AS id, c.repost_count AS repost_count FROM clusters c "
        "WHERE c.canonical_posting_id IS NOT NULL "
        f"{unscored}"
        "AND NOT EXISTS (SELECT 1 FROM decisions d WHERE d.cluster_id = c.id) "
        "ORDER BY c.id"
    ).fetchall()
    return [(int(row["id"]), int(row["repost_count"])) for row in rows]


def pending_review_count(conn: sqlite3.Connection) -> int:
    """Count scored clusters awaiting a decision.

    The digest leads with this number. The review TUI made the digest read-only,
    and its one real risk is an untriaged backlog accumulating invisibly — so
    the backlog is the first thing the digest says, every day.

    Args:
        conn: An open connection.

    Returns:
        How many scored clusters have no interested/pass decision against them.
    """
    row = conn.execute(
        "SELECT count(DISTINCT s.cluster_id) AS n FROM scores s "
        "WHERE NOT EXISTS (SELECT 1 FROM decisions d WHERE d.cluster_id = s.cluster_id "
        "AND d.decision IN ('interested', 'pass'))"
    ).fetchone()
    return int(row["n"])


def calibration(conn: sqlite3.Connection, rubric_version: str) -> list[CalibrationBand]:
    """Compare score bands against what actually happened.

    Args:
        conn: An open connection.
        rubric_version: Which rubric to report on. Scores from other versions
            are excluded rather than mixed, because a report that spans versions
            reads model drift as a change of preference.

    Returns:
        One band per score range that has applications, lowest first. The
        denominator is applications, not replies: an application that went
        unanswered is the most common kind there is, and leaving it out
        measures only the ones somebody bothered to answer.
    """
    rows = conn.execute(
        """
        SELECT
            s.score AS score,
            EXISTS (
                SELECT 1 FROM outcomes o
                WHERE o.cluster_id = applied.cluster_id
                  AND o.outcome IN ('recruiter_screen', 'interview', 'offer')
            ) AS reached_a_screen,
            EXISTS (
                SELECT 1 FROM outcomes o
                WHERE o.cluster_id = applied.cluster_id AND o.outcome = 'auto_reject'
            ) AS was_rejected
        FROM (
            -- An application is a submitted draft. An outcome counts too: a
            -- reply is proof of an application whether or not the keystroke
            -- marking it ever happened.
            SELECT cluster_id FROM drafts WHERE submitted_at IS NOT NULL
            UNION
            SELECT cluster_id FROM outcomes
        ) applied
        JOIN scores s ON s.cluster_id = applied.cluster_id
        WHERE s.rubric_version = ? AND s.id = (
            SELECT max(id) FROM scores WHERE cluster_id = applied.cluster_id
        )
        """,
        (rubric_version,),
    ).fetchall()

    bands: list[CalibrationBand] = []
    for low, high in _BANDS:
        in_band = [row for row in rows if low <= row["score"] <= high]
        if not in_band:
            continue
        bands.append(
            CalibrationBand(
                low=low,
                high=high,
                applications=len(in_band),
                interviews=sum(1 for row in in_band if row["reached_a_screen"]),
                rejections=sum(1 for row in in_band if row["was_rejected"]),
            )
        )
    return bands


def source_quality(conn: sqlite3.Connection) -> list[SourceQuality]:
    """Report outcomes per source.

    Args:
        conn: An open connection.

    Returns:
        One row per source that has produced an outcome, most applications
        first. A source producing volume and no screens is costing attention.
    """
    rows = conn.execute(
        "SELECT p.source AS source, o.outcome AS outcome FROM outcomes o "
        "JOIN clusters c ON c.id = o.cluster_id "
        "JOIN postings p ON p.id = c.canonical_posting_id"
    ).fetchall()

    totals: dict[str, list[int]] = {}
    for row in rows:
        entry = totals.setdefault(row["source"], [0, 0])
        entry[0] += 1
        entry[1] += int(row["outcome"] in _INTERVIEW_OUTCOMES)

    return sorted(
        (
            SourceQuality(source=source, applications=applications, interviews=interviews)
            for source, (applications, interviews) in totals.items()
        ),
        key=lambda row: -row.applications,
    )


def prune(conn: sqlite3.Connection, *, retention_days: int, now: datetime | None = None) -> int:
    """Delete postings that vanished from their board long enough ago.

    Scores and decisions survive: they are the training data, and pruning them
    to save a few megabytes of SQLite would be a bad trade.

    Args:
        conn: An open connection.
        retention_days: How long a vanished posting is kept.
        now: Reference time.

    Returns:
        How many postings were removed.
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    cursor = conn.execute(
        "DELETE FROM postings WHERE disappeared_at IS NOT NULL AND last_seen_at < ?",
        (cutoff,),
    )
    return int(cursor.rowcount)


#: Vetoes that settle how a job is worked, and what they settle it to.
_ARRANGEMENT_VETOES = {
    "onsite_required": RemoteStatus.ONSITE,
    "hybrid_required": RemoteStatus.HYBRID,
}

#: Provenance strong enough that a model reading prose does not displace it.
_STATED_SOURCES = frozenset({RemoteSource.STRUCTURED, RemoteSource.LOCATION_STRING})


def _record_vetoed_arrangement(
    conn: sqlite3.Connection, posting_id: int, record: ScoreRecord
) -> None:
    """Write down the arrangement a veto established.

    A board that gives a city and nothing else leaves the adapter no choice but
    UNKNOWN, and that is right. But when the model then reads "go into the
    office 3 days per week" and quotes it, the arrangement is no longer
    unknown — and that finding used to live only in the score, so the posting
    itself still said UNKNOWN for a role the system had already established and
    written down.

    Recorded as DESCRIPTION_TEXT, which is what it is: weaker than a structured
    field and weaker than a location string that named the arrangement
    outright. Neither of those is overwritten — an employer's own field beats a
    model reading their prose, and a veto that contradicts one is a
    disagreement worth keeping rather than resolving silently.
    """
    arrangement = next(
        (
            _ARRANGEMENT_VETOES[str(veto.get("gate"))]
            for veto in record.vetoes
            if isinstance(veto, dict) and str(veto.get("gate")) in _ARRANGEMENT_VETOES
        ),
        None,
    )
    if arrangement is None:
        return

    conn.execute(
        "UPDATE postings SET remote = ?, remote_source = ? "
        "WHERE id = ? AND remote_source NOT IN (?, ?)",
        (
            arrangement,
            RemoteSource.DESCRIPTION_TEXT,
            posting_id,
            RemoteSource.STRUCTURED,
            RemoteSource.LOCATION_STRING,
        ),
    )


def _line_to_dict(line: BreakdownLine) -> dict:
    return {
        "dimension": line.dimension,
        "score": line.score,
        "weight": line.weight,
        "contribution": line.contribution,
        "evidence": line.evidence,
        "computed": line.computed,
    }


def record_draft(conn: sqlite3.Connection, cluster_id: int, draft: Draft) -> int:
    """Put a draft in the queue.

    Nothing about this sends anything. The queue exists so that the drafting
    path and the sending path are separated by a human.

    Args:
        conn: An open connection.
        cluster_id: The opportunity the draft is for.
        draft: The drafted letter.

    Returns:
        The draft's row id.
    """
    provenance = {
        **draft.built_from,
        "unverified": list(draft.unverified),
        # Stored with the rest of the provenance rather than in a column: they
        # are the most actionable part of a draft, and re-opening one without
        # them showed the letter while losing the instructions.
        "tailoring_notes": list(draft.tailoring_notes),
    }
    cursor = conn.execute(
        "INSERT INTO drafts (cluster_id, kind, resume_variant, recipient, subject, body, "
        "built_from) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            cluster_id,
            draft.kind,
            draft.resume_variant,
            draft.recipient,
            draft.subject,
            draft.body,
            json.dumps(provenance),
        ),
    )
    return int(cursor.lastrowid)


def drafts_for(conn: sqlite3.Connection, cluster_id: int) -> list[StoredDraft]:
    """List a cluster's drafts, newest first.

    Args:
        conn: An open connection.
        cluster_id: The opportunity.

    Returns:
        Every draft written for it, sent or not.
    """
    rows = conn.execute(
        "SELECT * FROM drafts WHERE cluster_id = ? ORDER BY id DESC", (cluster_id,)
    ).fetchall()
    return [
        StoredDraft(
            id=int(row["id"]),
            cluster_id=int(row["cluster_id"]),
            kind=row["kind"],
            resume_variant=row["resume_variant"],
            recipient=row["recipient"],
            subject=row["subject"] or "",
            body=row["body"],
            built_from=json.loads(row["built_from"]),
            sent_at=row["sent_at"],
            submitted_at=row["submitted_at"],
            final_body=row["final_body"],
            final_source=row["final_source"],
        )
        for row in rows
    ]


def mark_draft_sent(conn: sqlite3.Connection, draft_id: int, message_id: str) -> None:
    """Record that a draft left, and under which Message-ID.

    That id is how an employer's reply is correlated back to the application,
    so it is stored on the draft and mirrored into ``messages``. Sending also
    submits the application, since emailing it is one of the two ways it gets
    submitted at all.

    Args:
        conn: An open connection.
        draft_id: The draft that was sent.
        message_id: The Message-ID of the sent message.
    """
    row = conn.execute("SELECT cluster_id FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    # Sending is submitting. Keeping them one event means the emailed path
    # cannot drift out of the count that the pasted path is measured by.
    sent_at = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE drafts SET sent_at = ?, sent_message_id = ?, "
        "submitted_at = coalesce(submitted_at, ?) WHERE id = ?",
        (sent_at, message_id, sent_at, draft_id),
    )
    if row is not None:
        conn.execute(
            "INSERT OR IGNORE INTO messages (message_id, direction, cluster_id) "
            "VALUES (?, 'outbound', ?)",
            (message_id, row["cluster_id"]),
        )


def mark_submitted(conn: sqlite3.Connection, draft_id: int, *, now: datetime | None = None) -> None:
    """Record that this draft's application was actually submitted.

    Emailing a draft submits it, and so does pasting one into an employer's own
    form — an act this process cannot see, which is why there is a key for it in
    review. Both land here, because to calibration they are the same event.

    Args:
        conn: An open connection.
        draft_id: The draft that was submitted.
        now: When, defaulting to the current time.
    """
    conn.execute(
        "UPDATE drafts SET submitted_at = ? WHERE id = ? AND submitted_at IS NULL",
        ((now or datetime.now(UTC)).isoformat(), draft_id),
    )


def age_submissions(conn: sqlite3.Connection, *, days: int, now: datetime | None = None) -> int:
    """Record silence as an outcome once an application has gone unanswered.

    Silence is an answer, and the most common one. Leaving it unrecorded keeps
    it out of calibration entirely, so a score band is judged only on the
    applications somebody replied to.

    Args:
        conn: An open connection.
        days: How long to wait before calling it. Past this, a non-reply is a
            decision rather than a delay.
        now: Reference time.

    Returns:
        How many applications were recorded as unanswered.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO outcomes (cluster_id, outcome, occurred_on)
        SELECT DISTINCT d.cluster_id, 'no_response', date(?)
        FROM drafts d
        WHERE d.submitted_at IS NOT NULL
          AND d.submitted_at <= ?
          AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.cluster_id = d.cluster_id)
        """,
        ((now or datetime.now(UTC)).date().isoformat(), cutoff),
    )
    return cursor.rowcount


def interested_unapplied(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """List clusters wanted but not yet applied for, with when they were wanted.

    The review queue drains on a decision, which is right for a list of
    questions and wrong for the answer *yes*: an interested role would leave
    the queue and appear nowhere else. This is where it lives instead.

    Args:
        conn: An open connection.

    Returns:
        ``(cluster_id, decided_at)`` pairs. A later decision supersedes an
        earlier one, so changing your mind removes it from here.
    """
    rows = conn.execute(
        """
        SELECT d.cluster_id AS cluster_id, d.created_at AS since
        FROM decisions d
        WHERE d.decision = 'interested'
          AND d.id = (SELECT max(id) FROM decisions WHERE cluster_id = d.cluster_id)
          AND NOT EXISTS (
              SELECT 1 FROM drafts f
              WHERE f.cluster_id = d.cluster_id AND f.submitted_at IS NOT NULL
          )
        """
    ).fetchall()
    return [(int(row["cluster_id"]), row["since"]) for row in rows]


def submitted_unanswered(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """List applications that have been sent and not yet answered.

    Args:
        conn: An open connection.

    Returns:
        ``(cluster_id, submitted_at)`` pairs, using the earliest submission
        where a cluster has several drafts. An outcome removes it from here,
        including the ``no_response`` that ageing eventually records.
    """
    rows = conn.execute(
        """
        SELECT f.cluster_id AS cluster_id, min(f.submitted_at) AS since
        FROM drafts f
        WHERE f.submitted_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.cluster_id = f.cluster_id)
        GROUP BY f.cluster_id
        """
    ).fetchall()
    return [(int(row["cluster_id"]), row["since"]) for row in rows]


def record_final(
    conn: sqlite3.Connection,
    draft_id: int,
    body: str,
    *,
    source: str,
    now: datetime | None = None,
) -> None:
    """Record the version of a draft that was actually sent.

    The proposal is left where it is. Keeping both is the point: the difference
    between them is the only correction the drafter ever receives, and
    overwriting the proposal would destroy the comparison on the way to making
    it.

    Args:
        conn: An open connection.
        draft_id: The draft that was revised.
        body: The text as sent.
        source: ``pasted`` when a human entered it during review, or
            ``sent_folder`` when it was read back out of the mailbox.
        now: Submission timestamp, defaulting to the current time.

    Raises:
        ValueError: If the source is not one the store will accept.
    """
    if source not in ("pasted", "sent_folder"):
        raise ValueError(f"{source!r} is not a way a sent letter can be learned")

    conn.execute(
        "UPDATE drafts SET final_body = ?, final_source = ? WHERE id = ?",
        (body, source, draft_id),
    )
    # Nobody revises a letter they are not about to send.
    mark_submitted(conn, draft_id, now=now)


def recent_sent_letters(conn: sqlite3.Connection, *, limit: int = 3) -> tuple[str, ...]:
    """Return the most recently sent letters, newest first.

    These go to the drafter as examples. Instructions can describe a register
    and examples demonstrate one, and the letters he actually sent are the only
    evidence of the register he actually uses — a proposal he never sent is
    evidence of nothing.

    Args:
        conn: An open connection.
        limit: How many to return. A handful is enough to show a voice, and
            enough more would start teaching the model to repeat sentences.

    Returns:
        The text that actually went out, newest first.

        Only letters whose text is *known* qualify. ``final_body`` is text
        somebody captured — pasted in during review, or read back out of the
        Sent folder. ``sent_at`` means this process emailed the stored body, so
        there the proposal is the letter.

        A submission marked by hand with neither is deliberately excluded. It
        records that an application happened and says nothing about what was
        sent, and if the letter was rewritten before pasting — the usual reason
        to rewrite one — then offering the stored proposal as an exemplar
        teaches the model that its own output is how he writes. That is this
        loop running backwards: reinforcing the register it exists to correct,
        while looking like it is learning.
    """
    rows = conn.execute(
        "SELECT coalesce(final_body, body) AS text FROM drafts "
        "WHERE final_body IS NOT NULL OR sent_at IS NOT NULL "
        "ORDER BY coalesce(submitted_at, sent_at) DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return tuple(row["text"] for row in rows)


def deferred_clusters(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """List clusters set aside for later, with when they were set aside.

    Args:
        conn: An open connection.

    Returns:
        ``(cluster_id, deferred_at)`` pairs. Only where deferring is still the
        last word — a later decision supersedes it, as with any other.
    """
    rows = conn.execute(
        """
        SELECT d.cluster_id AS cluster_id, d.created_at AS since
        FROM decisions d
        WHERE d.decision = 'defer'
          AND d.id = (SELECT max(id) FROM decisions WHERE cluster_id = d.cluster_id)
        """
    ).fetchall()
    return [(int(row["cluster_id"]), row["since"]) for row in rows]
