#!/usr/bin/env python3
"""amem_digest: the digest of one cloud Claude Code session, as one JSON row for PostgREST.

Self-contained: Python 3.10+, stdlib only, no agent_memory imports. It is vendored into
other repos as `tools/amem_digest.py`. The package keeps a byte-identical copy
(`agent_memory/amem_digest.py`, run as `amem digest-self`), and a test keeps the two equal
and checks the digest against `deviations.build_digest` (docs/cloud.md, "Cloud digests").

Run it as the last step of a cloud routine (claude.ai/code). Cloud transcripts live in the
routine's Linux VM and never reach the Mac, so the routine sends this digest to the
`cloud_digests` table itself, and `amem sessions ingest` turns it into a `sessions` row.

Which transcript:
- `--transcript PATH`: that file.
- else the newest `*.jsonl` (by modification time) directly inside a project folder of
  `--projects-dir` (default `~/.claude/projects`). Subagent transcripts
  (`<session>/subagents/agent-*.jsonl`) are never picked. The running session writes its
  own transcript while this step runs, so the newest file is the current session.
- a newest file older than `--max-age-minutes` (default 120, 0 = no limit) does not count:
  it is an earlier session, not this one.

Failure: no transcript (or none recent enough) prints one `amem_digest: ...` line on
stderr, nothing on stdout, and exits 0. Any other error does the same. The routine never
fails because of this step. It never reads or prints environment values, never reads a
`.env*` file, and never sends anything itself (the routine's curl does).

Output (stdout, one JSON object):
  source_key    cloud:<session id>
  routine       --routine, slugged (`Nightly Trials` -> `nightly-trials`)
  session_id    the transcript's sessionId (else the file name)
  started_at, ended_at   first and last event timestamp (ISO 8601) or null
  digest        redacted, capped digest (the same as deviations.build_digest)
  digest_sha    sha256 of the digest
  instructions  [{name, sha256, text}] per `--prompt PATH` (name `prompt`; or
                `--prompt-from-transcript`: the session's first user message, which is
                the routine prompt) and `--instruction NAME=PATH`. text is redacted and
                capped; sha256 is of the text as read.
  skills        Skill tool calls and slash commands (no Claude Code built-ins)
  meta          {cloud: true, repo, script_version, transcript_bytes, tool_calls, prompts}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_VERSION = "1"
SOURCE_PREFIX = "cloud:"
PROMPT_NAME = "prompt"     # the routine prompt: instruction source routine:<routine>

DIGEST_MAX = 60_000
HEAD_SHARE = 0.4
FIRST_USER_MAX = 6_000
USER_MAX = 1_500
ASSISTANT_MAX = 600
ARGS_MAX = 160
ERROR_MAX = 240
NOTICE_MAX = 300

INSTRUCTION_TEXT_MAX = 100_000   # per instruction file
INSTRUCTIONS_MAX_FILES = 12
DEFAULT_MAX_AGE_MINUTES = 120

# The redaction of deviations.redact: tasks_ingest's patterns, then the extra key shapes.
REDACT_RES = (
    re.compile(r"\b(?:sk-|sk_|ghp_|gho_|ghs_|github_pat_|xox[abposr]-)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|bearer)(\s*[:=]\s*|\s+)['\"]?[A-Za-z0-9_\-./+=]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
)

# Slash commands that are Claude Code built-ins, not skills (sessions_ingest.BUILTIN_COMMANDS).
BUILTIN_COMMANDS = frozenset({
    "add-dir", "agents", "bug", "clear", "compact", "config", "context", "cost", "doctor", "exit",
    "export", "goal", "help", "hooks", "ide", "init", "login", "logout", "mcp", "memory", "model",
    "permissions", "plugin", "pr-comments", "release-notes", "reload-plugins", "reload-skills",
    "resume", "review", "rewind", "status", "statusline", "terminal-setup", "upgrade", "usage",
    "vim", "fast", "effort", "output-style", "privacy-settings", "todos", "watch", "pet", "board",
    "artifacts", "loop", "agents-md",
})

SHORT_ARG_KEYS = {
    "Bash": ("command",), "Read": ("file_path",), "Edit": ("file_path",), "Write": ("file_path",),
    "Grep": ("pattern",), "Glob": ("pattern",), "Skill": ("skill", "args"),
    "Agent": ("subagent_type", "description"), "SendMessage": ("to",), "ToolSearch": ("query",),
    "WebFetch": ("url",), "WebSearch": ("query",), "SubagentHandback": ("message",),
}

COMMAND_RE = re.compile(r"<command-name>/?([\w:.-]+)</command-name>")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class DigestError(Exception):
    """A problem worth one stderr line. The script still exits 0."""


# --------------------------------------------------------------------------- reading


def load_events(path: Path) -> list[dict[str, Any]]:
    """Every JSON object line of a transcript. Bad lines are skipped."""
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") in ("text", "input_text"))
    return ""


def is_human(text: str) -> bool:
    """A prompt a person typed, not an injected block, command echo or notification."""
    t = (text or "").lstrip()
    return bool(t) and not t.startswith(("<", "Caveat:", "# AGENTS.md", "[Request interrupted",
                                         "This session is being continued"))


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- digest


def redact(text: str) -> str:
    for pattern in REDACT_RES:
        text = pattern.sub("[redacted]", text)
    return text


def _one_line(text: Any, limit: int) -> str:
    out = " ".join(str(text).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"


def short_args(name: str, inp: Any) -> str:
    if not isinstance(inp, dict):
        return ""
    keys = SHORT_ARG_KEYS.get(name)
    if keys:
        text = " ".join(str(inp.get(k, "")) for k in keys if inp.get(k) not in (None, ""))
        limit = 600 if name == "SubagentHandback" else ARGS_MAX
    else:
        text = ",".join(sorted(str(k) for k in inp))
        limit = ARGS_MAX
    return _one_line(text, limit)


def digest_lines(events: list[dict[str, Any]]) -> list[str]:
    """The Claude Code digest lines (deviations.digest_lines)."""
    lines: list[str] = []
    uses: dict[str, str] = {}
    turn = 0
    first_user = True
    for e in events:
        etype = e.get("type")
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = m.get("content")
        if etype == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    lines.append(f"ASSISTANT: {_clip(str(b['text']), ASSISTANT_MAX)}")
                elif b.get("type") == "tool_use":
                    name = str(b.get("name") or "?")
                    uses[str(b.get("id"))] = name
                    lines.append(f"TOOL {name}: {short_args(name, b.get('input'))}".rstrip(": "))
        elif etype == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                 for b in content):
                kind = e.get("toolDenialKind")
                pd = e.get("permissionDecision") if isinstance(e.get("permissionDecision"), dict) else {}
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    tool = uses.get(str(b.get("tool_use_id")), "?")
                    if kind or pd.get("decision") == "reject":
                        why = kind or pd.get("source") or "denied"
                        lines.append(f"DENIED {tool} ({why})")
                    elif b.get("is_error"):
                        body = b.get("content")
                        body = body if isinstance(body, str) else text_of(body)
                        lines.append(f"ERROR {tool}: {_one_line(body, ERROR_MAX)}")
                continue
            if e.get("isMeta"):
                continue
            text = text_of(content)
            if not text.strip():
                continue
            if first_user:
                first_user = False
                sched = re.search(r'<scheduled-task\b[^>]*name="([\w.-]+)"', text)
                if sched:
                    turn += 1
                    lines.append(f"[turn {turn}] SCHEDULED TASK {sched.group(1)} started "
                                 "(its prompt is in the instructions)")
                    continue
                if is_human(text):
                    turn += 1
                    lines.append(f"[turn {turn}] USER: {_clip(text, FIRST_USER_MAX)}")
                    continue
            cmds = re.findall(r"<command-name>/?([\w:.-]+)</command-name>", text)
            if cmds:
                turn += 1
                lines.append(f"[turn {turn}] COMMAND /{cmds[0]}")
            elif text.lstrip().startswith("[Request interrupted"):
                lines.append("INTERRUPTED by the user")
            elif is_human(text):
                turn += 1
                lines.append(f"[turn {turn}] USER: {_clip(text, USER_MAX)}")
            else:
                tag = re.match(r"\s*<([\w-]+)", text)
                if tag and tag.group(1) in ("task-notification", "teammate-message", "relay"):
                    lines.append(f"NOTICE {tag.group(1)}: {_one_line(text, NOTICE_MAX)}")
        elif etype == "attachment":
            a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
            if a.get("type") == "hook_blocking_error":
                lines.append(f"HOOK BLOCKED {a.get('hookEvent') or '?'}: "
                             f"{_one_line(a.get('blockingError') or '', ERROR_MAX)}")
    return lines


def cap(text: str, limit: int = DIGEST_MAX) -> str:
    """Head + tail of `text` within `limit` chars, cut on line boundaries."""
    if len(text) <= limit:
        return text
    marker = "\n[... {n} chars cut from the middle ...]\n"
    room = limit - len(marker.format(n=len(text)))
    head_n = int(room * HEAD_SHARE)
    tail_n = room - head_n
    head = text[:head_n]
    head = head[: head.rfind("\n")] if "\n" in head else head
    tail = text[-tail_n:]
    tail = tail[tail.find("\n") + 1:] if "\n" in tail else tail
    cut = len(text) - len(head) - len(tail)
    return head + marker.format(n=cut) + tail


def build_digest(events: list[dict[str, Any]], limit: int = DIGEST_MAX) -> str:
    """Redacted, capped digest. Redaction runs before the cap, so a cut never splits a secret."""
    return cap(redact("\n".join(digest_lines(events))), limit)


# --------------------------------------------------------------------------- facts


def _skill_name(raw: Any) -> str | None:
    name = str(raw or "").strip().lstrip("/")
    return name if re.fullmatch(r"[\w.-]+(:[\w.-]+)?", name) else None


def facts(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Times, skills, counts, session id and cwd. Labels and names only."""
    start = end = None
    skills: dict[str, None] = {}
    cwd = ""
    session_id = ""
    tool_calls = prompts = 0
    for e in events:
        ts = parse_ts(e.get("timestamp"))
        if ts:
            start = min(start, ts) if start else ts
            end = max(end, ts) if end else ts
        if not cwd and isinstance(e.get("cwd"), str):
            cwd = e["cwd"]
        sid = e.get("sessionId")
        if not session_id and isinstance(sid, str) and _SESSION_ID_RE.fullmatch(sid):
            session_id = sid
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = m.get("content")
        if e.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_calls += 1
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if b.get("name") == "Skill":
                        sk = _skill_name(inp.get("skill"))
                        if sk:
                            skills.setdefault(sk, None)
        elif e.get("type") == "user" and not e.get("isMeta"):
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                 for b in content):
                continue
            text = text_of(content)
            for cmd in COMMAND_RE.findall(text):
                sk = _skill_name(cmd)
                if sk and sk not in BUILTIN_COMMANDS:
                    skills.setdefault(sk, None)
            if is_human(text):
                prompts += 1
    return {"start": start, "end": end, "skills": list(skills), "cwd": cwd, "session_id": session_id,
            "tool_calls": tool_calls, "prompts": prompts}


