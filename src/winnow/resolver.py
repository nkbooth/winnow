"""Company to (vendor, board identifier) resolution.

Manual-first by measurement, not by preference. Probing the big three against
six known companies resolved three, guessed the wrong vendor twice, and a search
for Element's board returned Element Solutions' Lever board on the first query —
confidently wrong, and structurally indistinguishable from a correct answer. The
seed list is about twenty-five companies, which is twenty-five minutes of
pasting URLs. Elaborate discovery would trade that for months of silently
polling the wrong employer.

So: a human pastes a URL, this module parses it, hits the endpoint once to
confirm it answers, and stores it with its provenance.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

import httpx

from winnow import store
from winnow.models import Board
from winnow.sources.base import request_headers
from winnow.sources.careers_page import SITES, CareersPageAdapter, UnknownPageStructure
from winnow.sources.registry import VENDORS, Vendor, match_board_url

EMPTY_POLLS_BEFORE_STALE = 3


class ResolverError(Exception):
    """Base class for resolution failures."""


class UnrecognisedBoardURL(ResolverError):
    """The pasted URL matches no vendor in the registry."""


class BoardNotFound(ResolverError):
    """The endpoint answered 404; the identifier is wrong."""


class BoardAlreadyKnown(ResolverError):
    """This (vendor, identifier) pair is already recorded."""


class ProbeFailed(ResolverError):
    """The endpoint could not be reached, or the vendor publishes no JSON."""


class ProbeStatus(StrEnum):
    """Outcome of a single verification request.

    ``EMPTY`` is deliberately distinct from ``NOT_FOUND``: a board that answers
    with zero jobs may be correct and idle, or may be the wrong board, and
    collapsing the two would either discard a good board or keep a bad one.
    """

    OK = "ok"
    EMPTY = "empty"
    NOT_FOUND = "not_found"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ProbeResult:
    """What one verification request established.

    Attributes:
        status: Outcome of the request.
        job_count: Postings the endpoint returned.
        board_name: The board's own name, where the vendor states one. Only
            Greenhouse does, which is why it is the only vendor whose match may
            be accepted without a human confirming it.
        detail: Human-readable note for the error cases.
    """

    status: ProbeStatus
    job_count: int = 0
    board_name: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ParsedBoard:
    """A board URL, understood."""

    vendor: str
    identifier: dict[str, str]


class BoardProbe(Protocol):
    """Verifies that a board identifier addresses a real, answering board."""

    def probe(self, vendor: str, identifier: dict[str, str]) -> ProbeResult:
        """Make one request against the board's listing endpoint."""
        ...


def parse_board_url(url: str) -> ParsedBoard | None:
    """Parse a pasted board URL into a vendor and identifier.

    Args:
        url: A board or posting URL.

    Returns:
        The parsed board, or ``None`` when no registered vendor recognises it.
        ``None`` is the honest answer for a client-rendered careers page, which
        is the common case — four of five measured — and the reason this is a
        paste-a-URL flow rather than a scraper.
    """
    matched = match_board_url(url)
    if matched is None:
        return None
    vendor, identifier = matched
    return ParsedBoard(vendor=vendor.name, identifier=identifier)


def add_board(
    conn: sqlite3.Connection,
    *,
    company: str,
    url: str,
    probe: BoardProbe,
) -> Board:
    """Record a company's board from a pasted URL.

    Args:
        conn: An open connection.
        company: Company name as it should be displayed.
        url: The board URL.
        probe: Verifier used for the single confirmation request.

    Returns:
        The stored board.

    Raises:
        UnrecognisedBoardURL: If no vendor pattern matches the URL.
        BoardAlreadyKnown: If the pair is already recorded. Checked before the
            request, so re-adding a board costs nobody an HTTP call.
        BoardNotFound: If the endpoint answered 404.
        ProbeFailed: If the endpoint errored or the vendor has no public JSON.
    """
    parsed = parse_board_url(url)
    if parsed is None:
        raise UnrecognisedBoardURL(url)

    encoded = store.encode_identifier(parsed.identifier)
    existing = conn.execute(
        "SELECT id FROM company_boards WHERE vendor = ? AND identifier = ?",
        (parsed.vendor, encoded),
    ).fetchone()
    if existing is not None:
        raise BoardAlreadyKnown(f"{parsed.vendor} {encoded} is board {existing['id']}")

    result = probe.probe(parsed.vendor, parsed.identifier)
    if result.status is ProbeStatus.NOT_FOUND:
        raise BoardNotFound(f"{parsed.vendor} {encoded} returned 404")
    if result.status in (ProbeStatus.ERROR, ProbeStatus.UNSUPPORTED):
        raise ProbeFailed(result.detail or result.status.value)

    now = _now()
    company_id = store.insert_company(conn, company)
    board_id = store.insert_board(
        conn,
        company_id=company_id,
        vendor=parsed.vendor,
        identifier=parsed.identifier,
        source="manual",
        confidence=1.0,
        verified_at=now,
    )
    if result.status is ProbeStatus.OK:
        conn.execute("UPDATE company_boards SET last_ok_at = ? WHERE id = ?", (now, board_id))
    else:
        conn.execute(
            "UPDATE company_boards SET consecutive_empty_polls = 1 WHERE id = ?", (board_id,)
        )

    return Board(
        vendor=parsed.vendor,
        identifier=parsed.identifier,
        company=company,
        company_id=company_id,
        board_id=board_id,
    )


