"""Session timing ingestion, coverage and parent/worker summaries."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from decimal import Decimal

import psycopg

from agent_memory import config


def gate(write: bool) -> str | None:
    if config.backend() != "postgres":
        return "timing needs the Postgres backend"
    return config.remote_write_error(for_backend="postgres") if write else None


def run(args: argparse.Namespace) -> int:
    from agent_memory import timing_store

    try:
        if error := gate(args.timing_action in {"ingest", "sync", "flush", "collect"}):
            print("amem: " + error, file=sys.stderr)
            return 3
        if args.timing_action == "coverage":
            result = timing_store.coverage()
        elif args.timing_action == "analyze":
            from agent_memory import telemetry_analysis
            result = telemetry_analysis.run(days=args.days, limit=args.limit)
        elif args.timing_action == "tests":
            from agent_memory import test_timings
            result = test_timings.report(run_id=args.run, session_id=args.session, limit=args.limit)
        elif args.timing_action == "report":
            result = timing_store.summary(args.session)
            if result is None:
                print("amem: session not found", file=sys.stderr)
                return 1
        elif args.timing_action == "ingest":
            result = asdict(timing_store.ingest_spools([args.path] if args.path else None))
        elif args.timing_action == "sync":
            from agent_memory.timing_sync import sync_transcript
            result = sync_transcript(args.transcript,args.parent_session)
        elif args.timing_action == 'flush':
            from agent_memory.timing_sync import flush_queue
            result = flush_queue(args.queue)
        else:
            from agent_memory.timing_collector import serve

            serve(args.port)
            return 0
    except (OSError, ValueError, psycopg.Error, config.BackendError) as exc:
        # Database errors can contain connection credentials; only expose the class.
        print(f"amem timing failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, default=lambda x: float(x) if isinstance(x,Decimal) else str(x), sort_keys=True))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("timing", help="measured session timing and coverage")
    actions = parser.add_subparsers(dest="timing_action", required=True)
    for name in ("coverage", "report", "tests", "analyze", "ingest", "sync", "flush", "collect"):
        p = actions.add_parser(name)
        p.set_defaults(func=run)
        if name == "report":
            p.add_argument("--session", required=True)
        elif name == "analyze":
            p.add_argument("--days", type=int, choices=range(1,31), default=7)
            p.add_argument("--limit", type=int, choices=range(1,21), default=5)
        elif name == "tests":
            p.add_argument("--run")
            p.add_argument("--session")
            p.add_argument("--limit", type=int, choices=range(1, 101), default=10)
        elif name == "ingest":
            p.add_argument("--path", type=Path)
        elif name == "sync":
            p.add_argument("--transcript", type=Path, required=True)
            p.add_argument("--parent-session")
        elif name == "collect":
            p.add_argument("--port", type=int, default=43927)
        elif name == 'flush':
            p.add_argument('--queue',type=Path)
