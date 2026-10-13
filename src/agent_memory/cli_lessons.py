"""`amem lessons ...`, `amem lesson propose` and `amem gate friction`.

Exit codes:
- 0 success (for `lesson propose`: a decision was made, accept or reject).
- 1 a database, file or input error; for `gate friction`: reject.
- 2 `gate friction` could not decide (Jev error); argparse usage errors; for
  `lessons score`: AMEM_SCORE_MIN_USES is not a positive integer (nothing is read or written).
- 3 AMEM_REQUIRE_REMOTE=1 refused a write (same as the other write commands).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg

from agent_memory import config, jev, lessons

REMOTE_REQUIRED_EXIT = 3
USAGE_EXIT = 2

DEFAULT_MIN_REUSABLE = 0.6
DEFAULT_MIN_GENERALIZABLE = 0.5
DEFAULT_FRICTION_MIN = 0.5
DEFAULT_MIN_USES = 3


def _err(command: str, exc: BaseException) -> int:
    print(f"amem: {command} failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
    return 1


def _remote_guard() -> int | None:
    """Exit code when the command may not run here, else None.

    Lessons live in Postgres only (V1). On the REST backend the runs live elsewhere,
    so a lessons command would read and write a different store: refuse (exit 1).
    AMEM_REQUIRE_REMOTE=1 is checked against Postgres, like `db migrate` (exit 3).
    """
    if (code := _read_guard()) is not None:
        return code
    error = config.remote_write_error(for_backend="postgres")
    if error is None:
        return None
    print(error, file=sys.stderr)
    return REMOTE_REQUIRED_EXIT


def _read_guard() -> int | None:
    """Backend check for read-only commands (no AMEM_REQUIRE_REMOTE check)."""
    try:
        active = config.backend()
    except config.BackendError as exc:
        print(f"amem: {exc}", file=sys.stderr)
        return 1
    if active != "postgres":
        print("amem: lessons commands need the Postgres backend in V1 (AMEM_DB_URL); "
              f"backend is {active}. Set AMEM_BACKEND=postgres.", file=sys.stderr)
        return 1
    return None


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a uuid: {value!r}") from exc


def _positive(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (>= 1), got {value!r}")
    return n


def _min_uses(flag: int | None) -> int:
    """--min-uses, else AMEM_SCORE_MIN_USES, else the default. The env value gets the
    same check as the flag (a positive integer); LessonsError when it fails."""
    if flag is not None:
        return flag
    raw = os.environ.get("AMEM_SCORE_MIN_USES", "").strip()
    if not raw:
        return DEFAULT_MIN_USES
    try:
        return _positive(raw)
    except argparse.ArgumentTypeError as exc:
        raise lessons.LessonsError(f"AMEM_SCORE_MIN_USES {exc}") from None


# --- lessons import / export -----------------------------------------------------------


def cmd_import(args: argparse.Namespace) -> int:
    if (code := _remote_guard()) is not None:
        return code
    path: Path = args.path.expanduser()
    if not path.is_file():
        print(f"amem: lessons import failed: no such file: {path}", file=sys.stderr)
        return 1
    try:
        parsed = lessons.parse_markdown(path.read_text(encoding="utf-8"))
        new, updated, _unchanged = lessons.upsert(parsed)
    except (lessons.LessonsError, psycopg.Error) as exc:
        return _err("lessons import", exc)
    print(f"imported {new} new, {updated} updated")
    return 0


def cmd_export(_args: argparse.Namespace) -> int:
    if (code := _read_guard()) is not None:
        return code
    try:
        rows = lessons.all_lessons()
    except psycopg.Error as exc:
        return _err("lessons export", exc)
    sys.stdout.write(lessons.render_markdown(rows))
    return 0


# --- lessons for -------------------------------------------------------------------------


def cmd_for(args: argparse.Namespace) -> int:
    if not lessons.memory_enabled():
        return 0  # A/B switch: no output, nothing recorded
    if (code := _remote_guard()) is not None:
        return code
    try:
        floor = (args.min_relative_rank if args.min_relative_rank is not None
                 else lessons.env_float("AMEM_LESSONS_MIN_RELATIVE_RANK", lessons.DEFAULT_MIN_RELATIVE_RANK))
        hits = lessons.lessons_for(args.repo, args.task, limit=args.limit, run_id=args.run,
                                   min_relative_rank=floor)
    except (lessons.LessonsError, psycopg.Error) as exc:
        return _err("lessons for", exc)
    if hits:
        sys.stdout.write(lessons.render_brief_block(hits))
    return 0


# --- lessons score -----------------------------------------------------------------------


def cmd_score(args: argparse.Namespace) -> int:
    try:
        min_uses = _min_uses(args.min_uses)
    except lessons.LessonsError as exc:
        print(f"amem: lessons score: {exc}", file=sys.stderr)
        return USAGE_EXIT  # like a bad --min-uses; checked before any database access
    if (code := (_remote_guard() if args.apply else _read_guard())) is not None:
        return code
    try:
        rows = lessons.score(min_uses, apply=args.apply, repo=args.repo)
    except (lessons.LessonsError, psycopg.Error) as exc:
        return _err("lessons score", exc)
    for row in rows:
        print(json.dumps(row, sort_keys=True))
    return 0


# --- lesson propose (Jev write gate) ------------------------------------------------------


def cmd_propose(args: argparse.Namespace) -> int:
    if (code := _remote_guard()) is not None:
        return code
    try:
        min_reusable = (args.min_reusable if args.min_reusable is not None
                        else lessons.env_float("AMEM_PROPOSE_MIN_REUSABLE", DEFAULT_MIN_REUSABLE))
        min_general = (args.min_generalizable if args.min_generalizable is not None
                       else lessons.env_float("AMEM_PROPOSE_MIN_GENERALIZABLE", DEFAULT_MIN_GENERALIZABLE))
        run = lessons.get_run(args.run)
        if run is None:
            print(f"amem: lesson propose failed: no run with id {args.run}", file=sys.stderr)
            return 1
        state = lessons.propose_state(run, args.repo, args.task)
        results = jev.ask(state, lessons.PROPOSE_QUESTIONS)
    except (lessons.LessonsError, jev.JevError, psycopg.Error) as exc:
        return _err("lesson propose", exc)

    reusable, general = results["reusable"], results["generalizable"]
    p_reusable, g_score = float(reusable.value), float(general.value)
    accept = p_reusable >= min_reusable and g_score >= min_general
    detail = (f"reusable={p_reusable:.2f} (min {min_reusable:.2f}) "
              f"generalizable={g_score:.2f} (min {min_general:.2f}, confidence {general.confidence or 0:.2f})")
    if not accept:
        print(f"reject {detail}")
        return 0
    lesson = lessons.template_lesson(run, args.repo, args.task, results)
    try:
        created = lessons.insert_new(lesson)
    except psycopg.Error as exc:
        return _err("lesson propose", exc)
    note = "stored as trial" if created else "already stored, left unchanged"
    print(f"accept {detail} -> lesson {lesson.slug} ({note})")
    return 0


# --- gate friction (improver gate) ------------------------------------------------------


def cmd_friction(args: argparse.Namespace) -> int:
    try:
        threshold = (args.min if args.min is not None
                     else lessons.env_float("AMEM_FRICTION_MIN", DEFAULT_FRICTION_MIN))
        if not args.text.strip():
            raise lessons.LessonsError("--text is empty")
        results = jev.ask(lessons.friction_state(args.text, args.seen), lessons.FRICTION_QUESTIONS)
    except (lessons.LessonsError, jev.JevError) as exc:
        _err("gate friction", exc)
        return 2
    p = float(results["recurring"].value)
    accept = p >= threshold
    confidence = p if accept else 1 - p
    print(f"{'accept' if accept else 'reject'} confidence={confidence:.2f} "
          f"p_recurring={p:.2f} threshold={threshold:.2f}")
    return 0 if accept else 1


# --- parser wiring -------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    group = sub.add_parser("lessons", help="lessons: import/export lessons.md, retrieve, score")
    lsub = group.add_subparsers(dest="lessons_command", required=True)

    p = lsub.add_parser("import", help="upsert lessons from a lessons.md file (by Id)")
    p.add_argument("path", type=Path)
    p.set_defaults(func=cmd_import)

    p = lsub.add_parser("export", help="print every lesson in lessons.md format")
    p.set_defaults(func=cmd_export)

    p = lsub.add_parser("for", help="markdown block of the best lessons for a task; records each use")
    p.add_argument("--repo", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--limit", type=_positive, default=5)
    p.add_argument("--run", type=_uuid, help="run id to link the recorded uses to")
    p.add_argument("--min-relative-rank", type=float, default=None,
                   help=f"drop hits ranked below this share of the top hit (default "
                        f"{lessons.DEFAULT_MIN_RELATIVE_RANK}, or AMEM_LESSONS_MIN_RELATIVE_RANK; 0 keeps all)")
    p.set_defaults(func=cmd_for)

    p = lsub.add_parser("score", help="pass rate with vs without each lesson; JSON lines")
    p.add_argument("--min-uses", type=_positive, default=None,
                   help=f"uses needed before a lesson can be retired (default {DEFAULT_MIN_USES}, "
                        "or AMEM_SCORE_MIN_USES)")
    p.add_argument("--repo", help="only lessons scoped to this repo")
    p.add_argument("--apply", action="store_true", help="write `retired` (default: dry run)")
    p.set_defaults(func=cmd_score)

    single = sub.add_parser("lesson", help="lesson write gate")
    ssub = single.add_subparsers(dest="lesson_command", required=True)
    p = ssub.add_parser("propose", help="ask Jev whether a run holds a reusable lesson; store it as trial")
    p.add_argument("--run", required=True, type=_uuid)
    p.add_argument("--repo")
    p.add_argument("--task")
    p.add_argument("--min-reusable", type=float, default=None,
                   help=f"min P(reusable) (default {DEFAULT_MIN_REUSABLE}, or AMEM_PROPOSE_MIN_REUSABLE)")
    p.add_argument("--min-generalizable", type=float, default=None,
                   help=f"min generalizable score 0-1 (default {DEFAULT_MIN_GENERALIZABLE}, "
                        "or AMEM_PROPOSE_MIN_GENERALIZABLE)")
    p.set_defaults(func=cmd_propose)

    gate = sub.add_parser("gate", help="Jev gates")
    gsub = gate.add_subparsers(dest="gate_command", required=True)
    p = gsub.add_parser("friction", help="improver gate: is this friction recurring? exit 0 accept, 1 reject")
    p.add_argument("--text", required=True)
    p.add_argument("--seen", type=int, default=None, help="how many times this friction was seen")
    p.add_argument("--min", type=float, default=None,
                   help=f"min P(recurring) (default {DEFAULT_FRICTION_MIN}, or AMEM_FRICTION_MIN)")
    p.set_defaults(func=cmd_friction)
