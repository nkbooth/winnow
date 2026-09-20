"""The inbound listener.

IDLE's characteristic failure is silent: the TCP connection stays open, the
client believes it is subscribed, and the server has quietly stopped sending.
Nothing errors — mail simply stops arriving and you find out days later. So the
listener does not trust IDLE. A full sweep runs every 15 minutes regardless,
which bounds that failure at 15 minutes instead of indefinitely, and the IDLE
subscription is renewed before the 30-minute mark RFC 2177 permits servers to
drop it at.

The other rule here is about classification, not timing: anything the classifier
is unsure about stays unclassified and surfaces in review. Marking something
rejected that was not is the expensive mistake.
"""

from datetime import UTC, datetime

import pytest

from winnow import learning, listener, store
from winnow.dedupe import cluster_postings, persist_cluster
from winnow.drafting import Draft

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def test_idle_is_renewed_before_the_server_may_drop_it():
    """RFC 2177 lets a server drop an idle connection at 30 minutes."""
    assert listener.IDLE_RENEWAL_SECONDS < 30 * 60
    assert listener.IDLE_RENEWAL_SECONDS == 29 * 60


def test_the_safety_sweep_bounds_the_silent_failure():
    assert listener.SAFETY_SWEEP_SECONDS == 15 * 60


def test_idle_never_outlasts_the_safety_interval():
    """Waking is not evidence mail arrived, and not waking is not evidence none did."""
    assert min(listener.IDLE_RENEWAL_SECONDS, listener.SAFETY_SWEEP_SECONDS) == 15 * 60


def test_reconnect_backs_off_and_stops_growing():
    """A server restart must not become a hammer."""
    delays = listener.backoff_delays()
    first_five = [next(delays) for _ in range(5)]
    assert first_five == [5, 10, 20, 40, 80]
    assert all(next(delays) <= listener.MAX_BACKOFF_SECONDS for _ in range(20))


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        (
            "Thank you for applying to Grafana Labs",
            "We have received your application and will be in touch.",
            "confirmation",
        ),
        (
            "Your application to Zapier",
            "After careful consideration we have decided to move forward with other candidates.",
            "rejection",
        ),
        (
            "Next steps - Director of Business Systems",
            "I'd love to set up a time to chat. What does your availability look like next week?",
            "scheduling",
        ),
        (
            "Opportunity at Percona",
            "I came across your profile and wanted to reach out about a role we're hiring for.",
            "recruiter_outreach",
        ),
    ],
)
def test_the_classifier_recognises_the_patterned_cases(subject, body, expected):
    assert listener.classify(subject, body).classification == expected


def test_an_ambiguous_message_stays_unclassified():
    """Marking something rejected that was not is the failure to design against."""
    result = listener.classify(
        "Your application",
        "We have decided to move forward with other candidates for this role, "
        "but I would love to set up a time to discuss other openings.",
    )
    assert result.classification == "unclassified"
    assert result.confidence == 0.0


def test_an_unrecognised_message_stays_unclassified_rather_than_other():
    result = listener.classify("Lunch?", "Are you free Thursday?")
    assert result.classification == "unclassified"


def test_a_reply_is_correlated_back_to_its_application(conn, make_posting):
    """Threading is why every outbound message gets a Message-ID."""
    company_id = store.insert_company(conn, "Grafana Labs")
    posting = make_posting(company="Grafana Labs", description_complete=True)
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    conn.execute(
        "INSERT INTO messages (message_id, direction, cluster_id, company_id, subject) "
        "VALUES ('<sent-1@example.invalid>', 'outbound', ?, ?, 'Application')",
        (cluster_id, company_id),
    )

    ingested = listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<reply-1@grafana.example>",
            in_reply_to="<sent-1@example.invalid>",
            references="<sent-1@example.invalid>",
            from_addr="talent@grafana.example",
            subject="Re: Application",
            body="After careful consideration we have decided to move forward "
            "with other candidates.",
            received_at=NOW,
        ),
    )

    assert ingested.cluster_id == cluster_id
    assert ingested.classification == "rejection"

    outcome = conn.execute("SELECT * FROM outcomes WHERE cluster_id = ?", (cluster_id,)).fetchone()
    assert outcome["outcome"] == "auto_reject"
    assert "move forward with other candidates" in outcome["rejection_text"]


