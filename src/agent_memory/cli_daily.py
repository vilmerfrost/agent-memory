"""`amem daily`: ingest cloud runs, score, count new escalations, scan deviations, log one line (docs/daily.md)."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_memory import daily


def cmd_recover(args: argparse.Namespace) -> int:
    from agent_memory import daily_recovery
    try:
        status = daily_recovery.recover(state=daily.state_path(args.state),
                                        routines_log=daily.routines_log_path(args.routines_log),
                                        push=not args.no_push)
    except (OSError, ValueError) as exc:
        print(f"amem: auth recovery failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    failed = status in ("daily_failed", "expired", "night_pending", "brief_pending")
    line = f"{datetime.now().astimezone().strftime(daily.TS_FORMAT)} amem-auth-recovery {'FAIL' if failed else 'PASS'} status={status}"
    log = daily.routines_log_path(args.routines_log)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        print("amem: auth recovery cannot append routine status", file=sys.stderr)
        return 1
    print(line)
    return 1 if failed else 0


def cmd_daily(args: argparse.Namespace) -> int:
    try:
        result = daily.run_daily(push=not args.no_push, routines_log=args.routines_log, state=args.state)
    except OSError as exc:
        print(f"amem: daily: cannot append to the routines log: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for line in result.lines:
        print(line)
    return 0 if result.verdict == "PASS" else 1


def cmd_maintenance(args):
    from agent_memory import client_maintenance, config
    log = daily.routines_log_path(args.routines_log)
    result = client_maintenance.run(log=log, config_path=args.config or config.REPO_ROOT/'config/rsi-night.subscription.json',
                                    push=not args.no_push)
    status = 'FAIL' if result['status']=='failed' else 'PASS' if result['status']=='ready' else 'SKIP'
    line = f"{datetime.now().astimezone().strftime(daily.TS_FORMAT)} amem-client-maintenance {status} status={result['status']} reason={result.get('reason','none')}"
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('a') as fh:
        fh.write(line+'\n')
    print(line)
    return 1 if status=='FAIL' else 0


def register(sub: Any) -> None:
    maintenance = sub.add_parser('client-maintenance', help='bounded midnight CLI updates and readiness')
    maintenance.add_argument('--no-push',action='store_true')
    maintenance.add_argument('--routines-log',type=Path)
    maintenance.add_argument('--config',type=Path)
    maintenance.set_defaults(func=cmd_maintenance)
    recovery = sub.add_parser("daily-recover", help="resume auth-blocked daily and night jobs, bounded until noon")
    recovery.add_argument("--no-push", action="store_true")
    recovery.add_argument("--state", type=Path)
    recovery.add_argument("--routines-log", type=Path)
    recovery.set_defaults(func=cmd_recover)
    p = sub.add_parser(
        "daily", help="daily job (00:40): ingest git-ref, score every routine, count new escalations (the 07:30 "
                      "brief pushes them), scan deviations, log one line"
    )
    p.add_argument("--no-push", action="store_true",
                   help="never push (only the legacy AMEM_DAILY_EDITS=1 edits step pushes from here)")
    p.add_argument("--routines-log", type=Path, default=None,
                   help="routines log to append to (default AMEM_ROUTINES_LOG or ~/.claude/parent/routines.log)")
    p.add_argument("--state", type=Path, default=None,
                   help="pushed-ids state file (default AMEM_DAILY_STATE or ~/.claude/parent/amem-daily-state.json)")
    p.set_defaults(func=cmd_daily)
