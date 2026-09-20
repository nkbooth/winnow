"""The digest: one of the two surfaces touched daily.

Design rules that are asserted rather than described:

* **The pending-review count leads.** Making the digest read-only created one
  real risk — an untriaged backlog accumulating invisibly — so the number that
  exposes it is the first thing on the page.
* **Compensation always shows its provenance.** A modelled figure must never
  look like a quoted one.
* **One concern per entry, never zero.** If nothing is wrong, the concern says
  what is unknown. An entry with no downside reads as sales copy.
* **The link is the employer's own URL**, worth roughly 3.5x on interview rate.
* **The veto tally is shown but not itemised** — proof the filters work,
  without spending eight lines on rejects.
* Silence and breakage never look alike.
"""

from datetime import UTC, datetime, timedelta

import pytest

from winnow import learning, store
from winnow.dedupe import cluster_postings, persist_cluster
from winnow.digest import build_digest, render_html, render_text
from winnow.models import CompInterval, CompSource
from winnow.scoring import BreakdownLine, ScoreRecord

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _record(score, *, vetoes=(), concern="Comp unresolved — ask before investing time."):
    return ScoreRecord(
        score=score,
        breakdown=(BreakdownLine("growth_signal", 1.0, 20, 20.0, "own the function", False),),
        vetoes=tuple(vetoes),
        flags=(),
        unverified=(),
        why_fits="Owns the BizOps function outright, including a team of two.",
        concern=concern,
        vetoed=bool(vetoes),
        model="claude-opus-5",
        prompt_version="1",
        rubric_version="v1.test",
    )


@pytest.fixture
def add_cluster(conn, make_posting):
    def add(*, score, title, company="Grafana Labs", **posting_fields):
        company_id = store.insert_company(conn, company)
        posting = make_posting(
            company=company,
            title=title,
            source_id=title,
            description_text="Own and build out the BizOps function.",
            description_complete=True,
            **posting_fields,
        )
        cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
        learning.record_score(conn, cluster_id, score)
        return cluster_id

    return add


def test_the_header_leads_with_the_review_backlog(conn, profile, add_cluster):
    add_cluster(score=_record(87), title="Director, Business Systems")
    add_cluster(score=_record(74), title="Director of Revenue Operations")

    digest = build_digest(conn, profile, now=NOW)
    text = render_text(digest)

    assert text.splitlines()[0] == "winnow · 18 Sep · 2 new · 2 awaiting review"


def test_entries_are_ranked_and_carry_their_evidence(conn, profile, add_cluster):
    add_cluster(score=_record(74), title="Director of Revenue Operations")
    add_cluster(score=_record(87), title="Director, Business Systems")

    text = render_text(build_digest(conn, profile, now=NOW))

    assert text.index("87") < text.index("74")
    assert "Director, Business Systems — Grafana Labs" in text
    assert "Owns the BizOps function outright" in text


def test_every_entry_links_to_the_employers_own_posting(conn, profile, add_cluster):
    add_cluster(
        score=_record(87),
        title="Director, Business Systems",
        source_url="https://boards.greenhouse.io/grafanalabs/jobs/4001",
    )
    text = render_text(build_digest(conn, profile, now=NOW))
    assert "https://boards.greenhouse.io/grafanalabs/jobs/4001" in text


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "comp_min": 210000,
                "comp_max": 245000,
                "comp_interval": CompInterval.YEAR,
                "comp_source": CompSource.STATED,
            },
            "$210–245k (stated)",
        ),
        (
            {
                "comp_min": 205000,
                "comp_max": 205000,
                "comp_interval": CompInterval.YEAR,
                "comp_source": CompSource.PREDICTED,
            },
            "$205k (predicted)",
        ),
        (
            {
                "comp_min": 250,
                "comp_max": 300,
                "comp_interval": CompInterval.HOUR,
                "comp_source": CompSource.STATED,
            },
            "$250–300/hr (stated)",
        ),
        ({"comp_source": CompSource.WITHHELD}, "comp withheld"),
        ({"comp_source": CompSource.ABSENT}, "comp not disclosed"),
    ],
)
def test_compensation_always_shows_its_provenance(conn, profile, add_cluster, fields, expected):
    add_cluster(score=_record(87), title="Director, Business Systems", **fields)
    assert expected in render_text(build_digest(conn, profile, now=NOW))


def test_an_entry_always_carries_exactly_one_concern(conn, profile, add_cluster):
    add_cluster(score=_record(87, concern=""), title="Director, Business Systems")
    text = render_text(build_digest(conn, profile, now=NOW))
    assert text.count("⚠") == 1
    assert "not stated" in text.lower() or "unknown" in text.lower()


def test_the_cap_is_respected_and_the_remainder_is_counted(conn, profile, add_cluster):
    for index in range(11):
        add_cluster(score=_record(90 - index), title=f"Director of Business Systems {index}")

    digest = build_digest(conn, profile, now=NOW)
    text = render_text(digest)

    assert len(digest.entries) == profile.max_items == 8
    assert "3 more above threshold" in text


def test_a_quiet_day_still_shows_the_top_three(conn, profile, add_cluster):
    """A quiet week must not look like a broken one."""
    for index, score in enumerate((68, 61, 55, 40)):
        add_cluster(score=_record(score), title=f"Director of Business Systems {index}")

    digest = build_digest(conn, profile, now=NOW)

    assert len(digest.entries) == profile.always_show_top == 3
    assert [entry.score for entry in digest.entries] == [68, 61, 55]
    assert digest.above_threshold == 0
    assert "below threshold" in render_text(digest)


