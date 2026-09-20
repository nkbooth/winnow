"""Schema and migration behaviour.

The board identifier is the reason these tests are specific. A Greenhouse board
is a slug; a Workday board is (tenant, datacenter, site). A column that can only
hold the former is the one thing in this schema that is expensive to retrofit,
so it is asserted here rather than discovered later.
"""

import json
import sqlite3

import pytest

from winnow import store
from winnow.models import (
    CompSource,
    EmploymentType,
    LocationClass,
    PostedAtPrecision,
    RemoteStatus,
)


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "test.db")
    store.migrate(connection)
    yield connection
    connection.close()


def _tables(connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_migrate_creates_the_designed_tables(conn):
    assert {
        "companies",
        "company_aliases",
        "company_boards",
        "postings",
        "clusters",
        "scores",
        "decisions",
        "outcomes",
        "poll_runs",
        "drafts",
        "messages",
        "merge_confirmations",
    } <= _tables(conn)


def test_migrate_records_its_version(conn):
    assert store.schema_version(conn) == store.LATEST_VERSION


def test_migrate_is_idempotent(conn):
    assert store.migrate(conn) == []


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO company_boards (company_id, vendor, identifier, source) "
            "VALUES (9999, 'greenhouse', '{\"slug\":\"nope\"}', 'manual')"
        )


def test_board_identifier_holds_a_workday_triple(conn):
    company_id = store.insert_company(conn, "Red Hat")
    board_id = store.insert_board(
        conn,
        company_id=company_id,
        vendor="workday",
        identifier={"tenant": "redhat", "datacenter": "wd5", "site": "jobs"},
        source="manual",
    )
    row = conn.execute("SELECT identifier FROM company_boards WHERE id = ?", (board_id,)).fetchone()
    assert json.loads(row["identifier"])["datacenter"] == "wd5"


def test_board_identifier_holds_a_bare_slug(conn):
    company_id = store.insert_company(conn, "Tailscale")
    board_id = store.insert_board(
        conn,
        company_id=company_id,
        vendor="greenhouse",
        identifier={"slug": "tailscale"},
        source="greenhouse_name_match",
    )
    assert store.board_identifier(conn, board_id) == {"slug": "tailscale"}


def test_identifier_encoding_is_key_order_independent(conn):
    """Two spellings of one Workday board must collide, not duplicate."""
    company_id = store.insert_company(conn, "Red Hat")
    store.insert_board(
        conn,
        company_id=company_id,
        vendor="workday",
        identifier={"tenant": "redhat", "datacenter": "wd5", "site": "jobs"},
        source="manual",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_board(
            conn,
            company_id=company_id,
            vendor="workday",
            identifier={"site": "jobs", "datacenter": "wd5", "tenant": "redhat"},
            source="manual",
        )


def test_one_company_may_have_several_boards(conn):
    """Engineering on Greenhouse and sales on Lever is a real arrangement."""
    company_id = store.insert_company(conn, "Somebody")
    store.insert_board(
        conn,
        company_id=company_id,
        vendor="greenhouse",
        identifier={"slug": "eng"},
        source="manual",
    )
    store.insert_board(
        conn, company_id=company_id, vendor="lever", identifier={"slug": "sales"}, source="manual"
    )
    count = conn.execute(
        "SELECT count(*) AS n FROM company_boards WHERE company_id = ?", (company_id,)
    ).fetchone()["n"]
    assert count == 2


def test_decisions_key_on_the_cluster_not_the_posting(conn):
    """A passed role must not return via a second source or a repost."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
    assert "cluster_id" in columns
    assert "posting_id" not in columns


def test_enum_check_constraints_match_the_python_enums(conn):
    """Guards against the SQL and the enums drifting apart."""
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'postings'"
    ).fetchone()["sql"]
    for enum in (RemoteStatus, EmploymentType, CompSource, PostedAtPrecision):
        for member in enum:
            assert f"'{member.value}'" in sql, f"{enum.__name__}.{member.name} missing from CHECK"

    cluster_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'clusters'"
    ).fetchone()["sql"]
    for member in LocationClass:
        assert f"'{member.value}'" in cluster_sql


def test_scores_store_a_breakdown_not_only_a_total(conn):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(scores)")}
    assert {"breakdown", "rubric_version", "prompt_version", "model"} <= columns


def test_companies_carry_a_compensation_tier(conn):
    """Which floor applies is a fact about the employer, not about the posting."""
    company_id = store.insert_company(conn, "Red Hat")
    assert store.comp_tier(conn, company_id) == "mid_market"

    store.set_comp_tier(conn, company_id, "large_corporate")
    assert store.comp_tier(conn, company_id) == "large_corporate"


def test_an_invented_tier_is_refused(conn):
    company_id = store.insert_company(conn, "Red Hat")
    with pytest.raises(sqlite3.IntegrityError):
        store.set_comp_tier(conn, company_id, "enormous")


def test_migrations_apply_in_order_on_a_fresh_database(tmp_path):
    fresh = store.connect(tmp_path / "fresh.db")
    try:
        assert store.migrate(fresh) == list(range(1, store.LATEST_VERSION + 1))
    finally:
        fresh.close()


def test_a_pass_can_cite_evidence_the_agent_never_saw(conn, make_posting):
    """Everything else in the enum is a judgement about text the scorer read."""
    from winnow import learning, store
    from winnow.dedupe import cluster_postings, persist_cluster

    company_id = store.insert_company(conn, "Example Co")
    posting = make_posting(company="Example Co", description_complete=True)
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)

    learning.record_decision(
        conn,
        cluster_id,
        "pass",
        reason="external_feedback",
        note="Someone who worked there said the team had just lost its director.",
    )

    row = conn.execute("SELECT reason, note FROM decisions").fetchone()
    assert row["reason"] == "external_feedback"
    assert "lost its director" in row["note"]


def test_the_rebuild_keeps_the_decisions_already_recorded(tmp_path):
    """The live store has nine of them; a rebuild that drops them is a data loss."""
    import sqlite3

    from winnow import store
    from winnow.migrations import MIGRATIONS

    path = tmp_path / "upgrade.db"
    conn = store.connect(path)
    # Migrate only as far as the version before the rebuild.
    applied = 0
    for version, sql in MIGRATIONS:
        if version >= 6:
            break
        conn.executescript(sql)
        applied = version
    conn.execute(f"PRAGMA user_version = {applied:d}")
    conn.execute("INSERT INTO companies (name) VALUES ('Example Co')")
    conn.execute(
        "INSERT INTO clusters (id, company_id, normalized_title, location_class) "
        "VALUES (1, 1, 'director of business systems', 'US_REMOTE')"
    )
    conn.execute(
        "INSERT INTO decisions (cluster_id, decision, reason) VALUES (1, 'pass', 'timing')"
    )

    store.migrate(conn)

    row = conn.execute("SELECT decision, reason FROM decisions").fetchone()
    assert (row["decision"], row["reason"]) == ("pass", "timing")
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM sqlite_master "
            "WHERE type='index' AND name='decisions_cluster'"
        ).fetchone()["n"]
        == 1
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions (cluster_id, decision, reason) VALUES (1, 'pass', 'nonsense')"
        )
    conn.close()
