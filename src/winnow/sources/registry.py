"""The vendor registry.

Adding a vendor should be a row here plus a normaliser — not a refactor. That
claim is what the adapter contract is for, and Teamtailor turning up unplanned
during resolver testing is why it matters.

Everything vendor-specific that is *data* lives here: how a pasted board URL is
recognised, how its identifier is shaped, which endpoint lists postings, and
where the postings sit in the response. Only the payload-to-``Posting`` mapping
lives in the adapter modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Vendor:
    """One ATS vendor's addressing scheme.

    Attributes:
        name: Registry key, also the ``source`` recorded on every posting.
        identifier_keys: Named groups from ``url_patterns`` that make up the
            board identifier. Greenhouse needs one; Workday needs three.
        url_patterns: Regexes matched against a pasted board URL.
        list_url_template: Endpoint listing postings, formatted with the
            identifier. ``None`` for vendors with no public JSON.
        method: HTTP method the listing endpoint expects.
        list_key: Key holding the posting array, or ``None`` when the response
            body *is* the array.
        total_key: Key holding a total count, where the vendor reports one.
        detail_url_template: Endpoint for one posting's detail payload, formatted
            with the identifier plus ``path``. Only vendors with incomplete list
            payloads have one.
        name_url_template: Endpoint that returns the board's own name. Only
            Greenhouse self-identifies, which is why it is the one vendor whose
            probe may be auto-accepted.
        name_key: Key holding that name.
    """

    name: str
    identifier_keys: tuple[str, ...]
    url_patterns: tuple[str, ...]
    list_url_template: str | None
    method: str = "GET"
    list_key: str | None = None
    total_key: str | None = None
    detail_url_template: str | None = None
    name_url_template: str | None = None
    name_key: str | None = None

    def list_url(self, identifier: dict[str, str]) -> str:
        """Build the listing endpoint for one board.

        Args:
            identifier: This board's vendor-specific identifier.

        Returns:
            An absolute URL.

        Raises:
            ValueError: If the vendor publishes no machine-readable listing.
        """
        if self.list_url_template is None:
            raise ValueError(f"{self.name} has no public listing endpoint")
        return self.list_url_template.format(**identifier)

    def detail_url(self, identifier: dict[str, str], path: str) -> str:
        """Build the detail endpoint for one posting.

        Args:
            identifier: This board's vendor-specific identifier.
            path: The posting's vendor-native path.

        Returns:
            An absolute URL.

        Raises:
            ValueError: If the vendor's list payload needs no detail fetch.
        """
        if self.detail_url_template is None:
            raise ValueError(f"{self.name} list payloads are already complete")
        return self.detail_url_template.format(**identifier, path=path)

    def postings_in(self, payload: object) -> list[dict]:
        """Extract the posting array from a listing response.

        Args:
            payload: Decoded JSON body.

        Returns:
            The postings, or an empty list when the body is not shaped as
            expected — an unexpected shape is an empty board to the caller, and
            the caller distinguishes that from a 404.
        """
        if self.list_key is None:
            return payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            found = payload.get(self.list_key)
            if isinstance(found, list):
                return found
        return []


VENDORS: dict[str, Vendor] = {
    vendor.name: vendor
    for vendor in (
        Vendor(
            name="greenhouse",
            identifier_keys=("slug",),
            url_patterns=(
                r"^https?://(?:boards|job-boards)\.greenhouse\.io/(?P<slug>[A-Za-z0-9._-]+)",
            ),
            list_url_template="https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
            list_key="jobs",
            name_url_template="https://boards-api.greenhouse.io/v1/boards/{slug}",
            name_key="name",
        ),
        Vendor(
            name="lever",
            identifier_keys=("slug",),
            url_patterns=(r"^https?://jobs\.lever\.co/(?P<slug>[A-Za-z0-9._-]+)",),
            list_url_template="https://api.lever.co/v0/postings/{slug}?mode=json",
            list_key=None,
        ),
        Vendor(
            name="ashby",
            identifier_keys=("slug",),
            url_patterns=(r"^https?://jobs\.ashbyhq\.com/(?P<slug>[A-Za-z0-9._-]+)",),
            # includeCompensation is not a refinement. Without it the response
            # carries neither the compensation block nor the flag saying
            # whether comp was withheld - no keys at all - so every posting
            # reads as comp-absent and the floor gate can never fire. Found
            # live on a ClickHouse posting whose salary is plainly published.
            list_url_template=(
                "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
            ),
            list_key="jobs",
        ),
        Vendor(
            name="workday",
            identifier_keys=("tenant", "datacenter", "site"),
            url_patterns=(
                r"^https?://(?P<tenant>[A-Za-z0-9-]+)\.(?P<datacenter>wd\d+)"
                r"\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9._-]+)",
            ),
            list_url_template=(
                "https://{tenant}.{datacenter}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
            ),
            method="POST",
            list_key="jobPostings",
            total_key="total",
            detail_url_template=(
                "https://{tenant}.{datacenter}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
            ),
        ),
        Vendor(
            name="rippling",
            identifier_keys=("slug",),
            url_patterns=(
                r"^https?://ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<slug>[A-Za-z0-9._-]+)",
            ),
            list_url_template="https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs",
            list_key=None,
            detail_url_template=(
                "https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{path}"
            ),
        ),
        Vendor(
            name="hibob",
            identifier_keys=("slug",),
            url_patterns=(r"^https?://(?P<slug>[A-Za-z0-9-]+)\.careers\.hibob\.com",),
            # POST /hiring/job-ads/search exists and returns exactly what is
            # wanted — the active ads on a company's Bob careers page — but its
            # OpenAPI definition requires Basic or Bearer auth issued by that
            # employer's own workspace. It is for a company to power its own
            # careers site, not for a third party to read it. Checked 2026-09-19.
            list_url_template=None,
        ),
        Vendor(
            name="teamtailor",
            identifier_keys=("slug",),
            url_patterns=(r"^https?://(?P<slug>[A-Za-z0-9-]+)\.teamtailor\.com",),
            # Teamtailor's JSON API needs a per-employer token, so a board can be
            # recorded but not polled. Turned up unplanned on Mullvad's careers
            # page, which is exactly why the registry is not an enum.
            list_url_template=None,
        ),
        Vendor(
            name="workable",
            identifier_keys=("slug",),
            url_patterns=(r"^https?://apply\.workable\.com/(?P<slug>[A-Za-z0-9._-]+)",),
            # The endpoint an employer embeds in their own careers page. With
            # details=true it returns whole postings, so there is no detail
            # fetch. Corrected 2026-09-19: an earlier note here called Workable
            # unpollable on the evidence of seven boards that answered with an
            # empty array. They were empty. Nuvei's board answers the same URL
            # with 75 postings — "returns nothing" was read as "cannot be read".
            list_url_template=(
                "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
            ),
            list_key="jobs",
            # Like Greenhouse, the board states its own employer name, so a
            # resolver match here can be auto-accepted.
            name_url_template=(
                "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
            ),
            name_key="name",
        ),
        Vendor(
            name="careers_page",
            identifier_keys=("site",),
            url_patterns=(
                r"^https?://(?:www\.)?(?P<site>nextcloud)\.com/jobs",
                r"^https?://(?:www\.)?(?P<site>signal)\.org/workworkwork",
            ),
            # Not an ATS: employers with no board at all, each one a hand-written
            # parser in winnow.sources.careers_page against a captured page.
            # There is no listing endpoint to template, and no JSON to probe, so
            # the resolver verifies these by running the parser — see HttpProbe.
            list_url_template=None,
        ),
        Vendor(
            name="smartrecruiters",
            identifier_keys=("slug",),
            url_patterns=(
                r"^https?://(?:jobs|careers)\.smartrecruiters\.com/(?P<slug>[A-Za-z0-9._-]+)",
            ),
            list_url_template="https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            list_key="content",
            total_key="totalFound",
            detail_url_template=(
                "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{path}"
            ),
        ),
    )
}

_COMPILED: tuple[tuple[Vendor, re.Pattern[str]], ...] = tuple(
    (vendor, re.compile(pattern)) for vendor in VENDORS.values() for pattern in vendor.url_patterns
)


def match_board_url(url: str) -> tuple[Vendor, dict[str, str]] | None:
    """Recognise a board URL.

    Args:
        url: A URL pasted by a human.

    Returns:
        The vendor and the board identifier, or ``None`` when no pattern matches.
    """
    for vendor, pattern in _COMPILED:
        match = pattern.match(url.strip())
        if match is None:
            continue
        groups = match.groupdict()
        return vendor, {key: groups[key] for key in vendor.identifier_keys}
    return None
