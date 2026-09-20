"""The inbound listener.

Polling a mailbox turns the application tracker from a table maintained by hand
into one that maintains itself: employer mail is highly patterned, and a
classified reply advances an application's status on the day it happens rather
than when someone remembers to log it. "No response in 21 days" becomes a
computable fact, which is the most useful follow-up trigger in a job search and
the one everybody drops.

Two design constraints run through this module.

**IDLE is not trusted.** Its characteristic failure is silent — the connection
stays open, the client believes it is subscribed, and the server has quietly
stopped sending. Nothing errors. So a full sweep runs every 15 minutes whatever
IDLE thinks, which bounds that failure at 15 minutes instead of indefinitely,
and the subscription is renewed at 29 minutes because RFC 2177 lets a server
drop an idle connection at 30.

**This process cannot send mail.** Not by policy: it is handed the mailbox
credential and nothing else, and it imports no SMTP code. A bug here cannot
email an employer however badly it misbehaves.
"""

from __future__ import annotations

import contextlib
import imaplib
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.utils import parsedate_to_datetime

from winnow import learning
from winnow.normalize import html_to_text
from winnow.secrets import resolve as resolve_secret

#: Renewed before the 30 minutes RFC 2177 permits a server to drop IDLE at.
IDLE_RENEWAL_SECONDS = 29 * 60

#: A full UID sweep regardless of IDLE state. This is what bounds the silent
#: failure, and it costs one cheap command per cycle.
SAFETY_SWEEP_SECONDS = 15 * 60

#: How often the Sent folder is re-read. Waking is cheap and re-reading a week
#: of sent mail is not: Dovecot sends `* OK Still here` every two minutes, and
#: IDLE wakes on it, so an unguarded sweep would re-fetch the whole lookback
#: window 720 times a day. Nothing in Sent is urgent — it is a record of what
#: has already happened.
SENT_SWEEP_SECONDS = 15 * 60

FIRST_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 5 * 60

#: Logged to stdout, which journald collects. Silence is the failure mode this
#: whole module is built around; a listener that cannot authenticate must not
#: look like a mailbox with nothing in it. Bodies are never logged — the journal
#: is not the place for an employer's letter.
logger = logging.getLogger(__name__)

_REJECTION_MARKERS = (
    "move forward with other",
    "not moving forward",
    "will not be moving forward",
    "decided not to proceed",
    "other candidates",
    "unsuccessful on this occasion",
    "we have filled",
)

_CONFIRMATION_MARKERS = (
    "thank you for applying",
    "received your application",
    "application received",
    "we have your application",
)

_SCHEDULING_MARKERS = (
    "set up a time",
    "your availability",
    "schedule a call",
    "book a time",
    "invite you to interview",
    "calendar link",
)

_OUTREACH_MARKERS = (
    "came across your profile",
    "wanted to reach out",
    "reaching out about",
    "saw your background",
)

#: Content types whose body this host cannot read, ever. The PGP private key is
#: kept on one machine with no passphrase and never deployed, so
#: an encrypted message is opaque here by design rather than by accident.
_ENCRYPTED_TYPES = ("multipart/encrypted", "application/pkcs7-mime")

ENCRYPTED_BODY = (
    "[encrypted message - the listener holds no key and cannot read this. "
    "Decrypt it on the laptop; classification and any outcome must be entered by hand.]"
)

#: Classifications that only make sense if an application already exists. Used
#: to fence the company-domain fallback: a cold recruiter writes from the same
#: address a real reply does, and must not be read as evidence of one.
_IMPLIES_AN_APPLICATION = frozenset({"rejection", "confirmation", "scheduling"})

#: Which classifications advance an application, and to what.
_OUTCOME_FOR = {
    "rejection": "auto_reject",
    "scheduling": "recruiter_screen",
}


