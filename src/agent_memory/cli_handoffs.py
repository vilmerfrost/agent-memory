"""`amem handoffs log|resume|list` (docs/handoffs.md).

Both backends (Postgres and REST). Writes honour AMEM_REQUIRE_REMOTE=1 (exit 3).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from typing import Any

import psycopg

from agent_memory import config, handoffs

REMOTE_REQUIRED_EXIT = 3


def _err(command: str, exc: BaseException) -> int:
    print(f"amem: handoffs {command} failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
    return 1


def _remote_guard() -> int | None:
    error = config.remote_write_error()
    if error is None:
        return None
    print(error, file=sys.stderr)
    return REMOTE_REQUIRED_EXIT


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a handoff id (uuid): {value!r}") from None


def _non_negative(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be at least 0, got {n}")
    return n


def _positive(value: str) -> int:
    n = _non_negative(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def _pct(value: str) -> float:
    try:
        x = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not 0 <= x <= 100:
        raise argparse.ArgumentTypeError(f"must be 0..100, got {x}")
    return x


def _text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value.strip()


def cmd_log(args: argparse.Namespace) -> int:
    if (code := _remote_guard()) is not None:
        return code
    try:
        hid = handoffs.log(args.session, args.path, project=args.project, reason=args.reason,
                           context_tokens=args.tokens, context_pct=args.pct, title=args.title)
    except (psycopg.Error, config.BackendError) as exc:
        return _err("log", exc)
    print(hid)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    if (code := _remote_guard()) is not None:
        return code
    try:
        got = handoffs.resume(args.id, args.session)
    except (psycopg.Error, config.BackendError) as exc:
        return _err("resume", exc)
    if got.status == "missing":
        print(f"amem: handoffs resume: no handoff with id {args.id}", file=sys.stderr)
        return 1
    if got.status == "taken":
        print(f"amem: handoffs resume: handoff {args.id} was already resumed by session "
              f"{got.resumed_session_id}; nothing changed", file=sys.stderr)
        return 1
    print(f"resumed {args.id} by {args.session}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        rows = handoffs.recent(args.days)
    except (psycopg.Error, config.BackendError) as exc:
        return _err("list", exc)
    for row in rows:
        print(json.dumps(row, default=str, sort_keys=True))
    return 0


def register(sub: Any) -> None:
    h = sub.add_parser("handoffs", help="session handoff events for the RSI loop (docs/handoffs.md)")
    h_sub = h.add_subparsers(dest="handoffs_command", required=True)

    p = h_sub.add_parser("log", help="record one handoff and print its id")
    p.add_argument("--session", required=True, type=_text, help="the session that hands off")
    p.add_argument("--path", required=True, type=_text, help="the handoff file")
    p.add_argument("--project", type=_text, default=None, help="repo or project name")
    p.add_argument("--reason", choices=handoffs.REASONS, default="manual",
                   help="auto (hook / context limit) or manual (asked for); default manual")
    p.add_argument("--tokens", type=_non_negative, default=None, help="context tokens at handoff time")
    p.add_argument("--pct", type=_pct, default=None, help="context use 0..100 at handoff time")
    p.add_argument("--title", type=_text, default=None, help="the sequel chat's title")
    p.set_defaults(func=cmd_log)

    p = h_sub.add_parser("resume", help="mark a handoff as resumed by a new session")
    p.add_argument("--id", required=True, type=_uuid, help="the handoff id (`handoffs log` printed it)")
    p.add_argument("--session", required=True, type=_text, help="the session that resumed")
    p.set_defaults(func=cmd_resume)

    p = h_sub.add_parser("list", help="handoffs of the last N days as JSON lines, newest first")
    p.add_argument("--days", type=_positive, default=handoffs.DEFAULT_DAYS, help="default 7")
    p.set_defaults(func=cmd_list)
