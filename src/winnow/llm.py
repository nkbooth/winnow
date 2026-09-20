"""The Claude judge.

One call per cluster, on a complete description, asking only for the judgments
a model is actually good at. Three choices here are load-bearing:

* **The system prompt is one cached block.** It is identical for every cluster
  in a run — the instructions and the rubric's weights — so caching it is the
  difference between paying for it once and paying for it per posting.
* **The response is schema-constrained.** ``output_config.format`` makes the
  body valid JSON of the right shape, so the caller validates meaning rather
  than syntax.
* **No sampling parameters are sent.** ``temperature`` and its relatives are
  rejected outright by the current models. Reproducibility comes from recording
  the model id, prompt version and rubric version against every score — a score
  whose provenance is unknown cannot be calibrated, and pretending a knob gives
  determinism would be worse than admitting it does not.
"""

from __future__ import annotations

import json
import os

from winnow import config
from winnow.secrets import resolve as resolve_secret

#: Pinned deliberately. Swapping models silently re-baselines every score, so
#: the id is stored with each one and changed on purpose.
MODEL = "claude-opus-5"

#: Where the API key comes from when the environment has none.
#: Overridden by ``[llm] api_key_ref`` in config.toml. The default is the
#: variable the Anthropic SDK itself looks for, so an existing shell setup
#: works with no configuration at all.
API_KEY_REFERENCE = "env:ANTHROPIC_API_KEY"

#: Judging a posting against a rubric is not a reasoning-heavy task, and this
#: runs once per cluster per day across every board.
EFFORT = "medium"

#: Drafting runs once per application, by hand, and the output is read by a
#: human deciding whether to send it. Worth more thought than scoring.
DRAFTING_EFFORT = "high"

MAX_TOKENS = 16000

_DIMENSION = {
    "type": "object",
    "properties": {
        # No minimum/maximum: structured outputs reject numeric bounds outright
        # ("For 'number' type, properties maximum, minimum are not supported"),
        # and a schema the API refuses fails the run at the first posting. The
        # range is enforced in scoring._clamp_unit instead.
        "score": {"type": "number"},
        "evidence": {
            "type": ["string", "null"],
            "description": "A contiguous span copied from the posting, or null for a zero score.",
        },
    },
    "required": ["score", "evidence"],
    "additionalProperties": False,
}

SCORING_SCHEMA = {
    "type": "object",
    "properties": {
        "dimensions": {
            "type": "object",
            "properties": {
                "growth_signal": _DIMENSION,
                "builder_shaped": _DIMENSION,
                "team_leadership": _DIMENSION,
                "mission_alignment": _DIMENSION,
                "governance_heavy": _DIMENSION,
                "grind_culture": _DIMENSION,
            },
            "required": [
                "growth_signal",
                "builder_shaped",
                "team_leadership",
                "mission_alignment",
                "governance_heavy",
                "grind_culture",
            ],
            "additionalProperties": False,
        },
        "vetoes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gate": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["gate", "evidence"],
                "additionalProperties": False,
            },
        },
        "flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["kind", "evidence"],
                "additionalProperties": False,
            },
        },
        "why_fits": {"type": "string"},
        "concern": {"type": "string"},
    },
    "required": ["dimensions", "vetoes", "flags", "why_fits", "concern"],
    "additionalProperties": False,
}


DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        # The enum is measured as accepted, unlike numeric bounds. Without it
        # the model answered with the resume's full heading — "Resume A —
        # Business Systems / Operations Director" — and the draft was refused
        # after the call had been paid for. Constraining it in the schema is
        # stronger than asking nicely.
        "resume_variant": {"type": "string", "enum": ["A", "B", "C"]},
        "variant_reason": {"type": "string"},
        "tailoring_notes": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "evidence": {
                        "type": "string",
                        "description": "Exact span copied from the work inventory or a resume.",
                    },
                },
                "required": ["claim", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "resume_variant",
        "variant_reason",
        "tailoring_notes",
        "subject",
        "body",
        "claims",
    ],
    "additionalProperties": False,
}


class JudgementSchemaError(RuntimeError):
    """The model did not answer with a usable judgement."""


class ClaudeJudge:
    """Asks Claude for the judgment half of a score."""

    model = MODEL

    def __init__(self, client=None) -> None:
        self._client = client or _build_client()

    def judge(self, system: str, user: str) -> dict:
        """Score one posting.

        Args:
            system: The instruction prompt, identical across a run.
            user: The posting to judge.

        Returns:
            The parsed judgement.

        Raises:
            JudgementSchemaError: If the model declined, or answered with
                something that is not the agreed shape. Returning an empty
                judgement instead would score the posting zero on every
                dimension and look like a considered verdict.
        """
        return structured_call(self._client, system, user, schema=SCORING_SCHEMA, effort=EFFORT)


class ClaudeDrafter:
    """Asks Claude for a cover letter. Holds no credential that could send it."""

    model = MODEL

    def __init__(self, client=None) -> None:
        self._client = client or _build_client()

    def complete(self, system: str, user: str) -> dict:
        """Draft one letter.

        Args:
            system: The instruction prompt carrying the drafting assets.
            user: The posting being applied to.

        Returns:
            The parsed draft.

        Raises:
            JudgementSchemaError: If the model declined or answered unusably.
        """
        return structured_call(
            self._client, system, user, schema=DRAFT_SCHEMA, effort=DRAFTING_EFFORT
        )


def structured_call(client, system: str, user: str, *, schema: dict, effort: str) -> dict:
    """Make one schema-constrained call, with the system half cached.

    Args:
        client: An Anthropic client.
        system: The instruction prompt. Identical across a run by design, so it
            is the cached prefix and is sent as one cacheable block.
        user: The per-item half.
        schema: The JSON schema the response must satisfy.
        effort: How much thought the task is worth.

    Returns:
        The parsed response.

    Raises:
        JudgementSchemaError: If the model declined, returned no text, or
            returned something that is not JSON. An empty result would look
            like a considered answer and is never substituted.
    """
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )

    if getattr(response, "stop_reason", None) == "refusal":
        raise JudgementSchemaError("the model declined this request")

    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise JudgementSchemaError("the response carried no text block")

    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise JudgementSchemaError(f"response was not JSON: {error}") from error


def _build_client():
    """Build the Anthropic client, resolving a key only if the environment has none."""
    import anthropic

    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return anthropic.Anthropic()
    return anthropic.Anthropic(api_key=resolve_secret(config.settings().llm.api_key_ref))
