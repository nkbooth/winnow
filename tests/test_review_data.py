"""What the review TUI reads and writes.

Kept separate from the widgets on purpose: the queue draining, the structured
reasons and the tunables write-back are the parts that must be right, and none
of them should need a terminal to test.
"""

from datetime import UTC, datetime, timedelta

import pytest

from winnow import learning, store
from winnow.dedupe import cluster_postings, persist_cluster
from winnow.models import CompInterval, CompSource
from winnow.review import data
from winnow.scoring import BreakdownLine, ScoreRecord

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _record(score=87, vetoes=()):
    return ScoreRecord(
        score=score,
        breakdown=(
            BreakdownLine(
                "growth_signal", 1.0, 20, 20.0, "own and build out the BizOps function", False
            ),
            BreakdownLine("mission_alignment", 0.0, 15, 0.0, None, False),
            BreakdownLine("source_proximity", 1.0, 15, 15.0, "company ATS", True),
            BreakdownLine(
                "degree_required_stated", 1.0, -5, -5.0, "Bachelor's degree required", True
            ),
        ),
        vetoes=tuple(vetoes),
        flags=({"kind": "degree_required", "evidence": "Bachelor's degree required"},),
        unverified=(),
        why_fits="Owns the BizOps function outright.",
        concern="Bachelor's degree listed as required.",
        vetoed=bool(vetoes),
        model="claude-opus-5",
        prompt_version="1",
        rubric_version="v1.test",
    )


@pytest.fixture
def seeded(conn, make_posting):
    company_id = store.insert_company(conn, "Grafana Labs")
    clusters = {}
    for title, score in (("Director, Business Systems", 87), ("Director of RevOps", 74)):
        posting = make_posting(
            company="Grafana Labs",
            title=title,
            source_id=title,
            comp_min=210000,
            comp_max=245000,
            comp_interval=CompInterval.YEAR,
            comp_source=CompSource.STATED,
            description_text="Own and build out the BizOps function.",
            description_complete=True,
        )
        cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
        learning.record_score(conn, cluster_id, _record(score))
        clusters[title] = cluster_id
    return clusters


@pytest.fixture
def two_clusters(seeded):
    """The seeded pair, highest score first, as ids."""
    return (
        seeded["Director, Business Systems"],
        seeded["Director of RevOps"],
    )


def test_the_queue_is_ordered_by_score(conn, seeded):
    rows = data.queue(conn)
    assert [row.score for row in rows] == [87, 74]
    assert rows[0].title == "Director, Business Systems"
    assert rows[0].company == "Grafana Labs"


def test_the_queue_drains(conn, seeded):
    """This is a queue, not a feed. A decision removes the row."""
    before = len(data.queue(conn))
    data.decide(conn, seeded["Director of RevOps"], "pass", reason="no_growth_signal")
    assert len(data.queue(conn)) == before - 1


def test_a_deferral_drains_the_queue_like_any_other_decision(conn, seeded):
    """Changed 2026-09-20. It used to leave the row in place.

    The intent was that deferring is not an answer, so the question should stay
    asked. In use it read as a key that had not worked — the live store has two
    clusters carrying the same deferral twice, seconds apart. A decision that
    produces no visible change gets made twice.
    """
    data.decide(conn, seeded["Director of RevOps"], "defer")

    assert len(data.queue(conn)) == 1
    assert len(data.deferred(conn)) == 1


def test_passing_requires_a_reason_the_system_can_aggregate(conn, seeded):
    with pytest.raises(ValueError):
        data.decide(conn, seeded["Director of RevOps"], "pass")


def test_interested_needs_no_reason(conn, seeded):
    data.decide(conn, seeded["Director, Business Systems"], "interested")
    assert conn.execute("SELECT decision FROM decisions").fetchone()["decision"] == "interested"


def test_the_pass_reasons_offered_are_the_ones_the_schema_accepts(conn, seeded):
    """Offering a reason the store rejects would fail at the last keystroke."""
    for reason in data.PASS_REASONS:
        cluster_id = seeded["Director of RevOps"]
        conn.execute("DELETE FROM decisions WHERE cluster_id = ?", (cluster_id,))
        data.decide(conn, cluster_id, "pass", reason=reason)


def test_the_detail_shows_what_the_score_was_made_of(conn, seeded):
    detail = data.detail(conn, seeded["Director, Business Systems"])

    assert detail.score == 87
    assert detail.comp == "$210–245k (stated)"
    assert any(
        line.evidence == "own and build out the BizOps function" for line in detail.breakdown
    )
    assert detail.gates == "passed · no vetoes"


