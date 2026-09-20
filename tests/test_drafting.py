"""Drafting a cover letter for one opportunity.

The design's rule is "never invented experience", and a rule nothing checks is
a hope. So the drafter returns its claims with the span of the work inventory
or resume each one came from, and every span is verified against the assets
before the draft is stored. A claim that cannot be traced is surfaced, loudly,
against the draft it appears in.

The other rule is the credential boundary: nothing here sends anything. This
module builds text and hands it to a queue.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from winnow import drafting
from winnow.drafting import DraftingError, draft_cover_letter, load_assets

ASSETS = Path("examples/assets")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class StubDrafter:
    model = "claude-opus-5"

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> dict:
        self.calls.append((system, user))
        return self.response


def _response(**overrides):
    base = {
        "resume_variant": "A",
        "variant_reason": "The role is a business-systems directorship, not consulting.",
        "tailoring_notes": ["Lead with the capacity scheduler", "Mirror 'cross-functional'"],
        "subject": "Director of Business Systems — Alex Rivera",
        "body": "Dear hiring team,\n\nI build the systems that cross departments.\n\nAlex",
        "claims": [
            {
                "claim": "Built a shop-floor capacity scheduler",
                "evidence": "Capacity scheduler (shop floor)",
            }
        ],
    }
    return {**base, **overrides}


@pytest.fixture(scope="module")
def assets():
    return load_assets(ASSETS)


def test_assets_load_the_material_a_draft_is_built_from(assets):
    assert "privacy close" in assets.cover_letter_assets.lower()
    assert "capacity scheduler" in assets.work_inventory.lower()
    assert set(assets.resume_variants) == {"A", "B", "C"}
    assert assets.signature


def test_a_missing_asset_directory_is_an_error_not_an_empty_draft(tmp_path):
    with pytest.raises(DraftingError):
        load_assets(tmp_path / "nowhere")


def test_the_draft_carries_what_it_was_built_from(make_posting, assets):
    drafter = StubDrafter(_response())
    posting = make_posting(company="Grafana Labs", title="Director, Business Systems")

    result = draft_cover_letter(posting, assets, drafter, now=NOW)

    assert result.resume_variant == "A"
    assert result.subject.startswith("Director of Business Systems")
    assert "cross departments" in result.body
    assert result.built_from["company"] == "Grafana Labs"
    assert result.built_from["model"] == "claude-opus-5"
    assert result.built_from["claims"][0]["claim"].startswith("Built a shop-floor")


def test_a_claim_traceable_to_the_inventory_is_kept(make_posting, assets):
    result = draft_cover_letter(make_posting(), assets, StubDrafter(_response()), now=NOW)
    assert result.unverified == ()


def test_a_claim_that_cannot_be_traced_is_surfaced(make_posting, assets):
    """The whole point. An invented credential is the one unrecoverable mistake."""
    drafter = StubDrafter(
        _response(
            claims=[{"claim": "Led a team of forty", "evidence": "managed a department of forty"}]
        )
    )
    result = draft_cover_letter(make_posting(), assets, drafter, now=NOW)

    assert len(result.unverified) == 1
    assert result.unverified[0]["claim"] == "Led a team of forty"
    assert result.needs_attention is True


def test_a_verifiable_draft_needs_no_attention(make_posting, assets):
    result = draft_cover_letter(make_posting(), assets, StubDrafter(_response()), now=NOW)
    assert result.needs_attention is False


@pytest.mark.parametrize(
    "variant",
    [
        "Z",
        # What the model actually returned on the first live run, before the
        # schema constrained it and the prompt named the accepted values.
        "Resume A — Business Systems / Operations Director",
        "",
    ],
)
def test_an_unusable_resume_variant_is_refused(variant, make_posting, assets):
    drafter = StubDrafter(_response(resume_variant=variant))
    with pytest.raises(DraftingError):
        draft_cover_letter(make_posting(), assets, drafter, now=NOW)


def test_an_empty_body_is_refused(make_posting, assets):
    drafter = StubDrafter(_response(body="   "))
    with pytest.raises(DraftingError):
        draft_cover_letter(make_posting(), assets, drafter, now=NOW)


def test_the_posting_reaches_the_prompt_and_the_assets_stay_in_the_system_half(
    make_posting, assets
):
    """The assets are identical per run and belong in the cached prefix."""
    drafter = StubDrafter(_response())
    posting = make_posting(
        company="Grafana Labs",
        title="Director, Business Systems",
        description_text="Own and build out the BizOps function.",
        description_complete=True,
    )
    draft_cover_letter(posting, assets, drafter, now=NOW)

    system, user = drafter.calls[0]
    assert "capacity scheduler" in system.lower()
    assert "Grafana Labs" in user
    assert "Own and build out the BizOps function." in user
    assert "Grafana Labs" not in system


def test_nothing_in_this_module_can_send_mail():
    """Structural: drafting hands text to a queue and has no path to SMTP."""
    import inspect

    from winnow import drafting

    source = inspect.getsource(drafting)
    assert "smtplib" not in source
    assert "mailer" not in source


def test_the_reusable_paragraphs_count_as_source_material(assets):
    """They are the candidate's own words and full of real claims.

    Excluding them flagged three true statements on the first live draft,
    including the management philosophy, which appears nowhere else.
    """
    assert "needs me less" in assets.searchable()


@pytest.mark.parametrize(
    "evidence",
    [
        # Four near-quotes of material that is genuinely in the example assets.
        # The originals were the four a live draft flagged wrongly on day one.
        "I'm one of four people — with the CEO, CFO and VP of Operations — "
        "choosing the ERP an $80M\ndistributor will run on for the next decade.",
        "four consecutive\nyears of 12% growth at flat headcount, achieved by "
        "developing the team rather than adding to it.",
        "In the last year I shipped a scheduler covering 400 workcenters and an "
        "MCP server exposing ERP",
        "The measure of the work is whether the team needs me less each quarter.",
    ],
)
def test_a_near_quote_of_real_material_is_not_flagged(evidence, make_posting, assets):
    """A checker that cries wolf is a checker nobody reads."""
    drafter = StubDrafter(_response(claims=[{"claim": "A true thing", "evidence": evidence}]))
    result = draft_cover_letter(make_posting(), assets, drafter, now=NOW)
    assert result.unverified == ()


@pytest.mark.parametrize(
    ("evidence", "expected_missing"),
    [
        ("managed a department of forty", "forty"),
        ("led a $50M P&L across three continents", "continents"),
        ("holds a Bachelor's degree in Computer Science", "degree"),
    ],
)
def test_an_invented_claim_is_flagged_with_the_words_that_gave_it_away(
    evidence, expected_missing, make_posting, assets
):
    drafter = StubDrafter(_response(claims=[{"claim": "Invented", "evidence": evidence}]))
    result = draft_cover_letter(make_posting(), assets, drafter, now=NOW)

    assert len(result.unverified) == 1
    assert expected_missing in result.unverified[0]["missing"]


def test_sent_letters_are_shown_to_the_drafter(assets):
    """Register is learned from examples, not from adjectives about register."""
    prompt = drafting.build_system_prompt(
        assets, sent_letters=("Twenty-five years making the unmeasurable measurable.",)
    )

    assert "LETTERS HE ACTUALLY SENT" in prompt
    assert "Twenty-five years making the unmeasurable measurable." in prompt


def test_the_prompt_is_unchanged_when_nothing_has_been_sent_yet(assets):
    """A cold start must not leave an empty section implying he writes nothing."""
    prompt = drafting.build_system_prompt(assets)

    assert "LETTERS HE ACTUALLY SENT" not in prompt


def test_sent_letters_are_examples_of_voice_not_a_source_of_facts(assets):
    """A past letter names past employers; copied forward it becomes a lie.

    The claim-tracing rule checks spans against the inventory and the resumes,
    so a sentence lifted out of an old letter would fail that check — but only
    if the model is told plainly that these are not source material.
    """
    prompt = drafting.build_system_prompt(assets, sent_letters=("An old letter.",))

    section = prompt[prompt.index("LETTERS HE ACTUALLY SENT") :]
    assert "not a source of facts" in section.lower()


def test_a_word_followed_by_a_comma_is_still_the_same_word(assets):
    """The tokeniser keeps trailing punctuation, and a claim rarely quotes it.

    "covering 400 workcenters, replacing a spreadsheet" tokenises the word as
    `workcenters,`, so a claim saying `workcenters` looked untraceable. The
    pattern keeps `$ % . , -` on purpose, for `$80m`, `10%` and `2015-2019`;
    only trailing separators are dropped.
    """
    vocabulary = assets.vocabulary()

    assert "workcenters" in vocabulary
    assert any(token.startswith("$") for token in vocabulary), "money survives"
    assert any(token.endswith("%") for token in vocabulary), "percentages survive"


def test_the_drafter_is_told_whose_letter_it_is(assets):
    """The candidate's name belongs in the rubric, not in the source code."""
    prompt = drafting.build_system_prompt(assets, candidate="Alex Rivera")

    assert prompt.startswith("You draft a cover letter for one job posting, for Alex Rivera.")
    assert "what Alex Rivera has" in prompt, "the rules name them too"


def test_an_unnamed_candidate_gets_a_prompt_that_still_reads(assets):
    """A missing name must not produce a letter addressed from 'for '."""
    prompt = drafting.build_system_prompt(assets)

    assert "for one job posting." in prompt
    assert "the candidate" in prompt.lower()
