"""SQLite persistence and schema migration.

Connections are configured the same way everywhere: foreign keys on (SQLite
leaves them off by default, which would make every ``REFERENCES`` clause in the
schema decorative), WAL so the review TUI can read while a poll writes, and
``Row`` access so callers use column names.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from winnow.migrations import MIGRATIONS
from winnow.models import (
    Board,
    CompInterval,
    CompSource,
    EmploymentType,
    EmploymentTypeSource,
    PostedAtPrecision,
    Posting,
    RemoteSource,
    RemoteStatus,
)

LATEST_VERSION: int = max(version for version, _ in MIGRATIONS)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection to the store, creating the file if needed.

    Args:
        path: Location of the SQLite database.

    Returns:
        A connection with foreign keys enforced and rows accessible by name.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version the database is currently at."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration the database has not seen yet.

    Args:
        conn: An open connection.

    Returns:
        The versions applied by this call, in order; empty when already current.

    Raises:
        sqlite3.Error: If a migration fails, in which case it is rolled back and
            the recorded version is left untouched.
    """
    applied: list[int] = []
    current = schema_version(conn)
    for version, sql in sorted(MIGRATIONS):
        if version <= current:
            continue
        # BEGIN and COMMIT go inside the script: executescript commits any
        # pending transaction before it runs, so wrapping it from out here would
        # leave the DDL unprotected. PRAGMA cannot be parameterised, and the
        # version is an int from a module constant, never from a caller.
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version:d};\nCOMMIT;")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def encode_identifier(identifier: dict[str, str]) -> str:
    """Encode a board identifier canonically for storage and comparison.

    Keys are sorted so that two spellings of one Workday triple collide on the
    unique index instead of creating a second board for the same company.

    Args:
        identifier: Vendor-specific board identifier, e.g. ``{"slug": "tailscale"}``
            or ``{"tenant": ..., "datacenter": ..., "site": ...}``.

    Returns:
        Compact JSON with sorted keys.
    """
    return json.dumps(identifier, sort_keys=True, separators=(",", ":"))


def insert_company(conn: sqlite3.Connection, name: str) -> int:
    """Insert a company, or return the id of the existing row with that name.

    Args:
        conn: An open connection.
        name: Company name as it should be displayed.

    Returns:
        The company's row id.
    """
    conn.execute("INSERT OR IGNORE INTO companies (name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def insert_board(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    vendor: str,
    identifier: dict[str, str],
    source: str,
    confidence: float | None = None,
    verified_at: str | None = None,
) -> int:
    """Record a company's board on one ATS vendor.

    Args:
        conn: An open connection.
        company_id: Owning company.
        vendor: Registry name, e.g. ``greenhouse`` or ``workday``.
        identifier: Vendor-specific identifier mapping.
        source: How the board was established — ``manual``,
            ``greenhouse_name_match``, or ``human_confirmed_probe``. Provenance
            matters: when a board goes quiet, the first question is whether it
            was ever the right board.
        confidence: Optional confidence for a probed match.
        verified_at: Timestamp of the last successful verification.

    Returns:
        The board's row id.

    Raises:
        sqlite3.IntegrityError: If this (vendor, identifier) is already recorded.
    """
    cursor = conn.execute(
        "INSERT INTO company_boards (company_id, vendor, identifier, source, confidence, "
        "verified_at) VALUES (?, ?, ?, ?, ?, ?)",
        (company_id, vendor, encode_identifier(identifier), source, confidence, verified_at),
    )
    return int(cursor.lastrowid)


def board_identifier(conn: sqlite3.Connection, board_id: int) -> dict[str, str]:
    """Return a board's decoded vendor identifier.

    Args:
        conn: An open connection.
        board_id: The board's row id.

    Returns:
        The identifier mapping as stored.

    Raises:
        KeyError: If no board has that id.
    """
    row = conn.execute("SELECT identifier FROM company_boards WHERE id = ?", (board_id,)).fetchone()
    if row is None:
        raise KeyError(board_id)
    return json.loads(row["identifier"])


