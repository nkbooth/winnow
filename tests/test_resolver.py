"""Board resolution: pasted URL in, (vendor, identifier) out.

Measured on 2026-09-18: automated resolution tops out near 50% accuracy and
produces confidently wrong answers — a search for Element's board returned
Element Solutions' Lever board, which is indistinguishable from a correct
answer. So the primary path parses a URL a human pasted, and nothing here
guesses.
"""

from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from winnow import resolver, store
from winnow.resolver import ProbeResult, ProbeStatus


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "test.db")
    store.migrate(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize(
    ("url", "vendor", "identifier"),
    [
        ("https://boards.greenhouse.io/tailscale", "greenhouse", {"slug": "tailscale"}),
        (
            "https://job-boards.greenhouse.io/tailscale/jobs/4520139005",
            "greenhouse",
            {"slug": "tailscale"},
        ),
        ("https://jobs.lever.co/elementsolutions", "lever", {"slug": "elementsolutions"}),
        (
            "https://jobs.lever.co/elementsolutions/8ab0b1b1-1111-2222",
            "lever",
            {"slug": "elementsolutions"},
        ),
        ("https://jobs.ashbyhq.com/proton", "ashby", {"slug": "proton"}),
        ("https://jobs.ashbyhq.com/1password/some-posting-id", "ashby", {"slug": "1password"}),
        ("https://mullvad.teamtailor.com/jobs", "teamtailor", {"slug": "mullvad"}),
        ("https://apply.workable.com/acme-inc/", "workable", {"slug": "acme-inc"}),
        ("https://jobs.smartrecruiters.com/AcmeInc", "smartrecruiters", {"slug": "AcmeInc"}),
        (
            "https://redhat.wd5.myworkdayjobs.com/jobs",
            "workday",
            {"tenant": "redhat", "datacenter": "wd5", "site": "jobs"},
        ),
        (
            "https://redhat.wd5.myworkdayjobs.com/en-US/jobs/details/Junior-Consultant_R-059072",
            "workday",
            {"tenant": "redhat", "datacenter": "wd5", "site": "jobs"},
        ),
    ],
)
def test_parse_board_url(url, vendor, identifier):
    parsed = resolver.parse_board_url(url)
    assert parsed is not None
    assert (parsed.vendor, parsed.identifier) == (vendor, identifier)


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.proton.me/",
        "https://boards.greenhouse.io/",
        "https://example.com/jobs/tailscale",
        "not a url at all",
        "",
    ],
)
def test_unrecognised_urls_do_not_parse(url):
    """Failing loudly beats storing a plausible guess."""
    assert resolver.parse_board_url(url) is None


