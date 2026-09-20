"""Greenhouse.

The best documented vendor and the weakest for structured signal. Remote status
exists only inside a location string, employment type hides in a per-board
custom metadata array whose field names are not standardised, and descriptions
arrive entity-escaped. Comp is absent from the payload and occasionally buried in
the body, so it is parsed from prose and marked as such.
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
from winnow.normalize import fingerprint, html_to_text, parse_comp, parse_iso
from winnow.sources.base import default_client, request_headers
from winnow.sources.registry import VENDORS

# The field holding employment type is defined per board, so it is matched by
# name rather than indexed. Anything unrecognised stays UNKNOWN instead of being
# guessed at.
_EMPLOYMENT_FIELD_NAMES = (
    "employment type",
    "employment status",
    "job type",
    "type of employment",
    "worker type",
)

_EMPLOYMENT_VALUES = (
    ("full", EmploymentType.FULL_TIME),
    ("part", EmploymentType.PART_TIME),
    ("contract", EmploymentType.CONTRACT),
    ("temporary", EmploymentType.CONTRACT),
    ("intern", EmploymentType.INTERN),
)


class GreenhouseAdapter:
    """Adapter for Greenhouse job boards."""

    name = "greenhouse"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Request every posting on a board, descriptions included.

        Args:
            board: The board to poll.

        Returns:
            The raw posting payloads.

        Raises:
            httpx.HTTPStatusError: If the board does not answer with success. The
                caller records the per-source outcome; a failing board must not
                fail the run, but it must not look like an empty one either.
        """
        vendor = VENDORS[self.name]
        response = self._client.get(vendor.list_url(board.identifier), headers=request_headers())
        response.raise_for_status()
        return vendor.postings_in(response.json())

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one Greenhouse payload to a Posting.

        Args:
            raw: One entry from the board's ``jobs`` array.
            board: The board it came from, which supplies the company name when
                the payload omits it.
            now: First-seen timestamp; injected so normalisation stays pure.

        Returns:
            The normalised posting.
        """
        now = now or datetime.now(UTC)
        location = str((raw.get("location") or {}).get("name") or "").strip()
        locations = (location,) if location else ()

        remote = _remote_from_location(location)
        description = html_to_text(raw.get("content"))
        comp = parse_comp(description)
        employment_type, employment_source = _employment_type(raw.get("metadata") or [])
        departments = raw.get("departments") or []

        title = str(raw["title"])
        company = str(raw.get("company_name") or board.company)
        location_class = classify(locations, remote)

        return Posting(
            source=self.name,
            source_id=str(raw["id"]),
            company=company,
            title=title,
            source_url=str(raw["absolute_url"]),
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(company, title, location_class),
            posted_at=parse_iso(raw.get("first_published")),
            posted_at_precision=(
                PostedAtPrecision.EXACT if raw.get("first_published") else PostedAtPrecision.UNKNOWN
            ),
            updated_at=parse_iso(raw.get("updated_at")),
            remote=remote,
            remote_source=(RemoteSource.LOCATION_STRING if location else RemoteSource.ABSENT),
            locations=locations,
            employment_type=employment_type,
            employment_type_source=employment_source,
            comp_min=comp.minimum if comp else None,
            comp_max=comp.maximum if comp else None,
            comp_currency=comp.currency if comp else None,
            comp_interval=comp.interval if comp else CompInterval.UNKNOWN,
            comp_source=CompSource.PARSED if comp else CompSource.ABSENT,
            description_text=description,
            description_complete=description is not None,
            department=str(departments[0]["name"]) if departments else None,
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """Return ``None``: ``content=true`` already delivered the description."""
        return None


def _remote_from_location(location: str) -> RemoteStatus:
    lowered = location.lower()
    if "remote" in lowered:
        return RemoteStatus.REMOTE
    if "hybrid" in lowered:
        return RemoteStatus.HYBRID
    # A city name states a place, not an arrangement. Calling it onsite would
    # fire the onsite gate on an inference; the prose veto covers it later.
    return RemoteStatus.UNKNOWN


def _employment_type(metadata: list[dict]) -> tuple[EmploymentType, EmploymentTypeSource]:
    for field in metadata:
        name = str(field.get("name") or "").strip().lower()
        if not any(candidate in name for candidate in _EMPLOYMENT_FIELD_NAMES):
            continue
        value = str(field.get("value") or "").strip().lower()
        for marker, employment_type in _EMPLOYMENT_VALUES:
            if marker in value:
                return employment_type, EmploymentTypeSource.CUSTOM_FIELD
    return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
