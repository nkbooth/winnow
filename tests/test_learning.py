"""The learning store: scores, decisions, outcomes.

Storing a bare interested/pass is nearly useless — six months on you know you
rejected forty roles and nothing about why. So three tables, and the shape of
each one is the point:

* **scores** hold the breakdown rather than the total. A final number cannot be
  reverse-engineered into the factors that produced it.
* **decisions** hold a structured reason. The enum is what makes the signal
  aggregable; the free-text note is what makes it intelligible later.
* **outcomes** are the only ground truth in the system. Everything else is
  opinion, including the scores.
"""

from datetime import UTC, datetime, timedelta

import pytest

from winnow import learning, store
from winnow.dedupe import cluster_postings, persist_cluster
from winnow.scoring import score_posting

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
DESCRIPTION = "You will own and build out the BizOps function, hiring a team of two."


class StubJudge:
    model = "claude-opus-5"

    def __init__(self, growth=0.8):
        self.growth = growth

    def judge(self, system, user):
        return {
            "dimensions": {
                "growth_signal": {
                    "score": self.growth,
                    "evidence": "own and build out the BizOps function",
                },
                "builder_shaped": {"score": 0.0, "evidence": None},
                "team_leadership": {"score": 0.0, "evidence": None},
                "mission_alignment": {"score": 0.0, "evidence": None},
                "governance_heavy": {"score": 0.0, "evidence": None},
                "grind_culture": {"score": 0.0, "evidence": None},
            },
            "vetoes": [],
            "flags": [],
            "why_fits": "Owns the function.",
            "concern": "Comp unresolved.",
        }


@pytest.fixture
def scored(conn, profile, make_posting):
    """One persisted cluster with one score against it."""
    company_id = store.insert_company(conn, "Example Co")
    posting = make_posting(description_text=DESCRIPTION, description_complete=True)
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    record = score_posting(posting, profile, StubJudge(), now=NOW)
    score_id = learning.record_score(conn, cluster_id, record)
    return cluster_id, score_id, record


def test_a_score_stores_its_breakdown_not_only_its_total(conn, scored):
    import json

    cluster_id, score_id, record = scored
    row = conn.execute("SELECT * FROM scores WHERE id = ?", (score_id,)).fetchone()

    assert row["score"] == record.score
    breakdown = json.loads(row["breakdown"])
    growth = next(line for line in breakdown if line["dimension"] == "growth_signal")
    assert growth["weight"] == 20
    assert growth["evidence"] == "own and build out the BizOps function"
    assert row["why_fits"] == "Owns the function."
    assert row["concern"] == "Comp unresolved."


def test_a_score_pins_the_rubric_prompt_and_model(conn, scored, profile):
    _, score_id, _ = scored
    row = conn.execute("SELECT * FROM scores WHERE id = ?", (score_id,)).fetchone()
    assert row["rubric_version"] == profile.rubric_version
    assert row["prompt_version"] == "1"
    assert row["model"] == "claude-opus-5"


