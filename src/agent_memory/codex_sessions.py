"""Codex session transcripts (the ChatGPT desktop app's Codex, and the Codex CLI) for
`amem sessions ingest` and `amem deviations scan` (docs/deviations.md, "Codex").

Where: `<codex home>/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl` (and
`<codex home>/archived_sessions/`). One JSON object per line: `{"timestamp", "ordinal",
"type", "payload"}`. The types this module reads:

- `session_meta` (first line): `id` (the thread id), `cwd`, `originator` (`Codex Desktop`,
  `codex_exec`, `codex-tui`, `codex_vscode`), `source` (`vscode`, `exec`, or
  `{"subagent": {"thread_spawn": {"parent_thread_id", "agent_path", "agent_role", ...}}}`),
  `thread_source` (`user`, `subagent`, ...), `agent_path`, `git.repository_url`, and for a
  forked subagent `subagent_history_start_ordinal`: lines below that ordinal are a copy of
  the parent's history, not this thread's own work.
- `turn_context`: `cwd` per turn.
- `response_item` / `message`: `role` user | assistant | developer, `content` blocks
  (`input_text` / `output_text`). User messages also carry injected blocks: the AGENTS.md
  text (`# AGENTS.md instructions for ...` + `<INSTRUCTIONS>`), `<environment_context>`,
  and `<skill><name>X</name>...` when the user invoked a skill.
- `response_item` / `agent_message`: a message between agents (`author`, `recipient` are
  agent paths like `/root`, `/root/worker_api`). A subagent gets its brief this way.
- `response_item` / `function_call`: collaboration tools (`spawn_agent`, `send_message`, ...).
- `event_msg` / `item_completed`: what a turn did: `CommandExecution` (`command` argv,
  `status`, `exit_code`, `parsed_cmd`), `FileChange` (`changes` keyed by path),
  `McpToolCall` (`server`, `tool`, `status`, `error`), `WebSearch`.
- `event_msg` / `turn_aborted`: the user interrupted a turn.

Read only. This module never stores or prints transcript text; it returns labels, names
and counts (`scan_facts`) and the helpers the digest builder uses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PREFIX = "codex:"            # session ids: codex:<thread id>
HEADLESS_ORIGINATORS = frozenset({"codex_exec"})
SHELL_TOOLS = frozenset({"shell", "exec_command", "local_shell", "container.exec"})

SKILL_TAG_RE = re.compile(r"<skill>\s*<name>\s*([\w.:-]{1,80})\s*</name>")
# A SKILL.md read from one of the shared skill homes is a skill invocation (Codex has the model
# open the file itself). Reads from a repo checkout (editing skills) do not count.
SKILL_FILE_RE = re.compile(r"(?:^|/)\.(?:agents|codex|claude)/skills/(?:\.system/)?([\w.-]+)/SKILL\.md$")
_ID_RE = re.compile(r"[0-9a-fA-F][0-9a-fA-F-]{7,63}")
_FILE_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def payload(e: dict[str, Any]) -> dict[str, Any]:
    p = e.get("payload")
    return p if isinstance(p, dict) else {}


def is_codex(events: list[dict[str, Any]]) -> bool:
    """A Codex rollout starts with a `session_meta` line."""
    return any(e.get("type") == "session_meta" and payload(e) for e in events[:3])


def meta_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The thread's own session_meta payload (the first one; a fork copies the parent's after it)."""
    for e in events:
        if e.get("type") == "session_meta":
            return payload(e)
    return {}


def own_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The thread's own events: a forked subagent's copy of the parent history is dropped."""
    start = meta_of(events).get("subagent_history_start_ordinal")
    if not isinstance(start, int) or isinstance(start, bool):
        return [e for e in events if e.get("type") != "session_meta"]
    return [e for e in events if e.get("type") != "session_meta"
            and not (isinstance(e.get("ordinal"), int) and e["ordinal"] < start)]


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content
                         if isinstance(b, dict) and b.get("type") in ("input_text", "output_text", "text"))
    return ""


def is_prompt(text: str) -> bool:
    """A prompt a person typed: not an injected block (AGENTS.md, environment, skill, app context)."""
    t = (text or "").lstrip()
    if not t or t.startswith(("<", "[Request interrupted", "Caveat:")):
        return False
    return not (t.startswith("#") and ("<INSTRUCTIONS>" in t[:4000] or t.startswith("# AGENTS.md")))


def command_text(cmd: Any) -> str:
    """The command a CommandExecution ran: the script of `zsh -lc <script>`, else the argv joined."""
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list):
        argv = [str(a) for a in cmd]
        if len(argv) >= 3 and argv[0].rsplit("/", 1)[-1] in ("bash", "zsh", "sh") and argv[1] in ("-lc", "-c"):
            return argv[2]
        return " ".join(argv)
    return ""


def skill_reads(item: dict[str, Any]) -> list[str]:
    """Skill names whose SKILL.md this CommandExecution read (parsed_cmd `read` entries)."""
    out = []
    parsed = item.get("parsed_cmd")
    for p in parsed if isinstance(parsed, list) else []:
        if isinstance(p, dict) and p.get("type") == "read":
            m = SKILL_FILE_RE.search(str(p.get("path") or ""))
            if m:
                out.append(m.group(1))
    return out


def args_of(p: dict[str, Any]) -> dict[str, Any]:
    raw = p.get("arguments")
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def items(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `item_completed` items, in order."""
    out = []
    for e in events:
        p = payload(e)
        if e.get("type") == "event_msg" and p.get("type") == "item_completed" and isinstance(p.get("item"), dict):
            out.append(p["item"])
    return out


