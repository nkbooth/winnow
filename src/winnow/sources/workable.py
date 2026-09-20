"""Workable.

The endpoint is the one an employer embeds in their own site — the careers
widget — and with ``details=true`` it returns whole postings: description,
structured location, employment type and publish date, no detail fetch needed.

An earlier pass recorded Workable as unpollable on the evidence of one board
that answered with an empty array. It was empty because that employer had no
open roles. The endpoint was working the whole time, and the two fixtures behind
this module exist to keep an empty board and an unreadable board distinguishable
from each other.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from winnow.location import classify
from winnow.models import (
    Board,
    CompInterval,
    CompSource,
    EmploymentType,
    EmploymentTypeSource,
    PostedAtPrecision,
    Posting,
    RemoteSource,
    RemoteStatus,
)
from winnow.normalize import fingerprint, html_to_text, parse_iso
from winnow.sources.base import default_client, request_headers
from winnow.sources.registry import VENDORS

_EMPLOYMENT_TYPES = {
    "full-time": EmploymentType.FULL_TIME,
    "part-time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.CONTRACT,
    "internship": EmploymentType.INTERN,
    "intern": EmploymentType.INTERN,
}


class WorkableAdapter:
    """Adapter for Workable boards."""

    name = "workable"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Request every posting on a board.

        Args:
            board: The board to poll.

        Returns:
            One payload per posting, which may legitimately be empty. Rows
            advertising the same job in several cities are merged first; see
            :func:`_merge_locations`.

        Raises:
            httpx.HTTPStatusError: If the board does not answer with success.
        """
        vendor = VENDORS[self.name]
        response = self._client.get(vendor.list_url(board.identifier), headers=request_headers())
        response.raise_for_status()
        return _merge_locations(vendor.postings_in(response.json()))

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one Workable payload to a Posting.

        Args:
            raw: A posting from the widget payload.
            board: The board it came from.
            now: First-seen timestamp; injected so normalisation stays pure.

        Returns:
            The normalised posting. Workable states no compensation on this
            endpoint even where the employer configured one, so comp is absent
            rather than withheld — nothing here distinguishes the two.
        """
        now = now or datetime.now(UTC)
        company = str(board.company)
        title = str(raw["title"])
        locations = _locations(raw)
        remote, remote_source = _arrangement(raw.get("telecommuting"))
        employment_type, employment_source = _employment_type(raw.get("employment_type"))
        posted_at = _published_on(raw.get("published_on"))
        description = html_to_text(raw.get("description"))

        return Posting(
            source=self.name,
            source_id=str(raw["shortcode"]),
            company=company,
            title=title,
            source_url=str(raw.get("url") or raw.get("shortlink")),
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(company, title, classify(locations, remote)),
            posted_at=posted_at,
            posted_at_precision=(PostedAtPrecision.DAY if posted_at else PostedAtPrecision.UNKNOWN),
            remote=remote,
            remote_source=remote_source,
            locations=locations,
            employment_type=employment_type,
            employment_type_source=employment_source,
            comp_min=None,
            comp_max=None,
            comp_currency=None,
            comp_interval=CompInterval.UNKNOWN,
            comp_source=CompSource.ABSENT,
            description_text=description,
            description_complete=description is not None,
            department=raw.get("department"),
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """No detail fetch: ``details=true`` already returned the description."""
        return None


def _merge_locations(rows: Iterable[dict]) -> list[dict]:
    """Collapse the rows Workable emits per location into one row per job.

    A posting open in three cities arrives as three rows sharing a shortcode and
    a URL. Left alone they collide on ``source_id`` and overwrite each other in
    the store. Merging keeps the union of locations, which is what the location
    gate needs to see before anything else collapses.
    """
    merged: dict[str, dict] = {}
    for row in rows:
        shortcode = str(row.get("shortcode"))
        existing = merged.get(shortcode)
        if existing is None:
            merged[shortcode] = {**row, "locations": list(row.get("locations") or [])}
            continue
        known = {_join(place) for place in existing["locations"]}
        existing["locations"].extend(
            place for place in (row.get("locations") or []) if _join(place) not in known
        )
    return list(merged.values())


def _locations(raw: dict) -> tuple[str, ...]:
    """Assemble location strings from the structured block.

    Hidden locations are the employer asking for them not to be shown, so they
    are not shown.
    """
    block = raw.get("locations")
    if isinstance(block, list) and block:
        assembled = tuple(
            _join(place) for place in block if isinstance(place, dict) and not place.get("hidden")
        )
        found = tuple(place for place in assembled if place)
        if found:
            return found
    fallback = _join(
        {"city": raw.get("city"), "region": raw.get("state"), "country": raw.get("country")}
    )
    return (fallback,) if fallback else ()


def _join(place: dict) -> str:
    parts = (place.get("city"), place.get("region"), place.get("country"))
    return ", ".join(str(part) for part in parts if part)


def _arrangement(telecommuting: object) -> tuple[RemoteStatus, RemoteSource]:
    """Read the employer's remote flag.

    ``False`` is the field's default, so it says nothing; only ``True`` is
    evidence. Same handling as Ashby's ``isRemote``, for the same reason.
    """
    if telecommuting is True:
        return RemoteStatus.REMOTE, RemoteSource.STRUCTURED
    return RemoteStatus.UNKNOWN, RemoteSource.ABSENT


def _employment_type(value: object) -> tuple[EmploymentType, EmploymentTypeSource]:
    if not isinstance(value, str):
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    employment_type = _EMPLOYMENT_TYPES.get(value.strip().lower())
    if employment_type is None:
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    return employment_type, EmploymentTypeSource.STRUCTURED


def _published_on(value: object) -> datetime | None:
    """Parse ``published_on``, which is a bare date rather than a timestamp."""
    if not isinstance(value, str):
        return None
    parsed = parse_iso(value)
    return parsed.replace(tzinfo=UTC) if parsed and parsed.tzinfo is None else parsed
