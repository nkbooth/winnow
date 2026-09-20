"""The poll pipeline: fetch, gate, cluster, then fetch detail for survivors.

The order is the design, not an implementation detail:

    fetch -> normalize -> LOCATION + CHEAP GATES -> cluster -> detail -> score

Detail sits after clustering so Workday's N+1 is paid once per cluster rather
than once per duplicate, and a source that fails must not fail the run — the
digest has to be able to say "Greenhouse did not answer" rather than "no new
matches", because silence and breakage must never look alike.
"""

import httpx
import pytest

from winnow import learning, pipeline, store
from winnow.models import Board, CompInterval, CompSource, Posting, RemoteStatus
from winnow.pipeline import run_poll


class FakeAdapter:
    """An adapter that serves canned payloads and counts what was asked of it."""

    def __init__(self, name, rows, *, detail=None, error=None):
        self.name = name
        self._rows = rows
        self._detail = detail
        self._error = error
        self.list_calls = 0
        self.detail_calls: list[str] = []

    def fetch_list(self, board):
        self.list_calls += 1
        if self._error is not None:
            raise self._error
        return self._rows

    def normalize(self, raw, board, *, now=None):
        from datetime import UTC, datetime

        from winnow.normalize import fingerprint

        described = raw.get("description")
        return Posting(
            source=self.name,
            source_id=raw["id"],
            company=board.company,
            title=raw["title"],
            source_url=f"https://example.test/{raw['id']}",
            discovered_via=self.name,
            first_seen_at=now or datetime.now(UTC),
            fingerprint=fingerprint(board.company, raw["title"], "US_REMOTE"),
            remote=RemoteStatus(raw.get("remote", "REMOTE")),
            locations=(raw.get("location", "Remote (United States)"),),
            description_text=described,
            description_complete=described is not None,
            raw=raw,
        )

    def fetch_detail(self, posting):
        self.detail_calls.append(posting.source_id)
        return self._detail


@pytest.fixture
def board(conn):
    company_id = store.insert_company(conn, "Example Co")
    board_id = store.insert_board(
        conn,
        company_id=company_id,
        vendor="greenhouse",
        identifier={"slug": "example"},
        source="manual",
    )
    return Board(
        vendor="greenhouse",
        identifier={"slug": "example"},
        company="Example Co",
        company_id=company_id,
        board_id=board_id,
    )


def test_a_qualifying_posting_is_persisted_as_a_cluster(conn, board, profile):
    adapter = FakeAdapter(
        "greenhouse",
        [{"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."}],
    )
    report = run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)

    assert len(report.clusters) == 1
    assert conn.execute("SELECT count(*) AS n FROM clusters").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 1


def test_gated_postings_never_reach_the_store(conn, board, profile):
    adapter = FakeAdapter(
        "greenhouse",
        [
            {"id": "1", "title": "Senior Backend Engineer", "description": "Go and Rust."},
            {"id": "2", "title": "Director of Business Systems", "description": "Own BizOps."},
        ],
    )
    report = run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)

    assert len(report.clusters) == 1
    assert report.tally["title_off_target"] == 1
    titles = [row["title"] for row in conn.execute("SELECT title FROM postings")]
    assert titles == ["Director of Business Systems"]


def test_the_rejection_tally_is_recorded_against_the_run(conn, board, profile):
    """The digest distinguishes filtering from breakage, so the counts persist."""
    adapter = FakeAdapter(
        "greenhouse",
        [
            {"id": "1", "title": "Director of Business Systems", "remote": "HYBRID"},
            {"id": "2", "title": "Director of Business Operations", "remote": "ONSITE"},
        ],
    )
    run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)

    import json

    row = conn.execute("SELECT outcome, rejection_tally FROM poll_runs").fetchone()
    assert row["outcome"] == "ok"
    assert json.loads(row["rejection_tally"]) == {"hybrid_required": 1, "onsite_required": 1}


def test_detail_is_fetched_once_per_cluster_not_once_per_posting(conn, board, profile):
    """Red Hat lists 148 postings; the N+1 is what makes laziness necessary."""
    adapter = FakeAdapter(
        "greenhouse",
        [
            {"id": "a", "title": "Director of Business Systems"},
            {"id": "b", "title": "Director, Business Systems"},
        ],
        detail={"description": "Own and build out the BizOps function."},
    )
    report = run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)

    assert len(report.clusters) == 1, "both spellings are one job"
    assert len(adapter.detail_calls) == 1


def test_detail_is_not_fetched_for_a_complete_payload(conn, board, profile):
    adapter = FakeAdapter(
        "greenhouse",
        [{"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."}],
        detail={"description": "should not be requested"},
    )
    run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)
    assert adapter.detail_calls == []