@dataclass(frozen=True)
class ListenerConfig:
    """Where the mailbox is, and which credential to use.

    Read-only by construction: these are the IMAP credentials and there is
    deliberately no SMTP counterpart on this object.
    """

    host: str = ""
    port: int = 993
    mailbox: str = "INBOX"
    #: Where the mail client files what it sends. Named differently by every
    #: server; a name that does not exist is logged and skipped, never fatal.
    sent_folder: str = "Sent"
    #: How far back a Sent sweep looks. Sent mail is already \Seen, so there is
    #: no unread flag to sweep by, and re-reading a few days costs nothing
    #: because ingest is keyed on Message-ID.
    sent_lookback_days: int = 7
    username_ref: str = "env:WINNOW_MAIL_USERNAME"
    password_ref: str = "env:WINNOW_MAIL_PASSWORD"


@dataclass(frozen=True)
class InboundMessage:
    """One message as the listener sees it."""

    message_id: str
    from_addr: str
    subject: str
    body: str
    received_at: datetime
    in_reply_to: str | None = None
    references: str | None = None
    encrypted: bool = False
    #: Set when the message came out of the Sent folder rather than the inbox.
    #: Same shape either way: a sent letter differs in direction, not in form.
    to_addr: str = ""


@dataclass(frozen=True)
class Classification:
    """What a message looks like, and how sure that is."""

    classification: str
    confidence: float


@dataclass(frozen=True)
class Ingested:
    """What storing one message established."""

    message_id: str
    classification: str
    cluster_id: int | None
    outcome: str | None


def classify(subject: str, body: str) -> Classification:
    """Classify one employer message.

    Args:
        subject: The subject line.
        body: The plain-text body.

    Returns:
        The classification and a confidence. A message matching markers from
        more than one category comes back ``unclassified`` at zero confidence:
        the expensive mistake here is marking something rejected that was not,
        so an ambiguous message goes to a human rather than to a guess.
    """
    haystack = f"{subject}\n{body}".lower()
    matched = [
        name
        for name, markers in (
            ("rejection", _REJECTION_MARKERS),
            ("confirmation", _CONFIRMATION_MARKERS),
            ("scheduling", _SCHEDULING_MARKERS),
            ("recruiter_outreach", _OUTREACH_MARKERS),
        )
        if any(marker in haystack for marker in markers)
    ]

    if len(matched) != 1:
        return Classification("unclassified", 0.0)
    return Classification(matched[0], 0.8)


def ingest(
    conn: sqlite3.Connection,
    message: InboundMessage,
    *,
    escalate: Callable[[str], None] | None = None,
) -> Ingested:
    """Store one inbound message and advance what it establishes.

    Args:
        conn: An open connection.
        message: The message.
        escalate: Called with a short notice when the message is an interview
            request. Waiting for tomorrow's digest is the wrong latency for
            that one case.

    Returns:
        What was recorded. A message that could not be correlated is still
        stored: it surfaces in review rather than being dropped.
    """
    already = conn.execute(
        "SELECT id FROM messages WHERE message_id = ?", (message.message_id,)
    ).fetchone()
    if already is not None:
        existing = conn.execute(
            "SELECT classification, cluster_id FROM messages WHERE id = ?", (already["id"],)
        ).fetchone()
        return Ingested(
            message_id=message.message_id,
            classification=existing["classification"] or "unclassified",
            cluster_id=existing["cluster_id"],
            outcome=None,
        )

    verdict = classify(message.subject, message.body)
    cluster_id, company_id = _correlate(conn, message, verdict.classification)

    conn.execute(
        "INSERT INTO messages (message_id, in_reply_to, references_ids, direction, "
        "company_id, cluster_id, from_addr, subject, received_at, classification, "
        "confidence, body_text) VALUES (?, ?, ?, 'inbound', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message.message_id,
            message.in_reply_to,
            message.references,
            company_id,
            cluster_id,
            message.from_addr,
            message.subject,
            message.received_at.isoformat(),
            verdict.classification,
            verdict.confidence,
            message.body,
        ),
    )

    outcome = _OUTCOME_FOR.get(verdict.classification)
    if outcome is not None and cluster_id is not None:
        conn.execute(
            "INSERT INTO outcomes (cluster_id, outcome, occurred_on, rejection_text) "
            "VALUES (?, ?, ?, ?)",
            (
                cluster_id,
                outcome,
                message.received_at.date().isoformat(),
                message.body if verdict.classification == "rejection" else None,
            ),
        )

    if cluster_id is not None:
        _mark_application_submitted(conn, cluster_id)

    logger.info(
        "ingested %s from %s: %s%s%s",
        message.message_id,
        message.from_addr,
        verdict.classification,
        " (encrypted, unreadable here)" if message.encrypted else "",
        f" -> {outcome}" if outcome and cluster_id is not None else "",
    )

    if verdict.classification == "scheduling" and escalate is not None:
        escalate(f"Interview request from {message.from_addr}: {message.body}")

    return Ingested(
        message_id=message.message_id,
        classification=verdict.classification,
        cluster_id=cluster_id,
        outcome=outcome if cluster_id is not None else None,
    )


