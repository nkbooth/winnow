"""Location classing.

An enum rather than a string is what keeps Canadian and US postings of one role
in separate clusters without parsing every city name. The classes are coarse on
purpose.
"""

import pytest

from winnow.location import classify
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
