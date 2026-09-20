"""Phases 4 to 6 — the judged half, and the guard rails around it.

What the model decides: growth signal, builder-shapedness, team leadership,
mission fit, governance weight, grind-culture clustering, and the RTO language
that only exists in prose. What it never decides: any number that matters on its
own. The score is assembled here from dimension scores and rubric weights, so a
model that drifts changes emphasis rather than arithmetic.

Two rules make the judgments checkable:

* **Every judgment quotes the posting.** A dimension scored above zero without a
  quotable span, or quoting something the posting does not contain, is dropped
  and recorded as unverified. A veto is the same — a claimed RTO requirement
  with no sentence behind it is a caught error, not a deleted job.
* **A veto overrides the score rather than reducing it.** A posting scoring 88
  with a detected onsite requirement is rejected, not ranked 88th. The score is
  still recorded, because "how many 80+ roles died on RTO this month" is a
  question the learning store should be able to answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from winnow.models import Posting
from winnow.profile import Profile
from winnow.sources.registry import VENDORS

#: Bumped whenever the prompt changes. Scores are not comparable across
#: versions, and a calibration report that mixes them reads drift as preference.
PROMPT_VERSION = "1"

#: Where every score starts before the rubric moves it.
NEUTRAL_SCORE = 50

#: A posting live longer than this is a ghost-job signal.
GHOST_AGE_DAYS = 30

#: Model dimension names mapped to the rubric's weight keys.
_JUDGED_DIMENSIONS = {
    "growth_signal": "growth_signal",
    "builder_shaped": "builder_shaped",
    "team_leadership": "team_leadership",
    "mission_alignment": "mission_alignment",
    "governance_heavy": "governance_documentation_heavy",
    "grind_culture": "grind_culture_signals",
}

#: Vetoes the model may raise. Gates that need only structured data are not here
#: — those were settled before the model was asked anything.
_PROSE_VETOES = frozenset(
    {
        "onsite_required",
        "hybrid_required",
        "relocation_required",
        "contract_exceeds_15_hours_per_week",
        "llm_generated_boilerplate",
    }
)

_WHITESPACE = re.compile(r"\s+")


class MalformedJudgement(ValueError):
    """The model's response was not a judgement this code can use."""


@dataclass(frozen=True)
class BreakdownLine:
    """One weighted factor, and the evidence that earned it."""

    dimension: str
    score: float
    weight: int
    contribution: float
    evidence: str | None
    computed: bool


@dataclass(frozen=True)
class ScoreRecord:
    """Everything a score was made of.

    The breakdown is the record, not the total: a final number cannot be
    reverse-engineered into the factors that produced it, and without those
    there is nothing to calibrate against outcomes.
    """

    score: int
    breakdown: tuple[BreakdownLine, ...]
    vetoes: tuple[dict, ...]
    flags: tuple[dict, ...]
    unverified: tuple[dict, ...]
    why_fits: str
    concern: str
    vetoed: bool
    model: str
    prompt_version: str
    rubric_version: str


class Judge(Protocol):
    """Whatever answers the judgment half. A model, or a stub in tests."""

    model: str

    def judge(self, system: str, user: str) -> dict:
        """Return a judgement for one posting."""
        ...