def test_rescoring_appends_rather_than_overwrites(conn, scored, profile, make_posting):
    """Scores are written once. History is what calibration reads."""
    cluster_id, _, _ = scored
    posting = make_posting(description_text=DESCRIPTION, description_complete=True)
    learning.record_score(
        conn, cluster_id, score_posting(posting, profile, StubJudge(0.2), now=NOW)
    )

    rows = conn.execute(
        "SELECT score FROM scores WHERE cluster_id = ? ORDER BY id", (cluster_id,)
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["score"] != rows[1]["score"]


def test_the_latest_score_is_what_the_digest_reads(conn, scored, profile, make_posting):
    cluster_id, _, _ = scored
    posting = make_posting(description_text=DESCRIPTION, description_complete=True)
    newer = score_posting(posting, profile, StubJudge(0.2), now=NOW)
    learning.record_score(conn, cluster_id, newer)

    assert learning.latest_score(conn, cluster_id).score == newer.score


def test_a_decision_needs_a_reason_the_system_can_aggregate(conn, scored):
    cluster_id, _, _ = scored
    learning.record_decision(
        conn, cluster_id, "pass", reason="rto_suspected", note="third one this week"
    )

    row = conn.execute("SELECT * FROM decisions WHERE cluster_id = ?", (cluster_id,)).fetchone()
    assert row["decision"] == "pass"
    assert row["reason"] == "rto_suspected"
    assert row["note"] == "third one this week"


def test_an_invented_reason_is_refused(conn, scored):
    import sqlite3

    cluster_id, _, _ = scored
    with pytest.raises(sqlite3.IntegrityError):
        learning.record_decision(conn, cluster_id, "pass", reason="just because")


def test_a_rejection_reason_is_stored_verbatim(conn, scored):
    """The highest-value text in the system, and the part nobody logs by hand."""
    cluster_id, _, _ = scored
    learning.record_outcome(
        conn,
        cluster_id,
        "auto_reject",
        occurred_on="2026-10-01",
        rejection_text="We have decided to move forward with candidates whose "
        "experience more closely matches the requirements.",
    )
    row = conn.execute("SELECT * FROM outcomes WHERE cluster_id = ?", (cluster_id,)).fetchone()
    assert "more closely matches" in row["rejection_text"]


def test_pending_review_counts_what_has_not_been_triaged(conn, scored):
    cluster_id, _, _ = scored
    assert learning.pending_review_count(conn) == 1

    learning.record_decision(conn, cluster_id, "pass", reason="timing")
    assert learning.pending_review_count(conn) == 0


def test_a_deferral_leaves_the_cluster_in_the_queue(conn, scored):
    """Defer means "not now", not "decided"."""
    cluster_id, _, _ = scored
    learning.record_decision(conn, cluster_id, "defer")
    assert learning.pending_review_count(conn) == 1


def test_calibration_groups_score_bands_against_outcomes(conn, profile, make_posting):
    """If 72-78 never converts and 85+ always does, the threshold is wrong."""
    company_id = store.insert_company(conn, "Example Co")
    for index, (growth, outcome) in enumerate(
        [(0.2, "no_response"), (0.2, "auto_reject"), (1.0, "interview")]
    ):
        posting = make_posting(
            source_id=str(index),
            title=f"Director of Business Systems {index}",
            description_text=DESCRIPTION,
            description_complete=True,
        )
        cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
        learning.record_score(
            conn, cluster_id, score_posting(posting, profile, StubJudge(growth), now=NOW)
        )
        learning.record_outcome(conn, cluster_id, outcome)

    report = learning.calibration(conn, rubric_version=profile.rubric_version)
    assert sum(band.applications for band in report) == 3
    assert any(band.interviews for band in report)


def test_calibration_excludes_other_rubric_versions(conn, scored, profile):
    """Scores across versions are not comparable; mixing them reads drift as preference."""
    cluster_id, _, _ = scored
    learning.record_outcome(conn, cluster_id, "interview")

    assert sum(b.applications for b in learning.calibration(conn, profile.rubric_version)) == 1
    assert learning.calibration(conn, rubric_version="v99.deadbeef") == []


def test_source_quality_says_which_boards_are_worth_polling(conn, profile, make_posting):
    """A source producing volume and no screens is costing attention."""
    company_id = store.insert_company(conn, "Example Co")
    for index, (source, outcome) in enumerate(
        [("greenhouse", "interview"), ("adzuna", "no_response"), ("adzuna", "auto_reject")]
    ):
        posting = make_posting(
            source=source,
            source_id=f"{source}-{index}",
            title=f"Director of Business Systems {index}",
            description_text=DESCRIPTION,
            description_complete=True,
        )
        cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
        learning.record_score(
            conn, cluster_id, score_posting(posting, profile, StubJudge(), now=NOW)
        )
        learning.record_outcome(conn, cluster_id, outcome)

    by_source = {row.source: row for row in learning.source_quality(conn)}
    assert by_source["greenhouse"].interviews == 1
    assert by_source["adzuna"].interviews == 0
    assert by_source["adzuna"].applications == 2


def test_pruning_keeps_the_training_data(conn, scored, make_posting):
    """Postings age out at 180 days. Scores and decisions do not — pruning the
    training data to save a few megabytes would be a bad trade."""
    cluster_id, _, _ = scored
    old = (NOW - timedelta(days=200)).isoformat()
    conn.execute("UPDATE postings SET last_seen_at = ?, disappeared_at = ?", (old, old))
    conn.execute("UPDATE scores SET created_at = ?", (old,))
    learning.record_decision(conn, cluster_id, "pass", reason="timing")
    conn.execute("UPDATE decisions SET created_at = ?", (old,))

    removed = learning.prune(conn, retention_days=180, now=NOW)

    assert removed == 1
    assert conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM scores").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM decisions").fetchone()["n"] == 1


# ---------------------------------------------------------------------------
# Applications, and the silence that follows most of them
# ---------------------------------------------------------------------------


def _drafted(conn, profile, make_posting, *, index=0, growth=0.8):
    """Score a posting and attach a draft to it, returning the cluster and draft."""
    from winnow.drafting import Draft

    company_id = store.insert_company(conn, f"Example Co {index}")
    posting = make_posting(
        source_id=f"submitted-{index}",
        title=f"Director of Business Systems {index}",
        description_text=DESCRIPTION,
        description_complete=True,
    )
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    learning.record_score(
        conn, cluster_id, score_posting(posting, profile, StubJudge(growth), now=NOW)
    )
    draft_id = learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="A",
            recipient=None,
            subject="Director of Business Systems",
            body="Dear hiring team,",
            built_from={},
        ),
    )
    return cluster_id, draft_id