def test_an_uncorrelated_message_is_still_stored_for_review(conn):
    ingested = listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<cold-1@example.test>",
            from_addr="recruiter@example.test",
            subject="Opportunity",
            body="I came across your profile and wanted to reach out.",
            received_at=NOW,
        ),
    )

    assert ingested.cluster_id is None
    row = conn.execute(
        "SELECT * FROM messages WHERE message_id = ?", ("<cold-1@example.test>",)
    ).fetchone()
    assert row["classification"] == "recruiter_outreach"


def test_the_same_message_is_not_ingested_twice(conn):
    message = listener.InboundMessage(
        message_id="<dup-1@example.test>",
        from_addr="talent@example.test",
        subject="Thank you for applying",
        body="We have received your application.",
        received_at=NOW,
    )
    listener.ingest(conn, message)
    listener.ingest(conn, message)

    assert conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"] == 1


def test_an_unclassified_message_advances_nothing(conn, make_posting):
    company_id = store.insert_company(conn, "Grafana Labs")
    posting = make_posting(company="Grafana Labs", description_complete=True)
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    conn.execute(
        "INSERT INTO messages (message_id, direction, cluster_id, company_id) "
        "VALUES ('<sent-2@example.invalid>', 'outbound', ?, ?)",
        (cluster_id, company_id),
    )

    listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<odd-1@grafana.example>",
            in_reply_to="<sent-2@example.invalid>",
            from_addr="talent@grafana.example",
            subject="Re: Application",
            body="Quick question before we continue.",
            received_at=NOW,
        ),
    )

    assert conn.execute("SELECT count(*) AS n FROM outcomes").fetchone()["n"] == 0


def test_an_interview_request_escalates_immediately(conn, make_posting):
    """Waiting for tomorrow's digest is the wrong latency for this one case."""
    company_id = store.insert_company(conn, "Grafana Labs")
    posting = make_posting(company="Grafana Labs", description_complete=True)
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    conn.execute(
        "INSERT INTO messages (message_id, direction, cluster_id, company_id) "
        "VALUES ('<sent-3@example.invalid>', 'outbound', ?, ?)",
        (cluster_id, company_id),
    )

    escalations = []
    listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<sched-1@grafana.example>",
            in_reply_to="<sent-3@example.invalid>",
            from_addr="talent@grafana.example",
            subject="Re: Application",
            body="I'd love to set up a time to chat - what's your availability?",
            received_at=NOW,
        ),
        escalate=escalations.append,
    )

    assert len(escalations) == 1
    assert "availability" in escalations[0].lower()


def test_the_listener_holds_no_credential_that_could_send_mail():
    """Structural, not policy: it cannot send because it has nothing to send with."""
    config = listener.ListenerConfig()

    # The boundary that holds on every deployment: there is no SMTP host to
    # connect to and no sending code to reach it. A deployment resolving its
    # credentials through 1Password can go further, because service accounts
    # scope to vaults and the digest timer can then be handed a token that
    # physically cannot read the mailbox — but that is a property of how the
    # references are backed, not of this module, and the structural guarantee
    # has to stand without it.
    assert not hasattr(config, "smtp_host")
    for reference in (config.username_ref, config.password_ref):
        assert reference.startswith(("env:", "file:", "op://")), "a reference, not a value"

    import inspect

    source = inspect.getsource(listener)
    assert "smtplib" not in source
    assert "winnow.mailer" not in source


class FakeIdler:
    def __init__(self, events):
        self._events = events

    def __enter__(self):
        return iter(self._events)

    def __exit__(self, *exc):
        return False


