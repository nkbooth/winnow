"""The CLI's argument surface.

Asserted rather than assumed because the command names are what the systemd
units and the design notes both refer to; renaming one silently breaks
deployment.
"""

import pytest

from winnow.cli import build_parser


def test_prog_name_is_winnow():
    assert build_parser().prog == "winnow"


def test_no_command_parses_to_none():
    args = build_parser().parse_args([])
    assert args.command is None


def test_version_flag_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["--version"])
    assert exit_info.value.code == 0
    assert "winnow" in capsys.readouterr().out


def test_db_init_is_a_subcommand():
    args = build_parser().parse_args(["db", "init"])
    assert args.command == "db"
    assert args.db_command == "init"


def test_db_init_creates_and_migrates_the_store(tmp_path, monkeypatch, capsys):
    from winnow import store
    from winnow.cli import main

    db = tmp_path / "nested" / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))

    assert main(["db", "init"]) == 0
    assert db.exists()

    conn = store.connect(db)
    try:
        assert store.schema_version(conn) == store.LATEST_VERSION
    finally:
        conn.close()
    assert str(db) in capsys.readouterr().out


def test_company_add_requires_a_board_url():
    args = build_parser().parse_args(
        ["company", "add", "Proton", "--board", "https://jobs.ashbyhq.com/proton"]
    )
    assert (args.command, args.company_command) == ("company", "add")
    assert args.name == "Proton"
    assert args.board == "https://jobs.ashbyhq.com/proton"


def test_company_list_takes_no_arguments():
    args = build_parser().parse_args(["company", "list"])
    assert args.company_command == "list"


def test_fetch_is_a_subcommand():
    args = build_parser().parse_args(["fetch"])
    assert args.command == "fetch"


def test_fetch_reports_what_each_source_did(tmp_path, monkeypatch, capsys):
    """Silence and breakage must never look alike, including on the terminal."""
    from winnow import store
    from winnow.cli import main

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    monkeypatch.setenv("WINNOW_PROFILE", "examples/profile.yaml")

    conn = store.connect(db)
    store.migrate(conn)
    company_id = store.insert_company(conn, "Example Co")
    store.insert_board(
        conn,
        company_id=company_id,
        vendor="greenhouse",
        identifier={"slug": "example"},
        source="manual",
    )
    conn.close()

    class Adapter:
        name = "greenhouse"

        def fetch_list(self, board):
            raise RuntimeError("boards-api said 503")

        def normalize(self, raw, board, *, now=None):
            raise AssertionError("not reached")

        def fetch_detail(self, posting):
            return None

    monkeypatch.setattr("winnow.cli.adapter_for", lambda vendor, client=None: Adapter())

    assert main(["fetch"]) == 0
    output = capsys.readouterr().out
    assert "greenhouse" in output
    assert "failed" in output


def test_score_scores_every_unscored_cluster(tmp_path, monkeypatch, capsys):
    from datetime import UTC, datetime

    from winnow import learning, store
    from winnow.cli import main
    from winnow.dedupe import cluster_postings, persist_cluster
    from winnow.models import Posting, RemoteStatus

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    monkeypatch.setenv("WINNOW_PROFILE", "examples/profile.yaml")

    conn = store.connect(db)
    store.migrate(conn)
    company_id = store.insert_company(conn, "Example Co")
    posting = Posting(
        source="greenhouse",
        source_id="1",
        company="Example Co",
        title="Director of Business Systems",
        source_url="https://boards.greenhouse.io/example/jobs/1",
        discovered_via="greenhouse",
        first_seen_at=datetime(2026, 9, 18, tzinfo=UTC),
        remote=RemoteStatus.REMOTE,
        locations=("Remote (United States)",),
        description_text="You will own and build out the BizOps function.",
        description_complete=True,
    )
    cluster_id = persist_cluster(conn, cluster_postings([posting])[0], company_id=company_id)
    conn.close()

    class Judge:
        model = "claude-opus-5"

        def judge(self, system, user):
            return {
                "dimensions": {
                    "growth_signal": {
                        "score": 1.0,
                        "evidence": "own and build out the BizOps function",
                    },
                    "builder_shaped": {"score": 0.0, "evidence": None},
                    "team_leadership": {"score": 0.0, "evidence": None},
                    "mission_alignment": {"score": 0.0, "evidence": None},
                    "governance_heavy": {"score": 0.0, "evidence": None},
                    "grind_culture": {"score": 0.0, "evidence": None},
                },
                "vetoes": [],
                "flags": [],
                "why_fits": "Owns the function.",
                "concern": "Comp unresolved.",
            }

    monkeypatch.setattr("winnow.cli.ClaudeJudge", Judge)

    assert main(["score"]) == 0
    assert "scored 1 clusters" in capsys.readouterr().out

    conn = store.connect(db)
    try:
        assert learning.latest_score(conn, cluster_id) is not None
        assert learning.pending_review_count(conn) == 1
    finally:
        conn.close()