TOOL_ITEMS = frozenset({"CommandExecution", "FileChange", "McpToolCall", "WebSearch"})


def covered_by_items(p: dict[str, Any]) -> bool:
    """A function_call that an `item_completed` item also shows (MCP tools, shell commands)."""
    return str(p.get("namespace") or "").startswith("mcp__") or str(p.get("name") or "") in SHELL_TOOLS


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


@dataclass
class CodexFacts:
    """What the classifier needs from a Codex rollout. Labels, names and counts only."""

    thread_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    cwds: list[str] = field(default_factory=list)
    originator: str | None = None
    source: str | None = None           # vscode | exec | subagent | ...
    thread_source: str | None = None
    subagent: bool = False
    parent_thread: str | None = None
    agent_role: str | None = None
    repo_url: str | None = None
    skills: list[str] = field(default_factory=list)
    prompts: int = 0
    tool_calls: int = 0
    spawned_workers: int = 0

    @property
    def headless(self) -> bool:
        return self.originator in HEADLESS_ORIGINATORS or self.source == "exec"


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[\w .:/-]{1,60}", value) else None


def thread_id_of(meta: dict[str, Any], file_stem: str) -> str | None:
    tid = meta.get("id")
    if isinstance(tid, str) and _ID_RE.fullmatch(tid):
        return tid
    m = _FILE_ID_RE.search(file_stem)
    return m.group(1) if m else None


def scan_facts(events: list[dict[str, Any]], file_stem: str = "") -> CodexFacts:
    meta = meta_of(events)
    f = CodexFacts(thread_id=thread_id_of(meta, file_stem))
    f.originator = _label(meta.get("originator"))
    f.thread_source = _label(meta.get("thread_source"))
    src = meta.get("source")
    spawn: dict[str, Any] = {}
    if isinstance(src, dict):
        sub = src.get("subagent")
        f.source = "subagent" if sub is not None else _label(next(iter(src), None))
        if isinstance(sub, dict) and isinstance(sub.get("thread_spawn"), dict):
            spawn = sub["thread_spawn"]
    else:
        f.source = _label(src)
    f.subagent = f.thread_source == "subagent" or f.source == "subagent"
    parent = spawn.get("parent_thread_id") or meta.get("parent_thread_id")
    f.parent_thread = parent if isinstance(parent, str) and _ID_RE.fullmatch(parent) else None
    f.agent_role = _label(spawn.get("agent_role"))
    git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
    url = git.get("repository_url")
    f.repo_url = url if isinstance(url, str) and url.strip() else None

    seen_cwds: dict[str, None] = {}
    if isinstance(meta.get("cwd"), str) and meta["cwd"]:
        seen_cwds[meta["cwd"]] = None
    seen_skills: dict[str, None] = {}
    own = own_events(events)
    has_items = any(e.get("type") == "event_msg" and payload(e).get("type") == "item_completed" for e in own)
    for e in [*events[:1], *own]:
        ts = _ts(e.get("timestamp"))
        if ts:
            f.start = min(f.start, ts) if f.start else ts
            f.end = max(f.end, ts) if f.end else ts
    for e in own:
        t, p = e.get("type"), payload(e)
        pt = p.get("type")
        if t == "turn_context" and isinstance(p.get("cwd"), str) and p["cwd"]:
            seen_cwds.setdefault(p["cwd"], None)
        elif t == "response_item" and pt == "message" and p.get("role") == "user":
            text = message_text(p.get("content"))
            for name in SKILL_TAG_RE.findall(text):
                seen_skills.setdefault(name, None)
            if is_prompt(text):
                f.prompts += 1
        elif t == "response_item" and pt == "agent_message":
            me = meta.get("agent_path")
            if me and p.get("recipient") == me and p.get("author") != me:
                f.prompts += 1  # a subagent's brief or a follow-up: its "user" turn
        elif t == "response_item" and pt == "function_call":
            name = str(p.get("name") or "")
            if name == "spawn_agent":
                f.spawned_workers += 1
            if not (has_items and covered_by_items(p)):
                f.tool_calls += 1
        elif t == "response_item" and pt in ("custom_tool_call", "local_shell_call") and not has_items:
            f.tool_calls += 1
        elif t == "event_msg" and pt == "item_completed":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            if item.get("type") in TOOL_ITEMS:
                f.tool_calls += 1
            if item.get("type") == "CommandExecution":
                for name in skill_reads(item):
                    seen_skills.setdefault(name, None)
    f.cwds = list(seen_cwds)
    f.skills = list(seen_skills)
    return f
