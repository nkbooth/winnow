"""Workday.

The awkward vendor, built anyway because of where it sits: the large-corporate
tier is where the $275k floor is payable, and that tier runs on Workday. It is
also the only vendor that states work arrangement as a field — ``remoteType`` —
which is exactly the signal the RTO gate otherwise has to infer from prose.

Three differences from every other adapter:

* the listing endpoint is a POST with a JSON body, paged by limit and offset;
* the list payload carries no description and no compensation, so a posting is
  not scorable until :meth:`WorkdayAdapter.fetch_detail` has run — which is why
  detail is fetched lazily, for survivors only, rather than for all 148 postings;
* ``postedOn`` is prose rather than a date.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

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
from winnow.normalize import fingerprint, html_to_text, parse_comp
from winnow.sources.base import default_client, request_headers
from winnow.sources.registry import VENDORS

PAGE_SIZE = 20

_REMOTE_TYPES = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "onsite": RemoteStatus.ONSITE,
    "on-site": RemoteStatus.ONSITE,
}

_TIME_TYPES = {
    "full time": EmploymentType.FULL_TIME,
    "part time": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
}

_LOCATION_COUNT = re.compile(r"^\d+\s+locations?$", re.IGNORECASE)
_DAYS_AGO = re.compile(r"(\d+)\+?\s+days?\s+ago", re.IGNORECASE)
_PATH_LOCATION = re.compile(r"^/job/([^/]+)/")


class WorkdayAdapter:
    """Adapter for Workday tenant career sites."""

    name = "workday"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Page through a tenant's postings.

        Args:
            board: The board to poll, identified by (tenant, datacenter, site).

        Returns:
            Every posting the tenant lists. Paging stops on an empty page as
            well as on the reported total, because a tenant that reports more
            than it will serve must not turn into an endless loop.

        Raises:
            httpx.HTTPStatusError: If the tenant does not answer with success.
        """
        vendor = VENDORS[self.name]
        url = vendor.list_url(board.identifier)
        collected: list[dict] = []
        offset = 0
        while True:
            response = self._client.post(
                url,
                headers=request_headers(),
                json={
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": "",
                },
            )
            response.raise_for_status()
            payload = response.json()
            page = vendor.postings_in(payload)
            if not page:
                break
            collected.extend(page)
            total = payload.get("total") if isinstance(payload, dict) else None
            offset += PAGE_SIZE
            if not isinstance(total, int) or offset >= total:
                break
        return collected

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one Workday payload to a Posting.

        Args:
            raw: A list-payload row, optionally merged with the detail payload
                that :meth:`fetch_detail` returns. Normalisation is total over
                whichever of the two is present, so enrichment is a dict merge
                rather than a second code path.
            board: The board it came from.
            now: Reference time. Relative posting dates are resolved against it,
                so it is injected rather than read from the clock.

        Returns:
            The normalised posting. Without detail, ``description_complete`` is
            false and the scorer must not run on it.
        """
        now = now or datetime.now(UTC)
        detail = raw.get("jobPostingInfo") or {}
        external_path = str(raw["externalPath"])

        remote, remote_source = _arrangement(detail.get("remoteType") or raw.get("remoteType"))
        locations = _locations(raw, detail)
        description = html_to_text(detail.get("jobDescription"))
        comp = parse_comp(description)
        employment_type, employment_source = _employment_type(detail.get("timeType"))
        posted_at, posted_text = _posted_at(raw.get("postedOn"), now)

        title = str(raw.get("title") or detail.get("title") or "")
        location_class = classify(locations, remote)

        return Posting(
            source=self.name,
            source_id=external_path,
            company=board.company,
            title=title,
            source_url=_public_url(board, external_path),
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(board.company, title, location_class),
            posted_at=posted_at,
            posted_at_precision=(
                PostedAtPrecision.RELATIVE if posted_at else PostedAtPrecision.UNKNOWN
            ),
            posted_at_text=posted_text,
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
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """Fetch one posting's detail payload.

        Called only for postings that survived the cheap gates. Red Hat lists
        148 postings; fetching all of their details every poll would be 148
        requests for a handful of scorable roles.

        Args:
            posting: A posting normalised from this vendor's list payload.

        Returns:
            The detail payload, or ``None`` if the tenant does not answer with
            success — a missing description is better than a failed run.
        """
        identifier = _identifier_from_url(posting.source_url)
        if identifier is None:
            return None

        url = VENDORS[self.name].detail_url(identifier, posting.source_id)
        response = self._client.get(url, headers=request_headers())
        if response.status_code >= 400:
            return None
        return response.json()


def _public_url(board: Board, external_path: str) -> str:
    identifier = board.identifier
    return (
        f"https://{identifier['tenant']}.{identifier['datacenter']}.myworkdayjobs.com"
        f"/en-US/{identifier['site']}{external_path}"
    )


def _identifier_from_url(url: str) -> dict[str, str] | None:
    match = re.match(
        r"^https://(?P<tenant>[^.]+)\.(?P<datacenter>wd\d+)\.myworkdayjobs\.com"
        r"/en-US/(?P<site>[^/]+)/",
        url,
    )
    return match.groupdict() if match else None


def _arrangement(remote_type: object) -> tuple[RemoteStatus, RemoteSource]:
    if not isinstance(remote_type, str):
        return RemoteStatus.UNKNOWN, RemoteSource.ABSENT
    status = _REMOTE_TYPES.get(remote_type.strip().lower())
    if status is None:
        return RemoteStatus.UNKNOWN, RemoteSource.ABSENT
    return status, RemoteSource.STRUCTURED


def _employment_type(time_type: object) -> tuple[EmploymentType, EmploymentTypeSource]:
    if not isinstance(time_type, str):
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    employment_type = _TIME_TYPES.get(time_type.strip().lower())
    if employment_type is None:
        return EmploymentType.UNKNOWN, EmploymentTypeSource.ABSENT
    return employment_type, EmploymentTypeSource.STRUCTURED


def _locations(raw: dict, detail: dict) -> tuple[str, ...]:
    """Work out which places a posting actually names.

    ``locationsText`` is sometimes a count — "2 Locations" — which names nowhere.
    In that case the location slug in ``externalPath`` is the one place that is
    known, and it is used rather than inventing the others.
    """
    additional = detail.get("additionalLocations")
    if isinstance(additional, list):
        named = tuple(str(item) for item in additional if item)
        if named:
            return named

    text = str(raw.get("locationsText") or "").strip()
    if text and not _LOCATION_COUNT.match(text):
        return (text,)

    match = _PATH_LOCATION.match(str(raw.get("externalPath") or ""))
    if match:
        return (match.group(1).replace("---", " - ").replace("-", " ").strip(),)
    return ()


def _posted_at(posted_on: object, now: datetime) -> tuple[datetime | None, str | None]:
    """Resolve Workday's prose posting date.

    "Posted 30+ Days Ago" is not a date. It is resolved to 30 days for ordering
    and the wording is kept, because the wording is the ghost-job signal.
    """
    if not isinstance(posted_on, str) or not posted_on.strip():
        return None, None
    text = posted_on.strip()
    lowered = text.lower()

    if "today" in lowered:
        return now, text
    if "yesterday" in lowered:
        return now - timedelta(days=1), text
    match = _DAYS_AGO.search(lowered)
    if match:
        return now - timedelta(days=int(match.group(1))), text
    return None, text