def comp_tier(conn: sqlite3.Connection, company_id: int) -> str:
    """Return which compensation floor applies to a company.

    Args:
        conn: An open connection.
        company_id: The company's row id.

    Returns:
        ``mid_market``, ``large_corporate`` or ``crypto``.

    Raises:
        KeyError: If no company has that id.
    """
    row = conn.execute("SELECT comp_tier FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        raise KeyError(company_id)
    return str(row["comp_tier"])


def set_comp_tier(conn: sqlite3.Connection, company_id: int, tier: str) -> None:
    """Record which compensation floor applies to a company.

    Args:
        conn: An open connection.
        company_id: The company's row id.
        tier: ``mid_market``, ``large_corporate`` or ``crypto``.

    Raises:
        sqlite3.IntegrityError: If the tier is not one the schema recognises.
    """
    conn.execute("UPDATE companies SET comp_tier = ? WHERE id = ?", (tier, company_id))


_POSTING_COLUMNS = (
    "source",
    "source_id",
    "company",
    "title",
    "source_url",
    "apply_url",
    "discovered_via",
    "fingerprint",
    "posted_at",
    "posted_at_precision",
    "posted_at_text",
    "updated_at",
    "remote",
    "remote_source",
    "locations",
    "employment_type",
    "employment_type_source",
    "comp_min",
    "comp_max",
    "comp_currency",
    "comp_interval",
    "comp_source",
    "description_text",
    "description_complete",
    "department",
    "team",
    "raw",
)


def upsert_posting(
    conn: sqlite3.Connection,
    posting: Posting,
    *,
    cluster_id: int | None = None,
    company_id: int | None = None,
    board_id: int | None = None,
    previous_posting_id: int | None = None,
) -> int:
    """Insert a posting, or refresh the one already recorded under its natural key.

    ``(source, source_id)`` is that key. Seeing a posting again updates what is
    known about it and moves ``last_seen_at``; it is not a new posting and must
    not be counted as a repost.

    Args:
        conn: An open connection.
        posting: The normalised posting.
        cluster_id: The cluster it belongs to.
        company_id: The employer.
        board_id: The board it was polled from, when it came from one.
        previous_posting_id: The posting this one reposts, when it does.

    Returns:
        The posting's row id.
    """
    values = {
        "source": posting.source,
        "source_id": posting.source_id,
        "company": posting.company,
        "title": posting.title,
        "source_url": posting.source_url,
        "apply_url": posting.apply_url,
        "discovered_via": posting.discovered_via,
        "fingerprint": posting.fingerprint,
        "posted_at": _isoformat(posting.posted_at),
        "posted_at_precision": posting.posted_at_precision.value,
        "posted_at_text": posting.posted_at_text,
        "updated_at": _isoformat(posting.updated_at),
        "remote": posting.remote.value,
        "remote_source": posting.remote_source.value,
        "locations": json.dumps(list(posting.locations)),
        "employment_type": posting.employment_type.value,
        "employment_type_source": posting.employment_type_source.value,
        "comp_min": posting.comp_min,
        "comp_max": posting.comp_max,
        "comp_currency": posting.comp_currency,
        "comp_interval": posting.comp_interval.value,
        "comp_source": posting.comp_source.value,
        "description_text": posting.description_text,
        "description_complete": int(posting.description_complete),
        "department": posting.department,
        "team": posting.team,
        "raw": json.dumps(posting.raw, default=str),
    }
    seen_at = posting.first_seen_at.isoformat()

    existing = conn.execute(
        "SELECT id FROM postings WHERE source = ? AND source_id = ?",
        (posting.source, posting.source_id),
    ).fetchone()

    if existing is not None:
        assignments = ", ".join(f"{column} = :{column}" for column in _POSTING_COLUMNS)
        conn.execute(
            # disappeared_at is cleared: seeing a posting again means the board is
            # listing it again, whatever it did in between.
            f"UPDATE postings SET {assignments}, last_seen_at = :last_seen_at, "
            "disappeared_at = NULL, "
            "cluster_id = coalesce(:cluster_id, cluster_id), "
            "company_id = coalesce(:company_id, company_id), "
            "board_id = coalesce(:board_id, board_id) WHERE id = :id",
            {
                **values,
                "last_seen_at": seen_at,
                "cluster_id": cluster_id,
                "company_id": company_id,
                "board_id": board_id,
                "id": existing["id"],
            },
        )
        return int(existing["id"])

    columns = [
        *_POSTING_COLUMNS,
        "first_seen_at",
        "last_seen_at",
        "cluster_id",
        "company_id",
        "board_id",
        "previous_posting_id",
    ]
    placeholders = ", ".join(f":{column}" for column in columns)
    cursor = conn.execute(
        f"INSERT INTO postings ({', '.join(columns)}) VALUES ({placeholders})",
        {
            **values,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "cluster_id": cluster_id,
            "company_id": company_id,
            "board_id": board_id,
            "previous_posting_id": previous_posting_id,
        },
    )
    return int(cursor.lastrowid)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def active_boards(conn: sqlite3.Connection) -> list[Board]:
    """Return every board worth polling.

    Retired boards are skipped; stale ones are not. A board goes stale after
    three empty polls and is flagged for re-resolution, but it may simply be a
    company that is not hiring — so it keeps being polled, and a successful poll
    clears the flag.

    Args:
        conn: An open connection.

    Returns:
        The boards, with their company and row ids attached.
    """
    rows = conn.execute(
        "SELECT b.id, b.vendor, b.identifier, b.company_id, c.name FROM company_boards b "
        "JOIN companies c ON c.id = b.company_id "
        "WHERE b.status != 'retired' AND c.excluded = 0 ORDER BY c.name"
    ).fetchall()
    return [
        Board(
            vendor=row["vendor"],
            identifier=json.loads(row["identifier"]),
            company=row["name"],
            company_id=int(row["company_id"]),
            board_id=int(row["id"]),
        )
        for row in rows
    ]


def posting_from_row(row: sqlite3.Row) -> Posting:
    """Rebuild a Posting from a stored row.

    Args:
        row: A row from ``postings``.

    Returns:
        The posting as the pipeline would have produced it.
    """
    return Posting(
        source=row["source"],
        source_id=row["source_id"],
        company=row["company"],
        title=row["title"],
        source_url=row["source_url"],
        apply_url=row["apply_url"],
        discovered_via=row["discovered_via"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        fingerprint=row["fingerprint"],
        posted_at=_parse(row["posted_at"]),
        posted_at_precision=PostedAtPrecision(row["posted_at_precision"]),
        posted_at_text=row["posted_at_text"],
        updated_at=_parse(row["updated_at"]),
        remote=RemoteStatus(row["remote"]),
        remote_source=RemoteSource(row["remote_source"]),
        locations=tuple(json.loads(row["locations"])),
        employment_type=EmploymentType(row["employment_type"]),
        employment_type_source=EmploymentTypeSource(row["employment_type_source"]),
        comp_min=row["comp_min"],
        comp_max=row["comp_max"],
        comp_currency=row["comp_currency"],
        comp_interval=CompInterval(row["comp_interval"]),
        comp_source=CompSource(row["comp_source"]),
        description_text=row["description_text"],
        description_complete=bool(row["description_complete"]),
        department=row["department"],
        team=row["team"],
        raw=json.loads(row["raw"]),
    )


def canonical_posting(conn: sqlite3.Connection, cluster_id: int) -> Posting | None:
    """Return a cluster's canonical posting, if it has one.

    Args:
        conn: An open connection.
        cluster_id: The cluster to read.

    Returns:
        The posting whose URL and identity the digest should use.
    """
    row = conn.execute(
        "SELECT p.* FROM clusters c JOIN postings p ON p.id = c.canonical_posting_id "
        "WHERE c.id = ?",
        (cluster_id,),
    ).fetchone()
    return posting_from_row(row) if row else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