def test_an_application_is_a_submitted_draft_not_an_outcome(conn, profile, make_posting):
    """The denominator has to include the ones that went quiet.

    Calibration used to count outcome rows, and outcomes only ever arrived from
    a classifiable email reply. An application that got silence — the usual
    result — was invisible, so every band's interview rate was computed over
    the applications that happened to get answered. That is the one population
    guaranteed to look better than the truth.
    """
    cluster_id, draft_id = _drafted(conn, profile, make_posting)
    learning.mark_submitted(conn, draft_id, now=NOW)

    report = learning.calibration(conn, rubric_version=profile.rubric_version)

    assert sum(band.applications for band in report) == 1
    assert sum(band.interviews for band in report) == 0
    assert all(band.interview_rate == 0.0 for band in report)


def test_an_unsubmitted_draft_is_not_an_application(conn, profile, make_posting):
    """Drafting is thinking about applying. Only submitting is applying."""
    _drafted(conn, profile, make_posting)

    assert learning.calibration(conn, rubric_version=profile.rubric_version) == []


def test_an_outcome_counts_against_its_application(conn, profile, make_posting):
    cluster_id, draft_id = _drafted(conn, profile, make_posting)
    learning.mark_submitted(conn, draft_id, now=NOW)
    learning.record_outcome(conn, cluster_id, "interview")

    report = learning.calibration(conn, rubric_version=profile.rubric_version)

    assert sum(band.applications for band in report) == 1
    assert sum(band.interviews for band in report) == 1


def test_one_application_is_counted_once_however_many_drafts(conn, profile, make_posting):
    """A redraft is a revision, not a second application."""
    from winnow.drafting import Draft

    cluster_id, first = _drafted(conn, profile, make_posting)
    second = learning.record_draft(
        conn,
        cluster_id,
        Draft(
            kind="cover_letter",
            resume_variant="C",
            recipient=None,
            subject="Take two",
            body="Dear hiring team,",
            built_from={},
        ),
    )
    learning.mark_submitted(conn, first, now=NOW)
    learning.mark_submitted(conn, second, now=NOW)

    report = learning.calibration(conn, rubric_version=profile.rubric_version)
    assert sum(band.applications for band in report) == 1


def test_silence_becomes_an_outcome_once_the_window_passes(conn, profile, make_posting):
    """Three weeks without a word is the answer, and it has to be recorded as one."""
    cluster_id, draft_id = _drafted(conn, profile, make_posting)
    learning.mark_submitted(conn, draft_id, now=NOW)

    aged = learning.age_submissions(conn, days=21, now=NOW + timedelta(days=21))

    assert aged == 1
    outcomes = [row["outcome"] for row in conn.execute("SELECT outcome FROM outcomes")]
    assert outcomes == ["no_response"]


def test_silence_inside_the_window_is_left_alone(conn, profile, make_posting):
    _, draft_id = _drafted(conn, profile, make_posting)
    learning.mark_submitted(conn, draft_id, now=NOW)

    assert learning.age_submissions(conn, days=21, now=NOW + timedelta(days=20)) == 0


def test_an_answered_application_never_ages_into_silence(conn, profile, make_posting):
    """A reply already settled it; no_response would overwrite the real answer."""
    cluster_id, draft_id = _drafted(conn, profile, make_posting)
    learning.mark_submitted(conn, draft_id, now=NOW)
    learning.record_outcome(conn, cluster_id, "recruiter_screen")

    assert learning.age_submissions(conn, days=21, now=NOW + timedelta(days=60)) == 0


def test_ageing_twice_does_not_record_silence_twice(conn, profile, make_posting):
    _, draft_id = _drafted(conn, profile, make_posting)
    learning.mark_submitted(conn, draft_id, now=NOW)
    later = NOW + timedelta(days=30)

    assert learning.age_submissions(conn, days=21, now=later) == 1
    assert learning.age_submissions(conn, days=21, now=later) == 0


def test_an_application_that_progressed_is_still_one_application(conn, profile, make_posting):
    """A screen, then an interview, then an offer is one application going well.

    Counting a row per outcome would make the best result in the search look
    like three separate applications and inflate the band it sits in.
    """
    cluster_id, draft_id = _drafted(conn, profile, make_posting)
    learning.mark_submitted(conn, draft_id, now=NOW)
    for outcome in ("recruiter_screen", "interview", "offer"):
        learning.record_outcome(conn, cluster_id, outcome)

    report = learning.calibration(conn, rubric_version=profile.rubric_version)

    assert sum(band.applications for band in report) == 1
    assert sum(band.interviews for band in report) == 1
    assert sum(band.rejections for band in report) == 0
