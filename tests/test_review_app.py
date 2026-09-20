"""The review app itself, driven by Textual's test pilot.

Only the behaviour that would be wrong if the widgets were rewired: that the
queue shows what is waiting, that a decision drains it, and that a pass cannot
be recorded without a reason.
"""

from datetime import UTC, datetime

import pytest
from textual.widgets import Static

from winnow import learning, store
from winnow.dedupe import cluster_postings, persist_cluster
from winnow.review import data
from winnow.review.app import ReasonScreen, ReviewApp, render_detail
from winnow.scoring import BreakdownLine, ScoreRecord

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _record(score):
    return ScoreRecord(
        score=score,
        breakdown=(BreakdownLine("growth_signal", 1.0, 20, 20.0, "own the function", False),),
        vetoes=(),
        flags=(),
        unverified=(),
        why_fits="Owns the function.",
        concern="Comp unresolved.",
        vetoed=False,
        model="claude-opus-5",
        prompt_version="1",
        rubric_version="v1.test",
    )


@pytest.fixture
def app(conn, profile, make_posting, tmp_path):
    company_id = store.insert_company(conn, "Grafana Labs")
    for title, score in (("Director, Business Systems", 87), ("Director of RevOps", 74)):
        posting = make_posting(
            company="Grafana Labs",
            title=title,
            source_id=title,
            description_complete=True,
        )
        cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
        learning.record_score(conn, cluster_id, _record(score))

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text("digest:\n  score_threshold: 70\n")
    return ReviewApp(conn, profile, profile_path)


async def test_the_queue_shows_what_is_waiting(app):
    async with app.run_test() as pilot:
        table = app.query_one("#queue")
        assert table.row_count == 2
        assert "2 awaiting review" in app.sub_title
        await pilot.pause()


async def test_marking_interested_drains_the_row(app, conn):
    async with app.run_test() as pilot:
        await pilot.press("i")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 1
    assert conn.execute("SELECT count(*) AS n FROM decisions").fetchone()["n"] == 1


async def test_deferring_moves_the_row_to_the_deferred_list(app, conn):
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 1

        for _ in range(3):
            await pilot.press("v")
            await pilot.pause()
        assert "deferred" in app.sub_title
        assert app.query_one("#queue").row_count == 1


async def test_a_deferred_row_can_be_decided_from_its_own_list(app, conn):
    """Deferring is not an answer, so the list has to be one you can leave."""
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        for _ in range(3):
            await pilot.press("v")
            await pilot.pause()

        await pilot.press("i")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 0


async def test_passing_asks_for_a_reason_and_records_it(app, conn):
    async with app.run_test() as pilot:
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ReasonScreen)
        await pilot.press("enter")
        await pilot.pause()

    row = conn.execute("SELECT decision, reason FROM decisions").fetchone()
    assert row["decision"] == "pass"
    assert row["reason"] in data.PASS_REASONS


async def test_escaping_the_reason_prompt_records_nothing(app, conn):
    async with app.run_test() as pilot:
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 2
    assert conn.execute("SELECT count(*) AS n FROM decisions").fetchone()["n"] == 0


def test_the_detail_shows_evidence_beside_every_weighted_line(conn, app):
    detail = data.detail(conn, data.queue(conn)[0].cluster_id)
    rendered = render_detail(detail)
    assert "+20" in rendered
    assert '"own the function"' in rendered
    assert "⚠ Comp unresolved." in rendered


async def test_a_blocked_tunable_write_does_not_kill_the_app(app, monkeypatch):
    """The rubric may be mounted read-only; that is a message, not a crash."""
    from winnow.review import app as app_module

    def blocked(path, name, value):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(app_module.data, "set_tunable", blocked)

    async with app.run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("up")
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running
        assert "read-only" in app.status.lower()


async def test_the_tunables_panel_opens_and_renders(app):
    """It did not, for the first day of its life.

    A helper named `_render` silently overrode Textual's own internal
    `Widget._render`, so the screen tried to paint a plain string as its visual
    and the app died on the `t` key. Nothing caught it because no earlier test
    pressed `t`.
    """
    async with app.run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert app.is_running
        body = app.screen.query_one("#tunables-body")
        assert "score threshold" in body.render().plain


async def test_the_threshold_can_be_changed_and_is_written_back(app, tmp_path):
    from winnow.profile import Profile

    async with app.run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("up")
        await pilot.press("up")
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running

    assert Profile.load(app._profile_path).score_threshold == 72


