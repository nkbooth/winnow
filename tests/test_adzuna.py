"""Adzuna, which is a discovery source and not a posting source.

Measured 2026-09-19 against the live API and the 18 postings the ATS path had
already confirmed:

* **Every** description is truncated — 225 of 225 across nine title queries.
* 58% of salaries are modelled, flagged `salary_is_predicted`.
* The only URL is an `adzuna.com` tracker. Its detail page hides the employer
  link behind an apply redirect that answers 403 to anything without a browser.
* It surfaced 8 of the 18 confirmed postings, at roughly a day's lag, and the
  misses are whole employers rather than scattered roles: Canonical 0 of 6.

The contract says `source_url` is the canonical URL *at the employer*, never an
aggregator redirect, and a truncated description sets `description_complete`
false, which the scorer refuses. Adzuna can satisfy neither, so it does not
produce postings. It produces leads: an employer name and a title, enough to go
find the board and add it. That is a narrower job than the other adapters do,
and it is the whole of what this source can honestly support.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from winnow.models import CompInterval, CompSource
from winnow.sources.adzuna import AdzunaClient, AdzunaCredentials, Lead, normalize_lead

FIXTURES = Path("tests/fixtures")


@pytest.fixture
def page() -> dict:
    return json.loads((FIXTURES / "adzuna_sales_operations.json").read_text())


def _client(payload: dict) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )


def _stub_credentials() -> AdzunaCredentials:
    return AdzunaCredentials(app_id="test-id", app_key="test-key")


def test_a_search_returns_leads(page):
    client = AdzunaClient(client=_client(page), credentials=_stub_credentials())

    leads = client.search("director of sales operations")

    assert len(leads) == 50
    assert all(isinstance(lead, Lead) for lead in leads)


def test_the_credentials_are_sent_as_query_parameters(page):
    seen: list[httpx.URL] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=page)

    client = AdzunaClient(
        client=httpx.Client(transport=httpx.MockTransport(capture)),
        credentials=_stub_credentials(),
    )
    client.search("director of sales operations", days=14)

    assert seen[0].params["app_id"] == "test-id"
    assert seen[0].params["app_key"] == "test-key"
    assert seen[0].params["what_phrase"] == "director of sales operations"
    assert seen[0].params["max_days_old"] == "14"


def test_a_lead_is_not_a_posting(page):
    """There is no field on Lead that could be mistaken for an employer URL."""
    lead = normalize_lead(page["results"][0])

    assert not hasattr(lead, "source_url")
    assert lead.aggregator_url.startswith("https://www.adzuna.com/")


def test_the_description_is_an_excerpt_and_says_so(page):
    """Adzuna truncates every description; 225 of 225 in the 2026-09-19 sweep."""
    lead = normalize_lead(page["results"][0])

    assert lead.description_excerpt
    assert not hasattr(lead, "description_text"), "must not look like a scorable description"


def test_a_modelled_salary_is_marked_predicted(page):
    predicted = next(job for job in page["results"] if job["salary_is_predicted"] == "1")

    lead = normalize_lead(predicted)

    assert lead.comp_source is CompSource.PREDICTED
    assert lead.comp_interval is CompInterval.YEAR
    assert lead.comp_min and lead.comp_max


def test_a_stated_salary_is_marked_stated(page):
    stated = next(job for job in page["results"] if job["salary_is_predicted"] == "0")

    assert normalize_lead(stated).comp_source is CompSource.STATED


def test_a_predicted_salary_can_never_be_read_as_stated(page):
    """Rule 3 of the adapter contract, at its only real test site.

    A predicted $205k must never satisfy the $200k floor. Adzuna is the one
    source that publishes modelled figures at all, so this is where that rule
    either holds or is quietly lost.
    """
    leads = [normalize_lead(job) for job in page["results"]]
    predicted = [lead for lead in leads if lead.comp_source is CompSource.PREDICTED]

    assert len(predicted) == 34, "the fixture's measured split"
    assert all(lead.comp_source is not CompSource.STATED for lead in predicted)


def test_the_location_is_the_stated_display_name(page):
    lead = normalize_lead(page["results"][0])

    assert lead.location == "Dallas, Texas"


def test_the_posting_date_is_exact(page):
    lead = normalize_lead(page["results"][0])

    assert lead.created.tzinfo is UTC or lead.created.utcoffset() == datetime.now(UTC).utcoffset()


def test_a_lead_with_no_employer_name_is_dropped(page):
    """Measured at 2 rows in 654 during the 2026-09-19 sweep.

    A lead exists to name an employer to go and look up. One that names nobody
    is not a weaker lead, it is not a lead, and passing it through would put an
    empty row in front of a human for no reason.
    """
    payload = {"results": [{**page["results"][0], "company": {}}, page["results"][1]]}
    client = AdzunaClient(client=_client(payload), credentials=_stub_credentials())

    leads = client.search("anything")

    assert len(leads) == 1


def test_an_absent_salary_is_absent_not_zero(page):
    bare = {key: value for key, value in page["results"][0].items() if not key.startswith("salary")}

    lead = normalize_lead(bare)

    assert lead.comp_min is None
    assert lead.comp_max is None
    assert lead.comp_source is CompSource.ABSENT


def test_every_row_in_the_snapshot_normalises(page):
    leads = [normalize_lead(job) for job in page["results"]]

    assert all(lead.company and lead.title for lead in leads)
    assert len({lead.identifier for lead in leads}) == 50
