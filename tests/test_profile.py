"""The rubric loader.

``profile.yaml`` is the output of the intake interview and the one source of
truth for every weight, floor and threshold. Nothing downstream may hardcode a
number, so this module is what everything reads — and the file itself is
hand-edited and heavily commented, which is why the tunables write-back is
surgical rather than a dump.
"""

from pathlib import Path

import pytest

from winnow.profile import Profile

FIXTURE = Path("examples/profile.yaml")


@pytest.fixture
def profile():
    return Profile.load(FIXTURE)


def test_digest_tunables(profile):
    assert profile.score_threshold == 70
    assert profile.max_items == 8
    assert profile.always_show_top == 3
    assert profile.cadence == "on_hits"
    assert profile.weekly_summary == "monday"
    assert profile.retention_days == 180


def test_comp_floors_are_tiered(profile):
    assert profile.floor_for("mid_market") == 200000
    assert profile.floor_for("large_corporate") == 275000
    assert profile.floor_for("crypto") == 400000
    assert profile.contract_hourly_floor == 200
    assert profile.contract_max_hours == 15


def test_an_unknown_tier_falls_back_to_the_lowest_floor(profile):
    """Guessing a higher floor would silently discard qualifying roles."""
    assert profile.floor_for("something-new") == 200000


def test_weights_are_signed(profile):
    assert profile.weights["growth_signal"] == 20
    assert profile.weights["ghost_job_signals"] == -20
    assert profile.weights["degree_required_stated"] == -5


def test_hard_gates_include_the_parameterised_one(profile):
    assert "comp_below_applicable_floor" in profile.hard_gates
    assert "crypto_sector_below" in profile.hard_gates


def test_stealth_list_matching_ignores_case(profile):
    assert profile.is_excluded("contoso manufacturing") is True
    assert profile.is_excluded("Contoso Manufacturing") is True
    assert profile.is_excluded("Tailscale") is False


def test_mission_companies_are_available_for_scoring(profile):
    assert profile.mission_weight == 15
    assert profile.is_mission_company("1Password") is True
    assert profile.is_mission_company("Grafana Labs") is True
    assert profile.is_mission_company("Some Bank") is False


def test_red_flag_ratio(profile):
    assert profile.max_salary_range_ratio == 2.0


def test_rubric_version_changes_when_the_file_changes(tmp_path):
    """Scores are not comparable across rubric versions, so the version must move."""
    original = Profile.load(FIXTURE)
    edited_path = tmp_path / "profile.yaml"
    edited_path.write_text(
        FIXTURE.read_text().replace("score_threshold: 70", "score_threshold: 65")
    )
    edited = Profile.load(edited_path)
    assert original.rubric_version != edited.rubric_version
    assert edited.score_threshold == 65


def test_writing_a_tunable_preserves_comments(tmp_path):
    """The file is hand-maintained; a YAML dump would strip the reasoning out of it."""
    from winnow.profile import update_digest_tunables

    path = tmp_path / "profile.yaml"
    path.write_text(FIXTURE.read_text())
    before = path.read_text()

    update_digest_tunables(path, score_threshold=76, max_items=10)
    after = path.read_text()

    assert "score_threshold: 76" in after
    assert "max_items: 10" in after
    assert "# winnow scoring rubric" in after
    assert after.count("\n") == before.count("\n")
    assert "hypothesis; recalibrate against real throughput" in after

    reloaded = Profile.load(path)
    assert (reloaded.score_threshold, reloaded.max_items) == (76, 10)


def test_writing_an_unknown_tunable_is_refused(tmp_path):
    from winnow.profile import update_digest_tunables

    path = tmp_path / "profile.yaml"
    path.write_text(FIXTURE.read_text())
    with pytest.raises(KeyError):
        update_digest_tunables(path, invented_knob=1)


def test_the_silence_window_has_a_default():
    """How long an application waits before its silence counts as an answer."""
    from winnow.profile import Profile

    profile = Profile.load("examples/profile.yaml")
    assert profile.silence_window_days == 21


def test_the_rubric_names_the_candidate():
    """The drafter signs letters with it, so it cannot live in the code."""
    from winnow.profile import Profile

    assert Profile.load(FIXTURE).candidate_name == "Alex Rivera"
