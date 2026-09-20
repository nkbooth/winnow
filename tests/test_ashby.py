"""The Ashby adapter, against 1Password's board captured 2026-09-18.

Ashby states the most and withholds deliberately. Every posting on the captured
board has compensation configured and ``shouldDisplayCompensationOnJobPostings``
set to false, which is a different fact from having no compensation at all — and
the digest has to be able to say which.
"""

from datetime import UTC, datetime

import httpx
import pytest

from winnow.models import (
    Board,
    CompInterval,
    CompSource,
    EmploymentType,
    EmploymentTypeSource,
    RemoteSource,
    RemoteStatus,
)
from winnow.sources.ashby import AshbyAdapter
from winnow.sources.registry import VENDORS

BOARD = Board(vendor="ashby", identifier={"slug": "1password"}, company="1Password")
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
CLICKHOUSE = Board(vendor="ashby", identifier={"slug": "clickhouse"}, company="ClickHouse")


@pytest.fixture
def jobs(fixtures):
    return fixtures("ashby_1password_jobs.json")["jobs"]


@pytest.fixture
def gtm_analyst(jobs):
    return next(job for job in jobs if job["title"] == "Senior GTM Engineering Analyst")


def test_identity_and_urls(gtm_analyst):
    posting = AshbyAdapter().normalize(gtm_analyst, BOARD, now=NOW)
    assert posting.source == "ashby"
    assert posting.source_id == gtm_analyst["id"]
    assert posting.company == "1Password"
    assert posting.source_url == gtm_analyst["jobUrl"]
    assert posting.apply_url == gtm_analyst["applyUrl"]
    assert posting.department == "GTM"
    assert posting.team == "Revenue Operations"


def test_employment_type_is_a_structured_enum(gtm_analyst):
    posting = AshbyAdapter().normalize(gtm_analyst, BOARD, now=NOW)
    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.employment_type_source is EmploymentTypeSource.STRUCTURED


def test_remote_is_structured(gtm_analyst):
    posting = AshbyAdapter().normalize(gtm_analyst, BOARD, now=NOW)
    assert posting.remote is RemoteStatus.REMOTE
    assert posting.remote_source is RemoteSource.STRUCTURED


def test_withheld_compensation_is_not_absent_compensation(gtm_analyst):
    posting = AshbyAdapter().normalize(gtm_analyst, BOARD, now=NOW)
    assert posting.comp_source is CompSource.WITHHELD
    assert (posting.comp_min, posting.comp_max) == (None, None)


def test_published_at_has_milliseconds(gtm_analyst):
    posting = AshbyAdapter().normalize(gtm_analyst, BOARD, now=NOW)
    assert posting.posted_at is not None
    assert posting.posted_at.date().isoformat() == "2026-09-11"


def test_description_is_plain_text(gtm_analyst):
    posting = AshbyAdapter().normalize(gtm_analyst, BOARD, now=NOW)
    assert posting.description_text is not None
    assert "<p" not in posting.description_text
    assert "1Password" in posting.description_text
    assert posting.description_complete is True


def test_hybrid_and_onsite_postings_are_not_flattened_to_remote(jobs):
    adapter = AshbyAdapter()
    arrangements = {
        job["title"]: adapter.normalize(job, BOARD, now=NOW).remote
        for job in jobs
        if job.get("workplaceType") in ("Hybrid", "OnSite")
    }
    assert RemoteStatus.HYBRID in arrangements.values()
    assert RemoteStatus.ONSITE in arrangements.values()


def test_a_posting_stating_no_arrangement_is_unknown():
    raw = {
        "id": "abc",
        "title": "Some Role",
        "location": "United States",
        "jobUrl": "https://jobs.ashbyhq.com/1password/abc",
        "applyUrl": None,
        "isListed": True,
        "isRemote": None,
        "workplaceType": None,
        "employmentType": None,
        "publishedAt": "2026-09-11T17:18:27.824+00:00",
        "shouldDisplayCompensationOnJobPostings": True,
        "descriptionPlain": "A description.",
        "secondaryLocations": [],
    }
    posting = AshbyAdapter().normalize(raw, BOARD, now=NOW)
    assert posting.remote is RemoteStatus.UNKNOWN
    assert posting.remote_source is RemoteSource.ABSENT
    assert posting.employment_type is EmploymentType.UNKNOWN
    assert posting.comp_source is CompSource.ABSENT


def test_every_posting_on_the_real_board_normalises(jobs):
    adapter = AshbyAdapter()
    postings = [adapter.normalize(job, BOARD, now=NOW) for job in jobs]
    assert len(postings) == 62


def test_this_board_has_no_within_board_duplication(jobs):
    """Ashby carries secondary locations instead of splitting per country."""
    adapter = AshbyAdapter()
    titles = {adapter.normalize(job, BOARD, now=NOW).title for job in jobs}
    assert len(titles) == len(jobs) == 62


