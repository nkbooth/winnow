"""Storing and sending drafts.

The credential boundary is the point of this queue. Drafting writes here;
sending reads from here and happens only on an explicit keystroke against one
specific draft, in the one process that can resolve an SMTP credential.
"""

import pytest

from winnow import learning, store
from winnow.dedupe import cluster_postings, persist_cluster
from winnow.drafting import Draft
from winnow.review import data


def _draft(**overrides):
    base = {
        "kind": "cover_letter",
        "resume_variant": "A",
        "subject": "Director of Business Systems — Alex Rivera",
        "body": "Dear hiring team,\n\nI build the systems that cross departments.\n\nAlex",
        "built_from": {"company": "Grafana Labs", "model": "claude-opus-5", "claims": []},
    }
    return Draft(**{**base, **overrides})


@pytest.fixture
def cluster(conn, make_posting):
    company_id = store.insert_company(conn, "Grafana Labs")
    return persist_cluster(
        conn,
        cluster_postings([make_posting(company="Grafana Labs", description_complete=True)])[0],
        company_id=company_id,
    )


def test_a_draft_is_stored_with_its_provenance(conn, cluster):
    draft_id = learning.record_draft(conn, cluster, _draft())
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()

    assert row["kind"] == "cover_letter"
    assert row["resume_variant"] == "A"
    assert "cross departments" in row["body"]
    assert row["sent_at"] is None

    import json

    assert json.loads(row["built_from"])["company"] == "Grafana Labs"


def test_untraceable_claims_are_stored_against_the_draft(conn, cluster):
    """Kept, not dropped: an unverifiable claim is the thing to look at."""
    draft_id = learning.record_draft(
        conn, cluster, _draft(unverified=({"claim": "Led forty people", "evidence": "x"},))
    )
    stored = learning.drafts_for(conn, cluster)[0]

    assert stored.id == draft_id
    assert stored.needs_attention is True
    assert stored.unverified[0]["claim"] == "Led forty people"


def test_drafts_come_back_newest_first(conn, cluster):
    learning.record_draft(conn, cluster, _draft(subject="first"))
    learning.record_draft(conn, cluster, _draft(subject="second"))
    assert [d.subject for d in learning.drafts_for(conn, cluster)] == ["second", "first"]


def test_sending_a_draft_without_a_recipient_is_refused(conn, cluster, profile):
    """An ATS application is a form to paste into, not an email to send."""
    learning.record_draft(conn, cluster, _draft())
    stored = learning.drafts_for(conn, cluster)[0]

    with pytest.raises(data.NotSendable) as error:
        data.send_draft(conn, stored, profile, send=lambda message: "$never")

    assert "recipient" in str(error.value).lower()
    assert conn.execute("SELECT sent_at FROM drafts").fetchone()["sent_at"] is None


def test_sending_a_draft_records_what_left(conn, cluster, profile):
    """The Message-ID is how an employer's reply finds its way back."""
    learning.record_draft(conn, cluster, _draft(recipient="talent@grafana.example"))
    stored = learning.drafts_for(conn, cluster)[0]
    sent = []

    def send(message):
        sent.append(message)
        return "<out-1@example.invalid>"

    data.send_draft(conn, stored, profile, send=send)

    assert sent[0]["To"] == "talent@grafana.example"
    assert sent[0]["From"].startswith("alex@example.invalid")
    assert sent[0]["Subject"] == "Director of Business Systems — Alex Rivera"
    assert sent[0]["Date"] and sent[0]["Message-ID"]

    row = conn.execute("SELECT sent_at, sent_message_id FROM drafts").fetchone()
    assert row["sent_at"] is not None
    assert row["sent_message_id"] == "<out-1@example.invalid>"


def test_an_outbound_message_is_recorded_for_thread_correlation(conn, cluster, profile):
    learning.record_draft(conn, cluster, _draft(recipient="talent@grafana.example"))
    stored = learning.drafts_for(conn, cluster)[0]
    data.send_draft(conn, stored, profile, send=lambda message: "<out-2@example.invalid>")

    row = conn.execute(
        "SELECT direction, cluster_id FROM messages WHERE message_id = ?",
        ("<out-2@example.invalid>",),
    ).fetchone()
    assert row["direction"] == "outbound"
    assert row["cluster_id"] == cluster