def backoff_delays() -> Iterator[int]:
    """Yield reconnect delays, doubling to a ceiling.

    Returns:
        5, 10, 20, ... up to :data:`MAX_BACKOFF_SECONDS`, forever. A server
        restart should not turn the listener into a hammer.
    """
    delay = FIRST_BACKOFF_SECONDS
    while True:
        yield delay
        delay = min(delay * 2, MAX_BACKOFF_SECONDS)


def parse_message(raw: bytes, *, fallback_now: datetime | None = None) -> InboundMessage:
    """Parse a fetched message into the shape the listener works with.

    Args:
        raw: The RFC 5322 bytes as fetched.
        fallback_now: Used when the message carries no parseable Date.

    Returns:
        The parsed message.
    """
    parsed = message_from_bytes(raw)
    encrypted = parsed.get_content_type() in _ENCRYPTED_TYPES
    body = ENCRYPTED_BODY if encrypted else _plain_text(parsed)

    received_at = fallback_now or datetime.now(UTC)
    if parsed["Date"]:
        # A malformed Date is not a reason to drop the message; the time it was
        # fetched is close enough for a follow-up clock.
        with contextlib.suppress(TypeError, ValueError):
            received_at = parsedate_to_datetime(parsed["Date"])

    return InboundMessage(
        message_id=str(parsed["Message-ID"] or ""),
        from_addr=str(parsed["From"] or ""),
        to_addr=str(parsed["To"] or ""),
        subject=str(parsed["Subject"] or ""),
        body=body,
        received_at=received_at,
        in_reply_to=parsed["In-Reply-To"],
        references=parsed["References"],
        encrypted=encrypted,
    )


def connect(config: ListenerConfig) -> imaplib.IMAP4_SSL:
    """Open and authenticate an IMAP connection.

    Args:
        config: Mailbox location and credential references.

    Returns:
        A selected, ready connection.

    Raises:
        imaplib.IMAP4.error: If the server refuses the login.
        RuntimeError: If a credential cannot be resolved.
    """
    connection = imaplib.IMAP4_SSL(config.host, config.port)
    connection.login(resolve_secret(config.username_ref), resolve_secret(config.password_ref))
    connection.select(config.mailbox)
    return connection


def sweep(conn: sqlite3.Connection, connection, *, escalate=None) -> list[Ingested]:
    """Fetch and ingest everything unseen.

    Runs on every reconnect as a catch-up, and on the safety interval whatever
    IDLE believes. Messages are left unread on the server — the mailbox is a
    human's too, and marking it up is not this process's business.

    Args:
        conn: An open store connection.
        connection: An authenticated IMAP connection.
        escalate: Passed through to :func:`ingest`.

    Returns:
        What was ingested this sweep.
    """
    status, payload = connection.search(None, "UNSEEN")
    if status != "OK":
        return []

    uids = payload[0].split()
    ingested: list[Ingested] = []
    for uid in uids:
        status, fetched = connection.fetch(uid, "(BODY.PEEK[])")
        if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
            continue
        ingested.append(ingest(conn, parse_message(fetched[0][1]), escalate=escalate))

    logger.info("swept %d unseen, ingested %d", len(uids), len(ingested))
    return ingested


