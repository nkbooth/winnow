"""The digest — one of the two surfaces touched daily.

It posts on hits, plus a Monday summary even at zero, so that silence and
breakage never look alike. Everything else in the system exists to fill it, and
five rules shape what it says:

* **The pending-review count leads.** Choosing a review TUI over Matrix replies
  made the digest read-only, and its one real risk is an untriaged backlog
  accumulating invisibly. Putting the number first makes that impossible to
  miss.
* **Compensation always shows its provenance.** A predicted figure must never
  look like a quoted one.
* **One concern per entry, never zero.** If nothing is wrong, the concern says
  what is unknown; an entry with no downside reads as sales copy.
* **The link is the employer's own posting**, worth roughly 3.5x on interview
  rate over an aggregator's.
* **The veto tally is shown but not itemised.** It proves the filters are
  working without spending eight lines on rejects.
"""

from __future__ import annotations

import html
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from winnow import learning, store
from winnow.models import CompInterval, CompSource, Posting
from winnow.profile import Profile
from winnow.scoring import ScoreRecord

_MONDAY = 0


@dataclass(frozen=True)
class DigestEntry:
    """One ranked opportunity, as it appears in the message."""

    cluster_id: int
    score: int
    posting: Posting
    record: ScoreRecord
    below_threshold: bool


@dataclass(frozen=True)
class Digest:
    """Everything one digest needs to say."""

    generated_at: datetime
    entries: tuple[DigestEntry, ...]
    pending_review: int
    above_threshold: int
    withdrawn: int
    veto_tally: dict[str, int]
    failed_sources: tuple[str, ...]
    is_weekly_summary: bool
    #: Interested, not yet applied for, and the age of the oldest in days.
    open_work: tuple[int, int] = (0, 0)

    @property
    def should_send(self) -> bool:
        """Say whether this digest is worth posting.

        On hits, on breakage, and on the weekly summary day regardless — a
        Monday that says nothing is how you tell a quiet week from a dead
        process.
        """
        return bool(self.entries or self.failed_sources or self.is_weekly_summary)


def build_digest(
    conn: sqlite3.Connection, profile: Profile, *, now: datetime | None = None
) -> Digest:
    """Assemble the digest from what is in the store.

    Args:
        conn: An open connection.
        profile: The loaded rubric, which owns the threshold and the cap.
        now: Reference time.

    Returns:
        The digest. Vetoed clusters are counted, never listed; decided clusters
        are absent entirely.
    """
    now = now or datetime.now(UTC)
    candidates: list[DigestEntry] = []
    veto_tally: Counter[str] = Counter()
    withdrawn = 0

    for cluster_id, _ in learning.clusters_awaiting_score(conn, include_scored=True):
        record = learning.latest_score(conn, cluster_id)
        posting = store.canonical_posting(conn, cluster_id)
        if record is None or posting is None:
            continue
        if _has_closed(conn, cluster_id):
            # Aggregators lag the source and a role can close mid-week. The
            # worst thing this system can produce is an evening spent on one
            # that already has.
            withdrawn += 1
            continue
        if record.vetoed:
            for veto in record.vetoes:
                veto_tally[str(veto.get("gate"))] += 1
            continue
        candidates.append(
            DigestEntry(
                cluster_id=cluster_id,
                score=record.score,
                posting=posting,
                record=record,
                below_threshold=record.score < profile.score_threshold,
            )
        )

    candidates.sort(key=lambda entry: -entry.score)
    above = [entry for entry in candidates if not entry.below_threshold]

    entries = _select(candidates, above, profile)

    return Digest(
        generated_at=now,
        entries=tuple(entries),
        pending_review=learning.pending_review_count(conn),
        open_work=_open_work(conn, now),
        above_threshold=len(above),
        withdrawn=withdrawn,
        veto_tally=dict(veto_tally),
        failed_sources=_failed_sources(conn, now),
        is_weekly_summary=now.weekday() == _MONDAY,
    )


def _select(
    candidates: list[DigestEntry], above: list[DigestEntry], profile: Profile
) -> list[DigestEntry]:
    """Choose what to show.

    When anything clears the threshold, the cap decides. When nothing does, the
    top few show anyway marked for what they are — a quiet week must not look
    like a broken one.
    """
    if above:
        return above[: profile.max_items]
    return candidates[: profile.always_show_top]


def render_text(digest: Digest) -> str:
    """Render the plain-text body.

    Args:
        digest: The assembled digest.

    Returns:
        Text sized to be read in Element on a phone.
    """
    lines = [_header(digest)]

    if not digest.entries:
        lines.append("")
        lines.append("Nothing above threshold. The pipeline ran.")

    for entry in digest.entries:
        posting = entry.posting
        marker = " (below threshold)" if entry.below_threshold else ""
        lines.append("")
        lines.append(f"▸ {entry.score}  {posting.title} — {posting.company}{marker}")
        lines.append(f"      {_facts(posting)}")
        lines.append(f"      {entry.record.why_fits}")
        lines.append(f"      ⚠ {_concern(entry.record)}")
        lines.append(f"      {posting.source_url}")

    footer = _footer(digest)
    if footer:
        lines.append("")
        lines.append(footer)
    return "\n".join(lines)