class FakeConnection:
    """An IMAP connection that answers a fixed script."""

    def __init__(self, messages=()):
        self._messages = list(messages)
        self.sweeps = 0
        self.logged_out = False

    def search(self, charset, criteria):
        self.sweeps += 1
        return "OK", [b" ".join(str(i).encode() for i in range(len(self._messages)))]

    def fetch(self, uid, spec):
        return "OK", [(b"1 (BODY[] {1})", self._messages[int(uid)])]

    def idle(self, timeout):
        return FakeIdler(["EXISTS"])

    def logout(self):
        self.logged_out = True


RAW = (
    b"Message-ID: <sweep-1@example.test>\r\n"
    b"From: talent@example.test\r\n"
    b"Subject: Thank you for applying\r\n"
    b"Date: Thu, 18 Sep 2026 12:00:00 -0400\r\n"
    b"\r\n"
    b"We have received your application.\r\n"
)


def test_a_catch_up_sweep_runs_on_every_connect(conn):
    """Anything that arrived while disconnected is picked up, not missed."""
    connection = FakeConnection([RAW])
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 2

    listener.run(conn, connect_fn=lambda config: connection, stop=stop)

    assert connection.sweeps >= 1
    assert conn.execute("SELECT count(*) AS n FROM messages").fetchone()["n"] == 1
    assert connection.logged_out is True


def test_a_refused_connection_backs_off_rather_than_exiting(conn, monkeypatch):
    slept: list[int] = []
    monkeypatch.setattr(listener.time, "sleep", slept.append)
    attempts = {"n": 0}

    def refuse(config):
        attempts["n"] += 1
        raise OSError("connection refused")

    listener.run(conn, connect_fn=refuse, stop=lambda: attempts["n"] >= 3)

    assert attempts["n"] == 3
    assert slept == [5, 10, 20][: len(slept)]