class StubDrafter:
    model = "claude-opus-5"

    def complete(self, system, user):
        return {
            "resume_variant": "A",
            "variant_reason": "Business systems directorship.",
            "tailoring_notes": ["Lead with the capacity scheduler"],
            "subject": "Director, Business Systems — Alex Rivera",
            "body": "Dear hiring team,\n\nI build the systems that cross departments.\n\nAlex",
            "claims": [
                {
                    "claim": "Built a shop-floor capacity scheduler",
                    "evidence": "Capacity scheduler (shop floor)",
                }
            ],
        }


@pytest.fixture
def drafting_app(app, monkeypatch):
    from pathlib import Path

    from winnow.drafting import load_assets
    from winnow.review import app as app_module

    monkeypatch.setattr(app_module, "ClaudeDrafter", StubDrafter)
    monkeypatch.setattr(app_module, "load_assets", lambda *_: load_assets(Path("examples/assets")))
    return app


async def test_drafting_writes_to_the_queue_and_shows_it(drafting_app, conn):
    from winnow import learning

    async with drafting_app.run_test() as pilot:
        await pilot.press("D")
        await drafting_app.workers.wait_for_complete()
        await pilot.pause()
        assert drafting_app.is_running
        cluster_id = drafting_app.selected_cluster

    drafts = learning.drafts_for(conn, cluster_id)
    assert len(drafts) == 1
    assert drafts[0].resume_variant == "A"
    assert drafts[0].sent_at is None
    assert "cross departments" in drafts[0].body


async def test_sending_a_cover_letter_with_no_recipient_is_explained(drafting_app, conn):
    async with drafting_app.run_test() as pilot:
        await pilot.press("D")
        await drafting_app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert drafting_app.is_running
        assert "form" in drafting_app.status.lower()

    assert conn.execute("SELECT sent_at FROM drafts").fetchone()["sent_at"] is None


async def test_sending_asks_before_it_sends_and_declining_sends_nothing(
    drafting_app, conn, monkeypatch
):
    """No bulk send, no send as a consequence of anything else succeeding."""
    from winnow.review import app as app_module

    sent = []
    monkeypatch.setattr(app_module.data, "send_draft", lambda *a, **k: sent.append(1))

    async with drafting_app.run_test() as pilot:
        await pilot.press("D")
        await drafting_app.workers.wait_for_complete()
        await pilot.pause()
        conn.execute("UPDATE drafts SET recipient = 'talent@example.test'")
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(drafting_app.screen, app_module.ConfirmScreen)
        await pilot.press("escape")
        await pilot.pause()

    assert sent == [], "declining must send nothing"


async def test_a_draft_can_be_read_again_after_moving_away(drafting_app, conn):
    """A letter you cannot re-open is one you would send unread.

    The draft was always saved; it just became unreachable the moment the
    cursor moved, while `s` would still have sent it.
    """
    async with drafting_app.run_test() as pilot:
        await pilot.press("D")
        await drafting_app.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert "cross departments" not in str(drafting_app.status)

        await pilot.press("D")
        await pilot.pause()
        panel = drafting_app.query_one("#detail").render().plain
        assert "cross departments" in panel
        assert "DRAFT" in panel

    assert len(learning_drafts(conn)) == 1, "re-opening must not spend another call"


def learning_drafts(conn):
    from winnow import learning

    rows = conn.execute("SELECT DISTINCT cluster_id FROM drafts").fetchall()
    return [d for row in rows for d in learning.drafts_for(conn, row["cluster_id"])]


async def test_the_detail_view_says_a_draft_exists(drafting_app):
    async with drafting_app.run_test() as pilot:
        await pilot.press("D")
        await drafting_app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("up")
        await pilot.pause()
        assert "draft" in drafting_app.query_one("#detail").render().plain.lower()


async def test_pressing_a_marks_the_application_submitted(app, conn):
    """Pasting a letter into an employer's form is invisible from here.

    So there is a key for saying it happened. Without it the only applications
    the system can count are the ones it emailed itself, and most of them are
    not.
    """
    from winnow.drafting import Draft

    cluster_id = conn.execute("SELECT id FROM clusters ORDER BY id").fetchone()["id"]
    learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient=None,
            subject="Director, Business Systems",
            body="Dear hiring team,",
            built_from={},
        ),
    )

    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()

    row = conn.execute("SELECT submitted_at FROM drafts").fetchone()
    assert row["submitted_at"] is not None


