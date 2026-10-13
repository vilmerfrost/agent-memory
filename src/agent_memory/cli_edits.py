"""`amem edits run|list|undo|undo-poll|scoreboard` (docs/edits.md).

Postgres only (like the deviation commands). Writes honour AMEM_REQUIRE_REMOTE=1 (exit 3).
`edits run` calls the writer and `improve apply` only with AMEM_EDITS_LIVE=1; without it,
or with --dry-run, it prints the plan and calls and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import psycopg

from agent_memory import edits, edits_push, edits_writer, scoreboard
from agent_memory.cli_deviations import _positive, _read_guard, _write_guard


def _err(command: str, exc: BaseException) -> int:
    print(f"amem: {command} failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
    return 1


def _confidence(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not 0 <= v <= 1:
        raise argparse.ArgumentTypeError(f"must be in 0..1, got {v}")
    return v


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an edit id: {value!r}") from None


def cmd_run(args: argparse.Namespace) -> int:
    dry = args.dry_run or not edits.live()
    if not args.dry_run and dry:
        print(f"amem: edits run: {edits.LIVE_ENV} is not 1, so this is a dry run (no writer, no improve, "
              "nothing written).", file=sys.stderr)
    guard = _read_guard if dry else _write_guard
    if (code := guard("edits run")) is not None:
        return code
    try:
        report = edits.run(days=args.days, limit=args.limit, min_confidence=args.min_confidence, dry_run=dry,
                           push=not args.no_push, timeout=args.timeout)
    except psycopg.Error as exc:
        return _err("edits run", exc)
    for o in report.outcomes:
        print(o.line())
    print(report.summary())
    return 1 if report.errors else 0


def cmd_list(args: argparse.Namespace) -> int:
    if (code := _read_guard("edits list")) is not None:
        return code
    try:
        rows = edits.list_edits(days=args.days)
    except psycopg.Error as exc:
        return _err("edits list", exc)
    for r in rows:
        print(edits.list_line(r))
    print(f"edits (last {args.days} days): {len(rows)}")
    return 0


def _print_poll(report: edits_push.PollReport) -> int:
    for o in report.outcomes:
        print(o.line())
    print(report.summary())
    return 1 if report.count("retry") or report.count("failed") else 0


def cmd_undo(args: argparse.Namespace) -> int:
    if (code := _write_guard("edits undo")) is not None:
        return code
    try:
        edits_push.request_undo(args.edit_id)
        report = edits_push.undo_poll(push=not args.no_push, edit_id=args.edit_id)
    except edits_push.UndoError as exc:
        print(f"amem: edits undo: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        return _err("edits undo", exc)
    if not report.outcomes:
        print(f"nothing to do: edit {args.edit_id} was already reverted")
    return _print_poll(report)


def cmd_undo_poll(args: argparse.Namespace) -> int:
    if (code := _write_guard("edits undo-poll")) is not None:
        return code
    try:
        report = edits_push.undo_poll(push=not args.no_push)
    except psycopg.Error as exc:
        return _err("edits undo-poll", exc)
    return _print_poll(report)


def cmd_scoreboard(args: argparse.Namespace) -> int:
    if (code := _read_guard("edits scoreboard")) is not None:
        return code
    try:
        items = scoreboard.scores(days=args.days)
    except psycopg.Error as exc:
        return _err("edits scoreboard", exc)
    if args.json:
        print(json.dumps(scoreboard.as_json(items, args.days), indent=1, sort_keys=True))
    else:
        for line in scoreboard.text_lines(items, args.days):
            print(line)
    return 0


def register(sub: Any) -> None:
    e = sub.add_parser("edits", help="edit instructions from deviations, undo, scoreboard (docs/edits.md)")
    e_sub = e.add_subparsers(dest="edits_command", required=True)

    p = e_sub.add_parser("run", help="edit the rules Jev flagged (writer + improve apply); dry run unless live")
    p.add_argument("--dry-run", action="store_true", help="print the plan only: no writer, no improve, no writes")
    p.add_argument("--limit", type=_positive, default=edits.DEFAULT_LIMIT,
                   help=f"at most N edits + suggestions per run (default {edits.DEFAULT_LIMIT})")
    p.add_argument("--min-confidence", type=_confidence, default=edits.DEFAULT_MIN_CONFIDENCE,
                   help=f"Jev confidence needed (default {edits.DEFAULT_MIN_CONFIDENCE})")
    p.add_argument("--days", type=_positive, default=edits.DEFAULT_DAYS, help="deviation window (default 7)")
    p.add_argument("--no-push", action="store_true", help="never push (edits still get an undo token hash)")
    p.add_argument("--timeout", type=float, default=edits_writer.DEFAULT_TIMEOUT,
                   help="writer timeout per candidate in seconds (default 300)")
    p.set_defaults(func=cmd_run)

    p = e_sub.add_parser("list", help="recent edits, newest first")
    p.add_argument("--days", type=_positive, default=28)
    p.set_defaults(func=cmd_list)

    p = e_sub.add_parser("undo", help="revert one edit now (improve revert)")
    p.add_argument("edit_id", type=_uuid)
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_undo)

    p = e_sub.add_parser("undo-poll", help="handle pending phone undo requests (improve revert)")
    p.add_argument("--no-push", action="store_true")
    p.set_defaults(func=cmd_undo_poll)

    p = e_sub.add_parser("scoreboard", help="deviations per session before vs after each edit")
    p.add_argument("--days", type=_positive, default=scoreboard.DEFAULT_DAYS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_scoreboard)
