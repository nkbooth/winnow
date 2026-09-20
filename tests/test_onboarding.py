"""First run.

Two people arrive here. One has never used this and has nothing: they need to
be asked what work they want and what it has to pay, because every gate
downstream reads those answers and there is no sensible default for either.
The other already has a rubric — they were running this before it had a name,
or they copied one from a colleague — and asking them forty questions to
reproduce a file they already have would be a good way to lose them.

So `init` looks first and asks second.
"""

from pathlib import Path

from winnow import onboarding


def _answers(**overrides):
    """A full set of interview answers, so a test can vary one thing."""
    base = {
        "name": "Alex Rivera",
        "mailbox": "alex@example.invalid",
        "location": "Asheville, NC",
        "titles": "Director of Business Systems, Head of Business Operations",
        "floor": "180000",
        "target": "220000",
        "remote": "required",
        "contact": "alex@example.invalid",
    }
    return {**base, **overrides}


def _ask(answers):
    """Turn a dict of answers into the prompt callback the interview uses."""

    def ask(key: str, prompt: str, default: str = "") -> str:
        return answers.get(key, default)

    return ask


def test_the_interview_writes_a_rubric_that_loads(tmp_path):
    from winnow.profile import Profile

    path = tmp_path / "profile.yaml"

    onboarding.interview(path, ask=_ask(_answers()))

    profile = Profile.load(path)
    assert profile.candidate_name == "Alex Rivera"
    assert profile.mailbox == "alex@example.invalid"
    assert profile.floor_for("mid_market") == 180000
    assert "Director of Business Systems" in profile.primary_titles


def test_the_rubric_it_writes_passes_every_gate_the_pipeline_applies(tmp_path):
    """A rubric that parses but gates nothing would look like a broken install."""
    from winnow.profile import Profile

    path = tmp_path / "profile.yaml"
    onboarding.interview(path, ask=_ask(_answers()))
    profile = Profile.load(path)

    assert profile.hard_gates, "an ungated rubric surfaces everything"
    assert profile.weights, "unweighted scoring returns the same number for everything"
    assert profile.score_threshold > 0


def test_a_refused_salary_floor_is_asked_again_not_guessed(tmp_path):
    """Nothing downstream can recover from a floor of zero: it gates nothing."""
    attempts = iter(["not a number", "", "180000"])

    def ask(key: str, prompt: str, default: str = "") -> str:
        if key == "floor":
            return next(attempts)
        return _answers()[key]

    onboarding.interview(tmp_path / "profile.yaml", ask=ask)

    from winnow.profile import Profile

    assert Profile.load(tmp_path / "profile.yaml").floor_for("mid_market") == 180000


def test_an_existing_rubric_is_adopted_rather_than_re_interviewed(tmp_path):
    """Someone who already has one has already answered all of this."""
    existing = tmp_path / "profile.yaml"
    existing.write_text(Path("examples/profile.yaml").read_text())

    def refuse(key: str, prompt: str, default: str = "") -> str:
        raise AssertionError(f"asked {key!r} about a rubric that already exists")

    result = onboarding.init(tmp_path, ask=refuse)

    assert result.adopted_rubric is True
    assert existing.read_text() == Path("examples/profile.yaml").read_text()


def test_init_writes_a_settings_file_alongside_the_rubric(tmp_path):
    from winnow import settings

    onboarding.init(tmp_path, ask=_ask(_answers()))

    loaded = settings.load(tmp_path / "config.toml")
    assert loaded.contact == "alex@example.invalid"
    assert loaded.notify.backend == "terminal"


def test_init_does_not_overwrite_settings_that_already_exist(tmp_path):
    (tmp_path / "config.toml").write_text('[identity]\ncontact = "kept@example.invalid"\n')

    onboarding.init(tmp_path, ask=_ask(_answers()))

    from winnow import settings

    assert settings.load(tmp_path / "config.toml").contact == "kept@example.invalid"


def test_init_reports_what_is_still_needed(tmp_path):
    """An install that looks finished and cannot score is the worse outcome."""
    result = onboarding.init(tmp_path, ask=_ask(_answers()))

    assert any("ANTHROPIC_API_KEY" in step for step in result.next_steps)
    assert any("company add" in step for step in result.next_steps)


def test_drafting_assets_are_stubbed_with_their_own_instructions(tmp_path):
    """Empty files would fail at draft time with a stat error and no advice."""
    onboarding.init(tmp_path, ask=_ask(_answers()))

    inventory = (tmp_path / "assets" / "work-inventory.md").read_text()
    assert "traceable" in inventory.lower()
    assert len(list((tmp_path / "assets").glob("*.md"))) >= 6