def test_a_draft_is_not_sent_twice(conn, cluster, profile):
    learning.record_draft(conn, cluster, _draft(recipient="talent@grafana.example"))
    stored = learning.drafts_for(conn, cluster)[0]
    data.send_draft(conn, stored, profile, send=lambda message: "<out-3@example.invalid>")

    with pytest.raises(data.NotSendable):
        data.send_draft(
            conn, learning.drafts_for(conn, cluster)[0], profile, send=lambda m: "<again>"
        )


def test_tailoring_notes_survive_the_round_trip(conn, cluster):
    """They are the most actionable part of a draft and were not being stored.

    Re-opening a draft showed the letter and silently lost the instructions for
    which bullets to lead with.
    """
    learning.record_draft(
        conn,
        cluster,
        _draft(tailoring_notes=("Lead with the capacity scheduler", "Mirror 'cross-functional'")),
    )
    stored = learning.drafts_for(conn, cluster)[0]

    assert stored.tailoring_notes == (
        "Lead with the capacity scheduler",
        "Mirror 'cross-functional'",
    )


def test_sending_a_draft_submits_it(conn, cluster):
    """Emailing an application is submitting it; there is no second keystroke."""
    draft_id = learning.record_draft(
        conn,
        cluster,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient="jobs@example.com",
            subject="Hello",
            body="Dear hiring team,",
            built_from={},
        ),
    )

    learning.mark_draft_sent(conn, draft_id, "<abc@example.invalid>")

    row = conn.execute(
        "SELECT sent_at, submitted_at FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    assert row["sent_at"] is not None
    assert row["submitted_at"] == row["sent_at"]


def test_the_sent_version_is_kept_beside_the_proposal(conn, cluster):
    """The gap between the two is the only correction the drafter ever gets."""
    draft_id = learning.record_draft(conn, cluster, _draft())

    learning.record_final(conn, draft_id, "What he actually sent.", source="pasted")

    stored = learning.drafts_for(conn, cluster)[0]
    assert stored.body.startswith("Dear hiring team,"), "the proposal survives"
    assert stored.final_body == "What he actually sent."
    assert stored.final_source == "pasted"


def test_recording_a_sent_version_submits_the_application(conn, cluster):
    """Nobody revises a letter they are not about to send."""
    draft_id = learning.record_draft(conn, cluster, _draft())

    learning.record_final(conn, draft_id, "Sent text.", source="pasted")

    row = conn.execute("SELECT submitted_at FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    assert row["submitted_at"] is not None


def test_the_drafter_is_shown_what_was_actually_sent(conn, cluster):
    """Register is the thing examples can teach and instructions cannot."""
    first = learning.record_draft(conn, cluster, _draft())
    learning.record_final(conn, first, "The revised one.", source="pasted")

    assert learning.recent_sent_letters(conn, limit=3) == ("The revised one.",)


def test_an_emailed_letter_is_an_example_because_the_body_is_what_left(conn, cluster):
    """`s` sends the stored body verbatim, so here the proposal is the letter."""
    draft_id = learning.record_draft(conn, cluster, _draft(recipient="jobs@example.com"))
    learning.mark_draft_sent(conn, draft_id, "<x@example.invalid>")

    examples = learning.recent_sent_letters(conn, limit=3)
    assert len(examples) == 1
    assert examples[0].startswith("Dear hiring team,")


def test_a_submission_with_no_captured_text_is_not_a_voice_example(conn, cluster):
    """Marking `a` after pasting into a form says an application happened.

    It says nothing about what text went out. If the letter was rewritten
    before pasting — the usual reason to rewrite it — then feeding the stored
    proposal back as an exemplar teaches the model that its own output is how
    he writes. That is the feedback loop running backwards: it reinforces the
    register it exists to correct, and looks like it is learning while it does.
    """
    draft_id = learning.record_draft(conn, cluster, _draft())
    learning.mark_submitted(conn, draft_id)

    assert learning.recent_sent_letters(conn, limit=3) == ()


def test_capturing_the_sent_text_makes_it_an_example(conn, cluster):
    """Which is what `e`, and the Sent-folder sweep, are for."""
    draft_id = learning.record_draft(conn, cluster, _draft())
    learning.record_final(conn, draft_id, "What actually went out.", source="pasted")

    assert learning.recent_sent_letters(conn, limit=3) == ("What actually went out.",)


def test_an_unsent_draft_is_not_an_example(conn, cluster):
    learning.record_draft(conn, cluster, _draft())

    assert learning.recent_sent_letters(conn, limit=3) == ()
