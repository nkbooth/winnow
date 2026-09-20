"""Location classing.

An enum rather than a string is what keeps Canadian and US postings of one role
in separate clusters without parsing every city name. The classes are coarse on
purpose.
"""

import pytest

from winnow.location import classify, looks_non_us, looks_us
from winnow.models import LocationClass, RemoteStatus


@pytest.mark.parametrize(
    ("locations", "remote", "expected"),
    [
        (("Remote (United States)",), RemoteStatus.REMOTE, LocationClass.US_REMOTE),
        (("Remote (US)",), RemoteStatus.REMOTE, LocationClass.US_REMOTE),
        (("Remote",), RemoteStatus.REMOTE, LocationClass.US_REMOTE),
        (("Remote (Canada)",), RemoteStatus.REMOTE, LocationClass.NON_US),
        (("Remote (United Kingdom)",), RemoteStatus.REMOTE, LocationClass.NON_US),
        (("Toronto, Ontario, Canada",), RemoteStatus.UNKNOWN, LocationClass.NON_US),
        (("Geneva, Switzerland",), RemoteStatus.ONSITE, LocationClass.NON_US),
        (("Austin, TX",), RemoteStatus.HYBRID, LocationClass.US_HYBRID),
        (("Raleigh, North Carolina",), RemoteStatus.ONSITE, LocationClass.US_ONSITE),
        (("Washington D.C.",), RemoteStatus.HYBRID, LocationClass.US_HYBRID),
        (("San Francisco, CA",), RemoteStatus.UNKNOWN, LocationClass.UNKNOWN),
        ((), RemoteStatus.REMOTE, LocationClass.UNKNOWN),
        ((), RemoteStatus.UNKNOWN, LocationClass.UNKNOWN),
    ],
)
def test_classify(locations, remote, expected):
    assert classify(locations, remote) == expected


def test_a_us_and_canada_posting_is_us_eligible():
    """Firing the non-US gate needs evidence that the role excludes the US."""
    assert (
        classify(("Remote (United States | Canada)",), RemoteStatus.REMOTE)
        == LocationClass.US_REMOTE
    )


def test_us_remote_requires_evidence_of_remote():
    """A US city with no arrangement stated is unresolved, not onsite."""
    assert classify(("Boston, MA",), RemoteStatus.UNKNOWN) == LocationClass.UNKNOWN


@pytest.mark.parametrize(
    "location",
    [
        # "Solutions Architect - MENA" reached the scorer and was ranked 77,
        # because the classifier had never heard of a regional abbreviation.
        "Remote - MENA",
        "Remote, EMEA",
        "Remote - DACH",
        "Remote (UKI)",
        "Benelux",
        "Nordics - Remote",
        "Remote - ANZ",
        "LATAM",
        "Remote - APAC",
    ],
)
def test_regional_abbreviations_are_recognised_as_non_us(location):
    assert classify((location,), RemoteStatus.REMOTE) is LocationClass.NON_US


@pytest.mark.parametrize(
    "location",
    [
        # "Indiana" contains "india", "Menands" contains "mena", "Chinatown"
        # contains "china". Substring matching makes every one of these a
        # foreign posting, and gating on a guess deletes real jobs silently.
        "Remote - Indianapolis",
        "Indiana",
        "Menands, NY",
        "Chinatown, San Francisco",
        "Remote - Indiana",
    ],
)
def test_a_us_place_is_never_mistaken_for_a_country_it_contains(location):
    from winnow.location import looks_non_us

    assert looks_non_us(location) is False
    assert classify((location,), RemoteStatus.REMOTE) is not LocationClass.NON_US


# ---------------------------------------------------------------------------
# Country codes, which boards write instead of country names
# ---------------------------------------------------------------------------


def test_a_three_letter_country_code_after_a_comma_is_a_country():
    """Greenhouse writes "São Paulo, BRA". The classifier knew only "brazil".

    So a Salesforce role in Brazil reached the scorer as location-unknown, and
    the model then missed the sentence saying the office works onsite five days
    a week. A gate that fires on the country never depends on the model reading
    the prose correctly.
    """
    assert looks_non_us("São Paulo, BRA") is True
    assert classify(("São Paulo, BRA",), RemoteStatus.UNKNOWN) is LocationClass.NON_US


def test_a_us_city_with_a_state_code_is_not_a_country():
    """US cities carry two-letter state codes, which is what makes this safe."""
    for location in ("Austin, TX", "Indianapolis, IN", "Philadelphia, PA", "Chicago, IL"):
        assert looks_non_us(location) is False, location
        assert looks_us(location) is True, location


def test_a_country_code_is_matched_only_in_upper_case_after_a_comma():
    """The guard against the Indiana problem, in its newest form.

    "can", "are", "per" and "and" are all ISO codes for somewhere. Matching
    them as words would gate half of every posting, so a code counts only in
    the `City, CODE` shape a board actually writes.
    """
    assert looks_non_us("We can relocate you") is False
    assert looks_non_us("Compensation is listed per year") is False
    assert looks_non_us("Research and development, Boston") is False


def test_usa_after_a_comma_is_the_united_states():
    assert looks_us("Austin, TX, USA") is True
    assert looks_non_us("Austin, TX, USA") is False


def test_codes_that_collide_with_us_airport_shorthand_are_left_out():
    """AUS is Australia and Austin; IND is India and Indianapolis.

    Excluded deliberately. Being unable to classify those is honest; gating an
    Austin role as foreign is not, and unknown never satisfies a gate anyway.
    """
    assert looks_non_us("Somewhere, AUS") is False
    assert looks_non_us("Somewhere, IND") is False
    assert looks_non_us("Somewhere, PHL") is False


def test_common_non_iso_abbreviations_are_recognised():
    """Boards write what people say, not what the standard says."""
    assert looks_non_us("Dubai, UAE") is True
    assert looks_non_us("Riyadh, KSA") is True
