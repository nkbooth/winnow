"""Phase 1: the deterministic gates that run before any model does.

Two rules govern every case here.

**Unknown never satisfies a gate, in either direction.** A posting with no
stated compensation is not rejected for being under the floor and is not
admitted as though it cleared one. It proceeds carrying ``comp_unresolved``, and
the digest entry says so — the alternative silently discards the senior roles
that do not publish, which is most of them.

**A gate fires on evidence.** Rejecting on an inference would delete qualifying
roles invisibly, which is the one failure this system must not have.
"""

from winnow.gates import apply_structured_gates
from winnow.models import CompInterval, CompSource, LocationClass, RemoteStatus
from winnow.titles import TitleTier


def _gates(outcome):
    return {finding.gate for finding in outcome.rejections}


def _flags(outcome):
    return {finding.gate for finding in outcome.flags}


def test_comp_below_the_floor_is_rejected(make_posting, profile):
    """$185k against a $200k floor, and no model is asked about it."""
    posting = make_posting(
        comp_min=175000,
        comp_max=185000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    outcome = apply_structured_gates(posting, profile)
    assert not outcome.passed
    assert "comp_below_applicable_floor" in _gates(outcome)
    assert "185" in next(f.evidence for f in outcome.rejections)


def test_a_range_crossing_the_floor_survives(make_posting, profile):
    posting = make_posting(
        comp_min=180000,
        comp_max=220000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    assert apply_structured_gates(posting, profile).passed


def test_absent_comp_proceeds_and_is_flagged(make_posting, profile):
    posting = make_posting(comp_source=CompSource.ABSENT)
    outcome = apply_structured_gates(posting, profile)
    assert outcome.passed
    assert "comp_unresolved" in _flags(outcome)


def test_withheld_comp_is_flagged_as_withheld_not_unresolved(make_posting, profile):
    """The employer configured comp and chose not to publish it. Different fact."""
    posting = make_posting(comp_source=CompSource.WITHHELD)
    outcome = apply_structured_gates(posting, profile)
    assert outcome.passed
    assert "comp_withheld" in _flags(outcome)


def test_a_predicted_figure_never_clears_and_never_rejects(make_posting, profile):
    """A modelled number is not evidence, so it decides nothing either way."""
    above = make_posting(
        comp_min=205000,
        comp_max=205000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.PREDICTED,
    )
    below = make_posting(
        comp_min=150000,
        comp_max=150000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.PREDICTED,
    )
    for posting in (above, below):
        outcome = apply_structured_gates(posting, profile)
        assert outcome.passed
        assert "comp_predicted" in _flags(outcome)


def test_the_large_corporate_floor_applies_when_the_company_is_tiered(make_posting, profile):
    posting = make_posting(
        comp_min=240000,
        comp_max=250000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    assert apply_structured_gates(posting, profile).passed
    assert not apply_structured_gates(posting, profile, comp_tier="large_corporate").passed


def test_crypto_below_four_hundred_is_rejected(make_posting, profile):
    posting = make_posting(
        comp_min=300000,
        comp_max=350000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    outcome = apply_structured_gates(posting, profile, comp_tier="crypto")
    assert not outcome.passed
    assert "comp_below_applicable_floor" in _gates(outcome)


def test_hourly_rates_are_measured_against_the_contract_floor(make_posting, profile):
    low = make_posting(
        comp_min=120,
        comp_max=150,
        comp_interval=CompInterval.HOUR,
        comp_source=CompSource.STATED,
    )
    high = make_posting(
        comp_min=250,
        comp_max=300,
        comp_interval=CompInterval.HOUR,
        comp_source=CompSource.STATED,
    )
    assert not apply_structured_gates(low, profile).passed
    assert apply_structured_gates(high, profile).passed


def test_a_range_spanning_tiers_is_rejected(make_posting, profile):
    """$90k-$250k says there is no real budget behind the posting."""
    posting = make_posting(
        comp_min=90000,
        comp_max=250000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    outcome = apply_structured_gates(posting, profile)
    assert not outcome.passed
    assert "salary_range_spans_tiers" in _gates(outcome)


def test_hybrid_and_onsite_are_rejected(make_posting, profile):
    hybrid = make_posting(remote=RemoteStatus.HYBRID)
    onsite = make_posting(remote=RemoteStatus.ONSITE)
    assert "hybrid_required" in _gates(apply_structured_gates(hybrid, profile))
    assert "onsite_required" in _gates(apply_structured_gates(onsite, profile))


def test_an_unstated_arrangement_proceeds_to_the_prose_veto(make_posting, profile):
    """The RTO trap lives in sentences, so absence of a field decides nothing."""
    posting = make_posting(remote=RemoteStatus.UNKNOWN, locations=("Boston, MA",))
    outcome = apply_structured_gates(posting, profile)
    assert outcome.passed
    assert "remote_unresolved" in _flags(outcome)


def test_a_non_us_posting_is_rejected(make_posting, profile):
    posting = make_posting(locations=("Remote (Canada)",))
    outcome = apply_structured_gates(posting, profile)
    assert not outcome.passed
    assert "non_us_location" in _gates(outcome)
    assert outcome.location_class is LocationClass.NON_US


def test_the_stealth_list_employer_never_surfaces(make_posting, profile):
    posting = make_posting(company="Contoso Manufacturing")
    outcome = apply_structured_gates(posting, profile)
    assert not outcome.passed
    assert "employer_in_stealth_list" in _gates(outcome)


def test_an_off_target_title_is_dropped_before_the_model(make_posting, profile):
    posting = make_posting(title="Senior Backend Engineer")
    outcome = apply_structured_gates(posting, profile)
    assert not outcome.passed
    assert "title_off_target" in _gates(outcome)
    assert outcome.title_tier is TitleTier.OFF_TARGET


def test_a_discovery_title_without_comp_is_flagged_not_rejected(make_posting, profile):
    """Measured 2026-09-19: this rule killed 13 of 19 title-relevant postings.

    It required posted comp clearing the floor, and most employers publish
    none — so it rejected analyst-level roles by construction rather than on
    their merits. Primary titles with absent comp already proceed carrying a
    flag; discovery titles now do the same, and the digest's concern line and
    the cap do the limiting.
    """
    unresolved = make_posting(title="Business Systems Analyst", comp_source=CompSource.ABSENT)
    outcome = apply_structured_gates(unresolved, profile)

    assert outcome.passed
    assert "discovery_title_without_comp" not in _gates(outcome)
    assert "discovery_title_unproven" in _flags(outcome)


def test_a_discovery_title_below_the_floor_is_still_rejected(make_posting, profile):
    """Flagging absence is not the same as tolerating a published low number."""
    posting = make_posting(
        title="Business Systems Analyst",
        comp_min=95000,
        comp_max=120000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    outcome = apply_structured_gates(posting, profile)
    assert not outcome.passed
    assert "comp_below_applicable_floor" in _gates(outcome)


def test_a_discovery_title_with_clearing_comp_carries_no_flag(make_posting, profile):
    clearing = make_posting(
        title="Business Systems Analyst",
        comp_min=210000,
        comp_max=230000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    outcome = apply_structured_gates(clearing, profile)
    assert outcome.passed
    assert "discovery_title_unproven" not in _flags(outcome)


def test_a_primary_title_does_not_need_comp(make_posting, profile):
    posting = make_posting(title="Director of Business Systems", comp_source=CompSource.ABSENT)
    outcome = apply_structured_gates(posting, profile)
    assert outcome.passed
    assert outcome.title_tier is TitleTier.PRIMARY


def test_every_rejection_carries_its_evidence(make_posting, profile):
    posting = make_posting(company="Contoso Manufacturing", remote=RemoteStatus.ONSITE)
    outcome = apply_structured_gates(posting, profile)
    assert len(outcome.rejections) >= 2
    assert all(finding.evidence for finding in outcome.rejections)
