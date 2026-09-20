"""Companies with no ATS at all, scraped from their own pages.

The resolver design rejected careers-page scraping, and was right about the
case it measured: client-rendered boards needing a browser. These two are
different — the postings are in the HTML the server sends — so the objection is
maintenance rather than feasibility. Each site is a named parser against a
captured page, and each one breaks loudly when the page changes rather than
returning zero and looking like a company that stopped hiring.

Signal is the sharper case: it has no open roles today, so there is no posting
structure to parse. Rather than guess at one, its parser watches for the
sentence saying so and fails the moment that sentence goes away.
"""

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from winnow.models import Board, RemoteStatus
from winnow.sources.careers_page import CareersPageAdapter, UnknownPageStructure

FIXTURES = Path("tests/fixtures")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

NEXTCLOUD = Board(vendor="careers_page", identifier={"site": "nextcloud"}, company="Nextcloud")
SIGNAL = Board(vendor="careers_page", identifier={"site": "signal"}, company="Signal")


def _client(body: str) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    )


@pytest.fixture
def nextcloud_html():
    return (FIXTURES / "careers_nextcloud.html").read_text(errors="replace")


@pytest.fixture
def signal_html():
    return (FIXTURES / "careers_signal.html").read_text(errors="replace")


def test_nextcloud_postings_are_found(nextcloud_html):
    rows = list(CareersPageAdapter(client=_client(nextcloud_html)).fetch_list(NEXTCLOUD))
    titles = [row["title"] for row in rows]

    assert len(rows) >= 10
    assert "Financial Analyst" in titles
    assert "Sales Operations Manager" in titles


def test_the_section_headings_are_not_mistaken_for_jobs(nextcloud_html):
    """Every posting repeats Responsibilities / Requirements / Benefits."""
    rows = list(CareersPageAdapter(client=_client(nextcloud_html)).fetch_list(NEXTCLOUD))
    titles = {row["title"] for row in rows}

    assert not titles & {"Responsibilities", "Requirements", "Benefits", "Open positions"}


def test_each_posting_carries_its_own_description(nextcloud_html):
    rows = list(CareersPageAdapter(client=_client(nextcloud_html)).fetch_list(NEXTCLOUD))
    analyst = next(row for row in rows if row["title"] == "Financial Analyst")

    assert "FP&A" in analyst["description"] or "financial modeling" in analyst["description"]
    assert "Responsibilities" in analyst["description"]
    assert "Sales Operations Manager" not in analyst["description"], "bled into the next job"


def test_a_scraped_posting_normalises(nextcloud_html):
    adapter = CareersPageAdapter(client=_client(nextcloud_html))
    rows = list(adapter.fetch_list(NEXTCLOUD))
    posting = adapter.normalize(rows[0], NEXTCLOUD, now=NOW)

    assert posting.source == "careers_page"
    assert posting.company == "Nextcloud"
    assert posting.source_url.startswith("https://nextcloud.com/jobs/")
    assert posting.description_complete is True
    assert posting.source_id


def test_the_identifier_is_stable_across_polls(nextcloud_html):
    """No per-job URL exists, so identity has to come from the title."""
    adapter = CareersPageAdapter(client=_client(nextcloud_html))
    first = [
        adapter.normalize(r, NEXTCLOUD, now=NOW).source_id for r in adapter.fetch_list(NEXTCLOUD)
    ]
    second = [
        adapter.normalize(r, NEXTCLOUD, now=NOW).source_id for r in adapter.fetch_list(NEXTCLOUD)
    ]
    assert first == second
    assert len(set(first)) == len(first), "ids must be unique within a board"


def test_nextcloud_states_no_arrangement(nextcloud_html):
    adapter = CareersPageAdapter(client=_client(nextcloud_html))
    rows = list(adapter.fetch_list(NEXTCLOUD))
    posting = adapter.normalize(rows[0], NEXTCLOUD, now=NOW)
    assert posting.remote is RemoteStatus.UNKNOWN


def test_signal_reports_zero_while_it_says_it_has_none(signal_html):
    rows = list(CareersPageAdapter(client=_client(signal_html)).fetch_list(SIGNAL))
    assert rows == []


def test_signal_fails_loudly_once_that_sentence_goes_away():
    """The day they post something, the parser must stop rather than say zero.

    A scraper written against a structure nobody has seen would return nothing
    forever, which reads as "no openings" — the failure this project keeps
    finding and refusing.
    """
    changed = "<html><body><h2>Open roles</h2><h3>Senior Engineer</h3></body></html>"
    with pytest.raises(UnknownPageStructure):
        list(CareersPageAdapter(client=_client(changed)).fetch_list(SIGNAL))


def test_nextcloud_fails_loudly_if_the_section_disappears():
    restyled = "<html><body><h1>Careers</h1><p>Email us.</p></body></html>"
    with pytest.raises(UnknownPageStructure):
        list(CareersPageAdapter(client=_client(restyled)).fetch_list(NEXTCLOUD))


def test_an_unknown_site_is_refused():
    board = Board(vendor="careers_page", identifier={"site": "nobody"}, company="Nobody")
    with pytest.raises(KeyError):
        list(CareersPageAdapter(client=_client("<html></html>")).fetch_list(board))


def test_pay_stated_on_a_scraped_page_is_read(nextcloud_html):
    """A hand-parsed page has no fields at all, so prose is the only source."""
    from winnow.models import CompSource

    adapter = CareersPageAdapter(client=_client(nextcloud_html))
    row = dict(next(iter(adapter.fetch_list(NEXTCLOUD))))
    row["description"] = "We offer a salary of $150,000 - $175,000 USD for this role."

    posting = adapter.normalize(row, NEXTCLOUD, now=NOW)

    assert (posting.comp_min, posting.comp_max) == (150000, 175000)
    assert posting.comp_source is CompSource.PARSED


def test_a_scraped_page_that_states_no_pay_stays_absent(nextcloud_html):
    from winnow.models import CompSource

    adapter = CareersPageAdapter(client=_client(nextcloud_html))
    posting = adapter.normalize(next(iter(adapter.fetch_list(NEXTCLOUD))), NEXTCLOUD, now=NOW)

    assert posting.comp_source is CompSource.ABSENT
