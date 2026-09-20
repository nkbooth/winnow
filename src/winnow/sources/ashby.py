"""Ashby.

States the most of the four vendors and withholds on purpose.
``shouldDisplayCompensationOnJobPostings: false`` means the employer configured
compensation and chose not to publish it, which is a different fact from having
none — so it normalises to ``WITHHELD``, not ``ABSENT``, and the digest can say
"comp withheld" rather than "comp unknown".

``isListed: false`` postings also appear in the payload and are filtered before
normalisation.
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

_WORKPLACE_TYPES = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "onsite": RemoteStatus.ONSITE,
    "on-site": RemoteStatus.ONSITE,
}

_EMPLOYMENT_TYPES = {
    "fulltime": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "temporary": EmploymentType.CONTRACT,
    "intern": EmploymentType.INTERN,
    "internship": EmploymentType.INTERN,
}


class AshbyAdapter:
    """Adapter for Ashby job boards."""

    name = "ashby"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Request a board's listed postings.

        Args:
            board: The board to poll.

        Returns:
            Only the postings with ``isListed`` true. Unlisted ones are real
            payload entries but not real openings.

        Raises:
            httpx.HTTPStatusError: If the board does not answer with success.
        """
        vendor = VENDORS[self.name]
        response = self._client.get(vendor.list_url(board.identifier), headers=request_headers())
        response.raise_for_status()
        return [job for job in vendor.postings_in(response.json()) if job.get("isListed")]

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one Ashby payload to a Posting.

        Args:
            raw: One entry from the board's ``jobs`` array.
            board: The board it came from, which supplies the company name.
            now: First-seen timestamp; injected so normalisation stays pure.

        Returns:
            The normalised posting.
        """
        now = now or datetime.now(UTC)
        locations = tuple(
            location
            for location in [raw.get("location"), *(raw.get("secondaryLocations") or [])]
            if isinstance(location, str) and location
        )

        remote, remote_source = _arrangement(raw)
        employment_type, employment_source = _employment_type(raw.get("employmentType"))
        description = raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml"))
        comp_min, comp_max, currency, interval, comp_source = _compensation(raw)

        title = str(raw["title"])
        location_class = classify(locations, remote)

        return Posting(
            source=self.name,
            source_id=str(raw["id"]),
            company=board.company,
            title=title,
            source_url=str(raw["jobUrl"]),
            apply_url=raw.get("applyUrl"),
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(board.company, title, location_class),
            posted_at=parse_iso(raw.get("publishedAt")),
            posted_at_precision=(
                PostedAtPrecision.EXACT if raw.get("publishedAt") else PostedAtPrecision.UNKNOWN
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
            department=raw.get("department"),
            team=raw.get("team"),
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """Return ``None``: the list payload is already complete."""
        return None


def _arrangement(raw: dict) -> tuple[RemoteStatus, RemoteSource]:
    workplace_type = raw.get("workplaceType")
    if isinstance(workplace_type, str):
        status = _WORKPLACE_TYPES.get(workplace_type.strip().lower())
        if status is not None:
            return status, RemoteSource.STRUCTURED
    is_remote = raw.get("isRemote")
    if isinstance(is_remote, bool):
        return (
            RemoteStatus.REMOTE if is_remote else RemoteStatus.UNKNOWN,
            RemoteSource.STRUCTURED if is_remote else RemoteSource.ABSENT,
        )
    return RemoteStatus.UNKNOWN, RemoteSource.ABSENT


def _employment_type(value: object) -> tuple[EmploymentType, EmploymentTypeSource]:
    if not isinstance(value, str):
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    employment_type = _EMPLOYMENT_TYPES.get(value.strip().lower())
    if employment_type is None:
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    return employment_type, EmploymentTypeSource.STRUCTURED


def _compensation(
    raw: dict,
) -> tuple[int | None, int | None, str | None, CompInterval, CompSource]:
    compensation = raw.get("compensation") or {}
    summary = compensation.get("compensationTierSummary") or compensation.get(
        "scrapeableCompensationSalarySummary"
    )
    parsed = parse_comp(summary if isinstance(summary, str) else None)
    if parsed is not None:
        return (
            parsed.minimum,
            parsed.maximum,
            parsed.currency,
            parsed.interval,
            CompSource.STATED,
        )
    if raw.get("shouldDisplayCompensationOnJobPostings") is False:
        return None, None, None, CompInterval.UNKNOWN, CompSource.WITHHELD
    return None, None, None, CompInterval.UNKNOWN, CompSource.ABSENT
