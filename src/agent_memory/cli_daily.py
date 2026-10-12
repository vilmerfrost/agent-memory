"""`amem daily`: ingest cloud runs, score, push new escalations, log one line (docs/daily.md)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from agent_memory import daily


def cmd_daily(args: argparse.Namespace) -> int:
    try:
        result = daily.run_daily(push=not args.no_push, routines_log=args.routines_log, state=args.state)
    except OSError as exc:
        print(f"amem: daily: cannot append to the routines log: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for line in result.lines:
        print(line)
    return 0 if result.verdict == "PASS" else 1


def register(sub: Any) -> None:
    p = sub.add_parser(
        "daily", help="daily job: ingest git-ref, score every routine, push new escalations, log one line"
    )
    p.add_argument("--no-push", action="store_true",
                   help="never push; new escalations stay unmarked in the state file")
    p.add_argument("--routines-log", type=Path, default=None,
                   help="routines log to append to (default AMEM_ROUTINES_LOG or ~/.claude/parent/routines.log)")
    p.add_argument("--state", type=Path, default=None,
                   help="pushed-ids state file (default AMEM_DAILY_STATE or ~/.claude/parent/amem-daily-state.json)")
    p.set_defaults(func=cmd_daily)
