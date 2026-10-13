"""`amem score | label | escalations | calibrate | drift` (docs/scoring.md)."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

import psycopg

from agent_memory import config, jev, scoring
from agent_memory.questions import QuestionCache, QuestionConfigError

REMOTE_REQUIRED_EXIT = 3


def _backend_guard(name: str) -> int | None:
    """Scoring is Postgres only in V1: refuse the REST backend instead of silently using the default DSN."""
    try:
        active = config.backend()
    except config.BackendError as exc:
        print(f"amem: {name} failed: {exc}", file=sys.stderr)
        return 1
    if active != "postgres":
        print(f"amem: {name} needs the Postgres backend in V1 (backend={active}); "
              "set AMEM_DB_URL (or AMEM_BACKEND=postgres).", file=sys.stderr)
        return 1
    return None


def _remote_guard(name: str) -> int | None:
    if (code := _backend_guard(name)) is not None:
        return code
    error = config.remote_write_error(for_backend="postgres")
    if error is None:
        return None
    print(error, file=sys.stderr)
    return REMOTE_REQUIRED_EXIT


def _iso(value: str) -> datetime:
    """ISO timestamp for --since/--now. Without an offset it is read as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {value!r}") from exc
    return scoring.as_utc(parsed)


def _duration(value: str):
    try:
        return scoring.parse_duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _unit(value: str) -> float:
    x = float(value)
    if not 0 <= x <= 1:
        raise argparse.ArgumentTypeError(f"must be in 0..1, got {x}")
    return x


def _run_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a run id (uuid): {value!r}") from exc


