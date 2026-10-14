"""`amem sessions ingest|summarize|tokens|cost` and `amem deviations scan|report|reref|rules`
(docs/deviations.md, docs/sessions-live.md). `amem sessions ping` is session_live.main (cli.main
routes it). `sessions summarize` without `--dry-run` needs AMEM_SUMMARY_LIVE=1 (else exit 2).

Postgres only (like the lessons commands): on the REST backend they exit 1 (`rules` needs
no database). Writes honour AMEM_REQUIRE_REMOTE=1 (exit 3). `deviations scan` without
`--dry-run` calls the Claude Code CLI and needs AMEM_DEVIATIONS_LIVE=1 (else exit 2,
nothing called). `reref` never calls the writer or Jev.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config, deviations, questions, sessions_ingest, tokens_report

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
    tools = sessions_ingest.TOOLS if args.tool == "all" else (args.tool,)
    root = sessions_ingest.projects_dir(args.projects_dir)
    if "claude" in tools and not root.is_dir():
        print(f"amem: sessions ingest failed: no such directory: {root}", file=sys.stderr)
        return 1
    try:
        report = sessions_ingest.ingest(root, days=args.days, codex=args.codex_home, tools=tools)
    except (OSError, psycopg.Error) as exc:
        return _err("sessions ingest", exc)
    print(report.line())
    return 0


def cmd_sessions_summarize(args: argparse.Namespace) -> int:
    from agent_memory import session_summary

    if not args.dry_run and not session_summary.live():
        print(f"amem: sessions summarize calls the Claude Code CLI ({session_summary.MODEL}) for every finished "
              f"session and needs live mode: set {session_summary.LIVE_ENV}=1, or use --dry-run. "
              "Nothing was called or written.", file=sys.stderr)
        return USAGE_EXIT
    if (code := (_read_guard if args.dry_run else _write_guard)("sessions summarize")) is not None:
        return code
    try:
        report = session_summary.summarize(days=args.days, budget=args.budget, workers=args.concurrency,
                                           dry_run=args.dry_run, refresh_commits=True, window="started")
    except (OSError, psycopg.Error) as exc:
        return _err("sessions summarize", exc)
    print(report.line(dry_run=args.dry_run))
    return 0


def cmd_sessions_tokens(args: argparse.Namespace) -> int:
    if (code := _read_guard("sessions tokens")) is not None:
        return code
    try:
        rows = tokens_report.fetch(days=args.days, repo=args.repo, tool=args.tool)
    except psycopg.Error as exc:
        return _err("sessions tokens", exc)
    doc = tokens_report.summarize(rows, days=args.days, repo=args.repo, tool=args.tool, top=args.top)
    if args.json:
        print(json.dumps(doc, indent=1, sort_keys=True, default=str))
    else:
        for line in tokens_report.lines(doc, by=tuple(args.by) if args.by else tokens_report.DEFAULT_BY):
            print(line)
    return 0


def _when(value: str) -> datetime:
    """An ISO timestamp; no offset = UTC."""
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def cmd_sessions_cost(args: argparse.Namespace) -> int:
    if (code := _read_guard("sessions cost")) is not None:
        return code
    until = args.until or datetime.now(UTC)
    try:
        doc = tokens_report.cost(since=args.since, until=until, category=args.category, project=args.project)
    except psycopg.Error as exc:
        return _err("sessions cost", exc)
    if args.json:
        print(json.dumps(doc, sort_keys=True))
    else:
        for line in tokens_report.cost_lines(doc, since=args.since, until=until, category=args.category,
                                             project=args.project):
            print(line)
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
    for path in args.files:  # CLAUDE.md, AGENTS.md, SKILL.md, ...: the same markdown rules
        src = deviations.InstructionSource(path.name, [path])
        print(f"--- {path} ---")
        for i, rule in enumerate(deviations.source_catalog(src), 1):
            print(f"{i}. {rule.id} — {rule.title}")
    return 0


def register(sub: Any) -> None:
    s = sub.add_parser("sessions", help="local Claude Code and Codex transcripts (docs/deviations.md)")
    s_sub = s.add_subparsers(dest="sessions_command", required=True)
    p = s_sub.add_parser("ingest", help="index ~/.claude/projects and ~/.codex/sessions transcripts into "
                                        "sessions (idempotent)")
    p.add_argument("--days", type=_positive, default=sessions_ingest.DEFAULT_DAYS,
                   help="transcripts touched in the last N days (default 7)")
    p.add_argument("--projects-dir", type=Path, default=None,
                   help="Claude Code projects folder (default AMEM_PROJECTS_DIR or ~/.claude/projects)")
    p.add_argument("--codex-home", type=Path, default=None,
                   help="Codex home with sessions/ (default AMEM_CODEX_HOME or ~/.codex)")
    p.add_argument("--tool", choices=("all", *sessions_ingest.TOOLS), default="all",
                   help="which transcripts to index (default all)")
    p.set_defaults(func=cmd_sessions_ingest)

    p = s_sub.add_parser("summarize", help="backfill: commits and a 1-3 sentence summary (Haiku) for the finished "
                                           "sessions started in the last N days (docs/sessions-live.md, \"Summary\")")
    p.add_argument("--days", type=_positive, default=sessions_ingest.DEFAULT_DAYS,
                   help="sessions started in the last N days (default 7)")
    p.add_argument("--concurrency", type=_positive, default=4, help="model calls at once (default 4)")
    p.add_argument("--budget", type=float, default=None, help="stop starting new calls after this many seconds "
                                                              "(default: no limit)")
    p.add_argument("--dry-run", action="store_true",
                   help="count what would be done: no model call, nothing written (needs no live mode)")
    p.set_defaults(func=cmd_sessions_summarize)

    p = s_sub.add_parser("tokens", help="token totals per category / project, model, effort, outcome, ... and "
                                        "the top sessions (docs/sessions-live.md)")
    p.add_argument("--days", type=_positive, default=tokens_report.DEFAULT_DAYS,
                   help="sessions active in the last N days (default 7)")
    p.add_argument("--repo", default=None, help="this repo only")
    p.add_argument("--tool", choices=sessions_ingest.TOOLS, default=None, help="this tool only")
    p.add_argument("--top", type=_positive, default=tokens_report.TOP, help="top sessions to list (default 10)")
    p.add_argument("--by", action="append", choices=tokens_report.DIMS, default=None,
                   help="a section to show (repeatable; default: category with its projects, then model)")
    p.add_argument("--json", action="store_true", help="the report as JSON (every section)")
    p.set_defaults(func=cmd_sessions_tokens)

    p = s_sub.add_parser("cost", help="tokens and active / api / tool time of the sessions that started in a "
                                      "window (docs/sessions-live.md, \"Cost of a window\")")
    p.add_argument("--since", type=_when, required=True, help="window start (ISO; no offset = UTC)")
    p.add_argument("--until", type=_when, default=None, help="window end, exclusive (ISO; default now)")
    p.add_argument("--category", choices=sessions_ingest.CATEGORIES, default=None, help="this category only")
    p.add_argument("--project", default=None, help="this project only (the sessions.project value)")
    p.add_argument("--json", action="store_true", help="the totals as one JSON object (the improve contract)")
    p.set_defaults(func=cmd_sessions_cost)

    # Handled in cli.main before parsing (it must exit 0 even for bad arguments).
    s_sub.add_parser("ping", add_help=False,
                     help="Claude Code hook: upsert this session's live row from the hook payload on stdin "
                          "(docs/sessions-live.md); always exits 0")

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