def record_poll(conn: sqlite3.Connection, board_id: int, *, job_count: int) -> str:
    """Record the result of polling a board, and flag it stale if it stays empty.

    Companies switch ATS, so a slug that worked last month can quietly address
    nothing. Three consecutive empty polls flag the board for re-resolution in
    the review TUI — not silently dropped, and not retried forever.

    Args:
        conn: An open connection.
        board_id: The board polled.
        job_count: Postings the poll returned.

    Returns:
        The board's status after recording.
    """
    if job_count > 0:
        conn.execute(
            "UPDATE company_boards SET consecutive_empty_polls = 0, last_ok_at = ?, "
            "status = 'active' WHERE id = ?",
            (_now(), board_id),
        )
        return board_status(conn, board_id)

    conn.execute(
        "UPDATE company_boards SET consecutive_empty_polls = consecutive_empty_polls + 1 "
        "WHERE id = ?",
        (board_id,),
    )
    row = conn.execute(
        "SELECT consecutive_empty_polls FROM company_boards WHERE id = ?", (board_id,)
    ).fetchone()
    if row is not None and row["consecutive_empty_polls"] >= EMPTY_POLLS_BEFORE_STALE:
        conn.execute("UPDATE company_boards SET status = 'stale' WHERE id = ?", (board_id,))
    return board_status(conn, board_id)


def board_status(conn: sqlite3.Connection, board_id: int) -> str:
    """Return a board's current status.

    Args:
        conn: An open connection.
        board_id: The board's row id.

    Returns:
        ``active``, ``stale``, or ``retired``.

    Raises:
        KeyError: If no board has that id.
    """
    row = conn.execute("SELECT status FROM company_boards WHERE id = ?", (board_id,)).fetchone()
    if row is None:
        raise KeyError(board_id)
    return str(row["status"])


class HttpProbe:
    """Verifies a board over HTTP, one request per resolution.

    These are public endpoints used as intended, so the client identifies itself
    and asks once. No retry loop, no concurrency, no polling for confirmation.
    """

    def __init__(self, timeout: float = 15.0, client: httpx.Client | None = None) -> None:
        self._timeout = timeout
        # Only the careers-page path uses it; the JSON path predates injection
        # and calls httpx directly.
        self._client = client

    def probe(self, vendor: str, identifier: dict[str, str]) -> ProbeResult:
        """Request a board's listing endpoint and interpret the response.

        Args:
            vendor: Registry vendor name.
            identifier: The board's identifier.

        Returns:
            What the endpoint established. Network and decoding failures come
            back as ``ERROR`` rather than raising, so one bad board cannot end a
            resolution session.
        """
        if vendor == CareersPageAdapter.name:
            return self._probe_page(identifier)

        descriptor = VENDORS[vendor]
        if descriptor.list_url_template is None:
            return ProbeResult(
                status=ProbeStatus.UNSUPPORTED,
                detail=f"{vendor} publishes no unauthenticated listing endpoint",
            )

        url = descriptor.list_url(identifier)
        headers = request_headers()
        try:
            if descriptor.method == "POST":
                response = httpx.post(
                    url,
                    headers=headers,
                    json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
                    timeout=self._timeout,
                )
            else:
                response = httpx.get(url, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as error:
            return ProbeResult(status=ProbeStatus.ERROR, detail=str(error))

        if response.status_code == 404:
            return ProbeResult(status=ProbeStatus.NOT_FOUND)
        if response.status_code >= 400:
            return ProbeResult(status=ProbeStatus.ERROR, detail=f"HTTP {response.status_code}")

        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            return ProbeResult(status=ProbeStatus.ERROR, detail=f"malformed body: {error}")

        postings = descriptor.postings_in(payload)
        name = self._board_name(descriptor, identifier, headers)
        status = ProbeStatus.OK if postings else ProbeStatus.EMPTY
        return ProbeResult(status=status, job_count=len(postings), board_name=name)

    def _board_name(
        self, descriptor: Vendor, identifier: dict[str, str], headers: dict
    ) -> str | None:
        if descriptor.name_url_template is None or descriptor.name_key is None:
            return None
        try:
            response = httpx.get(
                descriptor.name_url_template.format(**identifier),
                headers=headers,
                timeout=self._timeout,
            )
            if response.status_code >= 400:
                return None
            value = response.json().get(descriptor.name_key)
        except httpx.HTTPError, json.JSONDecodeError:
            return None
        return str(value) if value else None

    def _probe_page(self, identifier: dict[str, str]) -> ProbeResult:
        """Verify a hand-parsed careers page by parsing it.

        Args:
            identifier: The board's identifier, naming a site parser.

        Returns:
            What the page established. A page that answers but no longer parses
            is an ERROR, not an empty board — the difference between a company
            that stopped hiring and a scraper that stopped working is the whole
            reason these sites are allowed in at all.
        """
        site = SITES.get(identifier.get("site", ""))
        if site is None:
            return ProbeResult(
                status=ProbeStatus.UNSUPPORTED,
                detail=f"no careers-page parser for {identifier.get('site')!r}",
            )

        adapter = CareersPageAdapter(client=self._client)
        try:
            rows = list(
                adapter.fetch_list(
                    Board(
                        vendor=CareersPageAdapter.name, identifier=identifier, company=site.company
                    )
                )
            )
        except UnknownPageStructure as error:
            return ProbeResult(status=ProbeStatus.ERROR, detail=str(error))
        except httpx.HTTPError as error:
            return ProbeResult(status=ProbeStatus.ERROR, detail=str(error))

        return ProbeResult(
            status=ProbeStatus.OK if rows else ProbeStatus.EMPTY,
            job_count=len(rows),
            board_name=site.company,
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
