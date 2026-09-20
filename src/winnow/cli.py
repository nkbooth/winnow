"""Command-line entry point.

One parser, one dispatch table. Subcommands are added as their modules land;
each one is a thin wrapper that parses arguments and calls into a module that
knows nothing about argparse.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
from pathlib import Path

from winnow import (
    __version__,
    config,
    digest,
    discovery,
    learning,
    notify,
    onboarding,
    pipeline,
    resolver,
    scoring,
    seeds,
    store,
)
from winnow.llm import ClaudeJudge, JudgementSchemaError
from winnow.models import CompSource
from winnow.profile import Profile
from winnow.sources import adapter_for
from winnow.sources.adzuna import AdzunaClient


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser.

    Returns:
        A parser whose ``command`` attribute names the chosen subcommand, or
        None when invoked bare.
    """
    parser = argparse.ArgumentParser(
        prog="winnow",
        description="Daily job-board agent: fetch, gate, score, digest.",
    )
    parser.add_argument("--version", action="version", version=f"winnow {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    initialise = subcommands.add_parser(
        "init", help="set up a rubric, settings and drafting assets"
    )
    initialise.add_argument(
        "--directory", default=None, help="where to write them (default: alongside config.toml)"
    )

    database = subcommands.add_parser("db", help="create or migrate the local store")
    database.add_subparsers(dest="db_command", required=True).add_parser(
        "init", help="create the store and apply pending migrations"
    )

    company = subcommands.add_parser("company", help="manage companies and their boards")
    company_commands = company.add_subparsers(dest="company_command", required=True)
    add = company_commands.add_parser("add", help="record a board from a pasted URL")
    add.add_argument(
        "--from",
        dest="seed_path",
        help="import every board in a seed file or directory (see seeds/)",
    )
    add.add_argument(
        "--tag",
        action="append",
        default=[],
        help="with --from, keep only entries carrying this tag; repeatable",
    )
    # Optional so that --from can stand alone. Requiring them would mean
    # inventing a company name to import a list of forty.
    add.add_argument("name", nargs="?", help="company name, as it should be displayed")
    add.add_argument("--board", help="board URL, e.g. https://jobs.ashbyhq.com/proton")
    company_commands.add_parser("list", help="list known boards and their status")
    tier = company_commands.add_parser(
        "tier", help="set which compensation floor applies to a company"
    )
    tier.add_argument("name", help="company name, as recorded")
    tier.add_argument(
        "tier",
        choices=("mid_market", "large_corporate", "crypto"),
        help="mid_market $200k, large_corporate $275k, crypto $400k",
    )

    subcommands.add_parser("fetch", help="poll every board once and store what survives")
    subcommands.add_parser("score", help="score every cluster that has not been scored")

    run_command = subcommands.add_parser(
        "run", help="the daily sequence: migrate, poll, score, post"
    )
    run_command.add_argument(
        "--dry-run", action="store_true", help="print the digest instead of posting it"
    )

    subcommands.add_parser(
        "prune", help="drop postings that left their board over the retention window"
    )

    notify = subcommands.add_parser("notify", help="post a message to the configured destination")
    notify.add_argument("message", help="what to say")
    discover = subcommands.add_parser(
        "discover", help="sweep the aggregator for employers not on the board list"
    )
    discover.add_argument("--days", type=int, default=30, help="how far back to look (default: 30)")
    subcommands.add_parser("listen", help="watch the mailbox and advance applications")
    subcommands.add_parser("review", help="triage the queue (the only path that can send mail)")

    digest_command = subcommands.add_parser("digest", help="render and post the digest")
    digest_command.add_argument(
        "--dry-run",
        action="store_true",
        help="print the digest instead of posting it",
    )

    return parser


def _configure_logging() -> None:
    """Send logs to stdout, where journald collects them.

    Unattended processes are the only ones anybody reads logs for, and the one
    thing this system must never do is fail quietly.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx logs a line per request. A poll of twenty boards would bury the
    # three lines that say what the run actually did.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit status.
    """
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    handlers = {
        "db": _db,
        "company": _company,
        "fetch": _fetch,
        "score": _score,
        "digest": _digest,
        "review": _review,
        "listen": _listen,
        "run": _run,
        "prune": _prune,
        "notify": _notify,
        "discover": _discover,
        "init": _init,
    }
    handler = handlers.get(args.command)
    if handler is None:
        raise NotImplementedError(args.command)
    return handler(args)


def _open_store() -> sqlite3.Connection:
    """Open the store, applying any pending migrations first."""
    conn = store.connect(config.db_path())
    store.migrate(conn)
    return conn


