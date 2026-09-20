"""Clustering, and the ordering constraint around it.

Tailscale's board is 55 postings and 29 distinct titles, because every role is
posted once per country. Roughly 47% of a single board is duplication — but
1Password's Ashby board has none at all, so duplication is a function of vendor
and employer configuration and cannot be assumed away.

The most important thing in this module is an ordering rule:

    **Location gating runs before cross-record collapsing.**

Collapse first and an arbitrary survivor may be the Canadian copy, after which
the location gate rejects it and a qualifying job has silently vanished — dedupe
destroying exactly the postings it exists to surface.
"""

from datetime import UTC, datetime, timedelta

import pytest

from winnow.dedupe import cluster_key, cluster_postings, merge_cluster
from winnow.gates import apply_structured_gates
from winnow.models import Board, CompInterval, CompSource, LocationClass, RemoteStatus
from winnow.sources.greenhouse import GreenhouseAdapter

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
TAILSCALE = Board(vendor="greenhouse", identifier={"slug": "tailscale"}, company="Tailscale")


@pytest.fixture
def tailscale_postings(fixtures):
    adapter = GreenhouseAdapter()
    return [
        adapter.normalize(job, TAILSCALE, now=NOW)
        for job in fixtures("greenhouse_tailscale_jobs.json")["jobs"]
    ]


def test_gating_first_then_collapsing_keeps_the_us_copy(tailscale_postings, profile):
    """The whole ordering rule, on the board that motivated it."""
    survivors = [
        posting
        for posting in tailscale_postings
        if apply_structured_gates(posting, profile).location_class is LocationClass.US_REMOTE
    ]
    assert len(tailscale_postings) == 55
    assert len(survivors) == 23

    clusters = cluster_postings(survivors)
    assert len(clusters) == 21

    analytics = [
        cluster for cluster in clusters if cluster.canonical.title == "Analytics Engineer, Data"
    ]
    assert len(analytics) == 1
    assert analytics[0].canonical.locations == ("Remote (United States) ".strip(),)


def test_collapsing_first_would_have_lost_it(tailscale_postings):
    """Stated as a test so the ordering cannot be quietly reversed later.

    Cluster without gating and the Canadian and US copies land in separate
    clusters anyway — because location class is part of the key. That is the
    mechanism that makes the safe order safe.
    """
    clusters = cluster_postings(tailscale_postings)
    analytics = [
        cluster for cluster in clusters if cluster.canonical.title == "Analytics Engineer, Data"
    ]
    assert len(analytics) == 2
    assert {cluster.key.location_class for cluster in analytics} == {
        LocationClass.US_REMOTE,
        LocationClass.NON_US,
    }


def test_the_key_separates_seniority(make_posting):
    director = make_posting(source_id="1", title="Director of Business Systems")
    senior = make_posting(source_id="2", title="Senior Director of Business Systems")
    assert cluster_key(director) != cluster_key(senior)
    assert len(cluster_postings([director, senior])) == 2


def test_the_same_title_at_two_companies_never_merges(make_posting):
    left = make_posting(source_id="1", company="Tailscale")
    right = make_posting(source_id="2", company="Grafana Labs")
    assert len(cluster_postings([left, right])) == 2


def test_one_role_from_two_sources_becomes_one_cluster(make_posting):
    ats = make_posting(source="greenhouse", source_id="1")
    aggregator = make_posting(
        source="adzuna",
        source_id="2",
        source_url="https://www.adzuna.com/details/12345",
        discovered_via="adzuna",
    )
    clusters = cluster_postings([aggregator, ats])
    assert len(clusters) == 1
    assert clusters[0].canonical.source == "greenhouse", "the source-proximate copy wins"


def test_a_predicted_figure_never_becomes_the_clusters_compensation(make_posting):
    """Merging by provenance is what stops a modelled number reaching a gate."""
    ats = make_posting(source="greenhouse", source_id="1", comp_source=CompSource.ABSENT)
    aggregator = make_posting(
        source="adzuna",
        source_id="2",
        comp_min=205000,
        comp_max=205000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.PREDICTED,
    )
    merged = merge_cluster(cluster_postings([ats, aggregator])[0])
    assert merged.source == "greenhouse"
    assert merged.comp_source is CompSource.PREDICTED
    assert merged.comp_max == 205000