async def test_pressing_a_without_a_draft_says_so(app, conn):
    """There is nothing to have submitted, and silence would look like success."""
    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        assert "nothing drafted" in app.status

    assert conn.execute("SELECT count(*) AS n FROM drafts").fetchone()["n"] == 0


async def test_v_cycles_through_the_lists(app, conn):
    """Review, what was wanted, what is waiting on them, what was put off."""
    async with app.run_test() as pilot:
        assert "awaiting review" in app.sub_title

        for expected in ("interested", "applied", "deferred", "awaiting review"):
            await pilot.press("v")
            await pilot.pause()
            assert expected in app.sub_title


async def test_an_interested_role_is_waiting_in_the_working_set(app, conn):
    """The whole point: 'i' no longer means the row is gone for good."""
    async with app.run_test() as pilot:
        await pilot.press("i")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 1

        await pilot.press("v")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 1
        assert "1 interested" in app.sub_title


async def test_decisions_still_work_inside_the_working_set(app, conn):
    """Changing your mind later is the other half of keeping the list."""
    async with app.run_test() as pilot:
        await pilot.press("i")
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()

        await pilot.press("p")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app.query_one("#queue").row_count == 0


async def test_o_opens_a_browser_when_there_is_one(app, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(app, "_open_in_browser", opened.append)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)

    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()

    assert len(opened) == 1


async def test_o_over_ssh_copies_the_link_instead_of_opening_nothing(app, monkeypatch):
    """The review session runs on the server, and the server has no browser.

    `webbrowser.open` there either fails or launches something nobody can see,
    which is indistinguishable from a broken key. OSC 52 travels back down the
    SSH connection to the terminal that is actually in front of a human, so the
    link lands on the laptop's clipboard.
    """
    opened: list[str] = []
    copied: list[str] = []
    monkeypatch.setattr(app, "_open_in_browser", opened.append)
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 51234 10.0.0.9 22")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)

    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        assert "copied" in app.status

    assert opened == []
    assert copied and copied[0].startswith("http")


async def test_o_shows_the_link_whatever_happens(app, monkeypatch):
    """If the clipboard is blocked too, the URL is at least on screen to select."""
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 51234 10.0.0.9 22")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)

    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        rendered = str(app.query_one("#detail", Static).content)

    assert "http" in rendered


async def test_a_headless_session_does_not_try_the_browser(app, monkeypatch):
    """No SSH, but no display either — a bare console or a container."""
    opened: list[str] = []
    monkeypatch.setattr(app, "_open_in_browser", opened.append)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)

    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()

    assert opened == []


async def test_e_opens_a_pane_holding_the_proposed_letter(app, conn):
    """Prefilled so a small edit is a small edit, and a paste is a paste."""
    from winnow.drafting import Draft
    from winnow.review.app import RevisionScreen

    cluster_id = conn.execute("SELECT id FROM clusters ORDER BY id").fetchone()["id"]
    learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient=None,
            subject="Director, Business Systems",
            body="The proposed letter.",
            built_from={},
        ),
    )

    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, RevisionScreen)
        assert app.screen.text == "The proposed letter."


async def test_saving_a_revision_keeps_both_versions(app, conn):
    from winnow.drafting import Draft
    from winnow.review.app import RevisionScreen

    cluster_id = conn.execute("SELECT id FROM clusters ORDER BY id").fetchone()["id"]
    learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient=None,
            subject="Director, Business Systems",
            body="The proposed letter.",
            built_from={},
        ),
    )

    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        app.screen.text = "What I actually sent."
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert not isinstance(app.screen, RevisionScreen)

    stored = learning.drafts_for(conn, cluster_id)[0]
    assert stored.body == "The proposed letter."
    assert stored.final_body == "What I actually sent."
    assert stored.submitted_at is not None


async def test_escaping_the_revision_pane_changes_nothing(app, conn):
    from winnow.drafting import Draft

    cluster_id = conn.execute("SELECT id FROM clusters ORDER BY id").fetchone()["id"]
    learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient=None,
            subject="S",
            body="The proposed letter.",
            built_from={},
        ),
    )

    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        app.screen.text = "Half-written."
        await pilot.press("escape")
        await pilot.pause()

    stored = learning.drafts_for(conn, cluster_id)[0]
    assert stored.final_body is None
    assert stored.submitted_at is None


async def test_e_without_a_draft_says_so(app, conn):
    async with app.run_test() as pilot:
        await pilot.press("e")
        await pilot.pause()
        assert "nothing drafted" in app.status