def score_posting(
    posting: Posting,
    profile: Profile,
    judge: Judge,
    *,
    repost_count: int = 0,
    now: datetime | None = None,
) -> ScoreRecord:
    """Score one posting against the rubric.

    Args:
        posting: A posting with a complete description.
        profile: The loaded rubric.
        judge: The model wrapper.
        repost_count: How many times this cluster has been reposted.
        now: Reference time, for the age-based signals.

    Returns:
        The assembled score and its full breakdown.

    Raises:
        MalformedJudgement: If the posting has no complete description, or the
            response is not shaped like a judgement. Scoring a fragment produces
            a confident number computed from a fragment, which is worse than
            refusing.
    """
    now = now or datetime.now(UTC)
    if not posting.description_complete or not posting.description_text:
        raise MalformedJudgement(f"{posting.source_id} has no complete description")

    judgement = judge.judge(build_system_prompt(profile), build_user_prompt(posting))
    _require_shape(judgement)

    haystack = _normalise(posting.description_text)
    unverified: list[dict] = []

    lines = _judged_lines(judgement, profile, posting, haystack, unverified)
    lines.extend(_computed_lines(judgement, profile, posting, repost_count, now))

    vetoes = _verified_vetoes(judgement, haystack, unverified)
    total = NEUTRAL_SCORE + sum(line.contribution for line in lines)

    return ScoreRecord(
        score=int(round(max(0, min(100, total)))),
        breakdown=tuple(lines),
        vetoes=tuple(vetoes),
        flags=tuple(judgement.get("flags") or ()),
        unverified=tuple(unverified),
        why_fits=str(judgement.get("why_fits") or ""),
        concern=str(judgement.get("concern") or ""),
        vetoed=bool(vetoes),
        model=judge.model,
        prompt_version=PROMPT_VERSION,
        rubric_version=profile.rubric_version,
    )


def build_system_prompt(profile: Profile) -> str:
    """Build the instruction half of the prompt.

    Identical for every posting in a run, because it is the cached prefix: a
    per-posting detail in here costs the cache on every call.

    Args:
        profile: The loaded rubric.

    Returns:
        The system prompt.
    """
    weights = "\n".join(f"  {name}: {value:+d}" for name, value in sorted(profile.weights.items()))
    return f"""You are scoring one job posting for a director-level business systems search.

You judge only what requires reading. Compensation, location, employment type,
posting age and title relevance have already been decided in code, and you must
not restate or second-guess them.

Score each dimension from 0.0 to 1.0 and quote the posting for every non-zero
score. The quote must be a contiguous span copied from the posting text. If you
cannot quote it, score the dimension 0.0 with null evidence — a score you cannot
evidence is worse than no score, because it cannot be checked.

Dimensions:
  growth_signal      scope expansion, budget ownership, a team to build, a role
                     with a ceiling above it. A lateral role with no growth is 0.
  builder_shaped     puzzles, tooling, automation, greenfield construction
  team_leadership    direct reports, hiring, a team to grow
  mission_alignment  open-source or privacy-focused product, credibly so
  governance_heavy   documentation, audit and compliance maintenance work
  grind_culture      "fast-paced" plus "wear many hats", founding titles at seed
                     stage, equity-heavy and cash-light, hustle language. None
                     of these disqualifies alone; the cluster is the signal.

Raise a veto only for a requirement stated in the text:
  onsite_required, hybrid_required, relocation_required,
  contract_exceeds_15_hours_per_week, llm_generated_boilerplate

Remote-first language with a hidden onsite requirement is the case that matters:
"must reside within 50 miles", "2 days onsite", "remote-flexible", a pending
return-to-office. Quote the sentence. A veto without a quotable sentence will be
discarded as an error.

Flags are observations, not rejections. Use kind "degree_required" when a degree
is stated as required, and "government_sector" for public-sector employers.

why_fits is two sentences. concern is one, and there is always one: if nothing
is wrong, say what is unknown.

The rubric's weights, for context on what matters (you do not apply them):
{weights}"""


def build_user_prompt(posting: Posting) -> str:
    """Build the per-posting half of the prompt.

    Args:
        posting: The posting to judge.

    Returns:
        The user prompt, carrying the text the model must quote from.
    """
    return (
        f"Company: {posting.company}\n"
        f"Title: {posting.title}\n"
        f"Locations: {', '.join(posting.locations) or 'not stated'}\n"
        f"Department: {posting.department or 'not stated'}\n\n"
        f"Posting text:\n{posting.description_text}"
    )


def _require_shape(judgement: object) -> None:
    if not isinstance(judgement, dict) or not isinstance(judgement.get("dimensions"), dict):
        raise MalformedJudgement("response has no dimensions object")