def _emit(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        print(json.dumps(row, default=str, sort_keys=True))


def _guarded(name: str, fn) -> int:
    """Run a command body; map config, Jev and DB errors to exit 1 with a one-line message.

    `score` records per-run Jev errors itself; the JevError branch is a last guard so
    no Jev failure ever ends in a traceback.
    """
    if (code := _backend_guard(name)) is not None:
        return code
    try:
        return fn()
    except QuestionConfigError as exc:
        print(f"amem: {name} failed: bad question file: {exc}", file=sys.stderr)
    except jev.JevError as exc:
        print(f"amem: {name} failed: jev error: {exc}", file=sys.stderr)
    except psycopg.Error as exc:
        print(f"amem: {name} failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
    return 1


def _rescore(args: argparse.Namespace) -> int:
    """`amem score --rescore --routine R`: JSON line per rescored run on stdout, the
    summary on stderr (so stdout stays pure JSON lines)."""
    cache = QuestionCache()
    if cache.get(args.routine) is None:
        print(f"amem: score --rescore: no question file for routine {args.routine!r}", file=sys.stderr)
        return 1
    report = scoring.rescore_runs(routine=args.routine, since=args.since, limit=args.limit, cache=cache)
    _emit(report.comparisons)
    for run_id, routine, msg in report.missing:
        print(f"amem: missing fixture: run {run_id} ({routine}): {msg}", file=sys.stderr)
    for run_id, routine, msg in report.failed:
        print(f"amem: jev error: run {run_id} ({routine}): {msg}", file=sys.stderr)
    c = report.comparisons
    line = (f"rescored {report.scored} runs ({args.routine}), {len(report.missing)} missing fixtures"
            + (f", {len(report.failed)} failed" if report.failed else "")
            + f"; escalated before (old rule) {sum(x['escalated_before_old_rule'] for x in c)}"
            + f", before {sum(x['escalates_before'] for x in c)}, after {sum(x['escalates_after'] for x in c)}")
    print(f"amem: {line}", file=sys.stderr)
    return 1 if report.failed else 0


def cmd_score(args: argparse.Namespace) -> int:
    if (code := _remote_guard("score")) is not None:
        return code
    if args.rescore:
        if args.routine is None:
            print("amem: score --rescore needs --routine (it replaces scores of one routine only)",
                  file=sys.stderr)
            return 1
        return _guarded("score", lambda: _rescore(args))

    def body() -> int:
        report = scoring.score_runs(routine=args.routine, since=args.since, limit=args.limit)
        for run_id, routine, msg in report.missing:
            print(f"amem: missing fixture: run {run_id} ({routine}): {msg}", file=sys.stderr)
        for run_id, routine, msg in report.failed:
            print(f"amem: jev error: run {run_id} ({routine}): {msg}", file=sys.stderr)
        line = (f"scored {report.scored} runs, skipped {report.skipped} (no questions), "
                f"{len(report.missing)} missing fixtures")
        if report.failed:
            line += f", {len(report.failed)} failed"
        print(line)
        return 1 if report.failed else 0

    return _guarded("score", body)


def cmd_label(args: argparse.Namespace) -> int:
    if (code := _remote_guard("label")) is not None:
        return code
    try:
        new = scoring.set_label(args.run_id, args.verdict, args.note)
    except psycopg.errors.ForeignKeyViolation:
        print(f"amem: label failed: no run with id {args.run_id}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"amem: label failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    print(f"labeled {args.run_id} {args.verdict} ({'new' if new else 'relabeled'})")
    return 0


def cmd_escalations(args: argparse.Namespace) -> int:
    def body() -> int:
        rows = scoring.escalations(routine=args.routine)
        _emit(rows)
        print(f"amem: {len(rows)} escalations", file=sys.stderr)
        return 0

    return _guarded("escalations", body)


def cmd_calibrate(args: argparse.Namespace) -> int:
    def body() -> int:
        cache = QuestionCache()
        if args.routine is not None and cache.get(args.routine) is None:
            print(f"amem: calibrate: no question file for routine {args.routine!r}", file=sys.stderr)
            return 1
        _emit(scoring.calibrate(routine=args.routine, min_labels=args.min_labels,
                                target=args.target, cache=cache))
        return 0

    return _guarded("calibrate", body)


def cmd_drift(args: argparse.Namespace) -> int:
    def body() -> int:
        now = args.now or datetime.now(timezone.utc)
        flags, checks = scoring.drift(
            now=now, window=args.window, baseline=args.baseline, routine=args.routine,
            delta=args.delta, min_runs=args.min_runs,
        )
        _emit(flags)
        small = sum(1 for c in checks if not c["enough_samples"])
        print(f"amem: drift: {len(checks)} checks, {len(flags)} flagged, "
              f"{small} skipped (fewer than {args.min_runs} samples in window or baseline)",
              file=sys.stderr)
        return 0

    return _guarded("drift", body)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("score", help="score unscored runs with Jev (routines with a question file)")
    p.add_argument("--routine")
    p.add_argument("--since", type=_iso, help="only runs started at or after this ISO time (no offset = UTC)")
    p.add_argument("--limit", type=_positive_int, default=100, help="max runs to send to Jev (default 100)")
    p.add_argument("--rescore", action="store_true",
                   help="ask Jev again about already scored runs of --routine (newest first) and replace "
                        "their scores; prints a before/after JSON line per run")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("label", help="set the human label of a run (relabel overwrites)")
    p.add_argument("run_id", type=_run_uuid)
    p.add_argument("verdict", choices=("up", "down"))
    p.add_argument("--note")
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("escalations", help="unlabeled runs with low Jev confidence, as JSON lines")
    p.add_argument("--routine")
    p.set_defaults(func=cmd_escalations)

    p = sub.add_parser("calibrate", help="Jev vs label agreement and a suggested threshold per routine")
    p.add_argument("--routine")
    p.add_argument("--min-labels", type=_positive_int, default=scoring.DEFAULT_MIN_LABELS)
    p.add_argument("--target", type=_unit, default=scoring.DEFAULT_TARGET, help="agreement target (default 0.9)")
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("drift", help="routines whose pass rate or Jev pass signal dropped, as JSON lines")
    p.add_argument("--routine")
    p.add_argument("--window", type=_duration, default=scoring.parse_duration("7d"))
    p.add_argument("--baseline", type=_duration, default=scoring.parse_duration("28d"))
    p.add_argument("--delta", type=_unit, default=scoring.DEFAULT_DELTA, help="flag when the drop exceeds this")
    p.add_argument("--min-runs", type=_positive_int, default=scoring.DEFAULT_MIN_RUNS)
    p.add_argument("--now", type=_iso, help="end of the window, ISO time, no offset = UTC (default: now); for replays and tests")
    p.set_defaults(func=cmd_drift)
