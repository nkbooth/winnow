"""Title tiering — the cheap gate in front of the LLM.

Tailscale's board alone is 29 distinct titles, nearly all engineering. Sending
those to a model to be told they are irrelevant would be the largest single cost
in the system and would buy nothing, so relevance is decided here: deterministic,
free, and reading its targets from the rubric rather than from a constant.

Tiering is deliberately generous at the boundary. A title that is plausibly in
scope goes on to be judged; only titles with no overlap at all are dropped.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from enum import StrEnum

from winnow.normalize import normalize_title
from winnow.profile import Profile

#: Above this, two normalised titles are the same kind of job.
SIMILARITY_THRESHOLD = 0.62

#: Function words that appear in target titles without narrowing them. A posting
#: matching only these has matched nothing.
_GENERIC = frozenset(
    {
        "director",
        "senior",
        "manager",
        "head",
        "principal",
        "lead",
        "staff",
        "analyst",
        "program",
        "programme",
        "and",
        "2",
        "3",
    }
)

#: Domain anchors. A posting naming one of these is in scope whatever else its
#: title says, which is what keeps a differently-worded real match from being
#: dropped by string similarity alone.
#:
#: Matched on word boundaries, not as substrings. "erp" inside "enterprise" sent
#: five account executives and two backend engineers to the scorer on the first
#: real run — every Enterprise title on every board looked like an ERP role.
_ANCHORS = (
    "business systems",
    "business operations",
    "business process",
    "bizops",
    "revenue operations",
    "revops",
    "sales operations",
    "systems operations",
    "erp",
    "netsuite",
    "process engineering",
    # Added 2026-09-19 after a live scan found these OFF_TARGET across twelve
    # boards: modern SaaS naming for the work the rubric already targets. They
    # are phrases, not words, so "enterprise systems" does not admit the dozens
    # of "Account Executive, Enterprise" roles those same boards are full of.
    "gtm engineering",
    "gtm systems",
    "go to market systems",
    "revenue systems",
    "enterprise systems",
    "sales systems",
    "deal desk",
)

_ANCHOR_PATTERNS = tuple(re.compile(rf"\b{re.escape(anchor)}\b") for anchor in _ANCHORS)


class TitleTier(StrEnum):
    """How relevant a posting's title is to the target search.

    ``DISCOVERY`` titles carry an extra condition from the rubric: they only
    survive if posted compensation independently clears the floor.
    """

    PRIMARY = "primary"
    DISCOVERY = "discovery"
    OFF_TARGET = "off_target"


def classify_title(title: str, profile: Profile) -> TitleTier:
    """Tier one posting title against the rubric's target titles.

    Args:
        title: Title as posted.
        profile: The loaded rubric.

    Returns:
        The tier. ``OFF_TARGET`` means no LLM call is worth making.
    """
    normalised = normalize_title(title)
    if not normalised:
        return TitleTier.OFF_TARGET

    primary = _best_similarity(normalised, profile.primary_titles)
    discovery = _best_similarity(normalised, profile.discovery_titles)
    if max(primary, discovery) >= SIMILARITY_THRESHOLD:
        # The rubric lists "Principal Business Systems Analyst" as a target and
        # "Business Systems Analyst" as discovery-only, so the closer match has
        # to win rather than whichever list is consulted first. Ties go to
        # discovery, which is the tier that carries the extra comp condition.
        return TitleTier.PRIMARY if primary > discovery else TitleTier.DISCOVERY

    anchored = any(pattern.search(normalised) for pattern in _ANCHOR_PATTERNS)
    if not anchored:
        return TitleTier.OFF_TARGET

    # An anchored title is in scope; whether it is a target-level role or a
    # discovery-level one is decided by its seniority words, since that is the
    # difference the comp floor turns on.
    return TitleTier.PRIMARY if _is_leadership(normalised) else TitleTier.DISCOVERY


def _best_similarity(normalised: str, targets: tuple[str, ...]) -> float:
    best = 0.0
    for target in targets:
        normalised_target = normalize_title(target)
        ratio = SequenceMatcher(None, normalised, normalised_target).ratio()
        if ratio > best and _shares_a_distinctive_word(normalised, normalised_target):
            best = ratio
    return best


def _shares_a_distinctive_word(left: str, right: str) -> bool:
    """Require overlap on something other than a function word.

    "Senior Director of Engineering" and "Senior Director of Business Systems"
    are similar as strings and unrelated as jobs.
    """
    left_words = set(left.split()) - _GENERIC
    right_words = set(right.split()) - _GENERIC
    return bool(left_words & right_words)


def _is_leadership(normalised: str) -> bool:
    return any(
        word in normalised.split() for word in ("director", "head", "vp", "principal")
    ) or normalised.startswith("senior manager")