class FakeProbe:
    """A prober that returns whatever the test says the endpoint returned."""

    def __init__(self, result: ProbeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def probe(self, vendor: str, identifier: dict[str, str]) -> ProbeResult:
        self.calls.append((vendor, identifier))
        return self.result


def test_add_board_stores_it_after_one_successful_probe(conn):
    probe = FakeProbe(ProbeResult(status=ProbeStatus.OK, job_count=55, board_name="Tailscale"))

    board = resolver.add_board(
        conn, company="Tailscale", url="https://boards.greenhouse.io/tailscale", probe=probe
    )

    assert board.vendor == "greenhouse"
    assert len(probe.calls) == 1, "one request per endpoint per resolution"
    row = conn.execute("SELECT * FROM company_boards WHERE id = ?", (board.board_id,)).fetchone()
    assert row["source"] == "manual"
    assert row["status"] == "active"
    assert row["verified_at"] is not None


def test_add_board_rejects_a_url_it_cannot_parse(conn):
    probe = FakeProbe(ProbeResult(status=ProbeStatus.OK, job_count=1))
    with pytest.raises(resolver.UnrecognisedBoardURL):
        resolver.add_board(conn, company="Proton", url="https://careers.proton.me/", probe=probe)
    assert probe.calls == [], "nothing is probed until the URL is understood"


def test_add_board_refuses_a_404(conn):
    probe = FakeProbe(ProbeResult(status=ProbeStatus.NOT_FOUND))
    with pytest.raises(resolver.BoardNotFound):
        resolver.add_board(
            conn, company="Nobody", url="https://boards.greenhouse.io/nobody", probe=probe
        )
    assert conn.execute("SELECT count(*) AS n FROM company_boards").fetchone()["n"] == 0


def test_an_empty_board_is_stored_but_flagged(conn):
    """200-with-zero-jobs is ambiguous: correct and idle, or the wrong board.

    It is not the same as a 404 and must not be treated as one.
    """
    probe = FakeProbe(ProbeResult(status=ProbeStatus.EMPTY, job_count=0))
    board = resolver.add_board(
        conn, company="Quiet Co", url="https://jobs.lever.co/quietco", probe=probe
    )
    row = conn.execute("SELECT * FROM company_boards WHERE id = ?", (board.board_id,)).fetchone()
    assert row["status"] == "active"
    assert row["consecutive_empty_polls"] == 1


def test_adding_the_same_board_twice_is_refused(conn):
    probe = FakeProbe(ProbeResult(status=ProbeStatus.OK, job_count=10))
    resolver.add_board(
        conn, company="Tailscale", url="https://boards.greenhouse.io/tailscale", probe=probe
    )
    with pytest.raises(resolver.BoardAlreadyKnown):
        resolver.add_board(
            conn, company="Tailscale", url="https://boards.greenhouse.io/tailscale", probe=probe
        )


def test_three_consecutive_empty_polls_flag_a_board_stale(conn):
    """Slugs rot. A quiet board is surfaced for re-resolution, not dropped."""
    probe = FakeProbe(ProbeResult(status=ProbeStatus.OK, job_count=3))
    board = resolver.add_board(
        conn, company="Tailscale", url="https://boards.greenhouse.io/tailscale", probe=probe
    )

    for _ in range(2):
        resolver.record_poll(conn, board.board_id, job_count=0)
    assert resolver.board_status(conn, board.board_id) == "active"

    resolver.record_poll(conn, board.board_id, job_count=0)
    assert resolver.board_status(conn, board.board_id) == "stale"


def test_a_successful_poll_clears_the_empty_streak(conn):
    probe = FakeProbe(ProbeResult(status=ProbeStatus.OK, job_count=3))
    board = resolver.add_board(
        conn, company="Tailscale", url="https://boards.greenhouse.io/tailscale", probe=probe
    )
    resolver.record_poll(conn, board.board_id, job_count=0)
    resolver.record_poll(conn, board.board_id, job_count=7)

    row = conn.execute(
        "SELECT consecutive_empty_polls, last_ok_at FROM company_boards WHERE id = ?",
        (board.board_id,),
    ).fetchone()
    assert row["consecutive_empty_polls"] == 0
    assert row["last_ok_at"] is not None


def test_every_pollable_vendor_has_an_adapter():
    """A vendor with a listing endpoint and no adapter would fail silently at poll time."""
    from winnow.sources import ADAPTERS
    from winnow.sources.registry import VENDORS

    pollable = {name for name, vendor in VENDORS.items() if vendor.list_url_template}
    assert pollable - set(ADAPTERS) == set(), (
        "every vendor with a listing endpoint must have an adapter; "
        "a vendor without one is recorded as unpollable instead"
    )
    # careers_page is the one adapter not addressed by a URL template: its sites
    # are hand-written parsers over whole pages, one URL each, held in the
    # adapter rather than the registry.
    assert set(ADAPTERS) - pollable == {"careers_page"}


def test_a_workable_board_is_pollable():
    """Corrected 2026-09-19. The earlier finding here was wrong.

    This test previously asserted that adding a Workable board *fails*, on the
    reasoning that the public JSON returns no postings for any board. Seven
    boards were measured and all seven answered with an empty array — but they
    were empty because those employers had no open roles, which is not the same
    thing as an endpoint that cannot be read. Nuvei's board answers the same URL
    with 75 postings.

    The mistake is worth naming because it is the exact failure this project
    keeps designing against, made in the other direction: "returns nothing" and
    "cannot be read" were treated as one state.
    """
    from winnow.sources.registry import VENDORS

    vendor = VENDORS["workable"]
    assert vendor.list_url({"slug": "fastmail-1"}).endswith("accounts/fastmail-1?details=true")
    assert vendor.name_key == "name", "the board states its own employer name"


def test_an_empty_board_can_still_be_added(conn):
    """Fastmail is on Workable with no open roles today.

    A board with nothing on it is a normal state, not a failure. Refusing it
    would mean a company can only be tracked while it happens to be hiring —
    which is backwards, since the point is to notice when that changes.
    """
    board = resolver.add_board(
        conn,
        company="Fastmail",
        url="https://apply.workable.com/fastmail-1",
        probe=_StubProbe(resolver.ProbeResult(status=resolver.ProbeStatus.EMPTY)),
    )
    assert board.vendor == "workable"
    assert board.identifier == {"slug": "fastmail-1"}


def test_a_careers_page_url_resolves():
    from winnow.sources.registry import match_board_url

    vendor, identifier = match_board_url("https://nextcloud.com/jobs/#openpositions")
    assert vendor.name == "careers_page"
    assert identifier == {"site": "nextcloud"}


def test_a_careers_page_is_verified_by_running_its_parser():
    """There is no JSON to probe, so the probe proves the parser still works.

    That is the verification that matters for a scraped page: not that the URL
    answers — a redesigned page answers fine — but that what comes back is still
    recognisable to the parser written against it.
    """
    page = Path("tests/fixtures/careers_nextcloud.html").read_text(errors="replace")
    probe = resolver.HttpProbe(client=_page_client(page))

    result = probe.probe("careers_page", {"site": "nextcloud"})

    assert result.status is resolver.ProbeStatus.OK
    assert result.job_count >= 10
    assert result.board_name == "Nextcloud"


def test_a_careers_page_that_stopped_parsing_fails_the_probe():
    probe = resolver.HttpProbe(client=_page_client("<html><body>Redesigned.</body></html>"))

    result = probe.probe("careers_page", {"site": "nextcloud"})

    assert result.status is resolver.ProbeStatus.ERROR
    assert "openpositions" in (result.detail or "")


def test_a_careers_page_with_no_roles_probes_empty():
    """Signal says it is not hiring, and that is a readable answer."""
    page = Path("tests/fixtures/careers_signal.html").read_text(errors="replace")
    probe = resolver.HttpProbe(client=_page_client(page))

    assert probe.probe("careers_page", {"site": "signal"}).status is resolver.ProbeStatus.EMPTY


def _page_client(body: str) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    )


