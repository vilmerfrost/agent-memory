"""`amem` command line."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from agent_memory import cli_cloud, config, jev, routines_log, store
from agent_memory import cli_ab
from agent_memory import cli_archive
from agent_memory import cli_daily
from agent_memory import cli_deviations
from agent_memory import cli_edits
from agent_memory import cli_handoffs
from agent_memory import cli_evals
from agent_memory import cli_rsi
from agent_memory import cli_admin
from agent_memory import cli_integrations
from agent_memory import dedupe
from agent_memory import cli_lessons
from agent_memory import cli_scoring
from agent_memory import cli_tasks
from agent_memory import cli_skills
from agent_memory import cli_synth


def _db_host(url: str) -> str:
    """Host part of the DSN only. Never the password or the full URL."""
    try:
        host = conninfo_to_dict(url).get("host")
    except psycopg.Error:
        return "invalid"
    return str(host) if host else "none"


def _db_reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("select 1")
        return True
    except Exception:
        return False


REMOTE_REQUIRED_EXIT = 3


def _remote_guard(for_backend: str | None = None) -> int | None:
    """Exit code if AMEM_REQUIRE_REMOTE=1 forbids this write, else None."""
    error = config.remote_write_error(for_backend)
    if error is None:
        return None
    print(error, file=sys.stderr)
    return REMOTE_REQUIRED_EXIT


def cmd_doctor(_args: argparse.Namespace) -> int:
    url = config.db_url()
    print(f"jev_mode={jev.mode()}")
    backend, host, ok = cli_cloud.doctor_backend(lambda: (_db_host(url), _db_reachable(url)))
    print(f"backend={backend}")
    print(f"db_host={host}")
    print(f"db={'reachable' if ok else 'unreachable'}")
    print(f"require_remote={'on' if config.require_remote() else 'off'}")
    return 0


def _iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from exc


def cmd_log_run(args: argparse.Namespace) -> int:
    if (code := _remote_guard()) is not None:
        return code
    try:
        run_id = store.insert_run(
            args.routine,
            args.status,
            source=args.source,
            started_at=args.started_at,
            finished_at=args.finished_at,
            summary=args.summary,
            failure_reason=args.failure_reason,
            report_path=args.report_path,
        )
    except (psycopg.Error, config.BackendError) as exc:
        print(f"amem: log-run failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    print(run_id)
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    try:
        rows = store.recent_runs(args.routine, args.limit)
    except (psycopg.Error, config.BackendError) as exc:
        print(f"amem: runs failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    for row in rows:
        print(json.dumps(row, default=str, sort_keys=True))
    return 0


def cmd_ingest_routines_log(args: argparse.Namespace) -> int:
    if (code := _remote_guard()) is not None:
        return code
    path: Path = args.path.expanduser()
    if not path.is_file():
        print(f"amem: ingest failed: no such file: {path}", file=sys.stderr)
        return 1
    parsed = routines_log.parse_file(path)
    try:
        new, present = store.ingest_runs(parsed.runs)
    except (psycopg.Error, config.BackendError) as exc:
        print(f"amem: ingest failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    if parsed.unparsed:
        shown = ", ".join(str(n) for n in parsed.unparsed[:20])
        more = " ..." if len(parsed.unparsed) > 20 else ""
        print(f"amem: unparsed lines: {shown}{more}", file=sys.stderr)
    if parsed.incomplete_tail:
        print("amem: skipped the last line: no trailing newline yet (next ingest picks it up)",
              file=sys.stderr)
    print(f"ingested {new} new, {present} already present, {len(parsed.unparsed)} unparsed")
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    if (code := _remote_guard("postgres")) is not None:
        return code
    from agent_memory import credentials
    try:
        admin = {"db_url": credentials.admin_db_url()} if credentials.admin_present() else {}   # dialog once split
        applied = store.migrate(migrations_dir=args.dir, **admin)
    except credentials.AdminUnavailable as exc:
        print(f"amem: migrate refused: {exc}", file=sys.stderr)
        return 2
    except (psycopg.Error, config.BackendError) as exc:
        print(f"amem: migrate failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    for name in applied:
        print(f"applied {name}")
    print(f"migrations up to date ({len(applied)} applied now)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amem", description="agent-memory: run log and Jev gate.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="print jev mode, db host, db reachability and require_remote")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("log-run", help="insert one run row and print its id")
    p.add_argument("--routine", required=True)
    p.add_argument("--status", required=True, choices=store.STATUSES)
    p.add_argument("--source", choices=store.SOURCES, default="manual")
    p.add_argument("--summary")
    p.add_argument("--failure-reason")
    p.add_argument("--report-path")
    p.add_argument("--started-at", type=_iso)
    p.add_argument("--finished-at", type=_iso)
    p.set_defaults(func=cmd_log_run)

    p = sub.add_parser("runs", help="print recent runs for a routine as JSON lines")
    p.add_argument("--routine", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_runs)

    ingest = sub.add_parser("ingest", help="ingest external logs into runs (idempotent)")
    ingest_sub = ingest.add_subparsers(dest="ingest_command", required=True)
    from agent_memory import dashboard
    dashboard.register(ingest_sub)
    p = ingest_sub.add_parser("routines-log", help="ingest ~/.claude/parent/routines.log lines into runs")
    p.add_argument("path", type=Path, help="path to routines.log")
    p.set_defaults(func=cmd_ingest_routines_log)

    db = sub.add_parser("db", help="database commands")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    p = db_sub.add_parser("migrate", help="apply supabase/migrations/*.sql in order")
    p.add_argument("--dir", type=Path, default=None, help="migrations directory (default: supabase/migrations)")
    p.set_defaults(func=cmd_db_migrate)
    dedupe.register(db_sub)

    cli_cloud.register(sub, ingest_sub=ingest_sub)
    cli_tasks.register(sub, ingest_sub=ingest_sub)
    cli_skills.register(sub, ingest_sub=ingest_sub)
    cli_lessons.register(sub)
    cli_scoring.register(sub)
    cli_ab.register(sub)
    cli_daily.register(sub)
    cli_deviations.register(sub)
    cli_edits.register(sub)
    cli_handoffs.register(sub)
    cli_evals.register(sub)
    cli_rsi.register(sub)
    cli_admin.register(sub)
    cli_integrations.register(sub)
    cli_archive.register(sub)
    cli_synth.register(sub)

    # Handled in main() before parsing: every argument goes to amem_digest as is.
    sub.add_parser("digest-self", add_help=False,
                   help="print this cloud session's digest row for cloud_digests (the package copy of "
                        "tools/amem_digest.py; same arguments, see docs/cloud.md)")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["digest-self"]:
        # Stdlib only and no env: the same code the cloud routine runs as tools/amem_digest.py.
        from agent_memory import amem_digest

        return amem_digest.main(argv[1:])
    if argv[:2] == ["sessions", "ping"]:
        # A Claude Code hook: always exit 0, even for bad arguments (docs/sessions-live.md).
        from agent_memory import session_live

        return session_live.main(argv[2:])
    config.load_env()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
