"""`amem ingest tasks`: dispatch tasks into runs (docs/tasks.md).

Registered from `cli.build_parser()` via `register(sub, ingest_sub=...)`.
Postgres only in V1: on the REST backend it exits 1, like the scoring commands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config, tasks_ingest

REMOTE_REQUIRED_EXIT = 3


def _err(msg: str) -> None:
    print(f"amem: {msg}", file=sys.stderr)


def notes(parsed: tasks_ingest.TasksParse, report: tasks_ingest.IngestReport | None = None) -> list[str]:
    """stderr detail lines: unparsed lines, duplicates, skipped tails, refreshed rows."""
    out: list[str] = []
    if parsed.unparsed:
        shown = ", ".join(f"{p.parent.name}/{p.name}:{n}" for p, n in parsed.unparsed[:20])
        more = " ..." if len(parsed.unparsed) > 20 else ""
        out.append(f"unparsed task lines: {shown}{more}")
    if parsed.duplicates:
        out.append(f"{parsed.duplicates} task line(s) repeat an earlier task id; the last line wins")
    for path in parsed.incomplete:
        out.append(f"skipped the last line of {path.parent.name}/{path.name}: "
                   "no trailing newline and not a whole JSON object yet (next ingest picks it up)")
    if report is not None and report.updated:
        out.append(f"refreshed {report.updated} task(s) whose line or checkpoint excerpt changed")
    return out


def cmd_ingest_tasks(args: argparse.Namespace) -> int:
    error = config.remote_write_error(for_backend="postgres")
    if error is not None:
        print(error, file=sys.stderr)
        return REMOTE_REQUIRED_EXIT
    try:
        backend = config.backend()
    except config.BackendError as exc:
        _err(f"ingest tasks failed: {exc}")
        return 1
    if backend != "postgres":
        _err(f"ingest tasks needs the Postgres backend in V1 (backend={backend}); nothing was read or written")
        return 1
    parent: Path = (args.parent_dir or tasks_ingest.DEFAULT_PARENT_DIR).expanduser()
    if not parent.is_dir():
        _err(f"ingest tasks failed: no such directory: {parent}")
        return 1
    try:
        parsed = tasks_ingest.parse_parent(parent)
        report = tasks_ingest.ingest(parsed.runs)
    except (OSError, psycopg.Error) as exc:
        _err(f"ingest tasks failed: {type(exc).__name__}: {exc}".strip())
        return 1
    for line in notes(parsed, report):
        _err(line)
    print(tasks_ingest.report_line(parsed, report))
    return 0


def register(sub: Any, ingest_sub: Any = None) -> None:
    """Add `ingest tasks`. `ingest_sub` is the `ingest` subparsers action."""
    if ingest_sub is None:
        return
    p = ingest_sub.add_parser("tasks", help="ingest dispatch tasks (<parent-dir>/<repo>/tasks.jsonl) into runs")
    p.add_argument("--parent-dir", type=Path, default=None,
                   help=f"parent layer folder (default {tasks_ingest.DEFAULT_PARENT_DIR})")
    p.set_defaults(func=cmd_ingest_tasks)
