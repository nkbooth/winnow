"""Workable, which is pollable after all.

A previous pass here recorded Workable as unreadable by a third party. That was
wrong, and wrong in an instructive way: the one board measured — Cognism —
returns an empty array because it genuinely has no open roles, and the finding
generalised a single empty board into a vendor-wide verdict. Nuvei's board
answers the same endpoint with 75 postings and full descriptions.

The fixture pair exists to keep that mistake from coming back: an empty board
and a full one, from the same endpoint, so "returns nothing" and "cannot be
read" can never again be the same test.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from winnow.models import (
    Board,
    EmploymentType,
    EmploymentTypeSource,
    PostedAtPrecision,
    RemoteSource,
    RemoteStatus,
)
from winnow.sources.workable import WorkableAdapter

FIXTURES = Path("tests/fixtures")
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

NUVEI = Board(vendor="workable", identifier={"slug": "nuvei"}, company="Nuvei")
FASTMAIL = Board(vendor="workable", identifier={"slug": "fastmail-1"}, company="Fastmail")


def _client(payload: dict) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )


@pytest.fixture
def nuvei() -> dict:
    return json.loads((FIXTURES / "workable_nuvei_widget.json").read_text())


@pytest.fixture
def fastmail() -> dict:
    return json.loads((FIXTURES / "workable_fastmail_widget.json").read_text())


def test_a_full_board_lists_its_postings(nuvei):
    rows = list(WorkableAdapter(client=_client(nuvei)).fetch_list(NUVEI))
    assert len(rows) == 61


def test_one_job_advertised_in_three_cities_is_one_job(nuvei):
    """Workable emits a row per location, all sharing one shortcode and URL.

    Greenhouse splits the same way but gives each split its own id, so dedupe
    collapses it later. Here the ids collide, so the adapter has to merge or the
    store sees eight postings overwriting each other. Merging at read time also
    keeps every location in front of the location gate, which runs before
    anything collapses.
    """
    assert len(nuvei["jobs"]) == 75, "raw payload"

    rows = list(WorkableAdapter(client=_client(nuvei)).fetch_list(NUVEI))
    merged = next(row for row in rows if row["shortcode"] == "425FCF4AC1")

    assert [place["city"] for place in merged["locations"]] == ["Sofia", "Iași", "Warsaw"]
    assert sum(1 for row in rows if row["shortcode"] == "425FCF4AC1") == 1


def test_an_empty_board_is_empty_not_broken(fastmail):
    """The distinction the earlier measurement collapsed."""
    rows = list(WorkableAdapter(client=_client(fastmail)).fetch_list(FASTMAIL))
    assert rows == []


def test_the_list_payload_is_already_complete(nuvei):
    """``details=true`` returns descriptions, so there is no detail fetch."""
    adapter = WorkableAdapter(client=_client(nuvei))
    posting = adapter.normalize(nuvei["jobs"][0], NUVEI, now=NOW)

    assert posting.description_complete is True
    assert posting.description_text
    assert "<p>" not in posting.description_text, "markup must be stripped"
    assert adapter.fetch_detail(posting) is None


def test_a_posting_normalises(nuvei):
    posting = WorkableAdapter(client=_client(nuvei)).normalize(nuvei["jobs"][0], NUVEI, now=NOW)

    assert posting.source == "workable"
    assert posting.source_id == "647F91EAC6"
    assert posting.company == "Nuvei"
    assert posting.title == "AI Product Owner"
    assert posting.source_url == "https://apply.workable.com/j/647F91EAC6"
    assert posting.department == "Product & Technology"


def test_the_publish_date_is_a_day_not_an_instant(nuvei):
    """``published_on`` is a bare date; pretending to a timestamp would lie."""
    posting = WorkableAdapter(client=_client(nuvei)).normalize(nuvei["jobs"][0], NUVEI, now=NOW)

    assert posting.posted_at == datetime(2026, 6, 17, tzinfo=UTC)
    assert posting.posted_at_precision is PostedAtPrecision.DAY


def test_employment_type_is_structured(nuvei):
    posting = WorkableAdapter(client=_client(nuvei)).normalize(nuvei["jobs"][0], NUVEI, now=NOW)

    assert posting.employment_type is EmploymentType.FULL_TIME
    assert posting.employment_type_source is EmploymentTypeSource.STRUCTURED


def test_locations_are_assembled_from_the_structured_block(nuvei):
    posting = WorkableAdapter(client=_client(nuvei)).normalize(nuvei["jobs"][0], NUVEI, now=NOW)

    assert posting.locations == ("Tel Aviv-Yafo, Tel Aviv District, Israel",)


def test_telecommuting_false_means_unknown_not_onsite(nuvei):
    """An unset employer flag is not a statement that the job is on-site.

    Same rule as Ashby's ``isRemote``: a gate may fire on evidence, and a
    default-valued boolean is not evidence.
    """
    posting = WorkableAdapter(client=_client(nuvei)).normalize(nuvei["jobs"][0], NUVEI, now=NOW)

    assert posting.remote is RemoteStatus.UNKNOWN
    assert posting.remote_source is RemoteSource.ABSENT


def test_telecommuting_true_is_structured_evidence(nuvei):
    remote = next((job for job in nuvei["jobs"] if job.get("telecommuting")), None)
    if remote is None:
        pytest.skip("no remote posting in this snapshot")

    posting = WorkableAdapter(client=_client(nuvei)).normalize(remote, NUVEI, now=NOW)
    assert posting.remote is RemoteStatus.REMOTE
    assert posting.remote_source is RemoteSource.STRUCTURED


def test_every_posting_in_the_snapshot_normalises(nuvei):
    """Every real posting in the snapshot, no optional field assumed present."""
    adapter = WorkableAdapter(client=_client(nuvei))
    postings = [adapter.normalize(job, NUVEI, now=NOW) for job in adapter.fetch_list(NUVEI)]

    assert len({posting.source_id for posting in postings}) == 61
    assert all(posting.title for posting in postings)


def test_pay_stated_in_the_description_is_read(nuvei):
    """Workable's widget publishes no salary field at all, only the body.

    So prose was the only place comp could come from, and the adapter never
    looked — every Workable posting reported comp-absent regardless of what it
    said. Same gap Ashby had, for a different reason.
    """
    from winnow.models import CompSource

    raw = dict(nuvei["jobs"][0])
    raw["description"] = (
        "<p>We are a $2B payments company.</p>"
        "<p>The base salary range for this role is $165,000 - $195,000 USD.</p>"
    )

    posting = WorkableAdapter(client=_client(nuvei)).normalize(raw, NUVEI, now=NOW)

    assert (posting.comp_min, posting.comp_max) == (165000, 195000)
    assert posting.comp_source is CompSource.PARSED


def test_a_description_that_states_no_pay_stays_absent(nuvei):
    from winnow.models import CompSource

    posting = WorkableAdapter(client=_client(nuvei)).normalize(nuvei["jobs"][0], NUVEI, now=NOW)

    assert posting.comp_source is CompSource.ABSENT