def _judged_lines(
    judgement: dict,
    profile: Profile,
    posting: Posting,
    haystack: str,
    unverified: list[dict],
) -> list[BreakdownLine]:
    dimensions = judgement["dimensions"]
    lines: list[BreakdownLine] = []

    for name, weight_key in _JUDGED_DIMENSIONS.items():
        raw = dimensions.get(name) or {}
        score = _clamp_unit(raw.get("score"))
        evidence = raw.get("evidence")
        weight = int(profile.weights.get(weight_key, 0))

        if name == "mission_alignment" and profile.is_mission_company(posting.company):
            # Thirteen companies are named in the rubric. Whether this is one of
            # them is a lookup, not a judgment call.
            score, evidence = 1.0, "named in the rubric's strong-pull list"
        elif score > 0 and not _verifiable(evidence, haystack):
            unverified.append({"dimension": name, "evidence": evidence})
            score, evidence = 0.0, None

        lines.append(
            BreakdownLine(
                dimension=weight_key,
                score=score,
                weight=weight,
                contribution=score * weight,
                evidence=evidence,
                computed=False,
            )
        )
    return lines


def _computed_lines(
    judgement: dict,
    profile: Profile,
    posting: Posting,
    repost_count: int,
    now: datetime,
) -> list[BreakdownLine]:
    """Apply the dimensions that are knowable without asking anyone."""
    lines: list[BreakdownLine] = []

    proximity, proximity_evidence = _source_proximity(posting)
    lines.append(_computed("source_proximity", proximity, profile, proximity_evidence))

    ghost, ghost_evidence = _ghost_signal(posting, repost_count, now)
    lines.append(_computed("ghost_job_signals", ghost, profile, ghost_evidence))

    flags = judgement.get("flags") or []
    degree = next((flag for flag in flags if flag.get("kind") == "degree_required"), None)
    lines.append(
        _computed(
            "degree_required_stated",
            1.0 if degree else 0.0,
            profile,
            degree.get("evidence") if degree else None,
        )
    )
    return lines


def _computed(
    dimension: str, score: float, profile: Profile, evidence: str | None
) -> BreakdownLine:
    weight = int(profile.weights.get(dimension, 0))
    return BreakdownLine(
        dimension=dimension,
        score=score,
        weight=weight,
        contribution=score * weight,
        evidence=evidence,
        computed=True,
    )


def _source_proximity(posting: Posting) -> tuple[float, str]:
    if posting.source in VENDORS:
        return 1.0, f"found on the company's own {posting.source} board"
    if any(vendor in posting.source_url for vendor in VENDORS):
        return 0.5, "aggregator listing that resolves to the employer's board"
    return 0.0, f"found via {posting.discovered_via}"


def _ghost_signal(posting: Posting, repost_count: int, now: datetime) -> tuple[float, str]:
    """Combine the computable ghost-job heuristics into one 0-1 signal.

    Research puts phantom listings at 20-35% overall and ~48% in tech. At an
    eight-item cap, a third of the digest being phantom is the difference
    between a tool worth opening and one that trains you to ignore it.
    """
    signals: list[str] = []
    strength = 0.0

    if posting.posted_at is not None:
        age = (now - posting.posted_at).days
        if age > GHOST_AGE_DAYS:
            strength += 0.5
            signals.append(f"live {age} days")
    if posting.posted_at_text and "30+" in posting.posted_at_text:
        strength += 0.5
        signals.append(posting.posted_at_text)
    if repost_count:
        strength += 0.5
        signals.append(f"reposted {repost_count}x")

    return min(1.0, strength), "; ".join(signals) or "no ghost-job signals"


def _verified_vetoes(judgement: dict, haystack: str, unverified: list[dict]) -> list[dict]:
    verified: list[dict] = []
    for veto in judgement.get("vetoes") or []:
        gate = veto.get("gate")
        if gate not in _PROSE_VETOES:
            unverified.append({"veto": gate, "reason": "gate not in the rubric"})
            continue
        if not _verifiable(veto.get("evidence"), haystack):
            unverified.append({"veto": gate, "reason": "evidence not found in the posting"})
            continue
        verified.append(dict(veto))
    return verified


def _verifiable(evidence: object, haystack: str) -> bool:
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    return _normalise(evidence) in haystack


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().lower()


def _clamp_unit(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return 0.0
    return max(0.0, min(1.0, float(value)))