def render_html(digest: Digest) -> str:
    """Render the light HTML body Matrix shows in place of the plain text.

    Everything a posting supplied is escaped: the title and description come
    from a third party, and this body is rendered as markup.

    Args:
        digest: The assembled digest.

    Returns:
        An HTML fragment.
    """
    parts = [f"<p><b>{html.escape(_header(digest))}</b></p>"]

    if not digest.entries:
        parts.append("<p>Nothing above threshold. The pipeline ran.</p>")

    for entry in digest.entries:
        posting = entry.posting
        marker = " (below threshold)" if entry.below_threshold else ""
        parts.append(
            "<p><b>{score}</b> {title} — {company}{marker}<br/>"
            "{facts}<br/>{why}<br/>⚠ {concern}<br/>"
            '<a href="{url}">{url}</a></p>'.format(
                score=entry.score,
                title=html.escape(posting.title),
                company=html.escape(posting.company),
                marker=marker,
                facts=html.escape(_facts(posting)),
                why=html.escape(entry.record.why_fits),
                concern=html.escape(_concern(entry.record)),
                url=html.escape(posting.source_url, quote=True),
            )
        )

    footer = _footer(digest)
    if footer:
        parts.append(f"<p>{html.escape(footer)}</p>")
    return "\n".join(parts)


def _header(digest: Digest) -> str:
    when = digest.generated_at.strftime("%-d %b")
    return f"winnow · {when} · {len(digest.entries)} new · {digest.pending_review} awaiting review"


def _facts(posting: Posting) -> str:
    where = ", ".join(posting.locations) or "location not stated"
    return f"{where} · {format_comp(posting)} · {posting.source}"


def _concern(record: ScoreRecord) -> str:
    """Return the entry's concern, never an empty one.

    An entry with no downside reads as sales copy, so when the scorer offers
    nothing the line says what is not known instead.
    """
    return record.concern.strip() or "Nothing flagged — the fit is not stated, only inferred."


def format_comp(posting: Posting) -> str:
    """Describe a posting's compensation with its provenance attached.

    Args:
        posting: The posting.

    Returns:
        A short phrase. Never a bare number: a modelled figure and a published
        one must not read the same way.
    """
    if posting.comp_source is CompSource.WITHHELD:
        return "comp withheld"
    if posting.comp_min is None or posting.comp_max is None:
        return "comp not disclosed"

    if posting.comp_interval is CompInterval.HOUR:
        amount = (
            f"${posting.comp_min}/hr"
            if posting.comp_min == posting.comp_max
            else f"${posting.comp_min}–{posting.comp_max}/hr"
        )
    else:
        low, high = posting.comp_min // 1000, posting.comp_max // 1000
        amount = f"${low}k" if low == high else f"${low}–{high}k"

    return f"{amount} ({posting.comp_source.value})"


def _footer(digest: Digest) -> str:
    parts: list[str] = []

    remainder = digest.above_threshold - len(digest.entries)
    if remainder > 0:
        parts.append(f"{remainder} more above threshold")

    if digest.veto_tally:
        detail = ", ".join(
            f"{count} {gate}"
            for gate, count in sorted(digest.veto_tally.items(), key=lambda item: -item[1])
        )
        parts.append(f"{sum(digest.veto_tally.values())} vetoed ({detail})")

    if digest.withdrawn:
        parts.append(f"{digest.withdrawn} withdrawn")

    waiting, oldest = digest.open_work
    if waiting:
        # The queue drains on a decision, so an interested role lives only in
        # the working set. A list nobody is reminded of is a list nobody opens.
        parts.append(f"{waiting} interested, not yet applied (oldest {oldest} days)")

    for source in digest.failed_sources:
        parts.append(f"{source} did not answer")

    return " · ".join(parts)


def _open_work(conn: sqlite3.Connection, now: datetime) -> tuple[int, int]:
    """Count what was wanted and never applied for, and how long it has waited."""
    pairs = learning.interested_unapplied(conn)
    ages = [_days_since(since, now) for _, since in pairs]
    return (len(pairs), max(ages)) if ages else (0, 0)


def _days_since(timestamp: str | None, now: datetime) -> int:
    if not timestamp:
        return 0
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return 0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((now - when).days, 0)


def _has_closed(conn: sqlite3.Connection, cluster_id: int) -> bool:
    """Say whether the cluster's canonical posting has left its board."""
    row = conn.execute(
        "SELECT p.disappeared_at AS gone FROM clusters c "
        "JOIN postings p ON p.id = c.canonical_posting_id WHERE c.id = ?",
        (cluster_id,),
    ).fetchone()
    return bool(row and row["gone"])


def _failed_sources(conn: sqlite3.Connection, now: datetime) -> tuple[str, ...]:
    """List sources that failed today, so breakage never reads as quiet."""
    rows = conn.execute(
        "SELECT DISTINCT source FROM poll_runs WHERE outcome = 'failed' "
        "AND date(started_at) = date(?)",
        (now.isoformat(),),
    ).fetchall()
    return tuple(row["source"] for row in rows)
