"""Shared normalisation: payload text to readable text, titles to cluster keys.

Title normalisation is the load-bearing half. It decides which postings are the
same job, and it is deliberately asymmetric: a false merge hides a real job
permanently and silently, while a false split costs one redundant digest line.
So decorations and connective words are folded away, and every word that could
distinguish two real roles — above all seniority — survives.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from winnow.models import CompInterval

_BREAK_TAGS = re.compile(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6]|ul|ol|table|section)\s*>")
_ANY_TAG = re.compile(r"<[^>]+>")
_HORIZONTAL_SPACE = re.compile(r"[ \t ]+")

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_REQ_ID = re.compile(r"#\S+")
_TRAILING_REGION = re.compile(
    r"\s*[-–—]\s*(?:us|usa|u\.s\.|remote|hybrid|onsite|on-site|canada|uk|"
    r"emea|apac|latam|amer|global|north america)\b.*$"
)
_PUNCTUATION = re.compile(r"[^a-z0-9+ ]+")

_ROMAN_NUMERALS = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}

# Dropped because they vary freely between spellings of one role. "and" is kept:
# it joins two scopes rather than decorating one.
_CONNECTIVES = frozenset({"of", "the", "for", "a", "an", "at", "in", "to", "with", "on"})

_ABBREVIATIONS = ((re.compile(r"\bsr\.?\b"), "senior"), (re.compile(r"\bjr\.?\b"), "junior"))


def html_to_text(raw: str | None) -> str | None:
    """Turn a vendor's description markup into plain text.

    Entities are decoded twice, before and after tags are removed. Greenhouse
    serves its descriptions entity-escaped, so decoding once yields HTML and the
    entities *inside* that HTML are only revealed by the second pass — doing it
    once leaves visible markup in the text the scorer reads.

    Args:
        raw: Markup from a board payload, possibly ``None``.

    Returns:
        Plain text with blocks separated by newlines, or ``None`` when there was
        nothing to decode.
    """
    if not raw:
        return None
    text = html.unescape(raw)
    text = _BREAK_TAGS.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = html.unescape(text)
    lines = (_HORIZONTAL_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    collapsed = "\n".join(line for line in lines if line)
    return collapsed or None


def normalize_title(title: str) -> str:
    """Reduce a job title to its cluster key form.

    Args:
        title: Title as posted.

    Returns:
        Lowercase title with decorations, req ids, trailing region markers,
        punctuation and connective words removed, abbreviations expanded and
        roman numerals folded to digits. Seniority words are left alone.
    """
    text = title.lower()
    text = _PARENTHETICAL.sub(" ", text)
    text = _REQ_ID.sub(" ", text)
    text = _TRAILING_REGION.sub("", text)
    text = text.replace("&", " and ")
    for pattern, expansion in _ABBREVIATIONS:
        text = pattern.sub(expansion, text)
    text = _PUNCTUATION.sub(" ", text)
    words = [_ROMAN_NUMERALS.get(word, word) for word in text.split() if word not in _CONNECTIVES]
    return " ".join(words)


def fingerprint(company: str, title: str, location_class: str) -> str:
    """Hash the identity of a posting for cross-source matching.

    Args:
        company: Company name as posted.
        title: Title as posted.
        location_class: The posting's :class:`~winnow.models.LocationClass`.

    Returns:
        A short stable hex digest. Location class is part of it so that a
        Canadian and a US posting of one role never collide.
    """
    key = f"{company.strip().lower()}|{normalize_title(title)}|{location_class}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CompRange:
    """A compensation range recovered from text.

    Attributes:
        minimum: Low end, or the single figure when only one was stated.
        maximum: High end, equal to ``minimum`` for a single figure.
        currency: ISO code; defaults to USD, which is what the target market
            quotes.
        interval: Period the figures are quoted over.
    """

    minimum: int
    maximum: int
    currency: str
    interval: CompInterval


_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?")
_RANGE_SEPARATOR = re.compile(r"^\s*(?:-|–|—|to|and)\s*$")
_HOURLY = re.compile(r"(?i)per\s+hour|/\s?hour|/\s?hr\b|\bhourly\b|an hour")
_CURRENCIES = ("USD", "CAD", "EUR", "GBP")

# Below this, an annual figure is something else entirely -- a stipend, a
# reimbursement, a bonus. Guessing salary from it would produce a wrong answer to
# the one question the comp gate asks.
_MINIMUM_CREDIBLE_ANNUAL = 1000


def parse_comp(text: str | None) -> CompRange | None:
    """Recover a compensation range from prose.

    Greenhouse and Workday bury comp in the description when they carry it at
    all, so this is how those boards reach the comp gate. What it returns is
    ``parsed`` provenance, never ``stated``: it may satisfy a floor, but the
    digest says where it came from.

    Args:
        text: Description or compensation prose.

    Returns:
        The range, or ``None`` when the text states nothing that can be a wage.
    """
    if not text:
        return None

    matches = list(_MONEY.finditer(text))
    if not matches:
        return None

    low = _money_to_int(matches[0])
    high = low
    if len(matches) > 1 and _RANGE_SEPARATOR.match(text[matches[0].end() : matches[1].start()]):
        high = _money_to_int(matches[1])

    hourly = _HOURLY.search(text) is not None and low < _MINIMUM_CREDIBLE_ANNUAL
    interval = CompInterval.HOUR if hourly else CompInterval.YEAR
    if interval is CompInterval.YEAR and low < _MINIMUM_CREDIBLE_ANNUAL:
        return None

    currency = next((code for code in _CURRENCIES if code in text), "USD")
    return CompRange(
        minimum=min(low, high), maximum=max(low, high), currency=currency, interval=interval
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    Args:
        value: Timestamp text, possibly ``None``.

    Returns:
        The parsed datetime, or ``None`` when absent or unparseable.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_epoch_millis(value: object) -> datetime | None:
    """Parse an epoch timestamp expressed in milliseconds.

    Lever quotes ``createdAt`` in milliseconds; treating it as seconds lands in
    1970 and makes every posting look ancient to the staleness heuristics.

    Args:
        value: Milliseconds since the epoch.

    Returns:
        The parsed UTC datetime, or ``None`` when the value is not a number.
    """
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _money_to_int(match: re.Match[str]) -> int:
    amount = float(match.group(1).replace(",", ""))
    if match.group(2):
        amount *= 1000
    return int(amount)