def test_a_stated_figure_from_the_aggregator_is_taken(make_posting):
    ats = make_posting(source="greenhouse", source_id="1", comp_source=CompSource.ABSENT)
    aggregator = make_posting(
        source="adzuna",
        source_id="2",
        comp_min=210000,
        comp_max=245000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    merged = merge_cluster(cluster_postings([ats, aggregator])[0])
    assert merged.source_url == ats.source_url, "identity comes from the canonical copy"
    assert (merged.comp_min, merged.comp_max) == (210000, 245000)
    assert merged.comp_source is CompSource.STATED


def test_the_strongest_provenance_wins_field_by_field(make_posting):
    from winnow.models import RemoteSource

    weak = make_posting(
        source="greenhouse",
        source_id="1",
        remote=RemoteStatus.REMOTE,
        remote_source=RemoteSource.LOCATION_STRING,
        description_text=None,
        description_complete=False,
    )
    strong = make_posting(
        source="adzuna",
        source_id="2",
        remote=RemoteStatus.REMOTE,
        remote_source=RemoteSource.STRUCTURED,
        description_text="A complete description.",
        description_complete=True,
    )
    cluster = cluster_postings([weak, strong])[0]
    assert len(cluster.postings) == 2
    merged = merge_cluster(cluster)
    assert merged.source == "greenhouse", "identity still comes from the proximate copy"
    assert merged.remote_source is RemoteSource.STRUCTURED
    assert merged.description_complete is True


def test_an_ambiguous_pair_is_flagged_rather_than_merged(make_posting):
    """A false merge hides a real job permanently; a false split costs one line."""
    left = make_posting(source_id="1", title="Director of Business Systems")
    right = make_posting(source_id="2", title="Director of Business Systems & Data")
    clusters = cluster_postings([left, right])
    flagged = [pair for cluster in clusters for pair in cluster.ambiguous_with]
    assert len(clusters) == 2
    assert flagged, "the pair should be queued for confirmation, not decided"


def test_seniority_differences_are_split_outright_not_queried(make_posting):
    """No human should be asked whether Director and Senior Director are one job."""
    director = make_posting(source_id="1", title="Director of Business Systems")
    senior = make_posting(source_id="2", title="Senior Director of Business Systems")
    clusters = cluster_postings([director, senior])
    assert len(clusters) == 2
    assert not [pair for cluster in clusters for pair in cluster.ambiguous_with]


def test_a_repost_is_linked_and_counted_not_merged_away(conn, make_posting):
    """Recurring requisitions are one of the few computable ghost-job signals."""
    from winnow import dedupe, store

    company_id = store.insert_company(conn, "Example Co")
    original = make_posting(source_id="old-1", first_seen_at=NOW - timedelta(days=60))
    cluster_id = dedupe.persist_cluster(
        conn, cluster_postings([original])[0], company_id=company_id
    )
    conn.execute(
        "UPDATE postings SET disappeared_at = ? WHERE source_id = 'old-1'",
        ((NOW - timedelta(days=10)).isoformat(),),
    )

    repost = make_posting(source_id="new-1", first_seen_at=NOW)
    again = dedupe.persist_cluster(conn, cluster_postings([repost])[0], company_id=company_id)

    assert again == cluster_id
    row = conn.execute("SELECT repost_count FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    assert row["repost_count"] == 1
    linked = conn.execute(
        "SELECT previous_posting_id FROM postings WHERE source_id = 'new-1'"
    ).fetchone()
    assert linked["previous_posting_id"] is not None


def test_a_second_sighting_of_a_live_posting_is_not_a_repost(conn, make_posting):
    from winnow import dedupe, store

    company_id = store.insert_company(conn, "Example Co")
    posting = make_posting(source_id="same-1")
    cluster_id = dedupe.persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    dedupe.persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)

    row = conn.execute("SELECT repost_count FROM clusters WHERE id = ?", (cluster_id,)).fetchone()
    assert row["repost_count"] == 0
    assert conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 1


def test_a_decision_attaches_to_the_cluster_so_a_pass_stays_passed(conn, make_posting):
    """Otherwise passing on a role found at source does not stop it arriving via Adzuna."""
    from winnow import dedupe, store

    company_id = store.insert_company(conn, "Example Co")
    from_ats = make_posting(source="greenhouse", source_id="gh-1")
    cluster_id = dedupe.persist_cluster(
        conn, cluster_postings([from_ats])[0], company_id=company_id
    )
    conn.execute(
        "INSERT INTO decisions (cluster_id, decision, reason) VALUES (?, 'pass', 'comp_too_low')",
        (cluster_id,),
    )

    from_aggregator = make_posting(
        source="adzuna", source_id="az-1", source_url="https://www.adzuna.com/details/1"
    )
    same_cluster = dedupe.persist_cluster(
        conn, cluster_postings([from_aggregator])[0], company_id=company_id
    )

    assert same_cluster == cluster_id
    assert dedupe.is_decided(conn, cluster_id) is True