# --------------------------------------------------------------------------- inputs


def find_transcript(projects: Path, *, max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
                    now: float | None = None) -> Path:
    """The newest `<projects>/<project>/*.jsonl`. Raises DigestError when there is none, or
    when the newest one is older than `max_age_minutes` (0 = no limit)."""
    if not projects.is_dir():
        raise DigestError(f"no transcript folder at {projects}")
    best: tuple[float, str, Path] | None = None
    for proj in projects.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            try:
                if not f.is_file():
                    continue
                key = (f.stat().st_mtime, str(f), f)
            except OSError:
                continue
            if best is None or key[:2] > best[:2]:
                best = key
    if best is None:
        raise DigestError(f"no transcript (*.jsonl) under {projects}")
    age = ((time.time() if now is None else now) - best[0]) / 60
    if max_age_minutes and age > max_age_minutes:
        raise DigestError(f"newest transcript is {age:.0f} minutes old (limit {max_age_minutes:g}): "
                          "not this session")
    return best[2]


def slug(name: str) -> str:
    """A routine name as an id: `Nightly Trials` -> `nightly-trials`."""
    out = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return out[:80] or "cloud"


def parse_instruction(spec: str) -> tuple[str, Path]:
    """`NAME=PATH` -> (name, path)."""
    name, sep, path = spec.partition("=")
    name = name.strip()
    if not sep or not path.strip() or not _NAME_RE.fullmatch(name):
        raise argparse.ArgumentTypeError(f"expected NAME=PATH with NAME like nightly-md, got {spec!r}")
    return name, Path(path.strip()).expanduser()


