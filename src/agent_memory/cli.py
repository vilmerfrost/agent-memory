"""`amem` command line."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from agent_memory import config, jev, store


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


def cmd_doctor(_args: argparse.Namespace) -> int:
    url = config.db_url()
    print(f"jev_mode={jev.mode()}")
    print(f"db_host={_db_host(url)}")
    print(f"db={'reachable' if _db_reachable(url) else 'unreachable'}")
    return 0


def _iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from exc


def cmd_log_run(args: argparse.Namespace) -> int:
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
    except psycopg.Error as exc:
        print(f"amem: log-run failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    print(run_id)
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    try:
        rows = store.recent_runs(args.routine, args.limit)
    except psycopg.Error as exc:
        print(f"amem: runs failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    for row in rows:
        print(json.dumps(row, default=str, sort_keys=True))
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    try:
        applied = store.migrate(migrations_dir=args.dir)
    except psycopg.Error as exc:
        print(f"amem: migrate failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    for name in applied:
        print(f"applied {name}")
    print(f"migrations up to date ({len(applied)} applied now)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amem", description="agent-memory: run log and Jev gate.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="print jev mode, db host and db reachability")
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

    db = sub.add_parser("db", help="database commands")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    p = db_sub.add_parser("migrate", help="apply supabase/migrations/*.sql in order")
    p.add_argument("--dir", type=Path, default=None, help="migrations directory (default: supabase/migrations)")
    p.set_defaults(func=cmd_db_migrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
