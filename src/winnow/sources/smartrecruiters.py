"""SmartRecruiters.

States more than Greenhouse does. Every posting names its own employer, and
location carries `remote` and `hybrid` booleans instead of a string to infer
from. What it withholds is the description, which lives behind a per-posting
fetch — so the laziness Workday forced applies here too.

Boards here can be enormous: Bosch Group alone lists 4,819 postings. Paging is
therefore bounded rather than exhaustive, because a poll that walks thousands of
records to find none is a poll that will eventually be switched off.
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

PAGE_SIZE = 100

#: How far a single poll will walk one board. Bosch Group lists 4,819 postings;
#: none of them are director-level business systems roles, and finding that out
#: costs 49 requests. A bound keeps a large board from dominating a run.
MAX_POSTINGS = 400

_EMPLOYMENT_LABELS = (
    ("full", EmploymentType.FULL_TIME),
    ("part", EmploymentType.PART_TIME),
    ("contract", EmploymentType.CONTRACT),
    ("temporary", EmploymentType.CONTRACT),
    ("intern", EmploymentType.INTERN),
)

#: The order a job ad reads in. Company boilerplate last: it is the least
#: informative part and the first thing a truncated prompt should lose.
_SECTIONS = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")


class SmartRecruitersAdapter:
    """Adapter for SmartRecruiters company boards."""

    name = "smartrecruiters"

    def __init__(
        self,
        client: httpx.Client | None = None,
        page_size: int = PAGE_SIZE,
        max_postings: int = MAX_POSTINGS,
    ) -> None:
        self._client = client or default_client()
        self._page_size = page_size
        self._max_postings = max_postings

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Page through a company's postings, up to the ceiling.

        Args:
            board: The board to poll.

        Returns:
            Posting summaries, without descriptions.

        Raises:
            httpx.HTTPStatusError: If the board does not answer with success.
        """
        vendor = VENDORS[self.name]
        url = vendor.list_url(board.identifier)
        collected: list[dict] = []
        offset = 0

        while len(collected) < self._max_postings:
            response = self._client.get(
                url,
                headers=request_headers(),
                params={"limit": self._page_size, "offset": offset},
            )
            response.raise_for_status()
            payload = response.json()
            page = vendor.postings_in(payload)
            if not page:
                break
            collected.extend(page)
            offset += self._page_size
            total = payload.get("totalFound") if isinstance(payload, dict) else None
            if not isinstance(total, int) or offset >= total:
                break

        return collected[: self._max_postings]

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one SmartRecruiters payload to a Posting.

        Args:
            raw: A list entry, optionally merged with its detail payload.
            board: The board it came from.
            now: First-seen timestamp; injected so normalisation stays pure.

        Returns:
            The normalised posting.
        """
        now = now or datetime.now(UTC)
        location = raw.get("location") or {}
        company_block = raw.get("company") or {}
        company = str(company_block.get("name") or board.company)
        identifier = str(company_block.get("identifier") or board.identifier.get("slug", ""))

        remote, remote_source = _arrangement(location)
        locations = _locations(location)
        description = _description(raw)
        comp = parse_comp(description)
        employment_type, employment_source = _employment_type(raw.get("typeOfEmployment"))
        department = (raw.get("department") or {}).get("label")

        title = str(raw["name"])
        location_class = classify(locations, remote)

        return Posting(
            source=self.name,
            source_id=str(raw["id"]),
            company=company,
            title=title,
            source_url=f"https://jobs.smartrecruiters.com/{identifier}/{raw['id']}",
            apply_url=raw.get("applyUrl"),
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(company, title, location_class),
            posted_at=parse_iso(raw.get("releasedDate")),
            posted_at_precision=(
                PostedAtPrecision.EXACT if raw.get("releasedDate") else PostedAtPrecision.UNKNOWN
            ),
            remote=remote,
            remote_source=remote_source,
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
            department=department,
            team=(raw.get("function") or {}).get("label"),
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """Fetch one posting's job ad.

        Called only for postings that survived the cheap gates, since the list
        payload carries no description and a board can run to thousands.

        Args:
            posting: A posting normalised from this vendor's list payload.

        Returns:
            The detail payload, or ``None`` if the board does not answer.
        """
        identifier = (posting.raw.get("company") or {}).get("identifier")
        if not identifier:
            return None

        url = VENDORS[self.name].detail_url({"slug": identifier}, posting.source_id)
        response = self._client.get(url, headers=request_headers())
        if response.status_code >= 400:
            return None
        return response.json()


def _arrangement(location: dict) -> tuple[RemoteStatus, RemoteSource]:
    """Read the work arrangement from the two booleans, and only from them.

    Both unset is treated as unknown rather than onsite. An employer who never
    touched the checkbox looks identical to one who deliberately cleared it, and
    the onsite gate deletes a posting outright.
    """
    if location.get("remote") is True:
        return RemoteStatus.REMOTE, RemoteSource.STRUCTURED
    if location.get("hybrid") is True:
        return RemoteStatus.HYBRID, RemoteSource.STRUCTURED
    return RemoteStatus.UNKNOWN, RemoteSource.ABSENT


def _locations(location: dict) -> tuple[str, ...]:
    full = location.get("fullLocation")
    if full:
        return (str(full),)
    parts = [location.get("city"), location.get("region"), location.get("country")]
    joined = ", ".join(str(part) for part in parts if part)
    return (joined,) if joined else ()


def _employment_type(block: object) -> tuple[EmploymentType, EmploymentTypeSource]:
    if not isinstance(block, dict):
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    label = str(block.get("label") or block.get("id") or "").lower()
    for marker, employment_type in _EMPLOYMENT_LABELS:
        if marker in label:
            return employment_type, EmploymentTypeSource.STRUCTURED
    return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT


def _description(raw: dict) -> str | None:
    sections = ((raw.get("jobAd") or {}).get("sections")) or {}
    if not sections:
        return None
    parts = []
    for key in _SECTIONS:
        text = html_to_text((sections.get(key) or {}).get("text"))
        if text:
            parts.append(text)
    return "\n\n".join(parts) if parts else None