def test_a_successful_connection_says_so(conn, caplog):
    """An idle listener and a broken one must not look the same in the journal."""
    import logging

    connection = FakeConnection([RAW])
    calls = {"n": 0}

    with caplog.at_level(logging.INFO, logger="winnow.listener"):
        listener.run(
            conn,
            connect_fn=lambda config: connection,
            stop=lambda: (calls.__setitem__("n", calls["n"] + 1), calls["n"] > 2)[1],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("connected" in message for message in messages)
    assert any("ingested" in message or "swept" in message for message in messages)


def test_a_refused_connection_is_logged_with_its_backoff(conn, caplog, monkeypatch):
    import logging

    monkeypatch.setattr(listener.time, "sleep", lambda seconds: None)
    attempts = {"n": 0}

    def refuse(config):
        attempts["n"] += 1
        raise OSError("connection refused")

    with caplog.at_level(logging.WARNING, logger="winnow.listener"):
        listener.run(conn, connect_fn=refuse, stop=lambda: attempts["n"] >= 2)

    messages = [record.getMessage() for record in caplog.records]
    assert any("connection refused" in message for message in messages)
    assert any("retrying in" in message for message in messages)


def test_classified_mail_is_logged_without_quoting_the_body(conn, caplog):
    """The journal is not the place for an employer's letter."""
    import logging

    with caplog.at_level(logging.INFO, logger="winnow.listener"):
        listener.ingest(
            conn,
            listener.InboundMessage(
                message_id="<log-1@example.test>",
                from_addr="talent@example.test",
                subject="Thank you for applying",
                body="We have received your application. Reference 12345.",
                received_at=NOW,
            ),
        )

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "confirmation" in messages
    assert "Reference 12345" not in messages


ENCRYPTED = (
    b"Message-ID: <enc-1@example.test>\r\n"
    b"From: you@example.invalid\r\n"
    b"Subject: This is a test email\r\n"
    b'Content-Type: multipart/encrypted; protocol="application/pgp-encrypted";\r\n'
    b' boundary="x"\r\n'
    b"\r\n--x\r\nContent-Type: application/pgp-encrypted\r\n\r\nVersion: 1\r\n"
    b"\r\n--x\r\nContent-Type: application/octet-stream\r\n\r\n"
    b"-----BEGIN PGP MESSAGE-----\r\nhQIMA...\r\n-----END PGP MESSAGE-----\r\n--x--\r\n"
)

HTML_ONLY = (
    b"Message-ID: <html-1@example.test>\r\n"
    b"From: talent@example.test\r\n"
    b"Subject: Your application\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n<html><body><p>After careful consideration we have decided to move "
    b"forward with other candidates.</p></body></html>\r\n"
)


def test_an_encrypted_message_says_so_rather_than_looking_empty():
    """The private key is laptop-only by design and never reaches this host.

    So an encrypted message is unreadable here, permanently. Storing it as an
    empty body would make "we could not read this" indistinguishable from "this
    had nothing in it", which is the confusion this whole system is built to
    avoid.
    """
    parsed = listener.parse_message(ENCRYPTED)
    assert parsed.encrypted is True
    assert "encrypted" in parsed.body.lower()


def test_an_encrypted_message_is_stored_unclassified_with_its_marker(conn):
    ingested = listener.ingest(conn, listener.parse_message(ENCRYPTED))
    assert ingested.classification == "unclassified"

    row = conn.execute("SELECT body_text FROM messages").fetchone()
    assert "encrypted" in row["body_text"].lower()


def test_an_html_only_message_is_read(conn):
    """Employer mail is routinely HTML-only; ignoring it loses the rejection text."""
    parsed = listener.parse_message(HTML_ONLY)
    assert "move forward with other candidates" in parsed.body
    assert parsed.encrypted is False

    ingested = listener.ingest(conn, parsed)
    assert ingested.classification == "rejection"


def test_plain_text_is_still_preferred_over_html():
    raw = (
        b"Message-ID: <alt-1@example.test>\r\n"
        b"From: talent@example.test\r\n"
        b"Subject: Thanks\r\n"
        b'Content-Type: multipart/alternative; boundary="b"\r\n'
        b"\r\n--b\r\nContent-Type: text/plain\r\n\r\nthe plain part\r\n"
        b"\r\n--b\r\nContent-Type: text/html\r\n\r\n<p>the html part</p>\r\n--b--\r\n"
    )
    assert listener.parse_message(raw).body.strip() == "the plain part"


# ---------------------------------------------------------------------------
# The backstop: applications submitted through a form, and never marked
# ---------------------------------------------------------------------------


def _drafted_cluster(
    conn, make_posting, company="Grafana Labs", source_id="1", title="Director of RevOps"
):
    company_id = store.insert_company(conn, company)
    posting = make_posting(
        company=company, source_id=source_id, title=title, description_complete=True
    )
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient=None,
            subject="Application",
            body="Dear hiring team,",
            built_from={},
        ),
    )
    return cluster_id


def test_a_reply_correlates_by_company_when_there_is_no_thread(conn, make_posting):
    """A form application sends nothing, so there is no Message-ID to reply to.

    The employer still writes back. Matching on the sender's domain against a
    cluster that has a draft is weaker evidence than threading, and it is used
    only where threading has already failed — but it recovers exactly the case
    the thread-only design could never see.
    """
    cluster_id = _drafted_cluster(conn, make_posting, company="Grafana Labs")
    conn.execute(
        "UPDATE companies SET name = 'Grafana Labs' WHERE id = "
        "(SELECT company_id FROM clusters WHERE id = ?)",
        (cluster_id,),
    )

    ingested = listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<reply-9@grafanalabs.com>",
            from_addr="talent@grafanalabs.com",
            subject="Your application",
            body="Are you free for a call next week? Let us know your availability.",
            received_at=NOW,
        ),
    )

    assert ingested.cluster_id == cluster_id
    assert ingested.outcome == "recruiter_screen"


