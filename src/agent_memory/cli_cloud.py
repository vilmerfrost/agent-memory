"""Cloud-facing `amem` commands: `ingest git-ref`, `jev record`, and the doctor backend probe.

Registered from `cli.build_parser()` via `register(sub, ingest_sub=...)`.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg

from agent_memory import backend_rest, config, jev, routines_log, store

REMOTE_REQUIRED_EXIT = 3
NOT_LIVE_EXIT = 2

# ---------------------------------------------------------------- doctor


def doctor_backend(postgres_probe: Callable[[], tuple[str, bool]]) -> tuple[str, str, bool]:
    """(backend, db_host, reachable). REST: host of AMEM_SUPABASE_URL plus a cheap GET."""
    try:
        backend = config.backend()
    except config.BackendError:
        return ("invalid", *postgres_probe())
    if backend == "rest":
        url = config.supabase_url()
        host = config.rest_host(url) if url else "none"
        return backend, host, bool(url) and backend_rest.reachable()
    return (backend, *postgres_probe())


# ---------------------------------------------------------------- ingest git-ref

GIT_REF_REPO = "vilmerfrost/agent-config"
GIT_REF_BRANCH = "claude/routine-reports"
GIT_REF_PATH = "reports/routines.log"
GIT_REF_LOG_SOURCE = "routine-reports"


class FetchError(RuntimeError):
    """Reading the published log failed for a reason other than 'not there yet'."""


def run_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Runs gh / git (read-only calls only). Tests replace this; they never hit the network."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def _not_found(result: subprocess.CompletedProcess[str]) -> bool:
    text = f"{result.stdout}\n{result.stderr}"
    return "HTTP 404" in text or '"Not Found"' in text or "No commit found for the ref" in text


def fetch_gh(repo: str, ref: str, path: str) -> str | None:
    """File text from the GitHub contents API via `gh api` (GET only). None when the
    branch or the file does not exist."""
    endpoint = f"repos/{repo}/contents/{quote(path)}?ref={quote(ref, safe='/')}"
    try:
        result = run_command(["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint])
    except FileNotFoundError:
        raise FetchError("gh is not installed") from None
    if result.returncode != 0:
        if _not_found(result):
            return None
        raise FetchError(f"gh api failed: {result.stderr.strip()[:300]}")
    try:
        body = json.loads(result.stdout)
    except ValueError:
        raise FetchError("gh api returned non-JSON") from None
    if not isinstance(body, dict) or body.get("type", "file") != "file":
        raise FetchError(f"{path} on {ref} is not a file")
    if body.get("encoding") == "base64" and (body.get("content") or not body.get("size")):
        return base64.b64decode(body.get("content") or "").decode("utf-8", errors="replace")
    # Files over 1 MB come back without inline content: ask for the raw media type.
    raw = run_command(["gh", "api", "-H", "Accept: application/vnd.github.raw+json", endpoint])
    if raw.returncode != 0:
        if _not_found(raw):
            return None
        raise FetchError(f"gh api (raw) failed: {raw.stderr.strip()[:300]}")
    return raw.stdout


def fetch_git(git_dir: Path, ref: str, path: str) -> str | None:
    """File text via `git show <ref>:<path>` in an existing clone (read-only; no fetch).
    None when the ref or the file does not exist."""
    base = ["git", "-C", str(git_dir)]
    if run_command([*base, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]).returncode != 0:
        return None
    if run_command([*base, "cat-file", "-e", f"{ref}:{path}"]).returncode != 0:
        return None
    shown = run_command([*base, "show", f"{ref}:{path}"])
    if shown.returncode != 0:
        raise FetchError(f"git show failed: {shown.stderr.strip()[:300]}")
    return shown.stdout


def parse_published(text: str) -> routines_log.ParseResult:
    """Same parser and keys as `ingest routines-log`; rows are tagged as cloud runs."""
    lines = text.split("\n")
    tail = lines.pop()  # "" when the text ends with a newline
    result = routines_log.parse_lines(lines)
    result.incomplete_tail = bool(tail)
    for run in result.runs:
        run.source = "cloud"
        run.meta["log_source"] = GIT_REF_LOG_SOURCE
    return result


def _remote_guard() -> int | None:
    error = config.remote_write_error()
    if error is None:
        return None
    print(error, file=sys.stderr)
    return REMOTE_REQUIRED_EXIT


def cmd_ingest_git_ref(args: argparse.Namespace) -> int:
    if (code := _remote_guard()) is not None:
        return code
    where = f"{args.repo}@{args.ref}:{args.path}" if args.git_dir is None else f"{args.git_dir}@{args.ref}:{args.path}"
    try:
        if args.git_dir is not None:
            text = fetch_git(args.git_dir.expanduser(), args.ref, args.path)
        else:
            text = fetch_gh(args.repo, args.ref, args.path)
    except (FetchError, subprocess.TimeoutExpired) as exc:
        print(f"amem: ingest git-ref failed: {exc}", file=sys.stderr)
        return 1
    if text is None:
        print(f"no routines.log published at {where} yet (branch or file missing); nothing to ingest")
        return 0
    parsed = parse_published(text)
    try:
        new, present = store.ingest_runs(parsed.runs)
    except (psycopg.Error, config.BackendError) as exc:
        print(f"amem: ingest failed: {type(exc).__name__}: {exc}".strip(), file=sys.stderr)
        return 1
    if parsed.unparsed:
        shown = ", ".join(str(n) for n in parsed.unparsed[:20])
        more = " ..." if len(parsed.unparsed) > 20 else ""
        print(f"amem: unparsed lines: {shown}{more}", file=sys.stderr)
    if parsed.incomplete_tail:
        print("amem: skipped the last line: no trailing newline yet (next ingest picks it up)",
              file=sys.stderr)
    print(f"ingested {new} new, {present} already present, {len(parsed.unparsed)} unparsed")
    return 0


# ---------------------------------------------------------------- jev record


def load_questions(path: Path) -> list[jev.Question]:
    """Questions from a TOML file (same schema as questions/*.toml).

    Ordered `[[questions]]` tables: `name`, `kind` (choice|score|noul), `prompt`,
    plus `options` (choice: label -> description), `levels` (score: ordered list)
    or optional `criteria` (noul: `true` / `false`). Other top-level keys
    (threshold, pass_question, ...) are ignored here.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise jev.JevError(f"cannot read questions file {path}: {exc}") from None
    items = data.get("questions")
    if not isinstance(items, list) or not items:
        raise jev.JevError(f"{path}: needs at least one [[questions]] table")
    questions: list[jev.Question] = []
    for i, item in enumerate(items, start=1):
        name, kind, prompt = item.get("name"), item.get("kind"), item.get("prompt")
        if not isinstance(name, str) or not name:
            raise jev.JevError(f"{path}: question {i} needs a non-empty name")
        if kind == "choice":
            options = item.get("options")
            if not isinstance(options, dict) or not options:
                raise jev.JevError(f"{path}: choice question {name!r} needs an [questions.options] table")
            questions.append(jev.choice(name, prompt, options))
        elif kind == "score":
            levels = item.get("levels")
            if not isinstance(levels, list) or not levels:
                raise jev.JevError(f"{path}: score question {name!r} needs a non-empty levels list")
            questions.append(jev.score(name, prompt, levels))
        elif kind == "noul":
            criteria = item.get("criteria")
            if criteria is not None and (not isinstance(criteria, dict) or set(criteria) - {"true", "false"}):
                raise jev.JevError(f"{path}: noul question {name!r}: criteria keys must be true/false")
            questions.append(jev.noul(name, prompt, criteria))
        else:
            raise jev.JevError(f"{path}: question {name!r} has unknown kind {kind!r}")
    return questions


def _read_state(value: str) -> str:
    """`@path` reads the file exactly (the key covers every byte); anything else is the text."""
    if value.startswith("@"):
        return Path(value[1:]).expanduser().read_text(encoding="utf-8")
    return value


def _live_body(state: str, questions: list[jev.Question]) -> dict[str, Any]:
    import typesafe_sdk

    with typesafe_sdk.TypeSafeClient(api_key=config.typesafe_api_key()) as client:
        response = client.system_one(state=state, questions={q.name: q.to_sdk() for q in questions})
    body = response.model_dump(mode="json") if hasattr(response, "model_dump") else dict(response)
    return {"model": body.get("model"), "usage": body.get("usage"), "answers": body.get("answers")}


def cmd_jev_record(args: argparse.Namespace) -> int:
    if jev.mode() != "live":
        print("amem: jev record makes one live Jev call and needs live mode: set TYPESAFE_API_KEY "
              "and AMEM_JEV_LIVE=1. Nothing was called or written.", file=sys.stderr)
        return NOT_LIVE_EXIT
    try:
        questions = load_questions(args.questions)
        state = _read_state(args.state)
        jev._validate(state, questions)
    except (jev.JevError, OSError) as exc:
        print(f"amem: jev record: {exc}", file=sys.stderr)
        return 1
    key = jev.fixture_key(state, questions)
    out_dir: Path = args.out
    path = out_dir / f"{key}.json"
    if path.exists() and not args.force:
        print(f"amem: jev record: fixture {path} exists; pass --force to re-record", file=sys.stderr)
        return 1
    try:
        body = _live_body(state, questions)
        # Same parse as fixture mode, so a bad response never lands as a fixture.
        adapter = jev._answer_adapter()
        answers = {n: adapter.validate_json(json.dumps(raw)) for n, raw in (body["answers"] or {}).items()}
        jev._collect(questions, answers)
    except Exception as exc:  # SDK, network or validation error: write nothing
        print(f"amem: jev record failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fixture = {"_note": f"Recorded live by `amem jev record` at {stamp}.", **body}
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(path)
    return 0


# ---------------------------------------------------------------- registration


def register(sub: Any, ingest_sub: Any = None) -> None:
    """Add this module's subcommands. `ingest_sub` is the `ingest` subparsers action."""
    if ingest_sub is not None:
        p = ingest_sub.add_parser(
            "git-ref", help="ingest the routines.log that cloud routines publish to a git branch"
        )
        p.add_argument("--repo", default=GIT_REF_REPO, help=f"GitHub repo (default {GIT_REF_REPO})")
        p.add_argument("--ref", default=GIT_REF_BRANCH, help=f"branch or ref (default {GIT_REF_BRANCH})")
        p.add_argument("--path", default=GIT_REF_PATH, help=f"file in the repo (default {GIT_REF_PATH})")
        p.add_argument("--git-dir", type=Path, default=None,
                       help="read with `git show <ref>:<path>` in this clone instead of `gh api`")
        p.set_defaults(func=cmd_ingest_git_ref)

    jev_parser = sub.add_parser("jev", help="Jev helpers")
    jev_sub = jev_parser.add_subparsers(dest="jev_command", required=True)
    p = jev_sub.add_parser("record", help="ask Jev live once and save the response as a fixture")
    p.add_argument("--questions", type=Path, required=True, help="questions TOML file")
    p.add_argument("--state", required=True, help="state text, or @path to read it from a file")
    p.add_argument("--out", type=Path, default=config.JEV_FIXTURES_DIR,
                   help="fixture directory (default tests/fixtures/jev/)")
    p.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    p.set_defaults(func=cmd_jev_record)
