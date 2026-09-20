"""Coarse location classing.

The class, not the city, is what dedupe keys on: it is what keeps the Canadian
and US copies of one role in separate clusters without parsing every place name
on earth.

Classing is evidence-led in both directions. ``NON_US`` is what fires the
non-US hard gate, so it requires positive evidence that a role is not open to
the US — a posting listing both the US and Canada is US-eligible, not foreign.
Equally, a US city with no stated arrangement is ``UNKNOWN`` rather than
``US_ONSITE``: guessing onsite there would fire the onsite gate on an inference
and quietly delete qualifying roles.
"""

from __future__ import annotations

import re

from winnow.models import LocationClass, RemoteStatus

#: Matched on word boundaries, never as substrings. "Indiana" contains "india"
#: and "Menands" contains "mena"; substring matching turns both into foreign
#: postings, and the non-US gate deletes those silently.
_US_MARKERS = (
    r"united states",
    r"usa?",
    r"u\.s\.a?\.?",
    r"d\.c\.",
    r"washington dc",
    r"anywhere in the us",
)

_NON_US_COUNTRIES = (
    "canada",
    "united kingdom",
    "uk)",
    " uk",
    "ireland",
    "germany",
    "france",
    "spain",
    "portugal",
    "netherlands",
    "belgium",
    "switzerland",
    "sweden",
    "norway",
    "denmark",
    "finland",
    "poland",
    "romania",
    "czech",
    "austria",
    "italy",
    "greece",
    "israel",
    "india",
    "singapore",
    "japan",
    "australia",
    "new zealand",
    "brazil",
    "mexico",
    "argentina",
    "colombia",
    "chile",
    "south africa",
    "nigeria",
    "kenya",
    "emea",
    "apac",
    "latam",
    "mena",
    "dach",
    "benelux",
    "nordics",
    "anz",
    "uki",
    "iberia",
    "japac",
    "russia",
    "ukraine",
    "turkey",
    "uae",
    "philippines",
    "vietnam",
    "malaysia",
    "korea",
    "taiwan",
    "hong kong",
    "china",
    "ontario",
    "quebec",
    "british columbia",
    "toronto",
    "vancouver",
    "montreal",
    "london",
    "berlin",
    "amsterdam",
    "dublin",
    "paris",
    "zurich",
    "geneva",
    "stockholm",
    "sydney",
    "tokyo",
    "bengaluru",
    "bangalore",
)

_US_STATE_CODES = frozenset(
    (
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    )
)

_US_STATE_NAMES = (
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "california",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "rhode island",
    "south carolina",
    "south dakota",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "west virginia",
    "wisconsin",
    "wyoming",
    "district of columbia",
)

_STATE_CODE_PATTERN = re.compile(r",\s*([A-Z]{2})\b")

#: Boards write "São Paulo, BRA" as often as they write the country name, and
#: the classifier knew only names. Matched in the `City, CODE` shape and in
#: upper case only, which is what makes it safe: "can", "are", "per" and "and"
#: are all ISO codes for somewhere, and matching them as words would gate half
#: of every posting. US cities carry two-letter state codes, so a three-letter
#: code after a comma is a country rather than a state.
_COUNTRY_CODE_PATTERN = re.compile(r",\s*([A-Z]{3})\b")

#: Codes that also stand for a US city are left out on purpose. AUS is
#: Australia and Austin; IND is India and Indianapolis; PHL is the Philippines
#: and Philadelphia. Being unable to classify those is honest. Gating an Austin
#: role as foreign is not, and an unknown location satisfies no gate anyway.
_AMBIGUOUS_CODES = frozenset({"AUS", "IND", "PHL", "CHI", "LAX", "ATL", "DEN", "POR"})

_NON_US_COUNTRY_CODES = frozenset(
    {
        "ARE",
        "ARG",
        "AUT",
        "BEL",
        "BGR",
        "BRA",
        "CAN",
        "CHE",
        "CHL",
        "CHN",
        "COL",
        "CRI",
        "CZE",
        "DEU",
        "DNK",
        "EGY",
        "ESP",
        "EST",
        "FIN",
        "FRA",
        "GBR",
        "GRC",
        "HKG",
        "HRV",
        "HUN",
        "IDN",
        "IRL",
        "ISR",
        "ITA",
        "JPN",
        "KEN",
        "KOR",
        "LTU",
        "LUX",
        "LVA",
        "MEX",
        "MYS",
        "NGA",
        "NLD",
        "NOR",
        "NZL",
        "PAK",
        "PER",
        "POL",
        "PRT",
        "ROU",
        "SAU",
        "SGP",
        "SRB",
        "SVK",
        "SVN",
        "SWE",
        "THA",
        "TUR",
        "TWN",
        "UKR",
        "URY",
        "VNM",
        "ZAF",
        # Not ISO, but what boards actually write.
        "UAE",
        "KSA",
    }
    - _AMBIGUOUS_CODES
)

#: A code after a comma that means the United States rather than somewhere else.
_US_COUNTRY_CODES = frozenset({"USA"})


_US_PATTERN = re.compile(r"\b(?:" + "|".join(_US_MARKERS) + r")", re.IGNORECASE)
_NON_US_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(name) for name in _NON_US_COUNTRIES) + r")\b",
    re.IGNORECASE,
)


def looks_us(location: str) -> bool:
    """Say whether a location string names somewhere in the United States.

    Args:
        location: A location string as a board wrote it.

    Returns:
        True when a US marker, state name, or ``City, ST`` state code is present.
    """
    if _US_PATTERN.search(location):
        return True
    padded = f" {location.lower()} "
    if any(state in padded for state in _US_STATE_NAMES):
        return True
    if any(code in _US_COUNTRY_CODES for code in _COUNTRY_CODE_PATTERN.findall(location)):
        return True
    return any(code in _US_STATE_CODES for code in _STATE_CODE_PATTERN.findall(location))


def looks_non_us(location: str) -> bool:
    """Say whether a location string names somewhere outside the United States.

    Matched on word boundaries: "Indiana" contains "india" and "Menands"
    contains "mena", and a substring match would gate both as foreign.

    Country codes are matched separately and more narrowly — see
    :data:`_COUNTRY_CODE_PATTERN` — because the same care is needed in a newer
    form: half the alpha-3 codes are ordinary English words.
    """
    if _NON_US_PATTERN.search(location):
        return True
    return any(code in _NON_US_COUNTRY_CODES for code in _COUNTRY_CODE_PATTERN.findall(location))


def classify(locations: tuple[str, ...] | list[str], remote: RemoteStatus) -> LocationClass:
    """Reduce a posting's locations and work arrangement to one class.

    Args:
        locations: Location strings from the posting.
        remote: The posting's established work arrangement.

    Returns:
        The class used in the cluster key and by the non-US gate. ``UNKNOWN``
        whenever the pair does not establish one, which never satisfies a gate.
    """
    if not locations:
        return LocationClass.UNKNOWN

    us = any(looks_us(location) for location in locations)
    non_us = any(looks_non_us(location) for location in locations)

    if non_us and not us:
        return LocationClass.NON_US
    if remote is RemoteStatus.REMOTE:
        # An unmarked remote posting is treated as US-eligible: only evidence
        # that it excludes the US may fire the non-US gate.
        return LocationClass.US_REMOTE
    if not us:
        return LocationClass.UNKNOWN
    if remote is RemoteStatus.HYBRID:
        return LocationClass.US_HYBRID
    if remote is RemoteStatus.ONSITE:
        return LocationClass.US_ONSITE
    return LocationClass.UNKNOWN
