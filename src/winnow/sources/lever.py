"""Lever.

The richest per-posting payload of the four: structured ``salaryRange`` and
structured ``workplaceType``, both of which the other vendors make you infer.
What it does not carry is the employer's identity — nothing in the payload names
the company, so the board supplies it. That is also why a Lever board cannot be
auto-verified during resolution.
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
from winnow.normalize import fingerprint, html_to_text, parse_comp, parse_epoch_millis
from winnow.sources.base import default_client, request_headers
from winnow.sources.registry import VENDORS

_WORKPLACE_TYPES = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "onsite": RemoteStatus.ONSITE,
    "on-site": RemoteStatus.ONSITE,
}

_COMMITMENTS = (
    ("full-time", EmploymentType.FULL_TIME),
    ("full time", EmploymentType.FULL_TIME),
    ("part-time", EmploymentType.PART_TIME),
    ("part time", EmploymentType.PART_TIME),
    ("contract", EmploymentType.CONTRACT),
    ("temporary", EmploymentType.CONTRACT),
    ("intern", EmploymentType.INTERN),
)

_INTERVALS = {
    "per-year-salary": CompInterval.YEAR,
    "per-year": CompInterval.YEAR,
    "per-hour-wage": CompInterval.HOUR,
    "per-hour": CompInterval.HOUR,
}


class LeverAdapter:
    """Adapter for Lever job boards."""

    name = "lever"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Request every posting on a board.

        Args:
            board: The board to poll.

        Returns:
            The raw posting payloads; Lever answers with a bare array.

        Raises:
            httpx.HTTPStatusError: If the board does not answer with success.
        """
        vendor = VENDORS[self.name]
        response = self._client.get(vendor.list_url(board.identifier), headers=request_headers())
        response.raise_for_status()
        return vendor.postings_in(response.json())

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one Lever payload to a Posting.

        Args:
            raw: One entry from the board's array.
            board: The board it came from, which supplies the company name.
            now: First-seen timestamp; injected so normalisation stays pure.

        Returns:
            The normalised posting.
        """
        now = now or datetime.now(UTC)
        categories = raw.get("categories") or {}
        locations = tuple(
            location
            for location in (categories.get("allLocations") or [categories.get("location")])
            if location
        )

        remote, remote_source = _arrangement(raw.get("workplaceType"))
        description = _description(raw)
        employment_type, employment_source = _commitment(categories.get("commitment"))
        comp_min, comp_max, currency, interval, comp_source = _compensation(raw, description)

        title = str(raw["text"])
        location_class = classify(locations, remote)

        return Posting(
            source=self.name,
            source_id=str(raw["id"]),
            company=board.company,
            title=title,
            source_url=str(raw["hostedUrl"]),
            apply_url=raw.get("applyUrl"),
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(board.company, title, location_class),
            posted_at=parse_epoch_millis(raw.get("createdAt")),
            posted_at_precision=(
                PostedAtPrecision.EXACT if raw.get("createdAt") else PostedAtPrecision.UNKNOWN
            ),
            remote=remote,
            remote_source=remote_source,
            locations=locations,
            employment_type=employment_type,
            employment_type_source=employment_source,
            comp_min=comp_min,
            comp_max=comp_max,
            comp_currency=currency,
            comp_interval=interval,
            comp_source=comp_source,
            description_text=description,
            description_complete=description is not None,
            department=categories.get("department"),
            team=categories.get("team"),
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """Return ``None``: the list payload is already complete."""
        return None


def _arrangement(workplace_type: object) -> tuple[RemoteStatus, RemoteSource]:
    if not isinstance(workplace_type, str):
        return RemoteStatus.UNKNOWN, RemoteSource.ABSENT
    status = _WORKPLACE_TYPES.get(workplace_type.strip().lower())
    if status is None:
        return RemoteStatus.UNKNOWN, RemoteSource.ABSENT
    return status, RemoteSource.STRUCTURED


def _commitment(commitment: object) -> tuple[EmploymentType, EmploymentTypeSource]:
    if not isinstance(commitment, str):
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    lowered = commitment.lower()
    for marker, employment_type in _COMMITMENTS:
        if marker in lowered:
            return employment_type, EmploymentTypeSource.TEXT
    return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT


def _description(raw: dict) -> str | None:
    """Join the body with the sections Lever keeps beside it.

    ``additionalPlain`` is where the location and benefits prose lives, and that
    prose is exactly where an RTO requirement hides, so it belongs in the text
    the scorer reads.
    """
    parts = [
        raw.get("descriptionPlain") or html_to_text(raw.get("description")),
        raw.get("additionalPlain") or html_to_text(raw.get("additional")),
    ]
    joined = "\n\n".join(part.strip() for part in parts if part and part.strip())
    return html_to_text(joined) if joined else None


def _compensation(
    raw: dict, description: str | None
) -> tuple[int | None, int | None, str | None, CompInterval, CompSource]:
    salary_range = raw.get("salaryRange") or {}
    minimum, maximum = salary_range.get("min"), salary_range.get("max")
    if isinstance(minimum, int | float) and isinstance(maximum, int | float):
        interval = _INTERVALS.get(str(salary_range.get("interval", "")), CompInterval.UNKNOWN)
        return (
            int(minimum),
            int(maximum),
            salary_range.get("currency"),
            interval,
            CompSource.STATED,
        )

    prose = raw.get("salaryDescriptionPlain") or description
    parsed = parse_comp(prose)
    if parsed is None:
        return None, None, None, CompInterval.UNKNOWN, CompSource.ABSENT
    return (
        parsed.minimum,
        parsed.maximum,
        parsed.currency,
        parsed.interval,
        CompSource.PARSED,
    )
