"""The Lever adapter, against Element Solutions' board captured 2026-09-18.

Lever is the richest source, not the poorest: the per-posting payload carries a
structured ``salaryRange`` and a structured ``workplaceType``. What it does not
carry is the company name — the board is the only place that exists.
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
    PostedAtPrecision,
    RemoteSource,
    RemoteStatus,
)
from winnow.sources.lever import LeverAdapter

BOARD = Board(vendor="lever", identifier={"slug": "elementsolutions"}, company="Element Solutions")
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def postings(fixtures):
    return fixtures("lever_elementsolutions_postings.json")


@pytest.fixture
def fhir(postings):
    return next(item for item in postings if item["text"] == "FHIR Implementation Specialist")


def test_company_comes_from_the_board(fhir):
    """Nothing in a Lever payload identifies the employer."""
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert posting.company == "Element Solutions"
    assert posting.title == "FHIR Implementation Specialist"
    assert posting.source_url == fhir["hostedUrl"]
    assert posting.apply_url == fhir["applyUrl"]


def test_structured_salary_range_is_stated_not_parsed(fhir):
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert (posting.comp_min, posting.comp_max) == (145000, 155000)
    assert posting.comp_currency == "USD"
    assert posting.comp_interval is CompInterval.YEAR
    assert posting.comp_source is CompSource.STATED


def test_structured_workplace_type(fhir):
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert posting.remote is RemoteStatus.REMOTE
    assert posting.remote_source is RemoteSource.STRUCTURED


def test_created_at_is_epoch_milliseconds(fhir):
    """Treated as seconds this lands in 1970 and looks ancient to the staleness rules."""
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert posting.posted_at is not None
    assert posting.posted_at.year == 2024
    assert posting.posted_at_precision is PostedAtPrecision.EXACT


def test_commitment_is_free_text_not_an_enum(fhir):
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.employment_type_source is EmploymentTypeSource.TEXT


def test_description_includes_the_additional_sections(fhir):
    """The RTO trap lives in prose, and Lever puts location prose in `additional`."""
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert posting.description_text is not None
    assert "remote first company based out of Washington, DC" in posting.description_text
    assert posting.description_complete is True
    assert "<div" not in posting.description_text


def test_locations_and_team(fhir):
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert posting.locations == ("United States",)
    assert posting.department == "Delivery"
    assert posting.team == "CMS Program"


def test_a_posting_without_a_salary_range_is_not_invented(postings):
    """Absent structured comp falls back to prose, and says so if it finds any."""
    without = next(item for item in postings if "salaryRange" not in item)
    posting = LeverAdapter().normalize(without, BOARD, now=NOW)
    assert posting.comp_source in (CompSource.ABSENT, CompSource.PARSED)
    if posting.comp_source is CompSource.ABSENT:
        assert (posting.comp_min, posting.comp_max) == (None, None)


def test_fetch_list_returns_the_bare_array(postings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.lever.co/v0/postings/elementsolutions?mode=json"
        return httpx.Response(200, json=postings)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert len(list(LeverAdapter(client=client).fetch_list(BOARD))) == 2


def test_lever_needs_no_detail_fetch(fhir):
    posting = LeverAdapter().normalize(fhir, BOARD, now=NOW)
    assert LeverAdapter().fetch_detail(posting) is None
