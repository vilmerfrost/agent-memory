"""`amem dispatch start|end|refresh|report|ingest`: data-based dispatch estimates.

start/end never touch the network or the database and always exit 0: an estimate
must never block a dispatch. Contract (agent-config reads it): the first stdout
line is `run_id=<uuid>`, the second is the ready `Expect:` (start) or `Actual:` (end) line.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _start(args: argparse.Namespace) -> int:
    from agent_memory import dispatch_estimates as D

    try:
        result = D.start(args.repo, args.workers, brief_chars=args.brief_chars,
                         files_claimed=args.files_claimed, tool=args.tool, session=args.session)
    except (OSError, ValueError) as exc:
        # Still exit 0: the dispatch goes on; the run is just not recorded.
        print("run_id=none")
        print(f"Expect: not recorded ({type(exc).__name__}: dispatch estimates unavailable)")
        return 0
    print(f"run_id={result['run_id']}")
    print(result["line"])
    return 0


def _end(args: argparse.Namespace) -> int:
    from agent_memory import dispatch_estimates as D

    try:
        result = D.end(args.run_id, session=args.session, status=args.status,
                       integration_test_ms=args.integration_test_ms)
    except (OSError, ValueError) as exc:
        print(f"run_id={args.run_id or 'none'}")
        print(f"Actual: not recorded ({type(exc).__name__})")
        return 0
    print(f"run_id={result['run_id'] or 'none'}")
    print(result["line"])
    return 0


def _db_source():
    """A read-only database source for refresh, or None (offline, unset or REST backend)."""
    from agent_memory import config

    if not os.environ.get("AMEM_DB_URL", "").strip() or config.backend() != "postgres":
        return None, None
    import psycopg
    from agent_memory import dispatch_store

    try:
        conn = psycopg.connect(config.db_url(), connect_timeout=3, autocommit=True)
    except psycopg.Error:
        return None, None
    return dispatch_store.DbSource(conn), conn


def _refresh(args: argparse.Namespace) -> int:
    from agent_memory import dispatch_estimates as D

    source, conn = _db_source()
    try:
        lookup = D.refresh(repo=args.repo, db=source)
    except (OSError, ValueError) as exc:
        print(f"amem dispatch refresh failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()
    print(D.refresh_line(lookup))
    if source is None:
        print("note: database not reached; tokens and test medians kept from the last refresh")
    for note in lookup.get("notes", []):
        print("note: " + note)
    return 0


def _report(args: argparse.Namespace) -> int:
    from agent_memory import dispatch_estimates as D

    for row in D.report(limit=args.limit):
        print(json.dumps(row, sort_keys=True))
    return 0


def _ingest(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    import psycopg
    from agent_memory import config, dispatch_estimates as D, timing_store

    if config.backend() != "postgres":
        print("amem: dispatch ingest needs the Postgres backend", file=sys.stderr)
        return 3
    if error := config.remote_write_error(for_backend="postgres"):
        print("amem: " + error, file=sys.stderr)
        return 3
    parent = D.parent_dir()
    try:
        result = timing_store.ingest_spools(root=parent, kind="dispatch",
                                            cursor_path=parent / "dispatch-cursors.json")
    except (OSError, ValueError, psycopg.Error, config.BackendError) as exc:
        print(f"amem dispatch ingest failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), sort_keys=True))
    return 0


def _count(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("dispatch", help="dispatch run marks and data-based time/usage estimates")
    actions = parser.add_subparsers(dest="dispatch_action", required=True)

    p = actions.add_parser("start", help="mark a dispatch; print run_id and one Expect line")
    p.add_argument("--repo", required=True)
    p.add_argument("--workers", type=_count, required=True)
    p.add_argument("--brief-chars", type=_count)
    p.add_argument("--files-claimed", type=_count)
    p.add_argument("--tool", choices=("claude", "codex"))
    p.add_argument("--session")
    p.set_defaults(func=_start)

    p = actions.add_parser("end", help="mark the brief; finds the open run by session when run_id is omitted")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--session")
    p.add_argument("--status", default="done", choices=("done", "partial", "blocked", "failed", "abandoned"))
    p.add_argument("--integration-test-ms", type=_count)
    p.set_defaults(func=_end)

    p = actions.add_parser("refresh", help="recompute ~/.claude/parent/dispatch-estimates.json")
    p.add_argument("--repo", help="only look up tokens for this repo's runs")
    p.set_defaults(func=_refresh)

    p = actions.add_parser("report", help="estimate vs actual per finished run (JSON lines)")
    p.add_argument("--limit", type=int, choices=range(1, 201), default=20)
    p.set_defaults(func=_report)

    p = actions.add_parser("ingest", help="upload dispatch-events.jsonl spools now (the collector does it too)")
    p.set_defaults(func=_ingest)