def test_a_vetoed_cluster_says_which_gate_killed_it(conn, make_posting):
    company_id = store.insert_company(conn, "Some Co")
    posting = make_posting(company="Some Co", description_complete=True)
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    learning.record_score(
        conn,
        cluster_id,
        _record(vetoes=[{"gate": "hybrid_required", "evidence": "2 days onsite"}]),
    )

    detail = data.detail(conn, cluster_id)
    assert "hybrid_required" in detail.gates
    assert "2 days onsite" in detail.gates


def test_tunables_show_the_measured_rate_beside_the_cap(conn, profile, seeded, tmp_path):
    """The number that justifies changing the control, next to the control."""
    data.decide(conn, seeded["Director, Business Systems"], "interested")

    view = data.tunables(conn, profile, now=NOW)

    assert view.score_threshold == 70
    assert view.max_items == 8
    assert view.applications_per_week >= 0


def test_writing_a_tunable_goes_back_to_the_profile(tmp_path, profile):
    from pathlib import Path

    from winnow.profile import Profile

    path = tmp_path / "profile.yaml"
    path.write_text(Path("examples/profile.yaml").read_text())

    data.set_tunable(path, "score_threshold", 76)

    assert Profile.load(path).score_threshold == 76
    assert "# winnow scoring rubric" in path.read_text()


def test_an_ambiguous_pair_can_be_settled(conn, make_posting):
    """The fuzzy band goes to a human rather than being decided by a threshold."""
    company_id = store.insert_company(conn, "Grafana Labs")
    left = make_posting(company="Grafana Labs", source_id="1", title="Director of Business Systems")
    right = make_posting(
        company="Grafana Labs", source_id="2", title="Director of Business Systems & Data"
    )
    for cluster in cluster_postings([left, right]):
        persist_cluster(conn, cluster, company_id=company_id)

    pending = data.pending_merges(conn)
    assert len(pending) == 1

    data.resolve_merge(conn, pending[0].id, merged=False)
    assert data.pending_merges(conn) == []


def test_stale_boards_surface_for_re_resolution(conn):
    company_id = store.insert_company(conn, "Quiet Co")
    board_id = store.insert_board(
        conn,
        company_id=company_id,
        vendor="greenhouse",
        identifier={"slug": "quietco"},
        source="manual",
    )
    conn.execute("UPDATE company_boards SET status = 'stale' WHERE id = ?", (board_id,))

    stale = data.stale_boards(conn)
    assert [row.company for row in stale] == ["Quiet Co"]


def test_an_outcome_can_be_recorded_by_hand(conn, seeded):
    """A phone screen and a verbal rejection arrive by neither mail nor board."""
    cluster_id = seeded["Director, Business Systems"]
    data.decide(conn, cluster_id, "interested")
    data.record_outcome(conn, cluster_id, "recruiter_screen", occurred_on="2026-09-25")

    row = conn.execute("SELECT * FROM outcomes WHERE cluster_id = ?", (cluster_id,)).fetchone()
    assert row["outcome"] == "recruiter_screen"


# ---------------------------------------------------------------------------
# The working set: what was wanted, and what is waiting on somebody else
# ---------------------------------------------------------------------------


def _interested(conn, cluster_id, *, when=None):
    learning.record_decision(conn, cluster_id, "interested")
    if when is not None:
        conn.execute(
            "UPDATE decisions SET created_at = ? WHERE cluster_id = ?",
            (when.isoformat(), cluster_id),
        )