def test_detail_is_never_fetched_for_a_gated_posting(conn, board, profile):
    adapter = FakeAdapter(
        "greenhouse",
        [{"id": "1", "title": "Senior Backend Engineer"}],
        detail={"description": "should not be requested"},
    )
    run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)
    assert adapter.detail_calls == []


def test_a_failing_board_does_not_fail_the_run(conn, board, profile):
    """Twelve boards polled with one 500ing is eleven boards of data plus a flag."""
    company_id = store.insert_company(conn, "Other Co")
    store.insert_board(
        conn,
        company_id=company_id,
        vendor="lever",
        identifier={"slug": "other"},
        source="manual",
    )
    other = Board(
        vendor="lever",
        identifier={"slug": "other"},
        company="Other Co",
        company_id=company_id,
    )

    broken = FakeAdapter(
        "lever",
        [],
        error=httpx.HTTPStatusError("500", request=None, response=None),
    )
    working = FakeAdapter(
        "greenhouse",
        [{"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."}],
    )
    adapters = {"lever": broken, "greenhouse": working}

    report = run_poll(
        conn, profile, [board, other], adapter_factory=lambda vendor: adapters[vendor]
    )

    assert len(report.clusters) == 1, "the working board's data is kept"
    outcomes = {
        row["source"]: row["outcome"]
        for row in conn.execute("SELECT source, outcome FROM poll_runs")
    }
    assert outcomes == {"greenhouse": "ok", "lever": "failed"}
    assert report.failed_sources == ("lever",)


def test_an_empty_board_is_recorded_and_counted_toward_staleness(conn, board, profile):
    adapter = FakeAdapter("greenhouse", [])
    run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)

    row = conn.execute(
        "SELECT consecutive_empty_polls FROM company_boards WHERE id = ?", (board.board_id,)
    ).fetchone()
    assert row["consecutive_empty_polls"] == 1


def test_a_cluster_already_decided_is_not_offered_again(conn, board, profile):
    """Passing on a role at source has to stop it arriving from anywhere else."""
    adapter = FakeAdapter(
        "greenhouse",
        [{"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."}],
    )
    first = run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)
    cluster_id = first.clusters[0].cluster_id
    conn.execute(
        "INSERT INTO decisions (cluster_id, decision, reason) VALUES (?, 'pass', 'timing')",
        (cluster_id,),
    )

    second = run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)
    assert second.clusters == ()
    assert second.suppressed == 1


def test_a_board_whose_vendor_has_no_adapter_is_reported_not_raised(conn, profile):
    """Teamtailor boards can be recorded but not polled. That is a fact to report."""
    company_id = store.insert_company(conn, "Mullvad")
    store.insert_board(
        conn,
        company_id=company_id,
        vendor="teamtailor",
        identifier={"slug": "mullvad"},
        source="manual",
    )
    board = Board(
        vendor="teamtailor",
        identifier={"slug": "mullvad"},
        company="Mullvad",
        company_id=company_id,
    )

    def factory(vendor):
        raise KeyError(vendor)

    report = run_poll(conn, profile, [board], adapter_factory=factory)

    assert report.failed_sources == ("teamtailor",)
    assert conn.execute("SELECT outcome FROM poll_runs").fetchone()["outcome"] == "failed"


def _poll(conn, profile, board, adapter):
    return run_poll(conn, profile, [board], adapter_factory=lambda vendor: adapter)


def test_a_posting_that_leaves_the_board_is_marked_gone(conn, board, profile):
    """Nothing set disappeared_at for the first day of this system's life.

    Three things depend on it and were all quietly inert: repost detection, the
    180-day prune, and the digest's ability to notice a role has closed.
    """
    both = [
        {"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."},
        {"id": "2", "title": "Director of Revenue Operations", "description": "Own RevOps."},
    ]
    _poll(conn, profile, board, FakeAdapter("greenhouse", both))
    _poll(conn, profile, board, FakeAdapter("greenhouse", both[:1]))

    gone = dict(conn.execute("SELECT source_id, disappeared_at FROM postings").fetchall())
    assert gone["1"] is None, "still listed"
    assert gone["2"] is not None, "no longer listed"


def test_postings_record_the_board_they_came_from(conn, board, profile):
    """Disappearance is per board; without this the update has nothing to key on."""
    _poll(
        conn,
        profile,
        board,
        FakeAdapter(
            "greenhouse",
            [{"id": "1", "title": "Director of Business Systems", "description": "x"}],
        ),
    )
    row = conn.execute("SELECT board_id FROM postings WHERE source_id = '1'").fetchone()
    assert row["board_id"] == board.board_id


def test_a_failed_poll_never_declares_anything_gone(conn, board, profile):
    """A 500 is not evidence that a company stopped hiring."""
    import httpx

    rows = [{"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."}]
    _poll(conn, profile, board, FakeAdapter("greenhouse", rows))
    _poll(
        conn,
        profile,
        board,
        FakeAdapter(
            "greenhouse", [], error=httpx.HTTPStatusError("500", request=None, response=None)
        ),
    )

    row = conn.execute("SELECT disappeared_at FROM postings WHERE source_id = '1'").fetchone()
    assert row["disappeared_at"] is None


