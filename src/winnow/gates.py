"""Phase 1 — the deterministic gates, run before the model.

Everything structured is decided here: cheap, testable, free, and incapable of
hallucinating a number. An LLM asked whether $185,000 clears a $200,000 floor
will occasionally say yes, so that question never reaches it.

Two rules run through all of it:

* **Unknown never satisfies a gate, in either direction.** Missing compensation
  is not a rejection and not a pass; it is a flag the digest carries.
* **A gate fires on evidence.** An unstated work arrangement is left to the
  prose veto in phase 5 rather than inferred here, because the RTO trap lives in
  sentences and rejecting on a guess deletes qualifying roles invisibly.
"""

from __future__ import annotations

from dataclasses import dataclass

from winnow.location import classify
from winnow.models import CompInterval, CompSource, LocationClass, Posting, RemoteStatus
from winnow.profile import Profile
from winnow.titles import TitleTier, classify_title

#: Compensation provenances that may satisfy or fire a floor gate. A predicted
#: figure is modelled rather than published, so it decides nothing.
_TRUSTWORTHY_COMP = (CompSource.STATED, CompSource.PARSED)


@dataclass(frozen=True)
class Finding:
    """One gate's verdict, with the evidence behind it.

    Evidence is not decoration: a rejection nobody can check is a rejection
    nobody can correct, and these accumulate into the record that says how many
    qualifying roles died on which rule this month.
    """

    gate: str
    evidence: str


@dataclass(frozen=True)
class GateOutcome:
    """What the structured gates established about one posting."""

    rejections: tuple[Finding, ...]
    flags: tuple[Finding, ...]
    title_tier: TitleTier
    location_class: LocationClass

    @property
    def passed(self) -> bool:
        """True when nothing rejected the posting."""
        return not self.rejections


def apply_structured_gates(
    posting: Posting, profile: Profile, *, comp_tier: str = "mid_market"
) -> GateOutcome:
    """Run every gate that needs no description text.

    Args:
        posting: A normalised posting.
        profile: The loaded rubric; every floor and threshold comes from it.
        comp_tier: Which compensation floor applies to this employer.

    Returns:
        The rejections and flags, plus the title tier and location class, which
        later phases reuse rather than recompute.
    """
    rejections: list[Finding] = []
    flags: list[Finding] = []

    title_tier = classify_title(posting.title, profile)
    location_class = classify(posting.locations, posting.remote)

    if profile.is_excluded(posting.company):
        rejections.append(Finding("employer_in_stealth_list", posting.company))

    if location_class is LocationClass.NON_US:
        rejections.append(Finding("non_us_location", ", ".join(posting.locations)))

    if posting.remote is RemoteStatus.ONSITE:
        rejections.append(Finding("onsite_required", f"remote={posting.remote}"))
    elif posting.remote is RemoteStatus.HYBRID:
        rejections.append(Finding("hybrid_required", f"remote={posting.remote}"))
    elif posting.remote is RemoteStatus.UNKNOWN:
        flags.append(Finding("remote_unresolved", f"source={posting.remote_source}"))

    if title_tier is TitleTier.OFF_TARGET:
        rejections.append(Finding("title_off_target", posting.title))

    rejections.extend(_comp_rejections(posting, profile, comp_tier))
    flags.extend(_comp_flags(posting))

    if title_tier is TitleTier.DISCOVERY and not _comp_clears_floor(posting, profile, comp_tier):
        # Flagged rather than rejected, changed 2026-09-19 after measuring that
        # this rule alone killed 13 of 19 title-relevant postings across twelve
        # boards. Requiring published comp rejected analyst-level roles by
        # construction rather than on their merits, because most employers
        # publish none. A stated figure below the floor still rejects, above;
        # what is flagged here is the absence of one.
        flags.append(
            Finding(
                "discovery_title_unproven",
                f"{posting.title} is a discovery-tier title and comp is "
                f"{posting.comp_source}; it has not earned the tier",
            )
        )

    return GateOutcome(
        rejections=tuple(rejections),
        flags=tuple(flags),
        title_tier=title_tier,
        location_class=location_class,
    )


def _comp_rejections(posting: Posting, profile: Profile, comp_tier: str) -> list[Finding]:
    if posting.comp_source not in _TRUSTWORTHY_COMP:
        return []
    if posting.comp_min is None or posting.comp_max is None:
        return []

    findings: list[Finding] = []
    ratio_limit = profile.max_salary_range_ratio
    if ratio_limit and posting.comp_min > 0 and posting.comp_max / posting.comp_min > ratio_limit:
        findings.append(
            Finding(
                "salary_range_spans_tiers",
                f"${posting.comp_min:,}-${posting.comp_max:,} spans "
                f"{posting.comp_max / posting.comp_min:.1f}x",
            )
        )

    floor = _applicable_floor(posting, profile, comp_tier)
    if floor and posting.comp_max < floor:
        findings.append(
            Finding(
                "comp_below_applicable_floor",
                f"${posting.comp_max:,} below ${floor:,} ({comp_tier})",
            )
        )
    return findings


def _comp_flags(posting: Posting) -> list[Finding]:
    if posting.comp_source is CompSource.WITHHELD:
        return [Finding("comp_withheld", "employer configured comp and did not publish it")]
    if posting.comp_source is CompSource.PREDICTED:
        return [Finding("comp_predicted", "figure is modelled, not published")]
    if posting.comp_source is CompSource.ABSENT:
        return [Finding("comp_unresolved", "no compensation stated")]
    return []


def _comp_clears_floor(posting: Posting, profile: Profile, comp_tier: str) -> bool:
    if posting.comp_source not in _TRUSTWORTHY_COMP or posting.comp_max is None:
        return False
    floor = _applicable_floor(posting, profile, comp_tier)
    return bool(floor) and posting.comp_max >= floor


def _applicable_floor(posting: Posting, profile: Profile, comp_tier: str) -> int:
    if posting.comp_interval is CompInterval.HOUR:
        return profile.contract_hourly_floor
    if posting.comp_interval is CompInterval.YEAR:
        return profile.floor_for(comp_tier)
    return 0