def sweep_sent(
    conn: sqlite3.Connection,
    connection,
    *,
    mailbox: str,
    sent_folder: str,
    lookback_days: int = 7,
    now: datetime | None = None,
) -> list[Ingested]:
    """Read recently sent mail, then put the connection back where it was.

    Sent mail is already flagged read, so there is no UNSEEN to sweep by and
    the search is by date instead. Re-reading the same messages is harmless:
    ingest is keyed on Message-ID and the second pass is a no-op.

    The connection must end up back on the watched folder. IDLE subscribes to
    whichever folder is selected, so leaving it on Sent would mean the next
    IDLE watches the wrong one and inbound mail goes unnoticed until the safety
    sweep — a failure indistinguishable from a quiet week.

    Args:
        conn: An open store connection.
        connection: An authenticated IMAP connection.
        mailbox: The folder to re-select afterwards.
        sent_folder: The folder to read.
        lookback_days: How far back to search.
        now: Reference time.

    Returns:
        What was learned this sweep.
    """
    now = now or datetime.now(UTC)
    ingested: list[Ingested] = []
    try:
        status, _ = connection.select(sent_folder)
        if status != "OK":
            logger.info("no %s folder on this server; skipping sent sweep", sent_folder)
            return []

        since = (now - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        status, payload = connection.search(None, "SINCE", since)
        if status != "OK":
            return []

        for uid in payload[0].split():
            status, fetched = connection.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not fetched or not isinstance(fetched[0], tuple):
                continue
            ingested.append(ingest_sent(conn, parse_message(fetched[0][1])))
    finally:
        connection.select(mailbox)

    learned = [row for row in ingested if row.cluster_id is not None]
    if learned:
        logger.info(
            "sent sweep learned %d hand-sent application%s",
            len(learned),
            "" if len(learned) == 1 else "s",
        )
    return learned


def run(
    conn: sqlite3.Connection,
    config: ListenerConfig | None = None,
    *,
    escalate: Callable[[str], None] | None = None,
    connect_fn: Callable[[ListenerConfig], object] = connect,
    stop: Callable[[], bool] = lambda: False,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Listen for employer mail until told to stop.

    Args:
        conn: An open store connection.
        config: Mailbox location and credential references.
        escalate: Called for interview requests.
        connect_fn: Injected for tests.
        stop: Checked between cycles.
        clock: Monotonic source, so the Sent interval is testable without
            waiting a quarter of an hour.
    """
    config = config or ListenerConfig()
    delays = backoff_delays()
    last_sent_sweep = float("-inf")

    while not stop():
        try:
            connection = connect_fn(config)
        except Exception as error:  # noqa: BLE001 - dying on one refusal is useless
            delay = next(delays)
            logger.warning("connect failed (%s); retrying in %ds", error, delay)
            time.sleep(delay)
            continue

        logger.info("connected to %s, watching %s", config.host, config.mailbox)
        delays = backoff_delays()
        sweep(conn, connection, escalate=escalate)
        _sweep_sent(conn, connection, config)
        last_sent_sweep = clock()

        try:
            while not stop():
                # The subscription is re-issued on the shorter of the two
                # intervals, so IDLE never outlives what RFC 2177 guarantees and
                # a sweep still happens every 15 minutes whether or not the
                # server said anything. Waking is not evidence that mail arrived,
                # and not waking is not evidence that none did.
                with connection.idle(min(IDLE_RENEWAL_SECONDS, SAFETY_SWEEP_SECONDS)) as idler:
                    for _ in idler:
                        break
                sweep(conn, connection, escalate=escalate)
                if clock() - last_sent_sweep >= SENT_SWEEP_SECONDS:
                    _sweep_sent(conn, connection, config)
                    last_sent_sweep = clock()
        except Exception as error:  # noqa: BLE001 - reconnect rather than exit
            delay = next(delays)
            logger.warning("listening failed (%s); retrying in %ds", error, delay)
            time.sleep(delay)
        finally:
            with contextlib.suppress(Exception):
                connection.logout()


def _sweep_sent(conn: sqlite3.Connection, connection, config: ListenerConfig) -> None:
    """Sweep Sent without letting its failure end the inbound watch.

    Reading Sent is a bonus: it recovers applications posted by hand. Watching
    the inbox is the job. A server that names the folder something else must
    not stop the second thing happening.
    """
    try:
        sweep_sent(
            conn,
            connection,
            mailbox=config.mailbox,
            sent_folder=config.sent_folder,
            lookback_days=config.sent_lookback_days,
        )
    except Exception as error:  # noqa: BLE001 - the inbound watch matters more
        logger.warning("sent sweep failed (%s); continuing", error)


def ingest_sent(conn: sqlite3.Connection, message: InboundMessage) -> Ingested:
    """Learn an application that was sent by hand, from the Sent folder.

    Some applications go out from a mail client rather than from here, and
    until now they were invisible three times over: never marked submitted, so
    never counted; never revised into the store, so the letter that actually
    worked was lost; and never threaded, so no reply to one could be correlated
    to anything.

    Reading the folder settles all three. The Sent folder is a human's, though,
    and almost everything in it is not an application, so the match is made on
    the recipient's domain against an employer with a draft, and refused when
    more than one draft could be meant.

    Args:
        conn: An open store connection.
        message: A message parsed out of the Sent folder.

    Returns:
        What was recorded. An uncorrelated message is simply ignored: unlike
        inbound mail there is nothing to review, since it is ordinary
        correspondence that happens to share a mailbox.
    """
    already = conn.execute(
        "SELECT cluster_id FROM messages WHERE message_id = ?", (message.message_id,)
    ).fetchone()
    if already is not None:
        return Ingested(
            message_id=message.message_id,
            classification="outbound",
            cluster_id=already["cluster_id"],
            outcome=None,
        )

    cluster_id, company_id = _correlate_sent(conn, message)
    if cluster_id is None:
        return Ingested(
            message_id=message.message_id,
            classification="outbound",
            cluster_id=None,
            outcome=None,
        )

    conn.execute(
        # classification stays NULL: it describes what an employer's mail
        # looks like, and this is ours. `direction` already says which it is.
        "INSERT INTO messages (message_id, direction, company_id, cluster_id, from_addr, "
        "subject, received_at, body_text) VALUES (?, 'outbound', ?, ?, ?, ?, ?, ?)",
        (
            message.message_id,
            company_id,
            cluster_id,
            message.from_addr,
            message.subject,
            message.received_at.isoformat(),
            message.body,
        ),
    )

    # Only the first letter settles it. A follow-up chasing a reply is not the
    # application, and overwriting would replace the evidence with the chaser.
    row = conn.execute(
        "SELECT id FROM drafts WHERE cluster_id = ? AND final_body IS NULL ORDER BY id LIMIT 1",
        (cluster_id,),
    ).fetchone()
    if row is not None:
        learning.record_final(
            conn, int(row["id"]), message.body, source="sent_folder", now=message.received_at
        )

    logger.info("learned a hand-sent application to cluster %s", cluster_id)
    return Ingested(
        message_id=message.message_id,
        classification="outbound",
        cluster_id=cluster_id,
        outcome=None,
    )


def _correlate_sent(
    conn: sqlite3.Connection, message: InboundMessage
) -> tuple[int | None, int | None]:
    """Match a sent letter to the application it was sent about.

    Same fences as the inbound company match: a draft must exist, and exactly
    one cluster may match, because two open roles at one employer leave nothing
    in the mail to say which was applied for.
    """
    domain = message.to_addr.rpartition("@")[2].strip(" >").lower()
    if not domain:
        return None, None
    label = _domain_label(domain)

    rows = conn.execute(
        """
        SELECT DISTINCT c.id AS cluster_id, co.id AS company_id, co.name AS name
        FROM clusters c
        JOIN companies co ON co.id = c.company_id
        WHERE EXISTS (SELECT 1 FROM drafts d WHERE d.cluster_id = c.id)
        """
    ).fetchall()
    matched = [row for row in rows if _slug(row["name"]) == label]

    if len(matched) != 1:
        return None, None
    return int(matched[0]["cluster_id"]), matched[0]["company_id"]


def _correlate(
    conn: sqlite3.Connection, message: InboundMessage, classification: str
) -> tuple[int | None, int | None]:
    """Match a reply back to the application it belongs to.

    Threading is the reason every outbound message gets a Message-ID: without
    one, employer replies cannot be correlated and the whole inbound design
    stops working.

    Most applications, though, are pasted into an employer's own form and send
    nothing, so there is no thread for their reply to join. Where threading
    fails, the sender's domain is matched against a company with a draft — see
    :func:`_correlate_by_company`, which is deliberately narrower than it could
    be.
    """
    candidates = [
        reference
        for reference in [message.in_reply_to, *(message.references or "").split()]
        if reference
    ]
    for reference in candidates:
        row = conn.execute(
            "SELECT cluster_id, company_id FROM messages WHERE message_id = ?", (reference,)
        ).fetchone()
        if row is not None and row["cluster_id"] is not None:
            return int(row["cluster_id"]), row["company_id"]

        draft = conn.execute(
            "SELECT cluster_id FROM drafts WHERE sent_message_id = ?", (reference,)
        ).fetchone()
        if draft is not None:
            return int(draft["cluster_id"]), None

    return _correlate_by_company(conn, message, classification)


def _correlate_by_company(
    conn: sqlite3.Connection, message: InboundMessage, classification: str
) -> tuple[int | None, int | None]:
    """Match an unthreaded reply to an application by the sender's domain.

    Weaker evidence than a thread, so it is fenced on three sides:

    * only for mail that implies an application already exists. A cold
      recruiter writes from the same domain as a real reply, and treating that
      as proof of an application would invent one.
    * only against clusters that have a draft. No draft, no application.
    * only when exactly one such cluster exists. Two open roles at one employer
      and nothing here says which replied, so it stays uncorrelated and
      surfaces in review rather than attaching a real outcome to a guess.
    """
    if classification not in _IMPLIES_AN_APPLICATION:
        return None, None

    domain = message.from_addr.rpartition("@")[2].lower()
    if not domain:
        return None, None
    label = _domain_label(domain)

    rows = conn.execute(
        """
        SELECT DISTINCT c.id AS cluster_id, co.id AS company_id, co.name AS name
        FROM clusters c
        JOIN companies co ON co.id = c.company_id
        WHERE EXISTS (SELECT 1 FROM drafts d WHERE d.cluster_id = c.id)
        """
    ).fetchall()
    matched = [row for row in rows if _slug(row["name"]) == label]

    if len(matched) != 1:
        return None, None
    return int(matched[0]["cluster_id"]), matched[0]["company_id"]


def _domain_label(domain: str) -> str:
    """The registrable label of a domain: ``talent.grafanalabs.com`` -> ``grafanalabs``."""
    parts = [part for part in domain.split(".") if part]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def _slug(name: str) -> str:
    """Reduce a company name to something a domain label can equal."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _mark_application_submitted(conn: sqlite3.Connection, cluster_id: int) -> None:
    """Treat a correlated reply as proof the application was submitted.

    The keystroke for a pasted application is easy to miss, and an unmarked
    application is one calibration never divides by. A reply is evidence that
    it happened, so the oldest unsubmitted draft for that opportunity is marked
    rather than left to age into a false silence.

    A cluster that already has a submitted draft is already settled, and a
    later redraft is not a second application. Seen live at Nextcloud: the Sent
    sweep recorded the letter that went out, their confirmation arrived, and
    this marked a redraft that had never been sent anywhere.
    """
    settled = conn.execute(
        "SELECT 1 FROM drafts WHERE cluster_id = ? AND submitted_at IS NOT NULL LIMIT 1",
        (cluster_id,),
    ).fetchone()
    if settled is not None:
        return

    row = conn.execute(
        "SELECT id FROM drafts WHERE cluster_id = ? AND submitted_at IS NULL ORDER BY id LIMIT 1",
        (cluster_id,),
    ).fetchone()
    if row is not None:
        learning.mark_submitted(conn, int(row["id"]))


def _plain_text(parsed) -> str:
    """Extract readable text, preferring a plain part and falling back to HTML.

    Employer mail is routinely HTML-only. Ignoring it would throw away the
    rejection text the design calls the highest-value data in the system.
    """
    if not parsed.is_multipart():
        text = _decode(parsed)
        return text if parsed.get_content_type() != "text/html" else (html_to_text(text) or "")

    for content_type in ("text/plain", "text/html"):
        for part in parsed.walk():
            if part.get_content_type() != content_type:
                continue
            text = _decode(part)
            if not text:
                continue
            return text if content_type == "text/plain" else (html_to_text(text) or "")
    return ""


def _decode(part) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    return payload.decode(part.get_content_charset() or "utf-8", "replace")