def test_fetch_list_drops_unlisted_postings(jobs):
    """Unlisted postings appear in the payload and are not real openings."""
    payload = {
        "jobs": [
            {**jobs[0], "isListed": False},
            {**jobs[1], "isListed": True},
        ],
        "apiVersion": "1",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        # The compensation parameter belongs here. Asserting the URL without it
        # is what locked the defect in: the adapter asked for a payload that
        # carries no comp keys at all, and a test agreed with it.
        assert str(request.url) == (
            "https://api.ashbyhq.com/posting-api/job-board/1password?includeCompensation=true"
        )
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetched = list(AshbyAdapter(client=client).fetch_list(BOARD))
    assert len(fetched) == 1
    assert fetched[0]["id"] == jobs[1]["id"]


# ---------------------------------------------------------------------------
# Compensation, which the endpoint withholds unless asked
# ---------------------------------------------------------------------------


def test_the_listing_url_asks_for_compensation():
    """Found live: every Ashby posting read as comp-absent.

    Without `includeCompensation=true` the endpoint returns neither the
    compensation block nor `shouldDisplayCompensationOnJobPostings` — not an
    empty one, no key at all. The parser handled both correctly and never saw
    either, so 77 boards' worth of published salaries were invisible and the
    comp floor could not fire on any of them.

    The fixture hid it. It was captured with the parameter while the code
    requested the URL without, so the tests exercised a payload the adapter
    could not actually obtain.
    """
    url = VENDORS["ashby"].list_url({"slug": "example"})

    assert "includeCompensation=true" in url


def test_the_fixture_could_have_come_from_the_url_the_code_requests(fixtures):
    """A fixture from a different URL is a test of something nobody runs."""
    payload = fixtures("ashby_clickhouse_jobs.json")

    assert "includeCompensation" in VENDORS["ashby"].list_url({"slug": "x"})
    assert any("compensation" in job for job in payload["jobs"])


def test_a_published_salary_is_read_as_stated(fixtures):
    """ClickHouse, AI Operations Engineer: $110K-$165K across two tiers."""
    payload = fixtures("ashby_clickhouse_jobs.json")
    raw = next(j for j in payload["jobs"] if j["id"].startswith("e0a5ee0f"))

    posting = AshbyAdapter().normalize(raw, CLICKHOUSE, now=NOW)

    assert (posting.comp_min, posting.comp_max) == (110000, 165000)
    assert posting.comp_currency == "USD"
    assert posting.comp_interval is CompInterval.YEAR
    assert posting.comp_source is CompSource.STATED


def test_a_tiered_range_reports_the_whole_span(fixtures):
    """Tier 1 is $130-165K and Tier 2 is $110-145K.

    The union is what a candidate could be offered, and it is what the
    range-ratio check needs to see — a posting spanning 110 to 165 is a
    different proposition from one offering 130 to 165, and collapsing to
    either tier alone would hide that.
    """
    payload = fixtures("ashby_clickhouse_jobs.json")
    raw = next(j for j in payload["jobs"] if j["id"].startswith("e0a5ee0f"))
    tiers = raw["compensation"]["compensationTiers"]

    assert len(tiers) == 2
    posting = AshbyAdapter().normalize(raw, CLICKHOUSE, now=NOW)
    assert posting.comp_min == 110000, "the lowest tier's floor"
    assert posting.comp_max == 165000, "the highest tier's ceiling"


def test_structured_numbers_are_preferred_over_the_display_string(fixtures):
    """`summaryComponents` carries integers; the summary is text for humans.

    Both agree today. Reading the numbers means a change to how Ashby formats
    a salary for display cannot silently change what the comp gate decides.
    """
    payload = fixtures("ashby_clickhouse_jobs.json")
    raw = next(
        j for j in payload["jobs"]
        if (j.get("compensation") or {}).get("summaryComponents")
        and j.get("shouldDisplayCompensationOnJobPostings")
    )
    salary = next(
        c for c in raw["compensation"]["summaryComponents"]
        if c.get("compensationType") == "Salary"
    )

    posting = AshbyAdapter().normalize(raw, CLICKHOUSE, now=NOW)

    assert posting.comp_min == int(salary["minValue"])
    assert posting.comp_max == int(salary["maxValue"])


def test_a_withheld_salary_is_still_distinguished_from_an_absent_one(fixtures):
    """44 of these 202 configured comp and chose not to publish it."""
    payload = fixtures("ashby_clickhouse_jobs.json")
    raw = next(
        j for j in payload["jobs"] if j.get("shouldDisplayCompensationOnJobPostings") is False
    )

    posting = AshbyAdapter().normalize(raw, CLICKHOUSE, now=NOW)

    assert posting.comp_source is CompSource.WITHHELD
    assert posting.comp_min is None
