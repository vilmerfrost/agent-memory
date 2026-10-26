"""`amem ingest skills` and `amem skills scores|cache|suggest` (docs/skills.md).

Registered from `cli.build_parser()` via `register(sub, ingest_sub=...)`.
Postgres only: on the REST backend every command exits 1, like `ingest tasks`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config, skill_refresh, skills

REMOTE_REQUIRED_EXIT = 3


def _err(msg: str) -> None:
    print(f"amem: {msg}", file=sys.stderr)


def _postgres_gate(name: str, *, write: bool) -> int | None:
    """Exit code when this command cannot run here, else None."""
    if write:
        error = config.remote_write_error(for_backend="postgres")
        if error is not None:
            print(error, file=sys.stderr)
            return REMOTE_REQUIRED_EXIT
    try:
        backend = config.backend()
    except config.BackendError as exc:
        _err(f"{name} failed: {exc}")
        return 1
    if backend != "postgres":
        _err(f"{name} needs the Postgres backend (backend={backend}); nothing was read or written")
        return 1
    return None


def cmd_ingest_skills(args: argparse.Namespace) -> int:
    if (code := _postgres_gate("ingest skills", write=True)) is not None:
        return code
    path: Path = args.path.expanduser()
    if not path.is_file():
        _err(f"ingest skills failed: no such file: {path}")
        return 1
    read_at = datetime.now(UTC)          # every call logged before this is in the file we read
    try:
        parsed = skills.parse_file(path)
        report = skills.ingest(parsed.calls)
    except (OSError, psycopg.Error) as exc:
        _err(f"ingest skills failed: {type(exc).__name__}: {exc}".strip())
        return 1
    try:                                 # the refresh's input window ends at this read (skill_refresh)
        skill_refresh.record_ingest(path=path.resolve(), read_at=read_at, new=report.new, present=report.present,
                                    unparsed=len(parsed.unparsed))
    except psycopg.Error as exc:
        _err(f"ingest skills: the read time was not recorded ({type(exc).__name__}); the next score refresh "
             "dates its window by the newest ingested row")
    if parsed.unparsed:
        shown = ", ".join(str(n) for n in parsed.unparsed[:20])
        more = " ..." if len(parsed.unparsed) > 20 else ""
        _err(f"unparsed lines: {shown}{more}")
    if parsed.duplicates:
        _err(f"{parsed.duplicates} line(s) repeat an earlier line exactly; kept once")
    if parsed.incomplete_tail:
        _err("skipped the last line: no trailing newline and not whole JSON yet (next ingest picks it up)")
    if report.matched:
        _err(f"matched {report.matched} call(s) to a session")
    print(f"ingested {report.new} new, {report.present} already present, {len(parsed.unparsed)} unparsed")
    return 0


def _print_scores(rows: list[dict[str, Any]]) -> None:
    for r in rows:
        mix = " ".join(f"{k}={v}" for k, v in sorted((r["outcomes"] or {}).items()))
        print(f"{r['skill']}\t{r['bucket']}\tscore={r['score']:.2f}\tcalls={r['n_calls']}"
              f"\tsessions={r['n_sessions']}\tscored={r['n_scored']}\t{mix}".rstrip())


def cmd_scores(args: argparse.Namespace) -> int:
    if (code := _postgres_gate("skills scores", write=args.refresh)) is not None:
        return code
    if args.refresh:
        return _refresh_and_print(args)
    try:
        rows = skills.load_scores()
    except psycopg.Error as exc:
        _err(f"skills scores failed: {type(exc).__name__}: {exc}".strip())
        return 1
    _print_selected(rows, args)
    return 0


def _print_selected(rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    if args.skill:
        rows = [r for r in rows if r["skill"] == args.skill]
    if not args.all_buckets:
        rows = [r for r in rows if r["bucket"] == skills.ALL_BUCKET or args.skill]
    _print_scores(rows)


def _refresh_and_print(args: argparse.Namespace) -> int:
    """A recorded refresh (skill_refresh.run_refresh). The last stdout line is always the run's
    `skills-refresh run=... window_end=... status=...` line: the routines.log wrapper parses it."""
    res = skill_refresh.run_refresh()
    code = 0
    if res.status == "ok":
        start, end = (t.isoformat(timespec="seconds") if t else "-" for t in (res.window_start, res.window_end))
        _err(f"refreshed {res.scored} score row(s); matched {res.matched} more call(s) to a session; "
             f"input window {start} .. {end} (end from: {res.basis})")
        try:
            _print_selected(skills.load_scores(), args)
        except psycopg.Error as exc:
            _err(f"skills scores: refreshed, but printing failed: {type(exc).__name__}")
            code = 1
    else:
        _err(f"skills scores failed: {res.error}" + ("" if res.recorded else " (the run could not be recorded)"))
        code = 1
    print(res.line(), flush=True)
    return code


def cmd_cache(args: argparse.Namespace) -> int:
    if (code := _postgres_gate("skills cache", write=args.refresh)) is not None:
        return code
    try:
        if args.refresh:
            skills.refresh_scores()
        rows = skills.load_scores()
    except psycopg.Error as exc:
        _err(f"skills cache failed: {type(exc).__name__}: {exc}".strip())
        return 1
    if not rows:
        _err("skill_scores is empty: run `amem skills scores --refresh` (or pass --refresh)")
    path: Path = args.path.expanduser()
    try:
        skills.write_cache(path, skills.cache_payload(rows))
    except OSError as exc:
        _err(f"skills cache failed: {type(exc).__name__}: {exc}".strip())
        return 1
    print(f"wrote {path} ({len({r['skill'] for r in rows})} skills, {len(rows)} buckets)")
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    if (code := _postgres_gate("skills suggest", write=False)) is not None:
        return code
    try:
        found = skills.suggest(args.days, args.min)
    except psycopg.Error as exc:
        _err(f"skills suggest failed: {type(exc).__name__}: {exc}".strip())
        return 1
    if not found:
        print(f"no skill run by hand {args.min}+ times in {args.days} days")
    for skill, n in found:
        print(f"{skill}: {n} manual runs in {args.days} days")
    return 0


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def register(sub: Any, ingest_sub: Any = None) -> None:
    """Add `ingest skills` (when `ingest_sub` is given) and the `skills` command group."""
    if ingest_sub is not None:
        p = ingest_sub.add_parser("skills", help=f"ingest the skill-usage log ({skills.DEFAULT_LOG}) into skill_calls")
        p.add_argument("path", type=Path, help="path to log.jsonl")
        p.set_defaults(func=cmd_ingest_skills)

    grp = sub.add_parser("skills", help="skill usage: outcome scores, local cache, manual-run suggestions")
    gsub = grp.add_subparsers(dest="skills_command", required=True)

    p = gsub.add_parser("scores", help="print skill scores (bucket * only unless --all-buckets or --skill)")
    p.add_argument("--refresh", action="store_true", help="match sessions and recompute skill_scores first")
    p.add_argument("--skill", help="only this skill (all its buckets)")
    p.add_argument("--all-buckets", action="store_true", help="print project and category buckets too")
    p.set_defaults(func=cmd_scores)

    p = gsub.add_parser("cache", help="write skill_scores as a small JSON file for fast local reads")
    p.add_argument("path", type=Path, help="where to write the JSON cache")
    p.add_argument("--refresh", action="store_true", help="recompute skill_scores first")
    p.set_defaults(func=cmd_cache)

    p = gsub.add_parser("suggest", help="skills run by hand (by=me) at least --min times in --days days")
    p.add_argument("--days", type=_positive, default=30)
    p.add_argument("--min", type=_positive, default=10)
    p.set_defaults(func=cmd_suggest)
