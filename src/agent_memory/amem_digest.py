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

Hook mode (`--hook`, script_version 3, docs/cloud.md "Cloud chats"): a Claude Code Stop /
SessionEnd command hook in a cloud session. It reads the hook JSON on stdin (session_id,
transcript_path, cwd, hook_event_name), builds the same row with `kind: chat`, no routine,
`ingested_at: null` and the CLAUDE.md / AGENTS.md of the hook's cwd as instructions
(`claude-md`, `agents-md`), and POSTs it itself (urllib, honours HTTPS_PROXY / NO_PROXY) to
`$AMEM_SUPABASE_URL/rest/v1/cloud_digests?on_conflict=source_key` with
`Prefer: resolution=merge-duplicates,return=minimal`. A Stop less than 60 s after the last
successful POST of the same session is skipped (SessionEnd never is). Hard timeout 8 s.
It prints nothing (stdout or stderr), always exits 0, and logs one line per run to
`$HOME/.claude/amem/hook.log` (event, session id prefix, status; never the URL or text).
The environment values it reads: AMEM_SUPABASE_URL, HOME, the proxy variables, and the CA
bundle paths REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / NODE_EXTRA_CA_CERTS. It sends no key: the
cloud network secret adds the `apikey` header.

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
  meta          {cloud: true, repo, script_version, transcript_bytes, tool_calls, prompts,
                tokens: {input, output, cache_read, cache_creation, models}} (script_version 2+:
                the session's token totals, see token_totals), plus `title` (script_version 3+)
                when the session was named: the last `custom-title` line, redacted, max 200 chars,
                `profile` (model, effort, duration: see profile) and `outcome` ({signals, end}:
                signal names and counts, see SignalTracker; script_version 5+: shell commands
                are read quote-aware, and `commit` counts only on proof from the call's result)
  hook mode adds `kind: chat`, `ingested_at: null` and meta.hook (the event); no `routine`.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_VERSION = "5"
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
TITLE_MAX = 200

HOOK_EVENTS = ("Stop", "SessionEnd")
HOOK_DEBOUNCE_SECONDS = 60.0
HOOK_TIMEOUT_SECONDS = 8
HTTP_TIMEOUT_SECONDS = 5.0
HOOK_STATE_KEEP_DAYS = 14
HOOK_PAYLOAD_MAX = 1_000_000
HOOK_LOG_MAX = 1_000_000
HOOK_INSTRUCTIONS = (("claude-md", "CLAUDE.md"), ("agents-md", "AGENTS.md"))
CA_BUNDLE_ENVS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")

# A copy of agent_memory/redact.py (this script stays standalone). tests/test_redact.py
# checks that the patterns and the results are the same. A `keep` group stays; the rest
# of the match becomes [redacted].
_KEY_NAMES = (r"(?:api[_-]?key|secret|password|passwd|pwd|token|auth[_-]?token|access[_-]?key"
              r"|private[_-]?key|service[_-]?role[_-]?key|authorization|credentials?)")
REDACT_RES = (
    re.compile(r"\b(?:sk-|sk_|ghp_|gho_|ghs_|ghu_|ghr_|github_pat_|glpat-|xox[abposr]-|sb_secret_)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"(?i)(?P<keep>\b(?:bearer|basic)\s+)(?=[A-Za-z0-9_\-.~+/=]*\d)[A-Za-z0-9_\-.~+/=]{12,}"),
    re.compile(r"(?i)(?P<keep>\b[\w-]*?" + _KEY_NAMES + r"(?:[_-]?(?:key|value|id))?['\"]?\s*[:=]\s*['\"]?)"
               r"[^\s'\"`,;(){}\[\]<>]{8,}"),
    re.compile(r"(?i)(?P<keep>\b(?:api[_-]?key|secret|password|passwd|token)\s+)"
               r"(?=[A-Za-z0-9_\-./+=]*\d)(?=[A-Za-z0-9_\-./+=]*[A-Za-z])[A-Za-z0-9_\-./+=]{16,}"),
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


def _redact_match(m: "re.Match[str]") -> str:
    keep = m.group("keep") if "keep" in m.re.groupindex else None
    return (keep or "") + "[redacted]"


def redact(text: str) -> str:
    for pattern in REDACT_RES:
        text = pattern.sub(_redact_match, text)
    return text


def _one_line(text: Any, limit: int) -> str:
    out = " ".join(str(text).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def clean_title(value: Any) -> str | None:
    """A session title as stored: redacted, one line, at most TITLE_MAX chars. None for no title."""
    if not isinstance(value, str):
        return None
    out = _one_line(redact(value), TITLE_MAX)
    return out or None


def title_of(events: list[dict[str, Any]]) -> str | None:
    """The session's name: the last `custom-title` line's `customTitle` (a rename wins)."""
    title = None
    for e in events:
        if e.get("type") == "custom-title":
            title = clean_title(e.get("customTitle")) or title
    return title


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


# --------------------------------------------------------------------------- tokens

TOKEN_FIELDS = ("input", "output", "cache_read", "cache_creation")
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _n(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def token_totals(events: list[dict[str, Any]]) -> dict[str, Any]:
    """{input, output, cache_read, cache_creation, models: {model: {..., calls}}} from the
    assistant lines' `message.usage` (session_tokens.claude_summary). One API response is
    written as several lines with the same `message.id`: the latest line of an id wins.
    `<synthetic>` messages are left out."""
    per_id: dict[str, tuple[str, list[int]]] = {}
    loose: list[tuple[str, list[int]]] = []
    for e in events:
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        if e.get("type") != "assistant" or not isinstance(m.get("usage"), dict):
            continue
        model = m.get("model").strip() if isinstance(m.get("model"), str) and m.get("model").strip() \
            and len(m.get("model")) <= 100 else "unknown"
        if model == "<synthetic>":
            continue
        counts = [_n(m["usage"].get(k)) for k in _USAGE_KEYS]
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            per_id.pop(mid, None)  # keeps first-seen order of the latest line
            per_id[mid] = (model, counts)
        else:
            loose.append((model, counts))
    total = [0, 0, 0, 0]
    models: dict[str, dict[str, int]] = {}
    for model, counts in [*per_id.values(), *loose]:
        m = models.setdefault(model, {**{f: 0 for f in TOKEN_FIELDS}, "calls": 0})
        for i, f in enumerate(TOKEN_FIELDS):
            total[i] += counts[i]
            m[f] += counts[i]
        m["calls"] += 1
    return {**dict(zip(TOKEN_FIELDS, total)), "models": dict(sorted(models.items()))}


IDLE_GAP_SECONDS = 600
_EFFORT_RE = re.compile(r"[a-z][a-z0-9_-]{0,30}")


def _effort(value: Any) -> str | None:
    if isinstance(value, str) and _EFFORT_RE.fullmatch(value.strip().lower()):
        return value.strip().lower()
    return None


def _real_model(name: str) -> bool:
    return bool(name) and name != "unknown" and not name.startswith("<")


def _nonneg(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def profile(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Model, effort and time facts (session_tokens.Summary.profile): the main model (most
    output tokens, a tie goes to the most recent), the latest reply's model, the effort per
    reply (`perTurnEffort`, else `effort`), wall clock and active seconds (gaps over 10 minutes
    are idle), and the last `cost-state` line's API / tool time (ms) and lines added / removed."""
    seq = 0
    ids: set[str] = set()
    model_seen: dict[str, int] = {}
    effort_seen: dict[str, int] = {}
    efforts: dict[str, int] = {}
    model_last = effort_last = None
    first = last = prev = None
    active = 0.0
    cost: dict[str, Any] | None = None
    for e in events:
        ts = parse_ts(e.get("timestamp"))
        if ts is not None:
            first = min(first, ts) if first else ts
            last = max(last, ts) if last else ts
            if prev is not None:
                gap = (ts - prev).total_seconds()
                if 0 < gap <= IDLE_GAP_SECONDS:
                    active += gap
            if prev is None or ts > prev:
                prev = ts
        if e.get("type") == "cost-state":
            cost = e
            continue
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        if e.get("type") != "assistant" or not isinstance(m.get("usage"), dict):
            continue
        raw = m.get("model")
        model = raw.strip() if isinstance(raw, str) and raw.strip() and len(raw) <= 100 else "unknown"
        if model == "<synthetic>":
            continue
        mid = m.get("id") if isinstance(m.get("id"), str) and m.get("id") else None
        if mid is None or mid not in ids:
            seq += 1
            if _real_model(model):
                model_last, model_seen[model] = model, seq
            eff = _effort(e.get("perTurnEffort")) or _effort(e.get("effort"))
            if eff:
                efforts[eff] = efforts.get(eff, 0) + 1
                effort_last, effort_seen[eff] = eff, seq
        if mid is not None:
            ids.add(mid)
    models = token_totals(events)["models"]
    named = [k for k in models if _real_model(k)]
    main_model = max(named, key=lambda k: (models[k]["output"], model_seen.get(k, -1), k)) if named else None
    main_effort = max(efforts, key=lambda k: (efforts[k], effort_seen.get(k, -1), k)) if efforts else None
    api = _nonneg(cost.get("totalAPIDuration")) if cost else None
    tool = _nonneg(cost.get("totalToolDuration")) if cost else None
    return {
        "model": main_model, "model_last": model_last,
        "effort": main_effort, "effort_last": effort_last, "efforts": dict(sorted(efforts.items())),
        "duration_seconds": round((last - first).total_seconds(), 3) if first and last else None,
        "active_seconds": round(active, 3) if first else None,
        "api_seconds": None if api is None else round(api / 1000, 3),
        "tool_seconds": None if tool is None else round(tool / 1000, 3),
        "lines_added": _nonneg(cost.get("totalLinesAdded")) if cost else None,
        "lines_removed": _nonneg(cost.get("totalLinesRemoved")) if cost else None,
    }


# --------------------------------------------------------------------------- outcome signals
# docs/sessions-live.md, "Outcome". Signal names and counts only: no command text is kept.

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.-]*))")


def strip_heredocs(command: str) -> str:
    """`command` without its heredoc bodies (session_commits.split_heredocs): the body of
    `<<'EOF' ... EOF` is data, not shell code. A `<<` inside quotes is not a heredoc."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        ch = command[i]
        if quote is not None:
            out.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if command.startswith("<<", i) and not command.startswith("<<<", i):
            m = _HEREDOC_RE.match(command, i)
            if m is not None:
                pending.append((m.group(2) or m.group(3) or m.group(4), m.group(1) == "-"))
                out.append(m.group(0))
                i = m.end()
                continue
        if ch == "\n" and pending:
            out.append("\n")
            i += 1
            for delim, dash in pending:
                while i < n:
                    end = command.find("\n", i)
                    line = command[i:] if end < 0 else command[i:end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if dash else line) == delim:
                        break
            pending = []
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def segments(command: str) -> list[tuple[str, str | None]]:
    """(segment, the operator after it) for the simple commands of a shell command line.
    Quote-aware: an operator inside '...', "...", `...`, $(...) or after a backslash is text,
    and heredoc bodies are cut out first. Operators: && || ; | |& & and a line break (a `&`
    of a redirection such as 2>&1 or &> is not one)."""
    text = strip_heredocs(command or "")
    out: list[tuple[str, str | None]] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0   # $( ... ) nesting outside quotes
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote == "'":
            buf.append(ch)
            quote = None if ch == "'" else quote
            i += 1
            continue
        if ch == "\\" and i + 1 < n and quote != "'":
            buf.append(text[i:i + 2])
            i += 2
            continue
        if quote in ('"', "`"):
            buf.append(ch)
            quote = None if ch == quote else quote
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if text.startswith("$(", i):
            depth += 1
            buf.append("$(")
            i += 2
            continue
        if depth and ch == ")":
            depth -= 1
            buf.append(ch)
            i += 1
            continue
        op = None
        if not depth:
            for cand in ("&&", "||", "|&", ";", "\n", "|"):
                if text.startswith(cand, i):
                    op = cand
                    break
            if op is None and ch == "&" and not (i and text[i - 1] in "<>") and not text.startswith("&>", i):
                op = "&"
        if op is None:
            buf.append(ch)
            i += 1
            continue
        if op == "\n" and not "".join(buf).strip():   # a blank line, or the line after `&&` / `|`
            buf.append(" ")
            i += 1
            continue
        out.append(("".join(buf), op))
        buf = []
        i += len(op)
    out.append(("".join(buf), None))
    return [(seg, op) for seg, op in out if seg.strip()]


# The tool must be the segment's command word: `echo "git push"` is not a push. Allowed before it:
# `(`, env assignments, and runners such as `sudo`, `uv run`, `npx`.
_START = (r"^[\s(]*(?:[A-Za-z_]\w*=\S*\s+)*"
          r"(?:(?:sudo|command|exec|time|env|npx|bunx|uv\s+run(?:\s+--?\S+)*|pnpm\s+(?:dlx|exec))\s+)*")
_GIT = _START + r"git(?:\s+(?:-C\s+(?:\"[^\"]*\"|'[^']*'|\S+)|-c\s+\S+|--no-pager))*\s+"
_SIGNAL_RES = (
    ("pr_merge", re.compile(_START + r"gh\s+pr\s+merge\b")),
    ("pr_create", re.compile(_START + r"gh\s+pr\s+create\b")),
    ("commit", re.compile(_GIT + r"commit\b")),
    ("deploy", re.compile(_START + r"(?:vercel\b.*\s--prod\b|supabase\s+functions\s+deploy\b|supabase\s+db\s+push\b"
                          r"|railway\s+up\b|amem\s+db\s+migrate\b)")),
)
_COMMIT_RE = re.compile(_GIT + r"commit\b")
_PUSH_RE = re.compile(_GIT + r"push\b(.*)")
_MERGE_RE = re.compile(_GIT + r"merge\b")
_TO_MAIN_RE = re.compile(r"(?:^|[\s:/])(?:main|master)(?:\s|$)")
_SWITCH_MAIN_RE = re.compile(_GIT + r"(?:checkout|switch)\s+(?:main|master)\b")
REPORT_RE = re.compile(r"^\W*Status:\W*(DONE|PARTIAL|BLOCKED)\b", re.M)


COMMIT_LINE_RE = re.compile(r"^\[(?:detached HEAD|[^\s\]]+)(?: \(root-commit\))? [0-9a-f]{7,40}\] ?.*$", re.M)
_ERREXIT_RE = re.compile(r"^\s*set\s+(?:-[a-zA-Z]*e[a-zA-Z]*|-o\s+errexit)\b")
_EXIT_CODE_RE = re.compile(r"(?:\"exit_code\"\s*:\s*|\bExit code:?\s*|\bexited with code\s+)(-?\d+)")
COMMIT_CERTAIN, COMMIT_UNSURE, COMMIT_BACKGROUND = "c", "u", "b"


def commit_call(command: str) -> str | None:
    """How a shell command's own result can prove its `git commit` (None: it runs none; a
    `git commit` inside quotes, a heredoc body or a grep / rg / echo argument is not one):
    - COMMIT_CERTAIN: the commit is not inside a pipe (`|` right before or after it) and not
      after `||`, and every list operator after it is `&&` (pipes between later commands are
      fine: `git commit -q -m x && git push 2>&1 | tail -1`), or `set -e` runs before it: a
      success exit means the commit was made;
    - COMMIT_BACKGROUND: the commit runs in the background (`... &`): its output is elsewhere;
    - COMMIT_UNSURE: anything else (`;`, `||`, `|` after it): only a commit line in the output counts."""
    segs = segments(command)
    for k, (seg, _op) in enumerate(segs):
        if not _COMMIT_RE.search(seg):
            continue
        after = [op for _s, op in segs[k:] if op is not None]
        if after and after[0] == "&":
            return COMMIT_BACKGROUND
        before = segs[k - 1][1] if k else None
        piped = before in ("|", "|&", "||") or (bool(after) and after[0] in ("|", "|&", "||"))
        if not piped and all(op in ("&&", "|", "|&") for op in after):
            return COMMIT_CERTAIN
        if not piped and any(_ERREXIT_RE.search(s) for s, _o in segs[:k]):
            return COMMIT_CERTAIN
        return COMMIT_UNSURE
    return None


def commit_confirmed(how: str | None, ok: bool, output: str) -> bool:
    """A commit call made a commit: its output shows git's commit line `[<branch> <sha>] <subject>`
    (a sha git printed is proof even when a later step of the command failed), or it is
    COMMIT_CERTAIN and the call succeeded (`ok`: no error, not denied). A failed or denied call
    without a commit line (`nothing to commit`, a hook that refused) and a background commit
    are never counted here."""
    if how is None:
        return False
    if COMMIT_LINE_RE.search(output or ""):
        return True
    return ok and how == COMMIT_CERTAIN


def command_signals(command: str) -> list[str]:
    """Signal names in one shell command (git / gh / deploy). Never the text itself. `commit`
    here means the command runs a `git commit`; the outcome counts it only on proof
    (commit_confirmed, at the call's result)."""
    out: list[str] = []
    on_main = False
    for seg, _op in segments(command):
        for name, pattern in _SIGNAL_RES:
            if pattern.search(seg):
                out.append(name)
        push = _PUSH_RE.search(seg)
        if push:
            out.append("push_main" if _TO_MAIN_RE.search(push.group(1)) else "push")
        if _SWITCH_MAIN_RE.search(seg):
            on_main = True
        elif on_main and _MERGE_RE.search(seg):
            out.append("merge_main")
    return out


def _report_signals(text: str) -> list[str]:
    return [f"report_{m.lower()}" for m in REPORT_RE.findall(text or "")]


def _command_of(name: str, inp: Any) -> str:
    """The shell command of a tool call (Claude `Bash`, Codex shell / exec_command), else ''."""
    if not isinstance(inp, dict):
        return ""
    cmd = inp.get("command", inp.get("cmd"))
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else ""


class SignalTracker:
    """Outcome signals of one Claude Code transcript, line by line. `state()` / `__init__(state)`
    let a hook ping continue where it stopped. `end`: how the session stands now:
    reply | user (a prompt with no reply yet) | tool (a call with no result yet) | error |
    interrupt.

    Every signal but `commit` counts at the tool call. `commit` counts at the call's result,
    and only on proof (commit_confirmed): the call id waits in `commit_calls` (state key
    `commit_calls`: {call id: COMMIT_*}, at most PENDING_MAX). A background commit
    (`run_in_background`, `... &`) is never counted here: its output is not in the transcript.
    An older state without `commit_calls` loads as none waiting."""

    PENDING_MAX = 64

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        s = state if isinstance(state, dict) else {}
        raw = s.get("signals") if isinstance(s.get("signals"), dict) else {}
        self.signals: dict[str, int] = {str(k): int(v) for k, v in raw.items()
                                         if isinstance(v, int) and not isinstance(v, bool) and v > 0}
        self.end: str | None = s.get("end") if isinstance(s.get("end"), str) else None
        self.pending: list[str] = [str(p) for p in s.get("pending") or [] if isinstance(p, str)][-self.PENDING_MAX:]
        calls = s.get("commit_calls") if isinstance(s.get("commit_calls"), dict) else {}
        self.commit_calls: dict[str, str] = dict(list(
            (str(k), v) for k, v in calls.items()
            if v in (COMMIT_CERTAIN, COMMIT_UNSURE, COMMIT_BACKGROUND))[-self.PENDING_MAX:])

    def state(self) -> dict[str, Any]:
        return {"signals": dict(self.signals), "end": self.end, "pending": list(self.pending),
                "commit_calls": dict(self.commit_calls)}

    def add(self, names: list[str]) -> None:
        for n in names:
            self.signals[n] = self.signals.get(n, 0) + 1

    def tool_call(self, call_id: str | None, name: str, inp: Any) -> None:
        if name in EDIT_TOOLS:
            self.add(["edit"])
        command = _command_of(name, inp)
        names = command_signals(command)
        self.add([n for n in names if n != "commit"])
        if "commit" in names and call_id:
            how = commit_call(command)
            if how is not None:
                bg = isinstance(inp, dict) and inp.get("run_in_background") is True
                self.commit_calls[call_id] = COMMIT_BACKGROUND if bg else how
                while len(self.commit_calls) > self.PENDING_MAX:
                    self.commit_calls.pop(next(iter(self.commit_calls)))
        if name == "SubagentHandback" and isinstance(inp, dict):
            self.add(_report_signals(str(inp.get("message") or "")))
        if call_id:
            self.pending = (self.pending + [call_id])[-self.PENDING_MAX:]
        self.end = "tool"

    def tool_result(self, call_id: str | None, error: bool, output: str = "", denied: bool = False) -> None:
        """One tool result. `error`: the call failed; `denied`: it never ran; `output`: its text."""
        if call_id in self.pending:
            self.pending.remove(call_id)
        how = self.commit_calls.pop(call_id, None) if call_id else None
        if how is not None and how != COMMIT_BACKGROUND and not denied \
                and commit_confirmed(how, not error, output):
            self.add(["commit"])
        self.end = "error" if error and not denied else ("tool" if self.pending else "result")

    def feed(self, e: dict[str, Any]) -> None:
        etype = e.get("type")
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = m.get("content")
        if etype == "assistant":
            if e.get("isApiErrorMessage"):
                self.end = "error"
                return
            if m.get("model") == "<synthetic>":
                return
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    self.add(_report_signals(str(b["text"])))
                    if not self.pending:
                        self.end = "reply"
                elif b.get("type") == "tool_use":
                    self.tool_call(str(b.get("id") or "") or None, str(b.get("name") or ""), b.get("input"))
        elif etype == "user" and not e.get("isMeta"):
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                 for b in content):
                denied = bool(e.get("toolDenialKind"))
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        body = b.get("content")
                        self.tool_result(str(b.get("tool_use_id") or "") or None, bool(b.get("is_error")),
                                         body if isinstance(body, str) else text_of(body), denied)
                return
            text = text_of(content)
            if text.lstrip().startswith("[Request interrupted"):
                self.pending = []
                self.commit_calls = {}
                self.end = "interrupt"
            elif is_human(text):
                self.end = "user"


def outcome_signals(events: list[dict[str, Any]]) -> dict[str, Any]:
    """{signals: {name: count}, end} of a whole Claude Code transcript (the digest carries it)."""
    t = SignalTracker()
    for e in events:
        t.feed(e)
    return {"signals": dict(sorted(t.signals.items())), "end": t.end}


ACTIVE_HOURS = 2
ABANDONED_HOURS = 24
OUTCOMES = ("shipped", "pushed", "committed", "partial", "active", "abandoned", "failed", "no-change")


def outcome(signals: dict[str, int], end: str | None, last_at: datetime | None,
            now: datetime | None = None, kind: str | None = None) -> tuple[str, dict[str, Any]]:
    """(outcome, evidence). The most advanced state wins (docs/sessions-live.md, "Outcome").
    `kind` `worker` (a subagent or dispatch worker): a worker's transcript ends on its
    hand-back (a tool result) or its final reply, so its end shape never makes it abandoned."""
    now = now or datetime.now(timezone.utc)
    s = {k: v for k, v in signals.items() if isinstance(v, int) and v > 0}
    evidence: dict[str, Any] = {**dict(sorted(s.items())), "end": end}
    age_h = (now - last_at).total_seconds() / 3600 if last_at else None
    if s.get("pr_merge") or s.get("push_main") or s.get("merge_main") or s.get("deploy"):
        return "shipped", evidence
    if s.get("push") or s.get("pr_create"):
        return "pushed", evidence
    if s.get("commit"):
        return "committed", evidence
    if s.get("report_partial") or s.get("report_blocked"):
        return "partial", evidence
    if age_h is not None and age_h < ACTIVE_HOURS:
        return "active", evidence
    if kind == "worker":
        if s.get("edit"):
            return "partial", evidence
        if end in ("error", "interrupt") and not s.get("report_done"):
            return "failed", evidence
        return "no-change", evidence
    # Unanswered: a prompt with no reply, or a tool call with no result. A session that ended
    # on a tool result (`result`) got its answer; it is not abandoned for that. Edits count as
    # abandoned only without a final reply (`end` `reply`): edited, then answered = finished, `partial` below.
    dangling = end in ("user", "tool")
    unfinished_edit = s.get("edit") and not s.get("report_done") and end != "reply"
    if age_h is not None and age_h > ABANDONED_HOURS and (dangling or unfinished_edit):
        return "abandoned", evidence
    if s.get("edit"):
        return "partial", evidence
    if end in ("error", "interrupt"):
        return "failed", evidence
    return "no-change", evidence


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


def build_row(transcript: Path, *, routine: str | None, instructions: list[tuple[str, Path]],
              repo: str | None = None, prompt_from_transcript: bool = False) -> dict[str, Any]:
    """The `cloud_digests` row for one transcript. `prompt_from_transcript`: when no
    `prompt` file is given, the session's first user message is the `prompt` instruction.
    `routine` None (hook mode): the row has no `routine` key."""
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
    meta: dict[str, Any] = {"cloud": True, "repo": repo, "script_version": SCRIPT_VERSION, "transcript_bytes": size,
                            "tool_calls": f["tool_calls"], "prompts": f["prompts"], "tokens": token_totals(events),
                            "profile": profile(events), "outcome": outcome_signals(events)}
    title = title_of(events)
    if title:
        meta["title"] = title
    row: dict[str, Any] = {"source_key": SOURCE_PREFIX + session_id}
    if routine is not None:
        row["routine"] = slug(routine)
    row.update({
        "session_id": session_id,
        "started_at": _iso(f["start"]),
        "ended_at": _iso(f["end"]),
        "digest": digest,
        "digest_sha": hashlib.sha256(digest.encode("utf-8")).hexdigest(),
        "instructions": instr,
        "skills": f["skills"],
        "meta": meta,
    })
    return row


# --------------------------------------------------------------------------- hook mode


class HookSkip(Exception):
    """The hook has nothing to do. The message is the status for the log line."""


def hook_dir() -> Path:
    """`$HOME/.claude/amem`: the debounce state and the log."""
    return Path.home() / ".claude" / "amem"


def _local_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint(base: str) -> str:
    """The cloud_digests upsert URL. https only; plain http only to a local host (a test stub).
    Raises HookSkip for anything else."""
    base = base.strip().rstrip("/")
    if not base:
        raise HookSkip("no AMEM_SUPABASE_URL")
    parts = urllib.parse.urlsplit(base)
    if not parts.hostname or parts.scheme not in ("https", "http") or (
            parts.scheme == "http" and not _local_host(parts.hostname)):
        raise HookSkip("bad AMEM_SUPABASE_URL")
    return base + "/rest/v1/cloud_digests?on_conflict=source_key"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 30x is an error: the body never goes to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _ssl_context() -> ssl.SSLContext:
    """The default trust store (honours SSL_CERT_FILE) plus any CA bundle the environment names
    for other tools: a TLS-inspecting proxy's CA is often only there."""
    ctx = ssl.create_default_context()
    for name in CA_BUNDLE_ENVS:
        path = os.environ.get(name, "").strip()
        if path and os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
            except (OSError, ssl.SSLError):
                pass
    return ctx


def post_row(url: str, row: dict[str, Any], timeout: float = HTTP_TIMEOUT_SECONDS) -> int:
    """POST one row as a merge upsert. Returns the HTTP status (0: no answer). Proxies come
    from the environment (HTTPS_PROXY, HTTP_PROXY, NO_PROXY), as urllib reads them."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=_ssl_context()))
    req = urllib.request.Request(url, data=json.dumps(row, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Prefer": "resolution=merge-duplicates,return=minimal"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict[str, Any], now: float) -> None:
    cutoff = now - HOOK_STATE_KEEP_DAYS * 86400
    keep = {k: v for k, v in state.items() if isinstance(v, (int, float)) and v >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(keep, fh, separators=(",", ":"))
    os.replace(tmp, path)


def hook_instructions(cwd: str) -> list[tuple[str, Path]]:
    """CLAUDE.md / AGENTS.md of the hook's cwd, else of the git root above it."""
    if not cwd or not os.path.isabs(cwd):
        return []
    base = Path(cwd)
    dirs = [base]
    for d in base.parents:
        if (d / ".git").exists():
            dirs.append(d)
            break
    out: list[tuple[str, Path]] = []
    for name, file in HOOK_INSTRUCTIONS:
        for d in dirs:
            if (d / file).is_file():
                out.append((name, d / file))
                break
    return out


def run_hook(raw: bytes, extra: list[tuple[str, Path]] | None = None, now: float | None = None) -> str:
    """Handle one hook payload. Returns the status for the log line."""
    now = time.time() if now is None else now
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HookSkip("bad payload") from None
    if not isinstance(data, dict):
        raise HookSkip("bad payload")
    event, sid = data.get("hook_event_name"), data.get("session_id")
    if event not in HOOK_EVENTS or data.get("agent_id"):
        raise HookSkip("ignored")
    if not isinstance(sid, str) or not _SESSION_ID_RE.fullmatch(sid):
        raise HookSkip("bad session id")
    url = endpoint(os.environ.get("AMEM_SUPABASE_URL", ""))
    state_path = hook_dir() / "hook-state.json"
    state = _load_state(state_path)
    last = state.get(sid)
    if event == "Stop" and isinstance(last, (int, float)) and now - last < HOOK_DEBOUNCE_SECONDS:
        raise HookSkip("debounced")
    tp = data.get("transcript_path")
    transcript = Path(tp).expanduser() if isinstance(tp, str) and tp.endswith(".jsonl") else None
    if transcript is None or not transcript.is_file():
        raise HookSkip("no transcript")
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    row = build_row(transcript, routine=None, instructions=hook_instructions(cwd) + list(extra or []))
    if row["session_id"] != sid:
        raise HookSkip("transcript of another session")
    row["kind"] = "chat"
    row["ingested_at"] = None
    row["meta"]["hook"] = event
    code = post_row(url, row)
    if 200 <= code < 300:
        state[sid] = now
        _save_state(state_path, state, now)
        return f"posted {code}"
    return f"failed {code:03d}"


def _log(event: str, sid: str, status: str, took: float) -> None:
    try:
        d = hook_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / "hook.log"
        try:
            if path.stat().st_size > HOOK_LOG_MAX:
                os.replace(path, d / "hook.log.1")
        except OSError:
            pass
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"amem-digest {stamp} {event} {sid} {status} {took:.2f}s\n".encode())
        finally:
            os.close(fd)
    except Exception:
        pass


class _Timeout(Exception):
    pass


def _on_alarm(_signum: int, _frame: Any) -> None:
    raise _Timeout()


def hook_main(argv: list[str]) -> int:
    """`--hook`: always exit 0, never a byte on stdout or stderr (docs/cloud.md, "Cloud chats")."""
    try:
        _hook_main(argv)
    except BaseException:  # a late alarm or anything else: still exit 0
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    return 0


def _hook_main(argv: list[str]) -> None:
    started = time.monotonic()
    status, event, sid = "error", "-", "-"
    alarm = hasattr(signal, "SIGALRM")
    saved = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    except OSError:
        pass
    try:
        if alarm:
            signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(HOOK_TIMEOUT_SECONDS)
        extra: list[tuple[str, Path]] = []
        for i, arg in enumerate(argv):
            if arg == "--instruction" and i + 1 < len(argv):
                try:
                    extra.append(parse_instruction(argv[i + 1]))
                except argparse.ArgumentTypeError:
                    pass
        raw = sys.stdin.buffer.read(HOOK_PAYLOAD_MAX)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                event = str(data.get("hook_event_name") or "-")[:20]
                sid = str(data.get("session_id") or "-")[:8]
        except (ValueError, UnicodeDecodeError):
            pass
        status = run_hook(raw, extra)
    except HookSkip as exc:
        status = str(exc)
    except _Timeout:
        status = "timeout"
    except BaseException as exc:  # never fail the session
        status = f"error ({type(exc).__name__})"
    finally:
        if alarm:
            signal.alarm(0)
        if sys.stdout is not saved[0]:
            try:
                sys.stdout.close()
            except Exception:
                pass
        sys.stdout, sys.stderr = saved
    _log(re.sub(r"[^\w-]", "?", event), re.sub(r"[^\w-]", "?", sid), status, time.monotonic() - started)


# --------------------------------------------------------------------------- main


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amem_digest",
        description="Print the redacted digest of this cloud session as one JSON row for cloud_digests. "
                    "--hook: a Stop / SessionEnd hook that POSTs it itself (docs/cloud.md).")
    p.add_argument("--hook", action="store_true",
                   help="hook mode: payload on stdin, POST to $AMEM_SUPABASE_URL, no output, exit 0")
    p.add_argument("--routine", default=None, help="routine name (slugged: 'Nightly Trials' -> nightly-trials)")
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
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--hook" in argv:
        return hook_main([a for a in argv if a != "--hook"])
    p = parser()
    args = p.parse_args(argv)
    if not args.routine:
        p.error("the following arguments are required: --routine (or --hook)")
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
