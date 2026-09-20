"""The Workday adapter, against Red Hat's tenant captured 2026-09-18.

Workday is a different request shape from every other adapter — a POST with a
JSON body, paged by limit and offset — and the only vendor that states work
arrangement as a field rather than leaving it to prose. That single field serves
the hardest gate in the rubric, which is why this adapter is built rather than
deferred.

It is also the only vendor whose list payload has no description and no
compensation, so the detail fetch is what makes a posting scorable. No detail
payload was captured on 2026-09-18, so the detail tests use a hand-written body
in the documented shape; the list tests use the recorded one.
"""

from datetime import UTC, datetime

import httpx
import pytest

from winnow.models import (
    Board,
    CompSource,
    PostedAtPrecision,
    RemoteSource,
    RemoteStatus,
)
from winnow.sources.workday import WorkdayAdapter

BOARD = Board(
    vendor="workday",
    identifier={"tenant": "redhat", "datacenter": "wd5", "site": "jobs"},
    company="Red Hat",
)
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def payload(fixtures):
    return fixtures("workday_redhat_jobs.json")


@pytest.fixture
def junior_consultant(payload):
    return next(job for job in payload["jobPostings"] if job["title"] == "Junior Consultant")


def test_external_path_is_the_natural_key(junior_consultant):
    """Requisition ids repeat across locations; the path does not."""
    posting = WorkdayAdapter().normalize(junior_consultant, BOARD, now=NOW)
    assert posting.source_id == "/job/Raleigh/Junior-Consultant_R-059072"
    assert posting.company == "Red Hat"
    assert posting.title == "Junior Consultant"


def test_source_url_points_at_the_employers_own_site(junior_consultant):
    posting = WorkdayAdapter().normalize(junior_consultant, BOARD, now=NOW)
    assert posting.source_url == (
        "https://redhat.wd5.myworkdayjobs.com/en-US/jobs/job/Raleigh/Junior-Consultant_R-059072"
    )


def test_remote_type_is_structured(payload):
    """The one field no other vendor provides, and the one the RTO gate needs."""
    adapter = WorkdayAdapter()
    by_title = {
        job["title"]: adapter.normalize(job, BOARD, now=NOW) for job in payload["jobPostings"]
    }
    assert by_title["Junior Consultant"].remote is RemoteStatus.HYBRID
    assert by_title["Junior Consultant"].remote_source is RemoteSource.STRUCTURED
    assert by_title["Cloud Consultant"].remote is RemoteStatus.REMOTE
    assert by_title["Strategic Account Manager - Telco"].remote is RemoteStatus.ONSITE


@pytest.mark.parametrize(
    ("posted_on", "expected_date"),
    [
        ("Posted Today", "2026-09-18"),
        ("Posted Yesterday", "2026-09-17"),
        ("Posted 3 Days Ago", "2026-09-15"),
        ("Posted 30+ Days Ago", "2026-08-19"),
    ],
)
def test_relative_posted_dates(posted_on, expected_date, junior_consultant):
    raw = {**junior_consultant, "postedOn": posted_on}
    posting = WorkdayAdapter().normalize(raw, BOARD, now=NOW)
    assert posting.posted_at is not None
    assert posting.posted_at.date().isoformat() == expected_date
    assert posting.posted_at_precision is PostedAtPrecision.RELATIVE


def test_the_thirty_plus_days_wording_is_preserved(junior_consultant):
    """Not a date, but a ghost-job signal, so the phrasing survives."""
    raw = {**junior_consultant, "postedOn": "Posted 30+ Days Ago"}
    posting = WorkdayAdapter().normalize(raw, BOARD, now=NOW)
    assert posting.posted_at_text == "Posted 30+ Days Ago"


def test_a_location_count_is_not_a_location(junior_consultant):
    """ "2 Locations" is a count. The path segment names the one we know."""
    posting = WorkdayAdapter().normalize(junior_consultant, BOARD, now=NOW)
    assert posting.locations == ("Raleigh",)


def test_a_real_location_string_is_used_as_given(payload):
    singapore = next(job for job in payload["jobPostings"] if job["locationsText"] == "Singapore")
    posting = WorkdayAdapter().normalize(singapore, BOARD, now=NOW)
    assert posting.locations == ("Singapore",)


def test_the_list_payload_alone_is_not_scorable(junior_consultant):
    posting = WorkdayAdapter().normalize(junior_consultant, BOARD, now=NOW)
    assert posting.description_text is None
    assert posting.description_complete is False
    assert posting.comp_source is CompSource.ABSENT


def test_detail_merged_into_the_list_row_completes_the_posting(junior_consultant):
    detail = {
        "jobPostingInfo": {
            "jobDescription": "<p>Own the BizOps function. Range: $210,000-$245,000 USD.</p>",
            "jobRequisitionId": "R-059072",
            "timeType": "Full time",
            "remoteType": "Hybrid",
            "additionalLocations": ["Raleigh", "Boston"],
        }
    }
    posting = WorkdayAdapter().normalize({**junior_consultant, **detail}, BOARD, now=NOW)
    assert posting.description_text is not None
    assert "Own the BizOps function" in posting.description_text
    assert posting.description_complete is True
    assert (posting.comp_min, posting.comp_max) == (210000, 245000)
    assert posting.comp_source is CompSource.PARSED


def test_fetch_detail_requests_the_posting_path(junior_consultant):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>Hi</p>"}})

    adapter = WorkdayAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    posting = adapter.normalize(junior_consultant, BOARD, now=NOW)
    detail = adapter.fetch_detail(posting)

    assert detail is not None
    assert requested == [
        "https://redhat.wd5.myworkdayjobs.com/wday/cxs/redhat/jobs"
        "/job/Raleigh/Junior-Consultant_R-059072"
    ]


def test_fetch_list_posts_the_documented_body_and_pages(payload):
    """N+1 is the real cost here, so the list request has to be right."""
    rows = [
        {**job, "externalPath": f"{job['externalPath']}-{page}"}
        for page in range(3)
        for job in payload["jobPostings"]
    ][:45]
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        bodies.append(body)
        offset, limit = body["offset"], body["limit"]
        return httpx.Response(
            200, json={"total": len(rows), "jobPostings": rows[offset : offset + limit]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetched = list(WorkdayAdapter(client=client).fetch_list(BOARD))

    assert len(fetched) == 45
    assert [body["offset"] for body in bodies] == [0, 20, 40]
    assert bodies[0] == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}


def test_fetch_list_stops_when_a_page_comes_back_empty():
    """A tenant that reports a total it cannot deliver must not loop forever."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"total": 10_000, "jobPostings": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert list(WorkdayAdapter(client=client).fetch_list(BOARD)) == []
    assert len(calls) == 1