def test_a_correlated_reply_marks_the_application_submitted(conn, make_posting):
    """The keystroke was missed; the reply proves it happened anyway.

    The body has to actually read as a reply to an application — unclassified
    mail never correlates by domain, which is the fence doing its job.
    """
    cluster_id = _drafted_cluster(conn, make_posting, company="Grafana Labs")

    listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<reply-10@grafanalabs.com>",
            from_addr="talent@grafanalabs.com",
            subject="Your application",
            body="We would like to set up a time to talk next week.",
            received_at=NOW,
        ),
    )

    row = conn.execute(
        "SELECT submitted_at FROM drafts WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    assert row["submitted_at"] is not None


def test_company_correlation_refuses_to_guess_between_two_clusters(conn, make_posting):
    """Two open roles at one employer: nothing here says which one replied.

    Picking either would attach a real outcome to a possibly wrong application
    and quietly corrupt the calibration it feeds. The message is stored
    uncorrelated instead, which surfaces in review.
    """
    _drafted_cluster(conn, make_posting, source_id="a", title="Director of RevOps")
    _drafted_cluster(conn, make_posting, source_id="b", title="Director of Business Systems")

    ingested = listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<reply-11@grafanalabs.com>",
            from_addr="talent@grafanalabs.com",
            subject="Your application",
            body="We would like to set up a time to talk next week.",
            received_at=NOW,
        ),
    )

    assert ingested.cluster_id is None


def test_a_cold_recruiter_does_not_correlate_to_an_undrafted_cluster(conn, make_posting):
    """No draft means no application, so a mail from that domain proves nothing."""
    company_id = store.insert_company(conn, "Grafana Labs")
    posting = make_posting(company="Grafana Labs", description_complete=True)
    persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)

    ingested = listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<cold-9@grafanalabs.com>",
            from_addr="talent@grafanalabs.com",
            subject="Opportunity",
            body="I came across your profile and wanted to reach out.",
            received_at=NOW,
        ),
    )

    assert ingested.cluster_id is None


# ---------------------------------------------------------------------------
# The Sent folder: applications posted by hand, from a mail client
# ---------------------------------------------------------------------------


def _sent(**overrides):
    base = dict(
        message_id="<outbound-1@example.invalid>",
        from_addr="alex@example.invalid",
        to_addr="jobs@grafanalabs.com",
        subject="Director of RevOps — Alex Rivera",
        body="Dear hiring team,\n\nThe version I actually sent.\n\nAlex",
        received_at=NOW,
    )
    return listener.InboundMessage(**{**base, **overrides})


def test_a_hand_sent_application_is_learned_from_the_sent_folder(conn, make_posting):
    """Some applications go out from a mail client and this never sees them.

    Without reading Sent, such an application is invisible: unmarked, so it
    never enters the denominator, and unthreaded, so no reply to it can ever be
    correlated. Reading it back fixes all three at once.
    """
    cluster_id = _drafted_cluster(conn, make_posting)

    ingested = listener.ingest_sent(conn, _sent())

    assert ingested.cluster_id == cluster_id
    draft = conn.execute(
        "SELECT final_body, final_source, submitted_at FROM drafts WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    assert "The version I actually sent." in draft["final_body"]
    assert draft["final_source"] == "sent_folder"
    assert draft["submitted_at"] is not None


def test_the_sent_message_id_is_recorded_so_replies_can_thread(conn, make_posting):
    """The real prize: a reply to a hand-sent letter now correlates properly."""
    cluster_id = _drafted_cluster(conn, make_posting)
    listener.ingest_sent(conn, _sent())

    reply = listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<their-reply@grafanalabs.com>",
            in_reply_to="<outbound-1@example.invalid>",
            references="<outbound-1@example.invalid>",
            from_addr="talent@grafanalabs.com",
            subject="Re: Director of RevOps — Alex Rivera",
            body="We have decided to move forward with other candidates.",
            received_at=NOW,
        ),
    )

    assert reply.cluster_id == cluster_id
    assert reply.outcome == "auto_reject"