def instruction(name: str, raw: str) -> dict[str, str]:
    """{name, sha256, text}: sha256 of the text as read, text redacted and capped."""
    text = redact(raw)
    if len(text) > INSTRUCTION_TEXT_MAX:
        text = text[:INSTRUCTION_TEXT_MAX].rstrip() + "\n[... cut ...]"
    return {"name": name, "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(), "text": text}


def read_instruction(name: str, path: Path) -> dict[str, str] | None:
    """An instruction from a file, or None (with a stderr line) for a missing file or an env file."""
    if path.name.lower().startswith(".env"):
        print(f"amem_digest: instruction {name}: env files are never read", file=sys.stderr)
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"amem_digest: instruction {name}: cannot read ({type(exc).__name__})", file=sys.stderr)
        return None
    return instruction(name, raw)


def first_prompt(events: list[dict[str, Any]]) -> str | None:
    """The session's first typed user message: for a cloud routine, the routine prompt."""
    for e in events:
        if e.get("type") != "user" or e.get("isMeta"):
            continue
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = m.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                             for b in content):
            continue
        text = text_of(content)
        if is_human(text):
            return text.strip()
    return None


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat() if ts else None


def build_row(transcript: Path, *, routine: str, instructions: list[tuple[str, Path]],
              repo: str | None = None, prompt_from_transcript: bool = False) -> dict[str, Any]:
    """The `cloud_digests` row for one transcript. `prompt_from_transcript`: when no
    `prompt` file is given, the session's first user message is the `prompt` instruction."""
    events = load_events(transcript)
    f = facts(events)
    digest = build_digest(events)
    session_id = f["session_id"] or (transcript.stem if _SESSION_ID_RE.fullmatch(transcript.stem) else "")
    if not session_id:
        raise DigestError("transcript has no usable session id")
    seen: set[str] = set()
    instr: list[dict[str, str]] = []
    for name, path in instructions:
        if name in seen:
            print(f"amem_digest: instruction {name}: given twice, the first one counts", file=sys.stderr)
            continue
        seen.add(name)
        if len(instr) >= INSTRUCTIONS_MAX_FILES:
            print(f"amem_digest: more than {INSTRUCTIONS_MAX_FILES} instructions, {name} left out",
                  file=sys.stderr)
            continue
        item = read_instruction(name, path)
        if item is not None:
            instr.append(item)
    if prompt_from_transcript and PROMPT_NAME not in seen:
        text = first_prompt(events)
        if text:
            instr.insert(0, instruction(PROMPT_NAME, text))
        else:
            print("amem_digest: no user message in the transcript: no prompt instruction", file=sys.stderr)
    cwd = f["cwd"]
    if repo is None and cwd:
        base = Path(cwd).name
        repo = base if base and Path(cwd) != Path.home() else None
    try:
        size = transcript.stat().st_size
    except OSError:
        size = 0
    return {
        "source_key": SOURCE_PREFIX + session_id,
        "routine": slug(routine),
        "session_id": session_id,
        "started_at": _iso(f["start"]),
        "ended_at": _iso(f["end"]),
        "digest": digest,
        "digest_sha": hashlib.sha256(digest.encode("utf-8")).hexdigest(),
        "instructions": instr,
        "skills": f["skills"],
        "meta": {"cloud": True, "repo": repo, "script_version": SCRIPT_VERSION, "transcript_bytes": size,
                 "tool_calls": f["tool_calls"], "prompts": f["prompts"]},
    }