def test_digest_dry_run_prints_without_sending(tmp_path, monkeypatch, capsys):
    from winnow import store
    from winnow.cli import main

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    monkeypatch.setenv("WINNOW_PROFILE", "examples/profile.yaml")
    conn = store.connect(db)
    store.migrate(conn)
    conn.close()

    def explode(*args, **kwargs):
        raise AssertionError("a dry run must not reach Matrix")

    monkeypatch.setattr("winnow.cli.notify.build", lambda _config: explode())

    assert main(["digest", "--dry-run"]) == 0
    assert "winnow ·" in capsys.readouterr().out


def test_review_is_a_subcommand():
    args = build_parser().parse_args(["review"])
    assert args.command == "review"


def test_listen_is_a_subcommand():
    args = build_parser().parse_args(["listen"])
    assert args.command == "listen"


def test_run_is_a_subcommand():
    assert build_parser().parse_args(["run"]).command == "run"


def test_run_performs_the_daily_sequence_in_order(monkeypatch):
    """One process, one credential resolution, one exit status for the timer.

    Ageing runs before the digest so a score band's numbers are current when it
    is read; pruning comes last so nothing can vanish out from under it.
    """
    from winnow import cli

    called = []
    monkeypatch.setattr(cli, "_db_init", lambda: called.append("db") or 0)
    monkeypatch.setattr(cli, "_fetch", lambda args: called.append("fetch") or 0)
    monkeypatch.setattr(cli, "_score", lambda args: called.append("score") or 0)
    monkeypatch.setattr(cli, "_age", lambda args: called.append("age") or 0)
    monkeypatch.setattr(cli, "_digest", lambda args: called.append("digest") or 0)
    monkeypatch.setattr(cli, "_prune", lambda args: called.append("prune") or 0)

    assert cli.main(["run"]) == 0
    assert called == ["db", "fetch", "score", "age", "digest", "prune"]


def test_run_stops_at_the_first_failure(monkeypatch):
    """Scoring against a board that never answered would digest a lie."""
    from winnow import cli

    called = []
    monkeypatch.setattr(cli, "_db_init", lambda: called.append("db") or 0)
    monkeypatch.setattr(cli, "_fetch", lambda args: called.append("fetch") or 3)
    monkeypatch.setattr(cli, "_score", lambda args: called.append("score") or 0)
    monkeypatch.setattr(cli, "_digest", lambda args: called.append("digest") or 0)

    assert cli.main(["run"]) == 3
    assert called == ["db", "fetch"]


def test_score_resolves_no_credential_when_there_is_nothing_to_score(tmp_path, monkeypatch, capsys):
    """A quiet day must not need an API key to end successfully."""
    from winnow import store
    from winnow.cli import main

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    monkeypatch.setenv("WINNOW_PROFILE", "examples/profile.yaml")
    conn = store.connect(db)
    store.migrate(conn)
    conn.close()

    def explode(*args, **kwargs):
        raise AssertionError("a judge was built with nothing to judge")

    monkeypatch.setattr("winnow.cli.ClaudeJudge", explode)

    assert main(["score"]) == 0
    assert "scored 0 clusters" in capsys.readouterr().out


