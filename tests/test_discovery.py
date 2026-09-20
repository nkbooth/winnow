"""Turning aggregator leads into a question a human can answer.

The point of the Adzuna path is not to add postings — it cannot, honestly — but
to name employers the board list does not already contain. So the output is a
short list of companies to go and look up, with the role that suggested each
one, and everything already tracked removed.
"""

from datetime import UTC, datetime

import pytest

from winnow import discovery, store
from winnow.models import CompInterval, CompSource
from winnow.sources.adzuna import Lead


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "test.db")
    store.migrate(connection)
    yield connection
    connection.close()


def _lead(company: str, title: str = "Director of Business Systems", **kwargs) -> Lead:
    defaults = dict(
        identifier=f"{company}-{title}",
        company=company,
        title=title,
        location="Remote, US",
        created=datetime(2026, 9, 18, tzinfo=UTC),
        comp_min=None,
        comp_max=None,
        comp_interval=CompInterval.UNKNOWN,
        comp_source=CompSource.ABSENT,
        category="IT Jobs",
        contract_time="full_time",
        aggregator_url="https://www.adzuna.com/details/1",
        description_excerpt="Something truncated…",
    )
    return Lead(**{**defaults, **kwargs})


def test_a_tracked_company_is_not_offered_again(conn):
    store.insert_company(conn, "GitLab")

    remaining = discovery.untracked(conn, [_lead("GitLab"), _lead("Sourcegraph")])

    assert [lead.company for lead in remaining] == ["Sourcegraph"]


def test_matching_ignores_case_and_punctuation(conn):
    """Aggregators render a name however the employer typed it that day."""
    store.insert_company(conn, "1Password")

    remaining = discovery.untracked(conn, [_lead("1password, Inc."), _lead("AgileBits")])

    assert [lead.company for lead in remaining] == ["AgileBits"]


def test_leads_are_grouped_by_employer(conn):
    grouped = discovery.by_company(
        [
            _lead("Ramp", "Director of Revenue Operations"),
            _lead("Ramp", "Director of Business Systems"),
            _lead("Navan", "Director of Sales Operations"),
        ]
    )

    assert list(grouped) == ["Ramp", "Navan"], "most roles first"
    assert len(grouped["Ramp"]) == 2


def test_a_staffing_agency_is_dropped(conn):
    """The profile declines staffing agencies outright — worst measured conversion.

    They are the bulk of what a keyword aggregator returns, so leaving them in
    would bury the handful of real employers a sweep finds.
    """
    remaining = discovery.untracked(
        conn,
        [
            _lead("Insight Global"),
            _lead("Robert Half Technology"),
            _lead("APN Software Services, Inc"),
            _lead("Ramp"),
        ],
    )

    assert [lead.company for lead in remaining] == ["Ramp"]


def test_a_sweep_searches_every_profile_title(profile):
    asked: list[str] = []

    class StubClient:
        def search(self, phrase: str, *, days: int = 30, **kwargs) -> list[Lead]:
            asked.append(phrase)
            return [_lead("Ramp", phrase)]

    leads = discovery.sweep(StubClient(), profile, days=14)

    assert set(asked) == set(profile.primary_titles)
    assert len(leads) == len(profile.primary_titles)


def test_a_sweep_keeps_one_lead_per_advert(profile):
    """The same ad answers several title phrases; a human should see it once."""

    class StubClient:
        def search(self, phrase: str, *, days: int = 30, **kwargs) -> list[Lead]:
            return [_lead("Ramp", "Director of Business Systems")]

    leads = discovery.sweep(StubClient(), profile, days=14)

    assert len(leads) == 1


def test_off_target_titles_are_dropped(profile):
    """A phrase search matches the advert *body*, not just its title.

    So a sweep for "director of sales operations" returns local account
    executives and transportation growth directors, because those ads mention
    the phrase somewhere. The same title gate the ATS path uses settles it,
    and without it the output was 183 companies, which is not a list anyone
    reads.
    """
    kept = discovery.on_target(
        [
            _lead("XPO", "Local Account Executive"),
            _lead("Jacobs", "Northeast Transportation Market Growth Director"),
            _lead("AvePoint", "Director of Revenue Operations, GTM Systems"),
        ],
        profile,
    )

    assert [lead.company for lead in kept] == ["AvePoint"]


def test_a_stated_salary_below_the_floor_is_dropped(profile):
    """Evidence, so it may gate. A modelled figure is not, so it may not."""
    kept = discovery.on_target(
        [
            _lead(
                "Publix",
                "Principal Business Systems Analyst",
                comp_min=10_400,
                comp_max=15_600,
                comp_source=CompSource.STATED,
                comp_interval=CompInterval.YEAR,
            ),
            _lead(
                "NeoGenomics",
                "Principal Business Systems Analyst",
                comp_min=93_449,
                comp_max=93_449,
                comp_source=CompSource.PREDICTED,
                comp_interval=CompInterval.YEAR,
            ),
        ],
        profile,
    )

    assert [lead.company for lead in kept] == ["NeoGenomics"], (
        "a modelled figure may never decide a gate, in either direction"
    )


def test_one_role_split_across_cities_is_listed_once(profile):
    """XPO advertised the same role in seven towns; a human needs one line."""
    leads = [
        _lead("XPO", "Director of Business Systems", identifier=str(n), location=city)
        for n, city in enumerate(["Marion, OH", "Arlington, OH", "Oxnard, CA"])
    ]

    collapsed = discovery.by_company(discovery.one_per_role(leads))

    assert len(collapsed["XPO"]) == 1