# --------------------------------------------------------------------------- main


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amem_digest",
        description="Print the redacted digest of this cloud session as one JSON row for cloud_digests.")
    p.add_argument("--routine", required=True, help="routine name (slugged: 'Nightly Trials' -> nightly-trials)")
    p.add_argument("--transcript", type=Path, default=None,
                   help="transcript file (default: the newest *.jsonl under --projects-dir)")
    p.add_argument("--projects-dir", type=Path, default=None, help="default ~/.claude/projects")
    p.add_argument("--max-age-minutes", type=float, default=DEFAULT_MAX_AGE_MINUTES,
                   help="ignore a newest transcript older than this (default 120, 0 = no limit)")
    p.add_argument("--prompt", type=Path, default=None,
                   help="file holding the routine prompt (instruction source routine:<routine>)")
    p.add_argument("--prompt-from-transcript", action="store_true",
                   help="no prompt file: the session's first user message (the routine prompt) is the prompt")
    p.add_argument("--instruction", action="append", type=parse_instruction, default=[], metavar="NAME=PATH",
                   help="another instruction file, e.g. nightly-md=NIGHTLY.md (source cloud-file:<NAME>)")
    p.add_argument("--repo", default=None, help="repo name (default: the session's working folder name)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.transcript is not None:
            transcript = args.transcript.expanduser()
            if not transcript.is_file():
                raise DigestError(f"no transcript at {transcript}")
        else:
            projects = (args.projects_dir or Path.home() / ".claude" / "projects").expanduser()
            transcript = find_transcript(projects, max_age_minutes=args.max_age_minutes)
        instructions = ([(PROMPT_NAME, args.prompt.expanduser())] if args.prompt else []) + list(args.instruction)
        row = build_row(transcript, routine=args.routine, instructions=instructions, repo=args.repo,
                        prompt_from_transcript=args.prompt_from_transcript)
    except DigestError as exc:
        print(f"amem_digest: {exc}. Nothing to send.", file=sys.stderr)
        return 0
    except Exception as exc:  # never fail the routine
        print(f"amem_digest: failed ({type(exc).__name__}). Nothing to send.", file=sys.stderr)
        return 0
    sys.stdout.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