def _init(args: argparse.Namespace) -> int:
    """Set this machine up, asking only what has no defensible default.

    An existing rubric is adopted rather than re-interviewed: someone arriving
    with one has already answered all of this, and asking again to reproduce a
    file they own is how a setup step loses people.
    """
    directory = Path(args.directory) if args.directory else config.config_path().parent
    result = onboarding.init(directory, ask=_prompt)

    print(f"\nwinnow configured in {directory}\n")
    for step in result.next_steps:
        print(f"  - {step}")
    return 0


def _prompt(key: str, question: str, default: str = "") -> str:
    """Ask on the terminal, showing the default and accepting it on Enter."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _db(_args: argparse.Namespace) -> int:
    return _db_init()


def _db_init() -> int:
    path = config.db_path()
    conn = store.connect(path)
    try:
        applied = store.migrate(conn)
        version = store.schema_version(conn)
    finally:
        conn.close()
    applied_note = f"applied {applied}" if applied else "already current"
    print(f"{path} — schema v{version}, {applied_note}")
    return 0


def _company(args: argparse.Namespace) -> int:
    conn = _open_store()
    try:
        if args.company_command == "add":
            if args.seed_path:
                return _company_add_seeds(conn, args.seed_path, tags=args.tag)
            if not args.name or not args.board:
                print("give a name and --board, or --from a seed file", file=sys.stderr)
                return 2
            return _company_add(conn, name=args.name, url=args.board)
        if args.company_command == "tier":
            return _company_tier(conn, name=args.name, tier=args.tier)
        return _company_list(conn)
    finally:
        conn.close()


def _company_add(conn: sqlite3.Connection, *, name: str, url: str) -> int:
    try:
        board = resolver.add_board(conn, company=name, url=url, probe=resolver.HttpProbe())
    except resolver.ResolverError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    identifier = ", ".join(f"{key}={value}" for key, value in sorted(board.identifier.items()))
    print(f"added {board.company}: {board.vendor} ({identifier})")
    return 0


def _company_add_seeds(conn: sqlite3.Connection, path: str, *, tags: list[str]) -> int:
    """Import a curated list of boards.

    One dead slug does not cost the rest of the list: a seed file is checked
    when it is written and boards rot afterwards, so the import reports each
    failure and carries on. Stopping at the first would make a forty-company
    file unusable the day one company got acquired.
    """
    entries = seeds.load(path, tags=tags)
    if not entries:
        print(f"no entries in {path}" + (f" tagged {', '.join(tags)}" if tags else ""))
        return 0

    probe = resolver.HttpProbe()
    added = skipped = failed = 0
    for entry in entries:
        try:
            board = resolver.add_board(conn, company=entry.name, url=entry.board, probe=probe)
        except resolver.BoardAlreadyKnown:
            # Not a failure. The duplicate check runs before the HTTP call, so
            # re-importing a list is free as well as harmless, and reporting it
            # as a failure would make a no-op look like an outage.
            print(f"  skipped {entry.name}: already recorded")
            skipped += 1
            continue
        except resolver.ResolverError as error:
            print(f"  failed  {entry.name}: {error}")
            failed += 1
            continue
        print(f"  added   {entry.name}: {board.vendor}")
        added += 1

    tally = f"{added} added, {skipped} already recorded, {failed} failed"
    print(f"\n{tally}, from {len(entries)} entries")
    return 0


def _company_tier(conn: sqlite3.Connection, *, name: str, tier: str) -> int:
    """Record which compensation floor applies to an employer."""
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    if row is None:
        print(f"no company named {name!r}", file=sys.stderr)
        return 2
    store.set_comp_tier(conn, int(row["id"]), tier)
    print(f"{name}: {tier}")
    return 0


def _company_list(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT c.name, b.vendor, b.identifier, b.status, b.last_ok_at, "
        "b.consecutive_empty_polls FROM company_boards b "
        "JOIN companies c ON c.id = b.company_id ORDER BY c.name, b.vendor"
    ).fetchall()
    if not rows:
        print("no boards yet — winnow company add <name> --board <url>")
        return 0
    for row in rows:
        marker = "" if row["status"] == "active" else f"  [{row['status']}]"
        last_ok = row["last_ok_at"] or "never"
        print(
            f"{row['name']:<24} {row['vendor']:<16} {row['identifier']:<48} "
            f"last ok {last_ok}{marker}"
        )
    return 0


def _discover(args: argparse.Namespace) -> int:
    """Sweep the aggregator and print the employers worth looking up.

    Nothing is stored. Adzuna cannot produce a posting this project will accept
    — truncated descriptions, modelled salaries, and no employer URL — so the
    output ends where a human pastes a board URL into ``company add``.
    """
    profile = Profile.load(config.profile_path())
    conn = _open_store()
    try:
        leads = discovery.sweep(AdzunaClient(), profile, days=args.days)
        relevant = discovery.one_per_role(discovery.on_target(leads, profile))
        fresh = discovery.untracked(conn, relevant)
    finally:
        conn.close()

    grouped = discovery.by_company(fresh)
    print(f"{len(leads)} adverts, {len(fresh)} from {len(grouped)} companies not yet tracked\n")
    for company, found in grouped.items():
        print(f"{company}")
        for lead in found:
            comp = _comp_note(lead)
            seen = lead.created.date().isoformat() if lead.created else "date unknown"
            print(f"    {lead.title}  —  {lead.location or 'location unstated'}  {seen}{comp}")
        print(f'    winnow company add "{company}" --board <paste their board URL>\n')
    return 0


def _comp_note(lead) -> str:
    """Render a salary, never letting a modelled figure read as a published one."""
    if lead.comp_min is None or lead.comp_max is None:
        return ""
    figure = f"  ${lead.comp_min:,}-${lead.comp_max:,}"
    return f"{figure} (modelled)" if lead.comp_source is CompSource.PREDICTED else figure


def _fetch(_args: argparse.Namespace) -> int:
    """Poll every board once and report what each source did."""
    profile = Profile.load(config.profile_path())
    conn = _open_store()
    try:
        boards = store.active_boards(conn)
        if not boards:
            print("no boards to poll — winnow company add <name> --board <url>")
            return 0
        report = pipeline.run_poll(
            conn, profile, boards, adapter_factory=lambda vendor: adapter_for(vendor)
        )
    finally:
        conn.close()

    print(f"polled {report.boards_polled} boards — {len(report.clusters)} to score")
    if report.suppressed:
        print(f"{report.suppressed} already decided, suppressed")
    for gate, count in sorted(report.tally.items(), key=lambda item: -item[1]):
        print(f"  gated {count:>4}  {gate}")
    for source in report.failed_sources:
        print(f"  {source}: failed — see poll_runs for the error")
    return 0


def _score(_args: argparse.Namespace) -> int:
    """Score every cluster that has not been scored yet."""
    profile = Profile.load(config.profile_path())
    conn = _open_store()
    scored = 0
    failures: list[str] = []
    judge = None
    try:
        for cluster_id, repost_count in learning.clusters_awaiting_score(conn):
            # Built on first use, not up front: resolving a credential on a day
            # with nothing to score would fail a run that had nothing to do.
            if judge is None:
                try:
                    judge = ClaudeJudge()
                except RuntimeError as error:
                    # A missing credential is a configuration problem, and the
                    # journal of an unattended timer should say what to fix
                    # rather than where the stack unwound. Nothing is scored, so
                    # the clusters stay queued for the next run.
                    print(f"cannot score: {error}", file=sys.stderr)
                    return 2
            posting = store.canonical_posting(conn, cluster_id)
            if posting is None:
                continue
            try:
                record = scoring.score_posting(posting, profile, judge, repost_count=repost_count)
            except (scoring.MalformedJudgement, JudgementSchemaError) as error:
                # One unscorable cluster is not a reason to abandon the rest,
                # but it is a reason to say so rather than log nothing.
                failures.append(f"{posting.company} — {posting.title}: {error}")
                continue
            learning.record_score(conn, cluster_id, record)
            scored += 1
        pending = learning.pending_review_count(conn)
    finally:
        conn.close()

    print(f"scored {scored} clusters — {pending} awaiting review")
    for failure in failures:
        print(f"  unscored: {failure}")
    return 0


def _digest(args: argparse.Namespace) -> int:
    """Render the digest and post it, unless there is nothing worth saying."""
    profile = Profile.load(config.profile_path())
    conn = _open_store()
    try:
        assembled = digest.build_digest(conn, profile)
    finally:
        conn.close()

    text = digest.render_text(assembled)
    if args.dry_run:
        print(text)
        return 0

    if not assembled.should_send:
        print("nothing to post")
        return 0

    event_id = notify.build(config.settings().notify).send(text, digest.render_html(assembled))
    print(f"posted {len(assembled.entries)} items as {event_id}")
    return 0


def _review(_args: argparse.Namespace) -> int:
    """Open the review queue.

    This is the only command that runs interactively, and the only one whose
    process ever resolves a credential capable of sending mail. The timer and
    the listener are not trusted to refrain from sending; they are given
    nothing that could.
    """
    from winnow.review.app import ReviewApp

    profile_path = config.profile_path()
    profile = Profile.load(profile_path)
    conn = _open_store()
    try:
        ReviewApp(conn, profile, profile_path).run()
    finally:
        conn.close()
    return 0


def _listen(_args: argparse.Namespace) -> int:
    """Watch the mailbox until asked to stop.

    A long-running process that, like the digest timer, holds no credential
    capable of sending mail. Interview requests escalate to Matrix immediately
    rather than waiting for the next digest.

    SIGTERM is handled explicitly because this runs as PID 1 in its container,
    and the kernel applies no default action to a signal PID 1 has no handler
    for. Without this the process ignored SIGTERM, was killed ten seconds later,
    and systemd recorded a failure — which fired the OnFailure alert to Matrix
    on every ordinary restart, teaching the one channel that is supposed to
    mean something to be ignored.
    """
    import signal

    from winnow import listener

    stopping = threading.Event()

    def asked_to_stop(signum, _frame) -> None:
        """Set the flag and interrupt whatever read is in progress.

        The flag alone is not enough: the loop spends nearly all its time
        blocked in IDLE, for up to fifteen minutes, and podman waits ten
        seconds before SIGKILL. Raising here aborts that read — PEP 475
        retries an interrupted syscall unless the handler raises. KeyboardInterrupt
        specifically, because the listener's reconnect logic catches Exception
        and would otherwise treat being asked to stop as a network fault and
        reconnect.
        """
        logging.getLogger(__name__).info("signal %s received; stopping", signum)
        stopping.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, asked_to_stop)
    signal.signal(signal.SIGINT, asked_to_stop)

    sender = notify.build(config.settings().notify)
    conn = _open_store()
    try:
        listener.run(
            conn,
            escalate=lambda notice: sender.send(notice, f"<p>{notice}</p>"),
            stop=stopping.is_set,
        )
    except KeyboardInterrupt:
        print("stopped")
    finally:
        conn.close()
    return 0


def _run(args: argparse.Namespace) -> int:
    """Do the whole day's work in one process.

    The timer calls this rather than a shell pipeline of four commands: one
    process means one credential resolution, one exit status for systemd to
    report, and no shell quoting in a unit file. It stops at the first failure —
    scoring a board that never answered would produce a digest that quietly
    understates the day.
    """
    steps = (
        ("db", lambda: _db_init()),
        ("fetch", lambda: _fetch(args)),
        ("score", lambda: _score(args)),
        # Before the digest, so a band's numbers are current when it is read.
        ("age", lambda: _age(args)),
        ("digest", lambda: _digest(args)),
        # Last, so nothing can vanish out from under the digest.
        ("prune", lambda: _prune(args)),
    )
    for name, step in steps:
        status = step()
        if status != 0:
            print(f"{name} failed with status {status}; stopping", file=sys.stderr)
            return status
    return 0


def _age(_args: argparse.Namespace) -> int:
    """Record silence as an outcome once an application has waited long enough.

    Nothing else ever writes ``no_response``: the listener only speaks when an
    employer does. Without this step the only applications calibration can see
    are the answered ones, which is the population that flatters every score.
    """
    profile = Profile.load(config.profile_path())
    conn = _open_store()
    try:
        aged = learning.age_submissions(conn, days=profile.silence_window_days)
    finally:
        conn.close()

    if aged:
        print(f"{aged} applications unanswered after {profile.silence_window_days} days")
    return 0


def _prune(_args: argparse.Namespace) -> int:
    """Drop postings that left their board longer ago than the rubric keeps them.

    Scores and decisions survive: they are the training data, and pruning them
    to save a few megabytes of SQLite would be a bad trade.
    """
    profile = Profile.load(config.profile_path())
    conn = _open_store()
    try:
        removed = learning.prune(conn, retention_days=profile.retention_days)
    finally:
        conn.close()
    print(f"pruned {removed} postings older than {profile.retention_days} days")
    return 0


def _notify(args: argparse.Namespace) -> int:
    """Post one message to the Matrix room.

    Deliberately touches neither the store nor the rubric: systemd calls this
    from OnFailure, and a unit that died because its database was unreadable
    still has to be able to say so.
    """
    import html as html_module

    try:
        event_id = notify.build(config.settings().notify).send(
            args.message, f"<p>{html_module.escape(args.message)}</p>"
        )
    except Exception as error:  # noqa: BLE001 - the last thing that can report
        print(f"could not post: {error}", file=sys.stderr)
        return 2
    print(f"posted as {event_id}")
    return 0
