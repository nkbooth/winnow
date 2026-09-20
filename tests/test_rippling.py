"""The Rippling adapter, against Framework's board captured 2026-09-19.

One of seven careers links that turned out to be pollable at all. Its list
payload is the thinnest of any vendor — a name, a department and one location
string — so nearly everything worth gating on arrives only with the detail
fetch, which makes the laziness the pipeline already does load-bearing here.

The detail payload is rich in return: it names the employer, dates the posting,
gives an employment type and carries a pay range when the employer set one.
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
from winnow.sources.rippling import RipplingAdapter

BOARD = Board(vendor="rippling", identifier={"slug": "framework"}, company="Framework")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def jobs(fixtures):
    return fixtures("rippling_framework_jobs.json")


@pytest.fixture
def detail(fixtures):
    return fixtures("rippling_framework_detail.json")


def test_identity_and_url(jobs):
    posting = RipplingAdapter().normalize(jobs[0], BOARD, now=NOW)
    assert posting.source == "rippling"
    assert posting.source_id == jobs[0]["uuid"]
    assert posting.title == "Mechanical Engineer"
    assert posting.source_url == jobs[0]["url"]
    assert posting.company == "Framework"


def test_the_employer_names_itself_in_the_detail(jobs, detail):
    """`companyName` has trailing whitespace in the real payload."""
    posting = RipplingAdapter().normalize({**jobs[0], **detail}, BOARD, now=NOW)
    assert posting.company == "Framework"


def test_the_list_payload_is_too_thin_to_score(jobs):
    posting = RipplingAdapter().normalize(jobs[0], BOARD, now=NOW)
    assert posting.description_text is None
    assert posting.description_complete is False
    assert posting.posted_at is None
    assert posting.employment_type is EmploymentType.UNKNOWN


def test_detail_supplies_description_date_and_employment_type(jobs, detail):
    posting = RipplingAdapter().normalize({**jobs[0], **detail}, BOARD, now=NOW)

    assert posting.description_complete is True
    assert posting.description_text
    assert "<div>" not in posting.description_text
    assert posting.posted_at is not None
    assert posting.posted_at.date().isoformat() == "2026-09-17"
    assert posting.posted_at_precision is PostedAtPrecision.EXACT
    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.employment_type_source is EmploymentTypeSource.STRUCTURED


def test_location_comes_from_the_board(jobs):
    posting = RipplingAdapter().normalize(jobs[0], BOARD, now=NOW)
    assert posting.locations == ("Taiwan",)
    assert posting.remote is RemoteStatus.UNKNOWN
    assert posting.remote_source is RemoteSource.ABSENT


def test_a_remote_location_string_is_read_as_remote(jobs):
    raw = {**jobs[0], "workLocation": {"label": "Remote - US", "id": "remote-us"}}
    posting = RipplingAdapter().normalize(raw, BOARD, now=NOW)
    assert posting.remote is RemoteStatus.REMOTE
    assert posting.remote_source is RemoteSource.LOCATION_STRING


def test_an_empty_pay_range_is_absent_not_zero(jobs, detail):
    """`payRangeDetails` is an empty list when the employer set none."""
    posting = RipplingAdapter().normalize({**jobs[0], **detail}, BOARD, now=NOW)
    assert posting.comp_source in (CompSource.ABSENT, CompSource.PARSED)
    if posting.comp_source is CompSource.ABSENT:
        assert posting.comp_min is None


def test_a_stated_pay_range_is_read_as_stated(jobs, detail):
    raw = {
        **jobs[0],
        **detail,
        "payRangeDetails": [
            {"minValue": 210000, "maxValue": 245000, "currency": "USD", "frequency": "Annual"}
        ],
    }
    posting = RipplingAdapter().normalize(raw, BOARD, now=NOW)
    assert (posting.comp_min, posting.comp_max) == (210000, 245000)
    assert posting.comp_currency == "USD"
    assert posting.comp_source is CompSource.STATED


def test_fetch_list_returns_the_bare_array(jobs):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://api.rippling.com/platform/api/ats/v1/board/framework/jobs"
        )
        return httpx.Response(200, json=jobs)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(list(RipplingAdapter(client=client).fetch_list(BOARD))) == 1


def test_fetch_detail_requests_the_job_by_uuid(jobs, detail):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=detail)

    adapter = RipplingAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    posting = adapter.normalize(jobs[0], BOARD, now=NOW)
    assert adapter.fetch_detail(posting) is not None
    assert requested == [
        f"https://api.rippling.com/platform/api/ats/v1/board/framework/jobs/{jobs[0]['uuid']}"
    ]