def _draft_for(conn, cluster_id, *, submitted=None):
    from winnow.drafting import Draft

    draft_id = learning.record_draft(
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
    if submitted is not None:
        learning.mark_submitted(conn, draft_id, now=submitted)
    return draft_id


def test_marking_interested_moves_a_cluster_into_the_working_set(conn, two_clusters):
    """Wanting a role is the start of the work, not the end of it.

    Before this, interested and pass did the same thing to the queue: the row
    vanished and no screen in the system mentioned it again. The one state that
    needs a follow-up list was the one that disappeared hardest.
    """
    first, _ = two_clusters
    _interested(conn, first)

    assert [row.cluster_id for row in data.queue(conn)] != [first]
    assert [row.cluster_id for row in data.working_set(conn)] == [first]


def test_a_passed_cluster_is_in_neither_list(conn, two_clusters):
    first, _ = two_clusters
    learning.record_decision(conn, first, "pass", reason="comp_too_low")

    assert first not in [row.cluster_id for row in data.queue(conn)]
    assert first not in [row.cluster_id for row in data.working_set(conn)]


def test_changing_your_mind_takes_it_out_of_the_working_set(conn, two_clusters):
    """Decisions are append-only, so the latest one is the one that counts."""
    first, _ = two_clusters
    _interested(conn, first)
    learning.record_decision(conn, first, "pass", reason="timing")

    assert data.working_set(conn) == []


def test_the_working_set_leads_with_what_has_waited_longest(conn, two_clusters):
    first, second = two_clusters
    _interested(conn, first, when=NOW - timedelta(days=9))
    _interested(conn, second, when=NOW - timedelta(days=2))

    rows = data.working_set(conn, now=NOW)

    assert [row.cluster_id for row in rows] == [first, second]
    assert [row.age_days for row in rows] == [9, 2]


def test_applying_takes_it_out_of_the_working_set(conn, two_clusters):
    """It is no longer work of yours; it is waiting on them."""
    first, _ = two_clusters
    _interested(conn, first)
    _draft_for(conn, first, submitted=NOW)

    assert first not in [row.cluster_id for row in data.working_set(conn)]
    assert [row.cluster_id for row in data.applied(conn)] == [first]


def test_drafting_alone_does_not_count_as_applying(conn, two_clusters):
    first, _ = two_clusters
    _interested(conn, first)
    _draft_for(conn, first)

    assert [row.cluster_id for row in data.working_set(conn)] == [first]
    assert data.applied(conn) == []


def test_the_applied_list_shows_how_long_it_has_been_quiet(conn, two_clusters):
    first, _ = two_clusters
    _interested(conn, first)
    _draft_for(conn, first, submitted=NOW - timedelta(days=12))

    row = data.applied(conn, now=NOW)[0]

    assert row.age_days == 12


def test_an_answered_application_leaves_the_applied_list(conn, two_clusters):
    """Once they have replied it is an outcome, not an open thread."""
    first, _ = two_clusters
    _interested(conn, first)
    _draft_for(conn, first, submitted=NOW - timedelta(days=3))
    learning.record_outcome(conn, first, "auto_reject")

    assert data.applied(conn) == []


def test_an_application_never_marked_interested_still_shows(conn, two_clusters):
    """The listener can mark one submitted without a decision ever being made."""
    first, _ = two_clusters
    _draft_for(conn, first, submitted=NOW)

    assert [row.cluster_id for row in data.applied(conn)] == [first]


def test_deferring_takes_it_out_of_the_queue(conn, two_clusters):
    """A deferral that leaves the row in place reads as a key that did nothing.

    Seen live: two clusters carry the same deferral twice, seconds apart,
    because pressing it produced no visible change and the obvious conclusion
    was that it had not registered.
    """
    first, second = two_clusters
    learning.record_decision(conn, first, "defer")

    assert [row.cluster_id for row in data.queue(conn)] == [second]


def test_a_deferred_cluster_waits_in_its_own_list(conn, two_clusters):
    first, _ = two_clusters
    learning.record_decision(conn, first, "defer")
    conn.execute(
        "UPDATE decisions SET created_at = ? WHERE cluster_id = ?",
        ((NOW - timedelta(days=4)).isoformat(), first),
    )

    rows = data.deferred(conn, now=NOW)

    assert [row.cluster_id for row in rows] == [first]
    assert rows[0].age_days == 4


def test_deciding_later_takes_it_out_of_the_deferred_list(conn, two_clusters):
    """Deferring is not an answer, so the list has to be one you can leave."""
    first, _ = two_clusters
    learning.record_decision(conn, first, "defer")
    learning.record_decision(conn, first, "interested")

    assert data.deferred(conn) == []
    assert [row.cluster_id for row in data.working_set(conn)] == [first]


def test_a_second_deferral_does_not_duplicate_the_row(conn, two_clusters):
    first, _ = two_clusters
    learning.record_decision(conn, first, "defer")
    learning.record_decision(conn, first, "defer")

    assert len(data.deferred(conn)) == 1


def test_an_interested_cluster_is_not_deferred(conn, two_clusters):
    first, _ = two_clusters
    learning.record_decision(conn, first, "interested")

    assert data.deferred(conn) == []


def test_the_last_decision_can_be_taken_back(conn, two_clusters):
    """`d` sits next to `p` and reads as dismiss. Mis-keys are a design problem.

    Undo is the general answer: decisions are append-only and every list reads
    the latest one, so removing it restores exactly the state before the press.
    """
    first, _ = two_clusters
    data.decide(conn, first, "defer")

    undone = data.undo_last_decision(conn)

    assert undone is not None
    assert undone.cluster_id == first
    assert undone.decision == "defer"
    assert [row.cluster_id for row in data.deferred(conn)] == []
    assert first in [row.cluster_id for row in data.queue(conn)]


def test_undo_reverses_only_the_most_recent_decision(conn, two_clusters):
    first, second = two_clusters
    data.decide(conn, first, "defer")
    data.decide(conn, second, "pass", reason="comp_too_low")

    data.undo_last_decision(conn)

    assert [row.cluster_id for row in data.deferred(conn)] == [first], "the earlier one stands"
    assert second in [row.cluster_id for row in data.queue(conn)]


def test_undo_restores_an_earlier_decision_rather_than_clearing_the_slate(conn, two_clusters):
    """Deferred, then changed to interested, then undone: it is deferred again."""
    first, _ = two_clusters
    data.decide(conn, first, "defer")
    data.decide(conn, first, "interested")

    data.undo_last_decision(conn)

    assert [row.cluster_id for row in data.deferred(conn)] == [first]
    assert data.working_set(conn) == []


def test_undo_with_nothing_to_undo_says_so(conn, seeded):
    assert data.undo_last_decision(conn) is None


def test_a_pass_can_say_the_title_gate_misfired(conn, two_clusters):
    """ "Business operations" is an anchor, and also an event coordinator's job."""
    first, _ = two_clusters

    data.decide(conn, first, "pass", reason="wrong_function", note="Events, not systems.")

    row = conn.execute("SELECT reason FROM decisions").fetchone()
    assert row["reason"] == "wrong_function"


def test_gate_misfires_are_counted_where_the_vocabulary_is_edited(conn, two_clusters, profile):
    """The count belongs next to the knob it justifies turning.

    A pass for the wrong function is not a judgement about a job, it is a
    report that the title anchors are too loose. That is a thing to go and
    change, so the number sits in the panel where such things get changed.
    """
    first, second = two_clusters
    data.decide(conn, first, "pass", reason="wrong_function")
    data.decide(conn, second, "pass", reason="comp_too_low")

    view = data.tunables(conn, profile, now=NOW)

    assert view.gate_misfires == 1


# ---------------------------------------------------------------------------
# The queue at scale
# ---------------------------------------------------------------------------


def test_the_queue_honours_the_threshold_the_digest_uses(conn, seeded, profile):
    """Measured at 217 boards: 136 rows, 49 of them below the rubric's own bar.

    The threshold was applied by the digest and never by review, which was
    harmless at twenty boards and useless at two hundred — a third of the list
    was roles the rubric had already judged too weak, and the count in the
    title claimed all of them were awaiting a decision.
    """
    rows = data.queue(conn, profile=profile)

    assert rows, "the queue is not empty"
    assert all(row.score >= profile.score_threshold for row in rows)


def test_everything_can_still_be_seen_on_request(conn, seeded, profile, make_posting):
    """Below-threshold is not the same as wrong, so it stays reachable."""
    _weak_cluster(conn, profile, make_posting)

    shown = data.queue(conn, profile=profile)
    everything = data.queue(conn, profile=profile, include_below_threshold=True)

    assert len(everything) == len(shown) + 1
    assert min(row.score for row in everything) == 48


def test_the_queue_reports_what_it_is_holding_back(conn, seeded, profile, make_posting):
    """A shorter list must say it is shorter, or it reads as a quiet day."""
    _weak_cluster(conn, profile, make_posting)

    assert data.below_threshold_count(conn, profile) == 1


def _weak_cluster(conn, profile, make_posting):
    """A scored cluster below the rubric's threshold."""
    company_id = store.insert_company(conn, "Marginal Co")
    posting = make_posting(
        company="Marginal Co",
        title="Business Systems Analyst",
        source_id="weak",
        description_text="Own and build out the BizOps function.",
        description_complete=True,
    )
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    learning.record_score(conn, cluster_id, _record(48))
    return cluster_id


def test_no_profile_means_no_filtering(conn, seeded):
    """The caller decides. Defaulting to a threshold nobody passed would hide
    rows on the authority of a rubric that was never consulted."""
    assert len(data.queue(conn)) == 2


def test_a_closed_role_leaves_the_review_queue(conn, seeded, profile):
    """The digest already refuses to list these; review was still showing them.

    "The worst thing this system can produce is an evening spent on a role that
    has already closed" — that reasoning is in the digest and was never applied
    to the screen where the evening actually gets spent.
    """
    cluster_id = seeded["Director of RevOps"]
    conn.execute(
        "UPDATE postings SET disappeared_at = ? WHERE id = "
        "(SELECT canonical_posting_id FROM clusters WHERE id = ?)",
        (NOW.isoformat(), cluster_id),
    )

    assert cluster_id not in [row.cluster_id for row in data.queue(conn, profile=profile)]


def test_a_closed_role_is_still_reachable_with_everything_else(conn, seeded, profile):
    """Closed is a fact about the posting, not a reason to lose the record."""
    cluster_id = seeded["Director of RevOps"]
    conn.execute(
        "UPDATE postings SET disappeared_at = ? WHERE id = "
        "(SELECT canonical_posting_id FROM clusters WHERE id = ?)",
        (NOW.isoformat(), cluster_id),
    )

    everything = data.queue(conn, profile=profile, include_below_threshold=True)
    assert cluster_id in [row.cluster_id for row in everything]