def test_company_tier_sets_which_floor_applies():
    args = build_parser().parse_args(["company", "tier", "Red Hat", "large_corporate"])
    assert (args.company_command, args.name, args.tier) == (
        "tier",
        "Red Hat",
        "large_corporate",
    )


def test_company_tier_only_offers_tiers_the_schema_accepts():
    """A tier the store rejects would fail after the thinking is done."""
    import pytest

    with pytest.raises(SystemExit):
        build_parser().parse_args(["company", "tier", "Red Hat", "enormous"])


def test_setting_a_tier_changes_the_floor_that_applies(tmp_path, monkeypatch, capsys):
    from winnow import store
    from winnow.cli import main

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    conn = store.connect(db)
    store.migrate(conn)
    company_id = store.insert_company(conn, "Red Hat")
    conn.close()

    assert main(["company", "tier", "Red Hat", "large_corporate"]) == 0

    conn = store.connect(db)
    try:
        assert store.comp_tier(conn, company_id) == "large_corporate"
    finally:
        conn.close()
    assert "large_corporate" in capsys.readouterr().out


def test_setting_a_tier_on_an_unknown_company_is_an_error(tmp_path, monkeypatch):
    from winnow import store
    from winnow.cli import main

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    conn = store.connect(db)
    store.migrate(conn)
    conn.close()

    assert main(["company", "tier", "Nobody", "crypto"]) == 2


def test_http_client_logging_is_not_at_info(monkeypatch):
    """httpx logs every request; a poll of twenty boards would bury the run."""
    import logging

    from winnow import cli

    cli._configure_logging()
    assert logging.getLogger("httpx").level >= logging.WARNING


