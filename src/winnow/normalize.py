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
#: What may sit between the two halves of a range. The connector is required;
#: a currency code or a period phrase either side of it is tolerated, because
#: "$113,000 USD and $158,000 USD" and "$150,000 per year to $180,000" are both
#: ordinary ways to write one. Allowing arbitrary words would pair a salary
#: with the next unrelated number in the sentence.
_RANGE_SEPARATOR = re.compile(
    r"^\s*(?:USD|CAD|EUR|GBP|AUD)?\s*(?:per\s+(?:year|hour|annum)|annually|/\s*(?:yr|hr))?"
    r"\s*(?:-|–|—|to|and)\s*$",
    re.IGNORECASE,
)
_HOURLY = re.compile(r"(?i)per\s+hour|/\s?hour|/\s?hr\b|\bhourly\b|an hour")
_CURRENCIES = ("USD", "CAD", "EUR", "GBP")

# Below this, an annual figure is something else entirely -- a stipend, a
# reimbursement, a bonus. Guessing salary from it would produce a wrong answer to
# the one question the comp gate asks.
_MINIMUM_CREDIBLE_ANNUAL = 1000

#: An hourly rate outside these is something other than a wage.
_MINIMUM_CREDIBLE_HOURLY = 10
_MAXIMUM_CREDIBLE_HOURLY = 999

#: Words a posting uses when the number that follows is the pay.
_COMP_MARKERS = (
    "salary",
    "compensation",
    "pay range",
    "base pay",
    "pay band",
    "hourly rate",
    "rate for this",
    "range for this",
)


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

    candidates = _wage_candidates(text)
    if not candidates:
        return None

    # A posting that names its pay usually says so. Where two credible figures
    # both look like wages — a funding round and a salary, say — the one the
    # posting itself calls compensation is the one meant.
    best = min(candidates, key=lambda c: (not c.announced, c.position))

    currency = next((code for code in _CURRENCIES if code in text), "USD")
    return CompRange(
        minimum=min(best.low, best.high),
        maximum=max(best.low, best.high),
        currency=currency,
        interval=best.interval,
    )


@dataclass(frozen=True)
class _WageCandidate:
    """One figure or range in the text that could be a wage."""

    low: int
    high: int
    interval: CompInterval
    position: int
    #: Whether compensation language introduces it, rather than it merely
    #: being the first money mentioned.
    announced: bool


def _wage_candidates(text: str) -> list[_WageCandidate]:
    """Every figure in the text that could credibly be somebody's pay.

    The parser used to read the first money figure and give up when it was not
    credible. Descriptions mention a market size, a monthly allowance and a
    stipend before they get to the salary, so on a real Wrike posting stating
    $180,000-$205,000 plainly, the whole range was invisible.
    """
    matches = list(_MONEY.finditer(text))
    candidates: list[_WageCandidate] = []

    index = 0
    while index < len(matches):
        match = matches[index]
        low = _money_to_int(match)
        high = low
        consumed = 1

        following = matches[index + 1] if index + 1 < len(matches) else None
        if following and _RANGE_SEPARATOR.match(text[match.end() : following.start()]):
            high = _money_to_int(following)
            consumed = 2

        interval = _interval_for(text, match.start(), low)
        if _is_credible(low, interval):
            candidates.append(
                _WageCandidate(
                    low=low,
                    high=high,
                    interval=interval,
                    position=match.start(),
                    announced=_announced_near(text, match.start()),
                )
            )
        index += consumed

    return candidates


def _interval_for(text: str, position: int, amount: int) -> CompInterval:
    """Decide hourly or annual from the words around the figure."""
    window = text[max(0, position - 120) : position + 120]
    hourly = _HOURLY.search(window) is not None and amount < _MINIMUM_CREDIBLE_ANNUAL
    return CompInterval.HOUR if hourly else CompInterval.YEAR


def _is_credible(amount: int, interval: CompInterval) -> bool:
    """Whether a figure could be a wage at all.

    A $40 allowance and a $14B market are both money and neither is a salary.
    """
    if interval is CompInterval.HOUR:
        return _MINIMUM_CREDIBLE_HOURLY <= amount <= _MAXIMUM_CREDIBLE_HOURLY
    return amount >= _MINIMUM_CREDIBLE_ANNUAL


def _announced_near(text: str, position: int) -> bool:
    """Whether compensation language introduces this figure."""
    window = text[max(0, position - 160) : position].lower()
    return any(marker in window for marker in _COMP_MARKERS)


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