async def test_d_does_not_redraft_an_application_already_sent_by_hand(app, conn):
    """`sent_at` means winnow emailed it; `submitted_at` means it went out.

    A letter posted by hand and recovered from the Sent folder has no
    `sent_at`, so keying "is this done?" off that field made `D` offer to
    redraft an application that had already been made — and `a` would then
    submit the redraft as if it were a second one.
    """
    from winnow.drafting import Draft

    cluster_id = conn.execute("SELECT id FROM clusters ORDER BY id").fetchone()["id"]
    draft_id = learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient=None,
            subject="Director, Business Systems",
            body="The proposal.",
            built_from={},
        ),
    )
    learning.record_final(conn, draft_id, "What went out by hand.", source="sent_folder")

    async with app.run_test() as pilot:
        await pilot.press("D")
        await pilot.pause()
        assert "already" in app.status.lower()

    assert conn.execute("SELECT count(*) AS n FROM drafts").fetchone()["n"] == 1


async def test_a_decision_says_what_it_did(app, conn):
    """The mis-press went unnoticed because nothing said what had happened."""
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()

    assert "deferred" in app.status
    assert "Grafana Labs" in app.status
    assert "u to undo" in app.status


async def test_u_takes_back_the_last_decision(app, conn):
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 1

        await pilot.press("u")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 2
        assert "undid" in app.status

    assert conn.execute("SELECT count(*) AS n FROM decisions").fetchone()["n"] == 0


async def test_u_works_from_any_list(app, conn):
    """After a mis-press the row has already gone, so there is nothing to select."""
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        for _ in range(3):
            await pilot.press("v")
            await pilot.pause()
        assert app.query_one("#queue").row_count == 1

        await pilot.press("u")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 0, "no longer deferred"


async def test_u_with_nothing_to_undo_is_harmless(app, conn):
    async with app.run_test() as pilot:
        await pilot.press("u")
        await pilot.pause()
        assert "nothing to undo" in app.status


async def test_the_tunables_panel_reports_gate_misfires(app, conn):
    """A number that points at an edit to the rubric, not at a job."""
    from winnow.review import data as review_data
    from winnow.review.app import TunablesScreen

    cluster_id = conn.execute("SELECT id FROM clusters ORDER BY id").fetchone()["id"]
    review_data.decide(conn, cluster_id, "pass", reason="wrong_function")

    async with app.run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TunablesScreen)
        body = str(app.screen.query_one("#tunables-body", Static).content)

    assert "wrong-function passes" in body
    assert "1" in body


async def test_the_tunables_panel_stays_quiet_with_no_misfires(app, conn):
    from winnow.review.app import TunablesScreen

    async with app.run_test() as pilot:
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TunablesScreen)
        body = str(app.screen.query_one("#tunables-body", Static).content)

    assert "wrong-function" not in body


async def test_rows_that_clear_the_threshold_are_shown_without_a_note(app):
    """Nothing held back means nothing to mention."""
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#queue").row_count == 2, "both seeded rows clear 70"
        assert app.sub_title == "2 awaiting review"


async def test_a_held_back_count_is_shown_not_swallowed(app, conn, profile, make_posting):
    """A shorter list has to say it is shorter."""
    from winnow import learning as learning_module
    from winnow import store as store_module
    from winnow.dedupe import cluster_postings, persist_cluster

    company_id = store_module.insert_company(conn, "Marginal Co")
    posting = make_posting(
        company="Marginal Co",
        title="Business Systems Analyst",
        source_id="weak",
        description_complete=True,
    )
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    learning_module.record_score(conn, cluster_id, _record(48))

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#queue").row_count == 2, "the weak one is hidden"
        assert "1 below" in app.sub_title


async def test_shift_a_reveals_everything_scored(app, conn, profile, make_posting):
    """Below the bar is not the same as wrong; overruling stays possible."""
    from winnow import learning as learning_module
    from winnow import store as store_module
    from winnow.dedupe import cluster_postings, persist_cluster

    company_id = store_module.insert_company(conn, "Marginal Co")
    posting = make_posting(
        company="Marginal Co",
        title="Business Systems Analyst",
        source_id="weak",
        description_complete=True,
    )
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    learning_module.record_score(conn, cluster_id, _record(48))

    async with app.run_test() as pilot:
        await pilot.press("A")
        await pilot.pause()
        assert app.query_one("#queue").row_count == 3
        assert "all scored" in app.sub_title
