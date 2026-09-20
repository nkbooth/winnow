"""Rippling.

The thinnest list payload of any vendor here: a name, a department and one
location string. Everything a gate or a scorer needs arrives only with the
detail fetch, which makes the pipeline's existing laziness load-bearing rather
than merely economical.

The detail payload repays it — the employer names itself, the posting is dated,
employment type is structured, and a pay range is present whenever the employer
configured one.
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

_EMPLOYMENT_TYPES = (
    ("full", EmploymentType.FULL_TIME),
    ("ft", EmploymentType.FULL_TIME),
    ("part", EmploymentType.PART_TIME),
    ("pt", EmploymentType.PART_TIME),
    ("contract", EmploymentType.CONTRACT),
    ("temp", EmploymentType.CONTRACT),
    ("intern", EmploymentType.INTERN),
)

_HOURLY = ("hour", "hourly")


class RipplingAdapter:
    """Adapter for Rippling ATS boards."""

    name = "rippling"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Request every posting on a board.

        Args:
            board: The board to poll.

        Returns:
            The raw posting payloads; Rippling answers with a bare array.

        Raises:
            httpx.HTTPStatusError: If the board does not answer with success.
        """
        vendor = VENDORS[self.name]
        response = self._client.get(vendor.list_url(board.identifier), headers=request_headers())
        response.raise_for_status()
        return vendor.postings_in(response.json())

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one Rippling payload to a Posting.

        Args:
            raw: A list entry, optionally merged with its detail payload.
            board: The board it came from.
            now: First-seen timestamp; injected so normalisation stays pure.

        Returns:
            The normalised posting. Without detail it carries no description,
            no date and no employment type, and the scorer will refuse it.
        """
        now = now or datetime.now(UTC)
        # The real payload has "Framework " with a trailing space.
        company = str(raw.get("companyName") or board.company).strip()
        locations = _locations(raw)
        remote, remote_source = _arrangement(locations)
        description = _description(raw)
        comp_min, comp_max, currency, interval, comp_source = _compensation(raw, description)
        employment_type, employment_source = _employment_type(raw.get("employmentType"))

        title = str(raw["name"])
        location_class = classify(locations, remote)

        return Posting(
            source=self.name,
            source_id=str(raw["uuid"]),
            company=company,
            title=title,
            source_url=str(raw["url"]),
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(company, title, location_class),
            posted_at=parse_iso(raw.get("createdOn")),
            posted_at_precision=(
                PostedAtPrecision.EXACT if raw.get("createdOn") else PostedAtPrecision.UNKNOWN
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
            department=(raw.get("department") or {}).get("label"),
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """Fetch one posting's detail payload.

        Args:
            posting: A posting normalised from this vendor's list payload.

        Returns:
            The detail payload, or ``None`` if the board does not answer. Nearly
            everything worth gating on lives here, so a posting without it stays
            unscorable rather than being scored on a title alone.
        """
        slug = (posting.raw.get("board") or {}).get("slug") or _slug_from_url(posting.source_url)
        if not slug:
            return None

        url = VENDORS[self.name].detail_url({"slug": slug}, posting.source_id)
        response = self._client.get(url, headers=request_headers())
        if response.status_code >= 400:
            return None
        return response.json()


def _slug_from_url(url: str) -> str | None:
    parts = url.split("ats.rippling.com/", 1)
    return parts[1].split("/", 1)[0] if len(parts) == 2 else None


def _locations(raw: dict) -> tuple[str, ...]:
    detailed = raw.get("workLocations")
    if isinstance(detailed, list) and detailed:
        return tuple(str(item) for item in detailed if item)
    label = (raw.get("workLocation") or {}).get("label")
    return (str(label),) if label else ()


def _arrangement(locations: tuple[str, ...]) -> tuple[RemoteStatus, RemoteSource]:
    """Infer the arrangement from the location strings, which is all there is.

    A place name states a place, not an arrangement, so anything without the
    word stays UNKNOWN and the prose veto handles it later.
    """
    joined = " ".join(locations).lower()
    if "remote" in joined:
        return RemoteStatus.REMOTE, RemoteSource.LOCATION_STRING
    if "hybrid" in joined:
        return RemoteStatus.HYBRID, RemoteSource.LOCATION_STRING
    return RemoteStatus.UNKNOWN, RemoteSource.ABSENT


def _description(raw: dict) -> str | None:
    block = raw.get("description")
    if not isinstance(block, dict):
        return None
    # Role before company boilerplate: the first is what gets scored.
    parts = [html_to_text(block.get(key)) for key in ("role", "company")]
    joined = "\n\n".join(part for part in parts if part)
    return joined or None


def _compensation(
    raw: dict, description: str | None
) -> tuple[int | None, int | None, str | None, CompInterval, CompSource]:
    ranges = raw.get("payRangeDetails")
    if isinstance(ranges, list) and ranges:
        first = ranges[0]
        minimum, maximum = first.get("minValue"), first.get("maxValue")
        if isinstance(minimum, int | float) and isinstance(maximum, int | float):
            frequency = str(first.get("frequency") or "").lower()
            interval = (
                CompInterval.HOUR
                if any(marker in frequency for marker in _HOURLY)
                else CompInterval.YEAR
            )
            return (
                int(minimum),
                int(maximum),
                first.get("currency"),
                interval,
                CompSource.STATED,
            )

    parsed = parse_comp(description)
    if parsed is None:
        return None, None, None, CompInterval.UNKNOWN, CompSource.ABSENT
    return parsed.minimum, parsed.maximum, parsed.currency, parsed.interval, CompSource.PARSED


def _employment_type(block: object) -> tuple[EmploymentType, EmploymentTypeSource]:
    if not isinstance(block, dict):
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    # label is the code ("SALARIED_FT") and id is the prose; read both.
    text = f"{block.get('label', '')} {block.get('id', '')}".lower()
    for marker, employment_type in _EMPLOYMENT_TYPES:
        if marker in text:
            return employment_type, EmploymentTypeSource.STRUCTURED
    return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
