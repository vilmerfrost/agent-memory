"""`amem evals harvest` (docs/evals-harvest.md).

Postgres only (it reads deviations, sessions and handoffs). Writes draft files into `--out`
and logs one `evals-harvest` run row with the `log-run` code path (`store.insert_run`).
`--dry-run` reads only: no files, no run row. Writes honour AMEM_REQUIRE_REMOTE=1 (exit 3).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import cli_deviations, config, coverage_daily, evals_harvest, store


def _log_run(status: str, started: datetime, summary: str, *, source: str,
             failure: str | None = None, meta: dict | None = None) -> Any:
    """Insert the run row; its id, or None when the log failed."""
    try:
        return store.insert_run(evals_harvest.ROUTINE, status, source=source, started_at=started,
                                finished_at=datetime.now(UTC), summary=summary, failure_reason=failure, meta=meta)
    except (psycopg.Error, config.BackendError) as exc:
        print(f"amem: evals harvest: run log failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return None


def _coverage(args: argparse.Namespace, started: datetime, run_id: Any, report: Any = None,
              error: str | None = None) -> None:
    """Coverage records of this harvest (job rsi-harvest, coverage_daily). A problem is a stderr note only."""
    if not coverage_daily.enabled():
        return
    try:
        records = coverage_daily.harvest_records(report, now=started, days=args.days, limit=args.limit,
                                                 run_id=run_id, error=error)
        notes = coverage_daily.write(records)
    except Exception as exc:  # noqa: BLE001 - coverage never fails the harvest
        notes = [f"coverage: {type(exc).__name__}: {str(exc)[:160]}"]
    for note in notes:
        print(f"amem: evals harvest: {note}", file=sys.stderr)


def cmd_evals_harvest(args: argparse.Namespace) -> int:
    guard = cli_deviations._read_guard if args.dry_run else cli_deviations._write_guard
    if (code := guard("evals harvest")) is not None:
        return code
    out: Path = args.out.expanduser()
    if out.exists() and not out.is_dir():
        print(f"amem: evals harvest failed: not a directory: {out}", file=sys.stderr)
        return 1
    started = datetime.now(UTC)
    try:
        report = evals_harvest.harvest(out, days=args.days, limit=args.limit, dry_run=args.dry_run, now=started)
    except (OSError, psycopg.Error) as exc:
        if not args.dry_run:
            run_id = _log_run("error", started, "evals harvest failed", source=args.source,
                              failure=f"{type(exc).__name__}: {exc}"[:500])
            _coverage(args, started, run_id, error=f"evals harvest: {type(exc).__name__}: {exc}")
        return cli_deviations._err("evals harvest", exc)
    line = report.line(dry_run=args.dry_run)
    for name in report.drafted:
        print(f"{'plan' if args.dry_run else 'draft'} {out / name}")
    if not report.handoffs_table:
        print("note: no public.handoffs table yet; 0 handoffs read")
    print(line)
    if not args.dry_run:
        run_id = _log_run("pass", started, line, source=args.source, meta=report.meta(out))
        _coverage(args, started, run_id, report)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    evals = sub.add_parser("evals", help="eval case drafts from the loop (docs/evals-harvest.md)")
    evals_sub = evals.add_subparsers(dest="evals_command", required=True)
    p = evals_sub.add_parser("harvest", help="write one draft eval case per edit_instruction deviation, "
                                             "worker incident and handoff (idempotent)")
    p.add_argument("--out", type=Path, required=True, help="directory for the draft case files")
    p.add_argument("--days", type=cli_deviations._positive, default=evals_harvest.DEFAULT_DAYS)
    p.add_argument("--limit", type=cli_deviations._positive, default=evals_harvest.DEFAULT_LIMIT,
                   help="most new drafts per run (the rest count as skipped: limit)")
    p.add_argument("--dry-run", action="store_true", help="print the plan; write no file and no run row")
    p.add_argument("--source", choices=store.SOURCES, default="manual", help="run log source (default manual)")
    p.set_defaults(func=cmd_evals_harvest)
