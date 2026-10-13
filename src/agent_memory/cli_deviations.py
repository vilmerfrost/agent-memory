"""`amem sessions ingest` and `amem deviations scan|report|reref|rules` (docs/deviations.md).

Postgres only (like the lessons commands): on the REST backend they exit 1 (`rules` needs
no database). Writes honour AMEM_REQUIRE_REMOTE=1 (exit 3). `deviations scan` without
`--dry-run` calls the Claude Code CLI and needs AMEM_DEVIATIONS_LIVE=1 (else exit 2,
nothing called). `reref` never calls the writer or Jev.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config, deviations, questions, sessions_ingest

USAGE_EXIT = 2
REMOTE_REQUIRED_EXIT = 3


def _err(command: str, exc: BaseException) -> int:
    print(f"amem: {command} failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
    return 1


def _read_guard(command: str) -> int | None:
    try:
        active = config.backend()
    except config.BackendError as exc:
        print(f"amem: {exc}", file=sys.stderr)
        return 1
    if active != "postgres":
        print(f"amem: {command} needs the Postgres backend in V1 (AMEM_DB_URL); backend is {active}. "
              "Nothing was read or written.", file=sys.stderr)
        return 1
    return None


def _write_guard(command: str) -> int | None:
    if (code := _read_guard(command)) is not None:
        return code
    error = config.remote_write_error(for_backend="postgres")
    if error is None:
        return None
    print(error, file=sys.stderr)
    return REMOTE_REQUIRED_EXIT


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def cmd_sessions_ingest(args: argparse.Namespace) -> int:
    if (code := _write_guard("sessions ingest")) is not None:
        return code
    root = sessions_ingest.projects_dir(args.projects_dir)
    if not root.is_dir():
        print(f"amem: sessions ingest failed: no such directory: {root}", file=sys.stderr)
        return 1
    try:
        report = sessions_ingest.ingest(root, days=args.days)
    except (OSError, psycopg.Error) as exc:
        return _err("sessions ingest", exc)
    print(report.line())
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    if not args.dry_run and not deviations.live():
        print(f"amem: deviations scan calls the Claude Code CLI (an LLM) for every session and needs "
              f"live mode: set {deviations.LIVE_ENV}=1, or use --dry-run. Nothing was called or written.",
              file=sys.stderr)
        return USAGE_EXIT
    guard = _read_guard if args.dry_run else _write_guard
    if (code := guard("deviations scan")) is not None:
        return code
    try:
        report = deviations.scan(days=args.days, limit=args.limit, session_id=args.session,
                                 dry_run=args.dry_run, timeout=args.timeout)
    except (questions.QuestionConfigError, psycopg.Error) as exc:
        return _err("deviations scan", exc)
    for o in report.outcomes:
        print(o.line())
    print(report.summary())
    return 1 if report.errors else 0


def cmd_report(args: argparse.Namespace) -> int:
    if (code := _read_guard("deviations report")) is not None:
        return code
    try:
        cands = deviations.candidates(days=args.days)
    except psycopg.Error as exc:
        return _err("deviations report", exc)
    if args.json:
        print(json.dumps(deviations.candidates_doc(cands, args.days), indent=1, sort_keys=True, default=str))
    else:
        for line in deviations.report_lines(cands, args.days):
            print(line)
    return 0


def cmd_reref(args: argparse.Namespace) -> int:
    guard = _read_guard if args.dry_run else _write_guard
    if (code := guard("deviations reref")) is not None:
        return code
    try:
        report = deviations.reref(dry_run=args.dry_run)
    except psycopg.Error as exc:
        return _err("deviations reref", exc)
    for line in report.lines():
        print(line)
    print(report.summary())
    return 0


def cmd_rules(args: argparse.Namespace) -> int:
    """The rule catalog of instruction files (read only, no database)."""
    for path in args.files:
        if path.name.lower().startswith(".env") or not path.is_file():
            print(f"amem: deviations rules: not a readable instruction file: {path}", file=sys.stderr)
            return 1
    for path in args.files:
        src = deviations.InstructionSource(path.name, [path])
        print(f"--- {path} ---")
        for i, rule in enumerate(deviations.source_catalog(src), 1):
            print(f"{i}. {rule.id} — {rule.title}")
    return 0


def register(sub: Any) -> None:
    s = sub.add_parser("sessions", help="local Claude Code transcripts (docs/deviations.md)")
    s_sub = s.add_subparsers(dest="sessions_command", required=True)
    p = s_sub.add_parser("ingest", help="index ~/.claude/projects transcripts into sessions (idempotent)")
    p.add_argument("--days", type=_positive, default=sessions_ingest.DEFAULT_DAYS,
                   help="transcripts touched in the last N days (default 7)")
    p.add_argument("--projects-dir", type=Path, default=None,
                   help="Claude Code projects folder (default AMEM_PROJECTS_DIR or ~/.claude/projects)")
    p.set_defaults(func=cmd_sessions_ingest)

    d = sub.add_parser("deviations", help="where agents deviated from their instructions (docs/deviations.md)")
    d_sub = d.add_subparsers(dest="deviations_command", required=True)
    p = d_sub.add_parser("scan", help="find deviations in unscanned sessions (writer + Jev)")
    p.add_argument("--days", type=_positive, default=deviations.DEFAULT_DAYS)
    p.add_argument("--limit", type=_positive, default=deviations.DEFAULT_LIMIT)
    p.add_argument("--session", default=None, help="scan this session id only (also when already scanned)")
    p.add_argument("--dry-run", action="store_true",
                   help="digest sizes and instructions only: no writer, no Jev, nothing written")
    p.add_argument("--timeout", type=float, default=deviations.DEFAULT_TIMEOUT,
                   help="writer timeout per session in seconds (default 300)")
    p.set_defaults(func=cmd_scan)

    p = d_sub.add_parser("report", help="deviations grouped by instruction, most recurring first")
    p.add_argument("--days", type=_positive, default=deviations.DEFAULT_DAYS)
    p.add_argument("--json", action="store_true", help="the candidates file for the improve ACT step")
    p.set_defaults(func=cmd_report)

    p = d_sub.add_parser("reref", help="backfill rule ids: snap stored refs to the current rule catalog")
    p.add_argument("--dry-run", action="store_true", help="print the mapping, write nothing")
    p.set_defaults(func=cmd_reref)

    p = d_sub.add_parser("rules", help="print the rule catalog (ids + titles) of instruction files")
    p.add_argument("files", nargs="+", type=Path, help="markdown instruction files (read only)")
    p.set_defaults(func=cmd_rules)
