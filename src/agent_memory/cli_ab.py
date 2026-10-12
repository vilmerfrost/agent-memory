"""`amem ab report` (docs/ab.md).

Exit codes:
- 0 report printed (also when there is no lesson to report).
- 1 REST backend, unknown lesson, bad question file or a database error.
- 2 argparse usage errors.
- 3 AMEM_REQUIRE_REMOTE=1 and AMEM_DB_URL is local or unset.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import psycopg

from agent_memory import ab, config
from agent_memory.questions import QuestionConfigError

REMOTE_REQUIRED_EXIT = 3


def _guard() -> int | None:
    """Postgres only in V1 (exit 1 on REST). AMEM_REQUIRE_REMOTE=1 refuses a local DB (exit 3):
    a verdict read from the local default database would be a wrong answer, not an empty one."""
    try:
        active = config.backend()
    except config.BackendError as exc:
        print(f"amem: ab report failed: {exc}", file=sys.stderr)
        return 1
    if active != "postgres":
        print(f"amem: ab report needs the Postgres backend in V1 (backend={active}); "
              "set AMEM_DB_URL (or AMEM_BACKEND=postgres).", file=sys.stderr)
        return 1
    error = config.remote_write_error(for_backend="postgres")
    if error is not None:
        print(error, file=sys.stderr)
        return REMOTE_REQUIRED_EXIT
    return None


def cmd_report(args: argparse.Namespace) -> int:
    if (code := _guard()) is not None:
        return code
    try:
        entries = ab.report(slug=args.lesson, repo=args.repo, n=args.n, margin=args.margin, since=args.since)
    except ab.AbError as exc:
        print(f"amem: ab report failed: {exc}", file=sys.stderr)
        return 1
    except QuestionConfigError as exc:
        print(f"amem: ab report failed: bad question file: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"amem: ab report failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    if not entries:
        print("amem: ab report: no lessons to report", file=sys.stderr)
        return 0
    if args.json:
        for entry in entries:
            print(json.dumps(entry, sort_keys=True))
    else:
        print("\n\n".join(ab.render_text(e) for e in entries))
    return 0


def _iso(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from exc


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _unit(value: str) -> float:
    x = float(value)
    if not 0 <= x <= 1:
        raise argparse.ArgumentTypeError(f"must be in 0..1, got {x}")
    return x


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    group = sub.add_parser("ab", help="A/B evidence for lessons")
    asub = group.add_subparsers(dest="ab_command", required=True)
    p = asub.add_parser("report", help="per lesson: pass rate with vs without it, verdict, runs to label")
    p.add_argument("--lesson", metavar="SLUG", help="only this lesson (any status)")
    p.add_argument("--repo", help="only lessons scoped to this repo (as `lessons score --repo`)")
    p.add_argument("--n", type=_positive, default=ab.DEFAULT_N,
                   help=f"runs each arm needs before a verdict (default {ab.DEFAULT_N})")
    p.add_argument("--margin", type=_unit, default=ab.DEFAULT_MARGIN,
                   help=f"pass-rate difference beyond which A wins or loses (default {ab.DEFAULT_MARGIN})")
    p.add_argument("--since", type=_iso, help="only runs started at or after this ISO time")
    p.add_argument("--json", action="store_true", help="one JSON line per lesson")
    p.set_defaults(func=cmd_report)
