"""The Greenhouse adapter, against the board captured 2026-09-18.

Greenhouse is the best documented vendor and the weakest for structured signal:
remote status is only inferable from a location string, employment type hides in
a per-board custom metadata field, and the description arrives entity-escaped.
Each of those is asserted here against a real payload rather than a hand-written
one.
"""

from datetime import UTC, datetime

import httpx
import pytest

from winnow.models import (
    Board,
    CompSource,
    EmploymentType,
    EmploymentTypeSource,
    PostedAtPrecision,
    RemoteSource,
    RemoteStatus,
)
from winnow.sources.greenhouse import GreenhouseAdapter

BOARD = Board(vendor="greenhouse", identifier={"slug": "tailscale"}, company="Tailscale")
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def jobs(fixtures):
    return fixtures("greenhouse_tailscale_jobs.json")["jobs"]


@pytest.fixture
def analytics_engineer(jobs):
    return next(job for job in jobs if job["id"] == 4721252005)


def test_identity_fields(analytics_engineer):
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert posting.source == "greenhouse"
    assert posting.source_id == "4721252005"
    assert posting.company == "Tailscale"
    assert posting.title == "Analytics Engineer, Data"
    assert posting.source_url == "https://job-boards.greenhouse.io/tailscale/jobs/4721252005"
    assert posting.first_seen_at == NOW


def test_description_is_readable_text(analytics_engineer):
    """Entity-escaped once, so decoding must happen before and after tag removal."""
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert posting.description_text is not None
    assert "&lt;" not in posting.description_text
    assert "<div" not in posting.description_text
    assert "&nbsp;" not in posting.description_text
    assert "&quot;" not in posting.description_text
    assert "Tailscale" in posting.description_text
    assert posting.description_complete is True


def test_remote_is_inferred_from_the_location_string(analytics_engineer):
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert posting.remote is RemoteStatus.REMOTE
    assert posting.remote_source is RemoteSource.LOCATION_STRING
    assert posting.locations == ("Remote (Canada)",)


def test_employment_type_comes_from_the_custom_metadata_field(analytics_engineer):
    """The field name is per-board, so it is matched rather than indexed."""
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.employment_type_source is EmploymentTypeSource.CUSTOM_FIELD


def test_dates(analytics_engineer):
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert posting.posted_at is not None
    assert posting.posted_at.date().isoformat() == "2026-08-04"
    assert posting.posted_at_precision is PostedAtPrecision.EXACT
    assert posting.updated_at is not None
    assert posting.updated_at.date().isoformat() == "2026-09-14"


def test_department(analytics_engineer):
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert posting.department == "Data"


def test_comp_is_absent_unless_the_body_states_it(analytics_engineer):
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert posting.comp_source in (CompSource.ABSENT, CompSource.PARSED)
    if posting.comp_source is CompSource.ABSENT:
        assert posting.comp_min is None


def test_comp_found_in_the_body_is_marked_parsed():
    """Greenhouse buries comp in prose; prose is 'parsed', never 'stated'."""
    raw = {
        "id": 1,
        "title": "Director, Business Systems",
        "absolute_url": "https://job-boards.greenhouse.io/x/jobs/1",
        "location": {"name": "Remote (United States)"},
        "content": "&lt;p&gt;The range is $210,000-$245,000 USD.&lt;/p&gt;",
        "first_published": "2026-09-01T10:00:00-04:00",
        "updated_at": "2026-09-02T10:00:00-04:00",
        "metadata": [],
        "departments": [],
    }
    posting = GreenhouseAdapter().normalize(raw, BOARD, now=NOW)
    assert (posting.comp_min, posting.comp_max) == (210000, 245000)
    assert posting.comp_source is CompSource.PARSED


def test_every_posting_on_the_real_board_normalises(jobs):
    adapter = GreenhouseAdapter()
    postings = [adapter.normalize(job, BOARD, now=NOW) for job in jobs]
    assert len(postings) == 55
    assert all(posting.source_id for posting in postings)
    assert all(posting.title for posting in postings)


def test_the_board_carries_the_duplication_the_notes_measured(jobs):
    """55 postings, 29 distinct titles — the case dedupe exists for."""
    adapter = GreenhouseAdapter()
    titles = {adapter.normalize(job, BOARD, now=NOW).title for job in jobs}
    assert len(jobs) == 55
    assert len(titles) == 29


def test_fetch_list_calls_the_board_endpoint(fixtures):
    payload = fixtures("greenhouse_tailscale_jobs.json")
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = list(GreenhouseAdapter(client=client).fetch_list(BOARD))

    assert len(jobs) == 55
    assert len(requested) == 1
    assert str(requested[0].url) == (
        "https://boards-api.greenhouse.io/v1/boards/tailscale/jobs?content=true"
    )
    assert "winnow" in requested[0].headers["user-agent"]


def test_fetch_list_raises_on_a_404(fixtures):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=fixtures("greenhouse_404_elementsolutions.json"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        list(GreenhouseAdapter(client=client).fetch_list(BOARD))


def test_greenhouse_needs_no_detail_fetch(analytics_engineer):
    """The list payload already carries the description with content=true."""
    posting = GreenhouseAdapter().normalize(analytics_engineer, BOARD, now=NOW)
    assert GreenhouseAdapter().fetch_detail(posting) is None


def test_a_location_that_states_no_arrangement_claims_no_provenance(fixtures):
    """`San Francisco, CA` is a place. Reading it taught us nothing about how.

    The source said LOCATION_STRING whenever a location existed, even when the
    status it produced was UNKNOWN — provenance claiming a string told us
    something it did not say. Every other adapter reports ABSENT here.
    """
    raw = {
        "id": 1,
        "title": "Head of Revenue Operations",
        "absolute_url": "https://boards.greenhouse.io/faire/jobs/1",
        "location": {"name": "San Francisco, CA"},
        "content": "<p>A role.</p>",
    }

    posting = GreenhouseAdapter().normalize(raw, BOARD, now=NOW)

    assert posting.remote is RemoteStatus.UNKNOWN
    assert posting.remote_source is RemoteSource.ABSENT


def test_a_location_that_does_state_an_arrangement_keeps_its_provenance(fixtures):
    raw = {
        "id": 2,
        "title": "Head of Revenue Operations",
        "absolute_url": "https://boards.greenhouse.io/faire/jobs/2",
        "location": {"name": "Remote - US"},
        "content": "<p>A role.</p>",
    }

    posting = GreenhouseAdapter().normalize(raw, BOARD, now=NOW)

    assert posting.remote is RemoteStatus.REMOTE
    assert posting.remote_source is RemoteSource.LOCATION_STRING
