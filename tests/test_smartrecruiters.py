"""The SmartRecruiters adapter, against Bosch Group captured 2026-09-19.

SmartRecruiters states more than Greenhouse does: the company identifies itself
in every posting, and location carries `remote` and `hybrid` booleans rather
than a string to guess from. What it does not carry is a description — that
needs a detail fetch, so the same laziness Workday forced applies here.
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
from winnow.sources.smartrecruiters import SmartRecruitersAdapter

BOARD = Board(vendor="smartrecruiters", identifier={"slug": "BoschGroup"}, company="Bosch Group")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def postings(fixtures):
    return fixtures("smartrecruiters_bosch_postings.json")["content"]


@pytest.fixture
def detail(fixtures):
    return fixtures("smartrecruiters_bosch_detail.json")


def test_identity_and_url(postings):
    posting = SmartRecruitersAdapter().normalize(postings[0], BOARD, now=NOW)
    assert posting.source == "smartrecruiters"
    assert posting.source_id == postings[0]["id"]
    assert posting.title == postings[0]["name"]
    assert posting.company == "Bosch Group", "the payload names the employer itself"
    assert posting.source_url == (
        f"https://jobs.smartrecruiters.com/BoschGroup/{postings[0]['id']}"
    )


def test_location_is_structured(postings):
    posting = SmartRecruitersAdapter().normalize(postings[0], BOARD, now=NOW)
    assert posting.locations
    assert "Germany" in posting.locations[0] or "de" in posting.locations[0].lower()


def test_remote_and_hybrid_are_booleans_not_prose(postings):
    adapter = SmartRecruitersAdapter()
    remote = adapter.normalize(
        {**postings[0], "location": {**postings[0]["location"], "remote": True}}, BOARD, now=NOW
    )
    hybrid = adapter.normalize(
        {**postings[0], "location": {**postings[0]["location"], "hybrid": True}}, BOARD, now=NOW
    )
    assert remote.remote is RemoteStatus.REMOTE
    assert remote.remote_source is RemoteSource.STRUCTURED
    assert hybrid.remote is RemoteStatus.HYBRID


def test_neither_flag_set_is_unknown_not_onsite(postings):
    """Both false is a default as often as it is a statement.

    Calling it onsite would fire a hard gate on an employer's unset checkbox.
    """
    posting = SmartRecruitersAdapter().normalize(postings[0], BOARD, now=NOW)
    assert posting.remote is RemoteStatus.UNKNOWN


def test_employment_type_comes_from_the_label(postings):
    posting = SmartRecruitersAdapter().normalize(postings[0], BOARD, now=NOW)
    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.employment_type_source is EmploymentTypeSource.STRUCTURED


def test_released_date(postings):
    posting = SmartRecruitersAdapter().normalize(postings[0], BOARD, now=NOW)
    assert posting.posted_at is not None
    assert posting.posted_at.year == 2026
    assert posting.posted_at_precision is PostedAtPrecision.EXACT


def test_the_list_payload_alone_has_no_description(postings):
    posting = SmartRecruitersAdapter().normalize(postings[0], BOARD, now=NOW)
    assert posting.description_text is None
    assert posting.description_complete is False
    assert posting.comp_source is CompSource.ABSENT


def test_detail_merged_in_supplies_the_description(postings, detail):
    posting = SmartRecruitersAdapter().normalize({**postings[0], **detail}, BOARD, now=NOW)
    assert posting.description_complete is True
    assert posting.description_text
    assert "<p>" not in posting.description_text
    assert posting.apply_url == detail["applyUrl"]


def test_every_posting_on_the_real_board_normalises(postings):
    adapter = SmartRecruitersAdapter()
    normalised = [adapter.normalize(row, BOARD, now=NOW) for row in postings]
    assert len(normalised) == 20
    assert all(p.source_id and p.title for p in normalised)


def test_fetch_list_pages_and_stops(postings):
    """Bosch alone lists 4,819 postings; a poll must not walk all of them blind."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        offset = int(dict(request.url.params).get("offset", 0))
        window = postings[offset : offset + 20]
        return httpx.Response(200, json={"totalFound": len(postings), "content": window})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetched = list(SmartRecruitersAdapter(client=client, page_size=20).fetch_list(BOARD))

    assert len(fetched) == 20
    assert "limit=20" in requested[0]
    assert len(requested) <= 2


def test_fetch_list_honours_a_ceiling():
    """A board of thousands is polled to a bound, not exhaustively."""
    page = [{"id": str(i), "name": f"Job {i}", "location": {}} for i in range(50)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"totalFound": 100_000, "content": page})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = SmartRecruitersAdapter(client=client, page_size=50, max_postings=120)
    assert len(list(adapter.fetch_list(BOARD))) == 120
