"""Phases 4 to 6: the judged half, and what it is not allowed to decide.

The dividing line is the whole design. Anything structured was settled in
``gates.py``; what reaches a model is growth signal, builder-shapedness,
grind-culture clustering, mission fit, and RTO language buried in prose. The
model returns dimension scores with evidence spans and never a number that
matters on its own.

Every judgment must quote the posting. That is not decoration: it makes review
meaningful, and it makes hallucination visible — a claimed veto with no
quotable sentence is a caught error rather than a deleted job.
"""

from datetime import UTC, datetime, timedelta

import pytest

from winnow.scoring import MalformedJudgement, score_posting

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

DESCRIPTION = (
    "You will own and build out the BizOps function at Grafana Labs, including "
    "a team of two with a third being hired. This is greenfield systems and "
    "internal tooling work rather than report maintenance. Bachelor's degree "
    "required."
)


class StubJudge:
    """Returns a canned judgement and records what it was asked."""

    model = "claude-opus-5"

    def __init__(self, judgement):
        self.judgement = judgement
        self.calls: list[tuple[str, str]] = []

    def judge(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        return self.judgement


def _judgement(**overrides):
    base = {
        "dimensions": {
            "growth_signal": {
                "score": 0.8,
                "evidence": "own and build out the BizOps function",
            },
            "builder_shaped": {
                "score": 0.6,
                "evidence": "greenfield systems and internal tooling",
            },
            "team_leadership": {
                "score": 1.0,
                "evidence": "a team of two with a third being hired",
            },
            "mission_alignment": {"score": 0.0, "evidence": None},
            "governance_heavy": {"score": 0.0, "evidence": None},
            "grind_culture": {"score": 0.0, "evidence": None},
        },
        "vetoes": [],
        "flags": [{"kind": "degree_required", "evidence": "Bachelor's degree required"}],
        "why_fits": "Owns the BizOps function outright, including a team of two.",
        "concern": "Bachelor's degree listed as required.",
    }
    return {**base, **overrides}


@pytest.fixture
def posting(make_posting):
    return make_posting(
        company="Grafana Labs",
        title="Director, Business Systems",
        description_text=DESCRIPTION,
        description_complete=True,
        first_seen_at=NOW,
        posted_at=NOW - timedelta(days=4),
    )


def test_a_truncated_description_is_never_scored(make_posting, profile):
    """Scoring a fragment produces a confident number computed from nothing."""
    fragment = make_posting(description_text="Own the BizOps fun...", description_complete=False)
    judge = StubJudge(_judgement())

    with pytest.raises(MalformedJudgement):
        score_posting(fragment, profile, judge, now=NOW)
    assert judge.calls == [], "no model call is made on an incomplete payload"


def test_the_score_is_assembled_from_weights_not_from_the_model(posting, profile):
    judge = StubJudge(_judgement())
    record = score_posting(posting, profile, judge, now=NOW)

    contributions = {line.dimension: line.contribution for line in record.breakdown}
    assert contributions["growth_signal"] == pytest.approx(0.8 * 20)
    assert contributions["builder_shaped"] == pytest.approx(0.6 * 15)
    assert contributions["team_leadership"] == pytest.approx(1.0 * 10)
    assert record.score == min(100, round(50 + sum(line.contribution for line in record.breakdown)))


def test_the_score_is_clamped_to_a_hundred(posting, profile):
    judge = StubJudge(
        _judgement(
            dimensions={
                "growth_signal": {"score": 1.0, "evidence": "own and build out"},
                "builder_shaped": {"score": 1.0, "evidence": "greenfield systems"},
                "team_leadership": {"score": 1.0, "evidence": "a team of two"},
                "mission_alignment": {"score": 1.0, "evidence": "Grafana Labs"},
                "governance_heavy": {"score": 0.0, "evidence": None},
                "grind_culture": {"score": 0.0, "evidence": None},
            }
        )
    )
    record = score_posting(posting, profile, judge, now=NOW)
    assert record.score == 100


def test_source_proximity_is_computed_not_judged(posting, profile, make_posting):
    """Worth roughly 3.5x on interview rate, and knowable without asking."""
    judge = StubJudge(_judgement())
    at_source = score_posting(posting, profile, judge, now=NOW)

    aggregated = make_posting(
        source="adzuna",
        source_url="https://www.adzuna.com/details/1",
        discovered_via="adzuna",
        description_text=DESCRIPTION,
        description_complete=True,
        first_seen_at=NOW,
        posted_at=NOW - timedelta(days=4),
    )
    via_board = score_posting(aggregated, profile, StubJudge(_judgement()), now=NOW)

    assert at_source.score > via_board.score


def test_a_named_mission_company_does_not_depend_on_the_model_noticing(posting, profile):
    judge = StubJudge(_judgement())
    record = score_posting(posting, profile, judge, now=NOW)
    mission = next(line for line in record.breakdown if line.dimension == "mission_alignment")
    assert mission.score == 1.0
    assert "rubric" in (mission.evidence or "")


def test_a_stale_posting_carries_the_ghost_job_penalty(profile, make_posting):
    def at_age(days):
        return make_posting(
            title="Director, Business Systems",
            description_text=DESCRIPTION,
            description_complete=True,
            first_seen_at=NOW,
            posted_at=NOW - timedelta(days=days),
        )

    fresh = score_posting(at_age(4), profile, StubJudge(_judgement()), now=NOW)
    old = at_age(75)
    stale = score_posting(old, profile, StubJudge(_judgement()), now=NOW)

    assert stale.score < fresh.score
    ghost = next(line for line in stale.breakdown if line.dimension == "ghost_job_signals")
    assert ghost.contribution < 0
    assert "75 days" in ghost.evidence


def test_a_repost_counts_toward_the_ghost_job_signal(profile, make_posting):
    plain = make_posting(
        title="Director, Business Systems",
        description_text=DESCRIPTION,
        description_complete=True,
        first_seen_at=NOW,
        posted_at=NOW - timedelta(days=4),
    )
    once = score_posting(plain, profile, StubJudge(_judgement()), now=NOW)
    again = score_posting(plain, profile, StubJudge(_judgement()), now=NOW, repost_count=2)
    assert again.score < once.score


def test_a_degree_flag_is_a_penalty_and_a_concern_never_a_rejection(posting, profile):
    """No degree. Much of this language is boilerplate; auto-rejecting deletes the search."""
    record = score_posting(posting, profile, StubJudge(_judgement()), now=NOW)
    assert not record.vetoed
    degree = next(line for line in record.breakdown if line.dimension == "degree_required_stated")
    assert degree.contribution == -5
    assert "degree" in record.concern.lower()


def test_a_veto_overrides_the_score_entirely(posting, profile):
    """An 88 with a detected RTO requirement is rejected, not ranked 88th."""
    judge = StubJudge(
        _judgement(
            vetoes=[
                {
                    "gate": "hybrid_required",
                    "evidence": "greenfield systems and internal tooling",
                }
            ]
        )
    )
    record = score_posting(posting, profile, judge, now=NOW)
    assert record.vetoed is True
    assert record.vetoes[0]["gate"] == "hybrid_required"
    assert record.score > 0, "the score is recorded, not discarded"


def test_a_veto_with_no_quotable_sentence_is_a_caught_error(posting, profile):
    judge = StubJudge(
        _judgement(
            vetoes=[{"gate": "hybrid_required", "evidence": "3 days per week in our office"}]
        )
    )
    record = score_posting(posting, profile, judge, now=NOW)
    assert record.vetoed is False
    assert record.unverified, "the fabricated claim is recorded rather than silently dropped"


def test_a_veto_naming_a_gate_the_rubric_does_not_have_is_dropped(posting, profile):
    judge = StubJudge(
        _judgement(vetoes=[{"gate": "vibes_were_off", "evidence": "greenfield systems"}])
    )
    record = score_posting(posting, profile, judge, now=NOW)
    assert record.vetoed is False


def test_a_dimension_scored_above_zero_without_evidence_is_dropped(posting, profile):
    judge = StubJudge(
        _judgement(
            dimensions={
                "growth_signal": {"score": 1.0, "evidence": None},
                "builder_shaped": {"score": 0.0, "evidence": None},
                "team_leadership": {"score": 0.0, "evidence": None},
                "mission_alignment": {"score": 0.0, "evidence": None},
                "governance_heavy": {"score": 0.0, "evidence": None},
                "grind_culture": {"score": 0.0, "evidence": None},
            }
        )
    )
    record = score_posting(posting, profile, judge, now=NOW)
    growth = next(line for line in record.breakdown if line.dimension == "growth_signal")
    assert growth.contribution == 0
    assert record.unverified


def test_a_dimension_quoting_something_absent_is_dropped(posting, profile):
    judge = StubJudge(
        _judgement(
            dimensions={
                "growth_signal": {"score": 1.0, "evidence": "you will report to the CFO"},
                "builder_shaped": {"score": 0.0, "evidence": None},
                "team_leadership": {"score": 0.0, "evidence": None},
                "mission_alignment": {"score": 0.0, "evidence": None},
                "governance_heavy": {"score": 0.0, "evidence": None},
                "grind_culture": {"score": 0.0, "evidence": None},
            }
        )
    )
    record = score_posting(posting, profile, judge, now=NOW)
    growth = next(line for line in record.breakdown if line.dimension == "growth_signal")
    assert growth.contribution == 0


def test_a_malformed_response_is_an_error_not_a_zero(posting, profile):
    with pytest.raises(MalformedJudgement):
        score_posting(posting, profile, StubJudge({"not": "a judgement"}), now=NOW)


def test_the_record_pins_what_produced_it(posting, profile):
    """Scores are not comparable across rubric, prompt or model versions."""
    record = score_posting(posting, profile, StubJudge(_judgement()), now=NOW)
    assert record.model == "claude-opus-5"
    assert record.rubric_version == profile.rubric_version
    assert record.prompt_version


def test_scoring_the_same_posting_twice_gives_the_same_record(posting, profile):
    first = score_posting(posting, profile, StubJudge(_judgement()), now=NOW)
    second = score_posting(posting, profile, StubJudge(_judgement()), now=NOW)
    assert first == second


def test_the_system_prompt_is_identical_across_postings(posting, profile, make_posting):
    """It is the cached prefix; a per-posting detail in it costs the cache."""
    first = StubJudge(_judgement())
    second = StubJudge(_judgement())
    score_posting(posting, profile, first, now=NOW)
    score_posting(
        make_posting(description_text=DESCRIPTION, description_complete=True),
        profile,
        second,
        now=NOW,
    )
    assert first.calls[0][0] == second.calls[0][0]
    assert first.calls[0][1] != second.calls[0][1]


def test_the_user_prompt_carries_the_posting_the_model_must_quote(posting, profile):
    judge = StubJudge(_judgement())
    score_posting(posting, profile, judge, now=NOW)
    _, user = judge.calls[0]
    assert DESCRIPTION in user
    assert "Grafana Labs" in user
    assert "Director, Business Systems" in user