@dataclass(frozen=True)
class _StubProbe:
    """Returns one canned result, for tests about what add_board does with it."""

    result: resolver.ProbeResult

    def probe(self, vendor: str, identifier: dict[str, str]) -> resolver.ProbeResult:
        return self.result


def test_adding_the_same_board_twice_costs_no_request(conn):
    """Re-importing a seed list must not re-probe forty boards.

    The duplicate check runs before the HTTP call, so a second import is free
    as well as harmless. A vendor being asked the same question forty times
    because somebody ran a command twice is how a polite tool becomes a rude
    one.
    """
    probe = _CountingProbe()
    url = "https://boards.greenhouse.io/tailscale"

    resolver.add_board(conn, company="Tailscale", url=url, probe=probe)
    with pytest.raises(resolver.BoardAlreadyKnown):
        resolver.add_board(conn, company="Tailscale", url=url, probe=probe)

    assert probe.calls == 1, "the second add must not reach the network"
    assert conn.execute("SELECT count(*) AS n FROM company_boards").fetchone()["n"] == 1


def test_the_same_board_under_a_different_name_is_still_a_duplicate(conn):
    """Identity is the board, not what somebody called the company."""
    probe = _CountingProbe()
    url = "https://boards.greenhouse.io/tailscale"
    resolver.add_board(conn, company="Tailscale", url=url, probe=probe)

    with pytest.raises(resolver.BoardAlreadyKnown):
        resolver.add_board(conn, company="Tailscale Inc", url=url, probe=probe)

    assert conn.execute("SELECT count(*) AS n FROM company_boards").fetchone()["n"] == 1


def test_one_company_may_have_two_boards(conn):
    """Some employers post to two ATSs, or keep a second board per region."""
    probe = _CountingProbe()
    resolver.add_board(
        conn, company="Example", url="https://boards.greenhouse.io/example", probe=probe
    )
    resolver.add_board(conn, company="Example", url="https://jobs.ashbyhq.com/example", probe=probe)

    assert conn.execute("SELECT count(*) AS n FROM company_boards").fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 1


@dataclass
class _CountingProbe:
    """Answers OK and counts how often it was asked."""

    calls: int = 0

    def probe(self, vendor: str, identifier: dict[str, str]) -> resolver.ProbeResult:
        self.calls += 1
        return resolver.ProbeResult(status=resolver.ProbeStatus.OK, job_count=3)