def test_a_sent_message_to_nobody_tracked_is_ignored(conn, make_posting):
    """The Sent folder is a human's. Most of what is in it is not an application."""
    _drafted_cluster(conn, make_posting)

    ingested = listener.ingest_sent(conn, _sent(to_addr="mum@example.test"))

    assert ingested.cluster_id is None
    assert (
        conn.execute("SELECT count(*) AS n FROM drafts WHERE submitted_at IS NOT NULL").fetchone()[
            "n"
        ]
        == 0
    )


def test_a_second_letter_to_the_same_employer_does_not_overwrite_the_first(conn, make_posting):
    """Once a draft records what was sent, it is settled."""
    cluster_id = _drafted_cluster(conn, make_posting)
    listener.ingest_sent(conn, _sent())

    listener.ingest_sent(
        conn, _sent(message_id="<outbound-2@example.invalid>", body="A follow-up.")
    )

    draft = conn.execute(
        "SELECT final_body FROM drafts WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    assert "The version I actually sent." in draft["final_body"]


def test_a_sent_message_is_only_ingested_once(conn, make_posting):
    _drafted_cluster(conn, make_posting)

    first = listener.ingest_sent(conn, _sent())
    second = listener.ingest_sent(conn, _sent())

    assert first.cluster_id == second.cluster_id
    assert (
        conn.execute("SELECT count(*) AS n FROM messages WHERE direction = 'outbound'").fetchone()[
            "n"
        ]
        == 1
    )


def test_an_ambiguous_employer_is_not_guessed_at(conn, make_posting):
    """Two open roles there and nothing in the mail says which was applied for."""
    _drafted_cluster(conn, make_posting, source_id="a", title="Director of RevOps")
    _drafted_cluster(conn, make_posting, source_id="b", title="Director of Business Systems")

    assert listener.ingest_sent(conn, _sent()).cluster_id is None


def test_the_sent_folder_is_swept_and_the_inbox_reselected(conn, make_posting):
    """IDLE is subscribed to one folder, so visiting another has to undo itself.

    Leaving the connection selected on Sent would mean the next IDLE watches
    the wrong mailbox, and inbound mail would go unnoticed until the safety
    sweep — a failure that looks exactly like a quiet week.
    """
    _drafted_cluster(conn, make_posting)
    raw = (
        b"Message-ID: <outbound-9@example.invalid>\r\n"
        b"From: alex@example.invalid\r\n"
        b"To: jobs@grafanalabs.com\r\n"
        b"Subject: Director of RevOps\r\n"
        b"Date: Fri, 18 Sep 2026 12:00:00 +0000\r\n\r\n"
        b"The letter as sent.\r\n"
    )

    class FakeConnection:
        def __init__(self):
            self.selected: list[str] = []

        def select(self, folder):
            self.selected.append(folder)
            return "OK", [b"1"]

        def search(self, charset, *criteria):
            return "OK", [b"1"]

        def fetch(self, uid, spec):
            return "OK", [(b"1 (BODY[])", raw)]

    connection = FakeConnection()
    ingested = listener.sweep_sent(conn, connection, mailbox="INBOX", sent_folder="Sent")

    assert [row.cluster_id for row in ingested] == [1]
    assert connection.selected == ["Sent", "INBOX"], "must return to the watched folder"


def test_a_sent_sweep_that_cannot_select_the_folder_is_not_fatal(conn):
    """Servers name it Sent, Sent Items, or [Gmail]/Sent Mail. Missing it is survivable."""

    class Refusing:
        def __init__(self):
            self.selected = []

        def select(self, folder):
            self.selected.append(folder)
            return ("NO", [b"nonexistent"]) if folder == "Sent" else ("OK", [b"1"])

        def search(self, charset, *criteria):
            raise AssertionError("must not search a folder it could not select")

    connection = Refusing()
    assert listener.sweep_sent(conn, connection, mailbox="INBOX", sent_folder="Sent") == []
    assert connection.selected == ["Sent", "INBOX"]


class SelectingConnection(FakeConnection):
    """A connection that also records folder selections, as the real one has."""

    def __init__(self, messages=()):
        super().__init__(messages)
        self.selected: list[str] = []

    def select(self, folder):
        self.selected.append(folder)
        return "OK", [b"0"]

    def search(self, charset, *criteria):
        self.sweeps += 1
        return "OK", [b""]


def test_the_sent_folder_is_not_swept_on_every_wake(conn, monkeypatch):
    """The server sends a keepalive every two minutes and IDLE wakes on it.

    Waking is cheap; re-reading a week of Sent is not. Measured on the live
    mailbox: Dovecot's `* OK Still here` fires every 2 minutes, so an unguarded
    sent sweep would re-select the folder and re-fetch every message in the
    lookback window 720 times a day for nothing.
    """
    connection = SelectingConnection()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 6

    # A clock that never advances: nothing may qualify for a second sweep.
    listener.run(conn, connect_fn=lambda config: connection, stop=stop, clock=lambda: 1000.0)

    assert connection.selected.count("Sent") == 1, "once on connect, then not again"


def test_the_sent_folder_is_swept_again_once_the_interval_passes(conn):
    connection = SelectingConnection()
    calls = {"n": 0}
    ticks = iter([0.0, 0.0, 10_000.0, 10_000.0, 20_000.0, 20_000.0, 30_000.0, 30_000.0])

    def stop():
        calls["n"] += 1
        return calls["n"] > 3

    listener.run(
        conn,
        connect_fn=lambda config: connection,
        stop=stop,
        clock=lambda: next(ticks, 99_999.0),
    )

    assert connection.selected.count("Sent") > 1


def test_a_reply_does_not_submit_a_second_draft(conn, make_posting):
    """Seen live: one application at Nextcloud, two drafts marked submitted.

    The sent sweep recorded the letter that went out. Their confirmation then
    arrived, and the backstop marked the oldest *unsubmitted* draft — a later
    redraft that was never sent anywhere. The backstop exists for applications
    with no mark at all, so a cluster that already has one is already settled.
    """
    cluster_id = _drafted_cluster(conn, make_posting)
    listener.ingest_sent(conn, _sent())
    _draft_for_cluster(conn, cluster_id)

    listener.ingest(
        conn,
        listener.InboundMessage(
            message_id="<confirm-1@grafanalabs.com>",
            from_addr="jobs@grafanalabs.com",
            subject="Thank you for applying",
            body="We have received your application and will be in touch.",
            received_at=NOW,
        ),
    )

    submitted = conn.execute(
        "SELECT count(*) AS n FROM drafts WHERE cluster_id = ? AND submitted_at IS NOT NULL",
        (cluster_id,),
    ).fetchone()["n"]
    assert submitted == 1


def _draft_for_cluster(conn, cluster_id):
    learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="C",
            recipient=None,
            subject="A later redraft",
            body="Take two.",
            built_from={},
        ),
    )


def test_the_listener_config_is_built_from_settings():
    """Settings exist to be read. A config that ignores them connects nowhere.

    Caught in deployment: the listener's dataclass defaults are empty, which is
    correct for a machine with no mailbox, and nothing carried config.toml into
    them — so a host with a perfectly good [mail] section tried to reach an
    empty hostname and reported connection refused every few seconds.
    """
    from winnow.settings import MailSettings

    mail = MailSettings(
        imap_host="imap.example.com",
        imap_port=1993,
        mailbox="Archive",
        sent_folder="Sent Items",
        username_ref="file:/run/secrets/u",
        password_ref="file:/run/secrets/p",
    )

    config = listener.ListenerConfig.from_settings(mail)

    assert config.host == "imap.example.com"
    assert config.port == 1993
    assert config.mailbox == "Archive"
    assert config.sent_folder == "Sent Items"
    assert config.username_ref == "file:/run/secrets/u"
    assert config.password_ref == "file:/run/secrets/p"
    assert not hasattr(config, "smtp_host"), "still no way to send"