def test_a_missing_api_key_is_a_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    """An unattended timer's journal should say what to fix, not where it crashed."""
    from datetime import UTC, datetime

    from winnow import learning, store
    from winnow.cli import main
    from winnow.dedupe import cluster_postings, persist_cluster
    from winnow.models import Posting, RemoteStatus

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    monkeypatch.setenv("WINNOW_PROFILE", "examples/profile.yaml")

    conn = store.connect(db)
    store.migrate(conn)
    company_id = store.insert_company(conn, "Example Co")
    persist_cluster(
        conn,
        cluster_postings(
            [
                Posting(
                    source="greenhouse",
                    source_id="1",
                    company="Example Co",
                    title="Director of Business Systems",
                    source_url="https://boards.greenhouse.io/example/jobs/1",
                    discovered_via="greenhouse",
                    first_seen_at=datetime(2026, 9, 18, tzinfo=UTC),
                    remote=RemoteStatus.REMOTE,
                    locations=("Remote (United States)",),
                    description_text="Own and build out the BizOps function.",
                    description_complete=True,
                )
            ]
        )[0],
        company_id=company_id,
    )
    conn.close()

    def unresolvable(*args, **kwargs):
        raise RuntimeError("op read failed: 'claude-api' isn't an item")

    monkeypatch.setattr("winnow.cli.ClaudeJudge", unresolvable)

    assert main(["score"]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "claude-api" in captured.err
    assert learning.pending_review_count(store.connect(db)) == 0, "nothing was scored"


def test_prune_uses_the_retention_window_from_the_rubric(tmp_path, monkeypatch, capsys):
    """180-day retention was implemented, tested, and never called by anything."""
    from datetime import UTC, datetime, timedelta

    from winnow import store
    from winnow.cli import main

    db = tmp_path / "winnow.db"
    monkeypatch.setenv("WINNOW_DB", str(db))
    monkeypatch.setenv("WINNOW_PROFILE", "examples/profile.yaml")

    old = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    conn = store.connect(db)
    store.migrate(conn)
    conn.execute(
        "INSERT INTO postings (source, source_id, fingerprint, company, title, source_url, "
        "discovered_via, first_seen_at, last_seen_at, disappeared_at, posted_at_precision, "
        "remote, remote_source, employment_type, employment_type_source, comp_interval, "
        "comp_source) VALUES ('greenhouse', 'gone', 'f', 'Co', 'T', 'u', 'greenhouse', ?, ?, ?, "
        "'unknown', 'UNKNOWN', 'absent', 'UNKNOWN', 'absent', 'UNKNOWN', 'absent')",
        (old, old, old),
    )
    conn.close()

    assert main(["prune"]) == 0
    assert "1" in capsys.readouterr().out

    conn = store.connect(db)
    try:
        assert conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 0
    finally:
        conn.close()


def test_notify_is_a_subcommand():
    args = build_parser().parse_args(["notify", "something broke"])
    assert (args.command, args.message) == ("notify", "something broke")


def test_notify_posts_the_message_to_matrix(monkeypatch, capsys):
    """Used by systemd OnFailure, so it must not depend on the store or rubric.

    A unit that fails because the database is unreadable still has to be able
    to say so.
    """
    from winnow.cli import main

    sent = {}

    class Sender:
        def send(self, text, formatted):
            sent["text"] = text
            sent["formatted"] = formatted
            return "$event-1"

    monkeypatch.setattr("winnow.cli.notify.build", lambda _config: Sender())
    monkeypatch.delenv("WINNOW_DB", raising=False)
    monkeypatch.delenv("WINNOW_PROFILE", raising=False)

    assert main(["notify", "winnow-digest.service failed"]) == 0
    assert sent["text"] == "winnow-digest.service failed"
    assert "winnow-digest.service failed" in sent["formatted"]
    assert "$event-1" in capsys.readouterr().out


def test_notify_escapes_what_it_is_given(monkeypatch):
    from winnow.cli import main

    sent = {}

    class Sender:
        def send(self, text, formatted):
            sent["formatted"] = formatted
            return "$event-2"

    monkeypatch.setattr("winnow.cli.notify.build", lambda _config: Sender())
    assert main(["notify", "<script>alert(1)</script>"]) == 0
    assert "<script>" not in sent["formatted"]
    assert "&lt;script&gt;" in sent["formatted"]


def test_notify_reports_a_failure_to_send(monkeypatch, capsys):
    from winnow.cli import main

    class Sender:
        def send(self, text, formatted):
            raise RuntimeError("homeserver refused")

    monkeypatch.setattr("winnow.cli.notify.build", lambda _config: Sender())
    assert main(["notify", "anything"]) == 2
    assert "homeserver refused" in capsys.readouterr().err


def test_listen_stops_cleanly_on_sigterm(monkeypatch):
    """PID 1 ignores signals it has no handler for, which is how this ended in 137.

    The container's entrypoint is the process itself, so Python is PID 1 and
    the kernel applies no default action for SIGTERM. Podman waited ten
    seconds, sent SIGKILL, systemd recorded exit-code failure, and OnFailure
    posted a false alarm to Matrix on every ordinary restart.
    """
    import signal

    from winnow import cli

    handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(cli, "_open_store", lambda: _NullConn())
    monkeypatch.setattr(cli.notify, "build", lambda _config: None)

    stops: list = []

    def fake_run(conn, escalate=None, stop=None, **kwargs):
        stops.append(stop)

    import winnow.listener as listener_module

    monkeypatch.setattr(listener_module, "run", fake_run)

    assert cli.main(["listen"]) == 0
    assert signal.SIGTERM in handlers, "a handler must be installed"

    stop = stops[0]
    assert stop() is False, "keeps running until asked"

    # The loop blocks in IDLE for up to fifteen minutes, and podman waits ten
    # seconds. Setting a flag the loop will notice eventually is not stopping;
    # the handler has to interrupt the read that is in progress.
    with pytest.raises(KeyboardInterrupt):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stop() is True, "and the flag is set for any cycle already past the read"


class _NullConn:
    def close(self):
        pass


def test_init_is_a_subcommand():
    args = build_parser().parse_args(["init"])
    assert args.command == "init"


def test_init_sets_up_a_directory_and_reports_what_is_left(tmp_path, monkeypatch, capsys):
    """The one command a stranger runs first, so it has to end by saying what next."""
    from winnow import cli

    answers = iter(
        [
            "Alex Rivera",
            "alex@example.invalid",
            "Asheville, NC",
            "Director of Business Systems",
            "180000",
            "required",
            "220000",
            "alex@example.invalid",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli.main(["init", "--directory", str(tmp_path)]) == 0

    assert (tmp_path / "profile.yaml").exists()
    assert (tmp_path / "config.toml").exists()
    assert (tmp_path / "assets" / "work-inventory.md").exists()
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_company_add_takes_a_seed_file(tmp_path, monkeypatch, capsys):
    """One command from clone to a working search, instead of forty."""
    from winnow import cli, resolver

    monkeypatch.setenv("WINNOW_DB", str(tmp_path / "test.db"))
    added: list[str] = []

    def fake_add(conn, *, company, url, probe):
        added.append(company)
        return resolver.Board(vendor="greenhouse", identifier={"slug": "x"}, company=company)

    monkeypatch.setattr(cli.resolver, "add_board", fake_add)

    assert cli.main(["company", "add", "--from", "seeds/developer-tools.yaml"]) == 0

    assert "GitLab" in added
    assert "added" in capsys.readouterr().out


def test_a_seed_import_can_be_filtered_by_tag(tmp_path, monkeypatch, capsys):
    from winnow import cli, resolver

    monkeypatch.setenv("WINNOW_DB", str(tmp_path / "test.db"))
    added: list[str] = []
    monkeypatch.setattr(
        cli.resolver,
        "add_board",
        lambda conn, *, company, url, probe: (
            added.append(company)
            or resolver.Board(vendor="greenhouse", identifier={"slug": "x"}, company=company)
        ),
    )

    cli.main(["company", "add", "--from", "seeds", "--tag", "nonprofit"])

    assert "Wikimedia Foundation" in added
    assert "Stripe" not in added


def test_a_seed_import_keeps_going_when_one_board_fails(tmp_path, monkeypatch, capsys):
    """Forty boards and one dead slug should not cost the other thirty-nine."""
    from winnow import cli, resolver

    monkeypatch.setenv("WINNOW_DB", str(tmp_path / "test.db"))
    calls = {"n": 0}

    def sometimes(conn, *, company, url, probe):
        calls["n"] += 1
        if calls["n"] == 1:
            raise resolver.BoardNotFound("gone")
        return resolver.Board(vendor="greenhouse", identifier={"slug": "x"}, company=company)

    monkeypatch.setattr(cli.resolver, "add_board", sometimes)

    assert cli.main(["company", "add", "--from", "seeds/developer-tools.yaml"]) == 0

    output = capsys.readouterr().out
    assert "failed" in output
    assert calls["n"] > 1, "stopped at the first failure"


def test_re_importing_reports_skipped_rather_than_failed(tmp_path, monkeypatch, capsys):
    """A second import of 65 boards must not read as 65 failures.

    Already-known and broken are both refusals to add, and lumping them
    together turns a no-op into something that looks like an outage.
    """
    from winnow import cli, resolver

    monkeypatch.setenv("WINNOW_DB", str(tmp_path / "test.db"))

    def already(conn, *, company, url, probe):
        raise resolver.BoardAlreadyKnown(f"{company} is already recorded")

    monkeypatch.setattr(cli.resolver, "add_board", already)

    assert cli.main(["company", "add", "--from", "seeds/gaming.yaml"]) == 0

    output = capsys.readouterr().out
    assert "skipped" in output
    assert "  failed" not in output, "no entry should be reported as a failure"
    assert "0 failed" in output
    assert "already recorded" in output