def test_vetoed_clusters_are_tallied_but_not_listed(conn, profile, add_cluster):
    add_cluster(score=_record(87), title="Director, Business Systems")
    for index in range(3):
        add_cluster(
            score=_record(
                88,
                vetoes=[{"gate": "hybrid_required", "evidence": "2 days onsite"}],
            ),
            title=f"Director of Business Operations {index}",
        )
    add_cluster(
        score=_record(
            80, vetoes=[{"gate": "relocation_required", "evidence": "relocation required"}]
        ),
        title="Director of Sales Operations",
    )

    digest = build_digest(conn, profile, now=NOW)
    text = render_text(digest)

    assert len(digest.entries) == 1
    assert "4 vetoed (3 hybrid_required, 1 relocation_required)" in text
    assert "2 days onsite" not in text


def test_a_decided_cluster_never_appears_again(conn, profile, add_cluster):
    cluster_id = add_cluster(score=_record(87), title="Director, Business Systems")
    learning.record_decision(conn, cluster_id, "pass", reason="timing")

    digest = build_digest(conn, profile, now=NOW)
    assert digest.entries == ()


def test_a_failed_source_is_never_shown_as_a_quiet_one(conn, profile, add_cluster):
    conn.execute(
        "INSERT INTO poll_runs (source, started_at, outcome, postings_seen, error) "
        "VALUES ('greenhouse', ?, 'failed', 0, 'HTTP 503')",
        (NOW.isoformat(),),
    )
    digest = build_digest(conn, profile, now=NOW)
    text = render_text(digest)

    assert digest.failed_sources == ("greenhouse",)
    assert "greenhouse did not answer" in text.lower()


def test_the_html_body_says_the_same_thing(conn, profile, add_cluster):
    add_cluster(
        score=_record(87),
        title="Director, Business Systems",
        source_url="https://boards.greenhouse.io/grafanalabs/jobs/4001",
    )
    digest = build_digest(conn, profile, now=NOW)
    html = render_html(digest)

    assert "awaiting review" in html
    assert 'href="https://boards.greenhouse.io/grafanalabs/jobs/4001"' in html
    assert "Director, Business Systems" in html


def test_html_escapes_what_a_posting_supplied(conn, profile, add_cluster):
    add_cluster(score=_record(87), title="Director, Business Systems <script>")
    html = render_html(build_digest(conn, profile, now=NOW))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_nothing_to_say_is_said_only_on_the_weekly_summary(conn, profile):
    """Post on hits, plus a Monday summary even at zero, so silence has a voice."""
    quiet_tuesday = build_digest(conn, profile, now=datetime(2026, 9, 15, 12, tzinfo=UTC))
    monday = build_digest(conn, profile, now=datetime(2026, 9, 14, 12, tzinfo=UTC))

    assert quiet_tuesday.should_send is False
    assert monday.should_send is True
    assert "nothing above threshold" in render_text(monday).lower()


def test_a_role_that_closed_is_not_offered(conn, profile, add_cluster):
    """The worst thing this system can produce is an evening spent on a role
    that closed last week. Once the poll marks departures, the digest can tell."""
    cluster_id = add_cluster(score=_record(87), title="Director, Business Systems")
    add_cluster(score=_record(80), title="Director of Revenue Operations")
    conn.execute(
        "UPDATE postings SET disappeared_at = ? WHERE id = "
        "(SELECT canonical_posting_id FROM clusters WHERE id = ?)",
        (NOW.isoformat(), cluster_id),
    )

    digest = build_digest(conn, profile, now=NOW)
    text = render_text(digest)

    assert [entry.score for entry in digest.entries] == [80]
    assert digest.withdrawn == 1
    assert "1 withdrawn" in text


def test_the_footer_names_what_is_waiting_on_you(conn, profile, add_cluster):
    """The digest is what gets read daily, so the open work belongs in it.

    An interested role with no application is work that only exists in the
    working set, and a list nobody is reminded of is a list nobody opens.
    """
    cluster_id = add_cluster(score=_record(87), title="Director, Business Systems")
    learning.record_decision(conn, cluster_id, "interested")
    conn.execute(
        "UPDATE decisions SET created_at = ? WHERE cluster_id = ?",
        ((NOW - timedelta(days=6)).isoformat(), cluster_id),
    )

    body = render_text(build_digest(conn, profile, now=NOW))

    assert "1 interested, not yet applied" in body
    assert "oldest 6 days" in body


def test_the_footer_stays_quiet_when_there_is_no_open_work(conn, profile, add_cluster):
    add_cluster(score=_record(87), title="Director, Business Systems")

    body = render_text(build_digest(conn, profile, now=NOW))

    assert "not yet applied" not in body


def test_a_deferred_role_is_not_in_the_digest(conn, profile, add_cluster):
    """Deferring means not now. Posting it again tomorrow is not honouring that."""
    cluster_id = add_cluster(score=_record(87), title="Director, Business Systems")
    add_cluster(score=_record(74), title="Director of Revenue Operations")
    learning.record_decision(conn, cluster_id, "defer")

    body = render_text(build_digest(conn, profile, now=NOW))

    assert "Director, Business Systems" not in body
    assert "Director of Revenue Operations" in body
