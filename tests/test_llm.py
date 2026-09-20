"""The Claude judge: what is actually sent, and what is pinned.

Two things here are guard rails rather than plumbing. The system prompt is one
cached block because it is identical for every cluster in a run and paying full
price for it per posting is the only real cost in this pipeline. And no sampling
parameter is sent at all — ``temperature`` is rejected by the current models, so
reproducibility comes from the schema and from recording the model, prompt and
rubric versions against every score, not from a knob.
"""

import json

import pytest

from winnow.llm import SCORING_SCHEMA, ClaudeJudge, JudgementSchemaError


class FakeMessages:
    def __init__(self, text):
        self.text = text
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs

        class Block:
            type = "text"

        block = Block()
        block.text = self.text
        return type("Response", (), {"content": [block], "stop_reason": "end_turn"})()


class FakeClient:
    def __init__(self, text):
        self.messages = FakeMessages(text)


VALID = json.dumps(
    {
        "dimensions": {
            "growth_signal": {"score": 0.8, "evidence": "own the function"},
            "builder_shaped": {"score": 0.0, "evidence": None},
            "team_leadership": {"score": 0.0, "evidence": None},
            "mission_alignment": {"score": 0.0, "evidence": None},
            "governance_heavy": {"score": 0.0, "evidence": None},
            "grind_culture": {"score": 0.0, "evidence": None},
        },
        "vetoes": [],
        "flags": [],
        "why_fits": "Fits.",
        "concern": "Comp unresolved.",
    }
)


def test_the_model_is_pinned_and_reported():
    client = FakeClient(VALID)
    judge = ClaudeJudge(client=client)
    judge.judge("system text", "user text")

    assert judge.model == "claude-opus-5"
    assert client.messages.kwargs["model"] == "claude-opus-5"


def test_no_sampling_parameters_are_sent():
    """Current models reject temperature outright; sending one is a 400."""
    client = FakeClient(VALID)
    ClaudeJudge(client=client).judge("system text", "user text")
    assert "temperature" not in client.messages.kwargs
    assert "top_p" not in client.messages.kwargs
    assert "top_k" not in client.messages.kwargs


def test_the_system_prompt_is_one_cached_block():
    client = FakeClient(VALID)
    ClaudeJudge(client=client).judge("system text", "user text")

    system = client.messages.kwargs["system"]
    assert system == [
        {
            "type": "text",
            "text": "system text",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_the_response_is_constrained_to_the_schema():
    client = FakeClient(VALID)
    ClaudeJudge(client=client).judge("system text", "user text")

    output_config = client.messages.kwargs["output_config"]
    assert output_config["format"] == {"type": "json_schema", "schema": SCORING_SCHEMA}
    assert output_config["effort"] in ("low", "medium", "high", "xhigh", "max")


def test_the_posting_is_the_only_thing_after_the_cache_breakpoint():
    client = FakeClient(VALID)
    ClaudeJudge(client=client).judge("system text", "user text")
    assert client.messages.kwargs["messages"] == [{"role": "user", "content": "user text"}]


def test_a_valid_body_is_parsed():
    judge = ClaudeJudge(client=FakeClient(VALID))
    judgement = judge.judge("system text", "user text")
    assert judgement["dimensions"]["growth_signal"]["score"] == 0.8


def test_a_body_that_is_not_json_is_an_error_not_an_empty_judgement():
    judge = ClaudeJudge(client=FakeClient("I'd rather not."))
    with pytest.raises(JudgementSchemaError):
        judge.judge("system text", "user text")


def test_a_refusal_is_surfaced_rather_than_scored_as_zero():
    client = FakeClient(VALID)

    def refuse(**kwargs):
        return type("Response", (), {"content": [], "stop_reason": "refusal"})()

    client.messages.create = refuse
    with pytest.raises(JudgementSchemaError):
        ClaudeJudge(client=client).judge("system text", "user text")


def test_the_schema_requires_an_evidence_field_on_every_dimension():
    dimensions = SCORING_SCHEMA["properties"]["dimensions"]["properties"]
    assert set(dimensions) == {
        "growth_signal",
        "builder_shaped",
        "team_leadership",
        "mission_alignment",
        "governance_heavy",
        "grind_culture",
    }
    for definition in dimensions.values():
        assert set(definition["required"]) == {"score", "evidence"}


def test_the_schema_avoids_keywords_structured_outputs_reject():
    """Found by a live 400, not by reading: numeric bounds are not supported.

    `output_config.format.schema: For 'number' type, properties maximum,
    minimum are not supported`. The range is enforced in `_clamp_unit` anyway,
    so nothing is lost — but a schema the API refuses fails the whole run, and
    it fails at the first posting, after the poll has already happened.
    """
    import json

    rendered = json.dumps(SCORING_SCHEMA)
    for rejected in ('"minimum"', '"maximum"', '"exclusiveMinimum"', '"exclusiveMaximum"'):
        assert rejected not in rendered


def test_dimension_scores_are_still_bounded_in_code():
    from winnow.scoring import _clamp_unit

    assert _clamp_unit(1.7) == 1.0
    assert _clamp_unit(-3) == 0.0
    assert _clamp_unit("nonsense") == 0.0


def test_the_draft_schema_constrains_the_resume_variant():
    """Measured as accepted, unlike numeric bounds.

    Without it the model answered with the resume's full heading and the draft
    was refused after the call had already been paid for.
    """
    from winnow.llm import DRAFT_SCHEMA

    assert DRAFT_SCHEMA["properties"]["resume_variant"]["enum"] == ["A", "B", "C"]
