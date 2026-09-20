"""Companies whose postings live on their own page and nowhere else.

The resolver refuses careers-page scraping, and it is right about the case it
was written for: boards that render client-side, where reading the HTML gets a
loading spinner. These two are different — their postings are in the bytes the
server sends — so what is left is maintenance risk, not feasibility, and that is
a trade a page worth applying to can be worth making.

Maintenance risk is handled by refusing to be vague about it. Every site here is
a named parser written against a captured page, and every one of them raises
when it stops recognising what it is reading. A scraper that returns zero on a
changed page is indistinguishable from a company that stopped hiring, and that
silence is the failure this project keeps finding in other people's tools.

Signal is the sharp end of that rule. It has no open roles today, so there is no
posting structure to write a parser against; guessing at one would produce code
that reports zero forever and looks like it is working. Instead its parser
watches for the sentence saying there are no roles and fails the day it goes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
from winnow.normalize import fingerprint, html_to_text
from winnow.sources.base import default_client, request_headers

#: Sent instead of the JSON Accept header the ATS adapters use: these are pages.
#: Pages, not JSON: the only adapter here that asks for markup.
_PAGE_ACCEPT = "text/html,application/xhtml+xml"


class UnknownPageStructure(RuntimeError):
    """A page no longer looks like the one its parser was written against."""


@dataclass(frozen=True)
class Site:
    """One hand-parsed careers page.

    Attributes:
        slug: Board identifier, and the key in :data:`SITES`.
        company: Employer name, which the page itself does not state.
        url: The page holding the postings.
        parse: Maps that page's HTML to raw posting rows.
    """

    slug: str
    company: str
    url: str
    parse: Callable[[str], list[dict]]


def _nextcloud(page: str) -> list[dict]:
    """Parse Nextcloud's WordPress careers page.

    Each posting is an accordion panel: an ``id`` that is a slug of the title, a
    heading holding the title, and the body until the next panel opens. The slug
    is what makes a posting identifiable at all — the page publishes no per-job
    URL, only one shared mailto.
    """
    section = _section(page, "openpositions")
    panels = list(re.finditer(r'<div id="([^"]+)" class="vc_toggle\b', section))
    if not panels:
        raise UnknownPageStructure("nextcloud: no accordion panels under #openpositions")

    rows: list[dict] = []
    for index, panel in enumerate(panels):
        end = panels[index + 1].start() if index + 1 < len(panels) else len(section)
        body = section[panel.start() : end]
        heading = re.search(r"<h4[^>]*>(.*?)</h4>", body, re.S)
        if heading is None:
            raise UnknownPageStructure(f"nextcloud: panel {panel.group(1)!r} has no title")
        rows.append(
            {
                "id": panel.group(1),
                "title": html_to_text(heading.group(1)),
                "description": html_to_text(body),
            }
        )
    return rows


def _signal(page: str) -> list[dict]:
    """Confirm Signal still says it is not hiring.

    Returns:
        Always empty, while the page says so.

    Raises:
        UnknownPageStructure: The moment it stops saying so, which is the only
            useful thing this parser can report until there is a real posting to
            write a parser against.
    """
    text = html_to_text(page) or ""
    if "do not have any open roles" in text.lower():
        return []
    raise UnknownPageStructure(
        "signal: the no-open-roles notice is gone — the page needs a real parser"
    )


def _section(page: str, element_id: str) -> str:
    """Return the ``<section>`` with this id, up to the next one."""
    start = page.find(f'<section id="{element_id}"')
    if start < 0:
        raise UnknownPageStructure(f"no <section id={element_id!r}> on the page")
    end = page.find("<section", start + len("<section"))
    return page[start:end] if end > start else page[start:]


SITES: dict[str, Site] = {
    "nextcloud": Site(
        slug="nextcloud",
        company="Nextcloud",
        url="https://nextcloud.com/jobs/",
        parse=_nextcloud,
    ),
    "signal": Site(
        slug="signal",
        company="Signal",
        url="https://signal.org/workworkwork/",
        parse=_signal,
    ),
}


class CareersPageAdapter:
    """Adapter for employers with no ATS to poll."""

    name = "careers_page"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or default_client()

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Read a careers page and pull its postings out of the markup.

        Args:
            board: The board to poll; its identifier names a site in
                :data:`SITES`.

        Returns:
            Raw posting rows.

        Raises:
            KeyError: If no parser has been written for that site.
            UnknownPageStructure: If the page no longer parses.
            httpx.HTTPStatusError: If the page does not answer with success.
        """
        site = SITES[board.identifier["site"]]
        response = self._client.get(site.url, headers=request_headers(_PAGE_ACCEPT))
        response.raise_for_status()
        return site.parse(response.text)

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one scraped row to a Posting.

        Args:
            raw: A row from :meth:`fetch_list`.
            board: The board it came from.
            now: First-seen timestamp; injected so normalisation stays pure.

        Returns:
            The normalised posting. A hand-written page states far less than an
            ATS payload does: there is no date, no location field and no
            structured employment type, so those stay unknown and the gates see
            them as unknown rather than as denials.
        """
        now = now or datetime.now(UTC)
        site = SITES[board.identifier["site"]]
        title = str(raw["title"])
        description = raw.get("description")
        remote, remote_source = _arrangement(title, description)

        return Posting(
            source=self.name,
            source_id=str(raw["id"]),
            company=site.company,
            title=title,
            # No per-job URL exists; the panel anchor is the closest honest thing.
            source_url=f"{site.url}#{raw['id']}",
            discovered_via=self.name,
            first_seen_at=now,
            fingerprint=fingerprint(site.company, title, classify((), remote)),
            posted_at=None,
            posted_at_precision=PostedAtPrecision.UNKNOWN,
            remote=remote,
            remote_source=remote_source,
            locations=(),
            employment_type=EmploymentType.UNKNOWN,
            employment_type_source=EmploymentTypeSource.ABSENT,
            comp_min=None,
            comp_max=None,
            comp_currency=None,
            comp_interval=CompInterval.UNKNOWN,
            comp_source=CompSource.ABSENT,
            description_text=description,
            description_complete=description is not None,
            department=None,
            raw=raw,
        )

    def fetch_detail(self, posting: Posting) -> dict | None:
        """No detail fetch: the page carries the whole posting already."""
        return None


def _arrangement(title: str, description: str | None) -> tuple[RemoteStatus, RemoteSource]:
    """Read the arrangement out of the only place it can appear: the prose.

    Nextcloud puts it in the title — "(remote / home office)" — often enough to
    be worth reading, but a description that merely mentions remote working is
    not a statement about this job, so nothing weaker than the title counts.
    """
    lowered = title.lower()
    if "remote" in lowered or "home office" in lowered:
        return RemoteStatus.REMOTE, RemoteSource.DESCRIPTION_TEXT
    if "hybrid" in lowered:
        return RemoteStatus.HYBRID, RemoteSource.DESCRIPTION_TEXT
    return RemoteStatus.UNKNOWN, RemoteSource.ABSENT