def test_an_empty_board_never_declares_anything_gone(conn, board, profile):
    """200-with-zero-jobs is ambiguous, and it is already tracked as staleness.

    Concluding from it that every role closed would hide a whole company on one
    bad response.
    """
    rows = [{"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."}]
    _poll(conn, profile, board, FakeAdapter("greenhouse", rows))
    _poll(conn, profile, board, FakeAdapter("greenhouse", []))

    row = conn.execute("SELECT disappeared_at FROM postings WHERE source_id = '1'").fetchone()
    assert row["disappeared_at"] is None


def test_a_posting_that_comes_back_is_no_longer_gone(conn, board, profile):
    """Same id returning is a board flapping, not a repost."""
    rows = [{"id": "1", "title": "Director of Business Systems", "description": "Own BizOps."}]
    other = [{"id": "2", "title": "Director of Revenue Operations", "description": "Own RevOps."}]

    _poll(conn, profile, board, FakeAdapter("greenhouse", rows))
    _poll(conn, profile, board, FakeAdapter("greenhouse", other))
    assert (
        conn.execute("SELECT disappeared_at FROM postings WHERE source_id = '1'").fetchone()[
            "disappeared_at"
        ]
        is not None
    )

    _poll(conn, profile, board, FakeAdapter("greenhouse", rows))
    assert (
        conn.execute("SELECT disappeared_at FROM postings WHERE source_id = '1'").fetchone()[
            "disappeared_at"
        ]
        is None
    )


def test_a_posting_that_starts_failing_a_gate_stops_being_surfaced(conn, profile, make_posting):
    """A posting can pass today and fail tomorrow, and the queue must notice.

    Gated postings are never stored, so when one that was already stored starts
    failing — an employer adds a salary below the floor, or an onsite
    requirement appears, or a bug that hid the comp gets fixed — nothing writes
    to the existing row. It kept its old values and kept surfacing.

    Seen live: a ClickHouse role sat in the queue at 90 reading 'comp not
    disclosed' after the poll that revealed it pays $110-165K against a
    $200,000 floor.
    """
    company_id = store.insert_company(conn, "ClickHouse")
    board = Board(
        vendor="ashby",
        identifier={"slug": "clickhouse"},
        company="ClickHouse",
        company_id=company_id,
        board_id=store.insert_board(
            conn,
            company_id=company_id,
            vendor="ashby",
            identifier={"slug": "clickhouse"},
            source="manual",
        ),
    )

    passing = make_posting(
        company="ClickHouse",
        title="Director of Business Systems",
        source_id="e0a5",
        description_complete=True,
    )
    adapter = _StubAdapter([passing])
    report = pipeline.run_poll(conn, profile, [board], adapter_factory=lambda _v: adapter)
    assert report.clusters, "it passed the first time"

    # Scored, because the review queue ignores clusters that have none — an
    # unscored cluster would make the assertion below pass without meaning it.
    from winnow.scoring import BreakdownLine, ScoreRecord

    learning.record_score(
        conn,
        report.clusters[0].cluster_id,
        ScoreRecord(
            score=90,
            breakdown=(BreakdownLine("growth_signal", 1.0, 20, 20.0, "own it", False),),
            vetoes=(),
            flags=(),
            unverified=(),
            why_fits="Owns the function.",
            concern="None.",
            vetoed=False,
            model="stub",
            prompt_version="1",
            rubric_version=profile.rubric_version,
        ),
    )
    from winnow.review import data as review_data

    assert [r.title for r in review_data.queue(conn, profile=profile)] == [
        "Director of Business Systems"
    ], "it is surfaced before anything changes"

    # The same posting, now stating a salary far below the floor.
    failing = make_posting(
        company="ClickHouse",
        title="Director of Business Systems",
        source_id="e0a5",
        description_complete=True,
        comp_min=110000,
        comp_max=165000,
        comp_interval=CompInterval.YEAR,
        comp_source=CompSource.STATED,
    )
    adapter._postings = [failing]
    pipeline.run_poll(conn, profile, [board], adapter_factory=lambda _v: adapter)

    surfaced = [row.title for row in review_data.queue(conn, profile=profile)]
    assert "Director of Business Systems" not in surfaced


class _StubAdapter:
    """Returns fixed postings, so a poll can be replayed with different data."""

    name = "ashby"

    def __init__(self, postings):
        self._postings = postings

    def fetch_list(self, board):
        return [{"id": p.source_id} for p in self._postings]

    def normalize(self, raw, board, *, now=None):
        return next(p for p in self._postings if p.source_id == raw["id"])

    def fetch_detail(self, posting):
        return None
