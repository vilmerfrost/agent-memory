"""`amem sessions ingest`: index local Claude Code transcripts into `sessions` (docs/deviations.md).

Reads `<projects-dir>/<project>/*.jsonl` (main sessions) and
`<projects-dir>/<project>/<session>/subagents/agent-*.jsonl` (subagents, kind `worker`).
Read only. Nothing here stores transcript text: a row holds the kind, the repo, times,
the transcript size, skill names, instruction source names and a few labels in `meta`.

The parsing rules are copied (not imported) from agent-config `lib/sessions.py`:
- working directory: never a Desktop scratch workspace; prefer the cwd whose encoded
  path is the project folder name; else the first non-scratch cwd (`work_cwd`).
- skills: `Skill` tool calls (input `skill`) plus `<command-name>/x</command-name>` tags.
- eval sessions: the project folder matches `SKIP_PROJECT_RE` (eval scratch clones, probes,
  the memory observer), or the cwd is under `~/.cache/agent-evals` / `~/.cache/skill-evals`.

Kind, first match wins:
1. eval      see above
2. worker    the file is under a `subagents/` folder (meta.agent_type from `agent-*.meta.json`)
3. routine   a scheduled task: the first user message is a `<scheduled-task ...>` block,
             or a turn has `turnOrigin = scheduled`
4. headless  `claude -p` / SDK: `entrypoint` starts with `sdk` (`sdk-cli`, `sdk-ts`)
5. parent    the session ran `parent-lock`, the `dispatch` skill, or spawned a `worker`
6. chat      everything else

Idempotent: one upsert per session_id. A re-ingest refreshes the facts and keeps
`scanned_at` and the scan keys in `meta`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from agent_memory import config, store

KINDS = ("chat", "parent", "worker", "headless", "eval", "routine")
DEFAULT_DAYS = 7

SCRATCH_CWD = "/scratch-workspaces/"
SKIP_PROJECT_RE = re.compile(r"-cache-(agent|skill)-evals|-cache-sbx|claude-mem|-private-tmp-claude-\d+-probe|stallprobe")
EVAL_CWD_RE = re.compile(r"/\.cache/(agent|skill)-evals(/|$)")
COMMAND_RE = re.compile(r"<command-name>/?([\w:.-]+)</command-name>")
SCHEDULED_RE = re.compile(r'^\s*<scheduled-task\b([^>]*)>')
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_ROUTINE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")

# Slash commands that are Claude Code built-ins, not skills.
BUILTIN_COMMANDS = frozenset({
    "add-dir", "agents", "bug", "clear", "compact", "config", "context", "cost", "doctor", "exit",
    "export", "goal", "help", "hooks", "ide", "init", "login", "logout", "mcp", "memory", "model",
    "permissions", "plugin", "pr-comments", "release-notes", "reload-plugins", "reload-skills",
    "resume", "review", "rewind", "status", "statusline", "terminal-setup", "upgrade", "usage",
    "vim", "fast", "effort", "output-style", "privacy-settings", "todos", "watch", "pet", "board",
    "artifacts", "loop", "agents-md",
})


def claude_home() -> Path:
    """AMEM_CLAUDE_HOME (tests), else ~/.claude."""
    config.load_env()
    env = os.environ.get("AMEM_CLAUDE_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".claude"


def projects_dir(arg: Path | None = None) -> Path:
    """--projects-dir, else AMEM_PROJECTS_DIR, else <claude home>/projects."""
    if arg is not None:
        return arg.expanduser()
    config.load_env()
    env = os.environ.get("AMEM_PROJECTS_DIR", "").strip()
    return Path(env).expanduser() if env else claude_home() / "projects"


def agent_config_dir() -> Path:
    """AMEM_AGENT_CONFIG_DIR (tests), else ~/agent-config."""
    config.load_env()
    env = os.environ.get("AMEM_AGENT_CONFIG_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / "agent-config"


# ---------------------------------------------------------------- reading


def load_events(path: Path) -> list[dict[str, Any]]:
    """Every JSON object line of a transcript. Bad lines are skipped."""
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ts = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


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


def work_cwd(cwds: list[str], project: str = "") -> tuple[str, bool]:
    """(cwd, scratch_only) for a session's distinct cwds in first-seen order."""
    real = [c for c in cwds if SCRATCH_CWD not in c]
    if not real:
        return "", bool(cwds)
    folder = [c for c in real if re.sub(r"[^A-Za-z0-9]", "-", c) == project]
    return (folder or real)[0], False


def repo_of(cwd: str) -> str | None:
    """The repo a cwd belongs to: `<...>/worktrees/<repo>/<name>` -> repo, else the basename.
    None for no cwd or the home folder."""
    if not cwd:
        return None
    path = Path(cwd)
    parts = path.parts
    if "worktrees" in parts:
        i = len(parts) - 1 - parts[::-1].index("worktrees")
        if i + 1 < len(parts):
            return parts[i + 1]
    if path == Path.home() or path.name in ("", "/"):
        return None
    return path.name


def skill_name(raw: Any) -> str | None:
    name = str(raw or "").strip().lstrip("/")
    return name if re.fullmatch(r"[\w.-]+(:[\w.-]+)?", name) else None


# ---------------------------------------------------------------- one transcript


@dataclass
class SessionRow:
    session_id: str
    kind: str
    repo: str | None
    project_dir: str
    started_at: datetime | None
    ended_at: datetime | None
    transcript_bytes: int
    skills: list[str]
    instructions: list[str]
    meta: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @property
    def cwd(self) -> str:
        return str(self.meta.get("cwd") or "")


@dataclass
class Facts:
    """What the classifier needs from a transcript. Labels and names only."""

    start: datetime | None = None
    end: datetime | None = None
    cwds: list[str] = field(default_factory=list)
    entrypoints: set[str] = field(default_factory=set)
    skills: list[str] = field(default_factory=list)
    scheduled: dict[str, str] | None = None  # attributes of the <scheduled-task> tag
    turn_origins: set[str] = field(default_factory=set)
    ran_parent_lock: bool = False
    spawned_workers: int = 0
    prompts: int = 0
    tool_calls: int = 0


def scan_facts(events: list[dict[str, Any]]) -> Facts:
    f = Facts()
    seen_cwds: dict[str, None] = {}
    seen_skills: dict[str, None] = {}
    first_user = True
    for e in events:
        ts = parse_ts(e.get("timestamp"))
        if ts:
            f.start = min(f.start, ts) if f.start else ts
            f.end = max(f.end, ts) if f.end else ts
        if isinstance(e.get("cwd"), str) and e["cwd"]:
            seen_cwds.setdefault(e["cwd"], None)
        if isinstance(e.get("entrypoint"), str):
            f.entrypoints.add(e["entrypoint"])
        if isinstance(e.get("turnOrigin"), str):
            f.turn_origins.add(e["turnOrigin"])
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = m.get("content")
        if e.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                f.tool_calls += 1
                name, inp = b.get("name"), b.get("input") if isinstance(b.get("input"), dict) else {}
                if name == "Skill":
                    sk = skill_name(inp.get("skill"))
                    if sk:
                        seen_skills.setdefault(sk, None)
                elif name == "Agent" and inp.get("subagent_type") == "worker":
                    f.spawned_workers += 1
                elif name == "Bash" and "parent-lock" in str(inp.get("command", "")):
                    f.ran_parent_lock = True
        elif e.get("type") == "user" and not e.get("isMeta"):
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                 for b in content):
                continue
            text = text_of(content)
            if first_user and text.strip():
                first_user = False
                tag = SCHEDULED_RE.match(text)
                if tag:
                    f.scheduled = dict(_ATTR_RE.findall(tag.group(1)))
            for cmd in COMMAND_RE.findall(text):
                sk = skill_name(cmd)
                if sk and sk not in BUILTIN_COMMANDS:
                    seen_skills.setdefault(sk, None)
            if is_human(text):
                f.prompts += 1
    f.cwds = list(seen_cwds)
    f.skills = list(seen_skills)
    return f


def classify(f: Facts, *, project_dir: str, cwd: str, subagent: bool) -> str:
    if SKIP_PROJECT_RE.search(project_dir) or (cwd and EVAL_CWD_RE.search(cwd)) \
            or any(EVAL_CWD_RE.search(c) for c in f.cwds):
        return "eval"
    if subagent:
        return "worker"
    if f.scheduled is not None or "scheduled" in f.turn_origins:
        return "routine"
    if any(ep.startswith("sdk") for ep in f.entrypoints):
        return "headless"
    if f.ran_parent_lock or f.spawned_workers or "dispatch" in f.skills:
        return "parent"
    return "chat"


def routine_name(f: Facts) -> str | None:
    if f.scheduled is None:
        return None
    name = f.scheduled.get("name", "")
    return name if _ROUTINE_NAME_RE.fullmatch(name) else None


def _subagent_meta(path: Path) -> dict[str, Any]:
    mf = path.with_suffix(".meta.json")
    try:
        data = json.loads(mf.read_text(encoding="utf-8")) if mf.is_file() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def session_row(path: Path, project_dir: str, *, parent_session: str | None = None) -> SessionRow:
    """One transcript file to a row (no database)."""
    events = load_events(path)
    facts = scan_facts(events)
    subagent = parent_session is not None
    cwd, scratch_only = work_cwd(facts.cwds, project_dir)
    kind = classify(facts, project_dir=project_dir, cwd=cwd, subagent=subagent)
    meta: dict[str, Any] = {
        "cwd": cwd or None,
        "scratch_only": scratch_only,
        "entrypoints": sorted(facts.entrypoints),
        "prompts": facts.prompts,
        "tool_calls": facts.tool_calls,
        "spawned_workers": facts.spawned_workers,
    }
    if subagent:
        sub = _subagent_meta(path)
        meta["parent_session"] = parent_session
        meta["agent_type"] = str(sub.get("agentType") or "") or None
        sid = f"{parent_session}/{path.stem}"
    else:
        sid = path.stem
    rname = routine_name(facts)
    if rname:
        meta["routine"] = rname
        file_attr = (facts.scheduled or {}).get("file")
        if file_attr:
            meta["routine_file"] = file_attr
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    row = SessionRow(sid, kind, repo_of(cwd), project_dir, facts.start, facts.end, size,
                     facts.skills, [], meta, path)
    from agent_memory import deviations  # local import: deviations imports this module

    row.instructions = [src.name for src in deviations.instruction_sources(row)]
    return row


# ---------------------------------------------------------------- discovery


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def transcripts(root: Path, since: datetime) -> Iterator[tuple[Path, str, str | None]]:
    """(file, project folder name, parent session id or None) for files touched since `since`."""
    if not root.is_dir():
        return
    for proj in sorted(root.iterdir()):
        if not proj.is_dir():
            continue
        for f in sorted(proj.glob("*.jsonl")):
            try:
                if _mtime(f) >= since:
                    yield f, proj.name, None
            except OSError:
                continue
        for f in sorted(proj.glob("*/subagents/agent-*.jsonl")):
            try:
                if _mtime(f) >= since:
                    yield f, proj.name, f.parent.parent.name
            except OSError:
                continue


@dataclass
class IngestReport:
    files: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0  # empty transcripts or ones that ended before the window
    kinds: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        kinds = " ".join(f"{k}={self.kinds.get(k, 0)}" for k in KINDS)
        return (f"sessions: {self.files} files, {self.new} new, {self.updated} updated, "
                f"{self.skipped} skipped ({kinds})")


def collect(root: Path, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> tuple[list[SessionRow], int]:
    """Rows for every transcript in the window, plus the number skipped."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    rows: list[SessionRow] = []
    skipped = 0
    for path, project, parent in transcripts(root, since):
        row = session_row(path, project, parent_session=parent)
        if row.started_at is None or (row.ended_at is not None and row.ended_at < since):
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


_UPSERT_SQL = """
insert into sessions (session_id, kind, repo, project_dir, started_at, ended_at, transcript_bytes,
                      skills, instructions, meta)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (session_id) do update
    set kind = excluded.kind, repo = excluded.repo, project_dir = excluded.project_dir,
        started_at = excluded.started_at, ended_at = excluded.ended_at,
        transcript_bytes = excluded.transcript_bytes, skills = excluded.skills,
        instructions = excluded.instructions, meta = sessions.meta || excluded.meta
    where (sessions.kind, sessions.repo, sessions.started_at, sessions.ended_at, sessions.transcript_bytes,
           sessions.skills, sessions.instructions)
          is distinct from
          (excluded.kind, excluded.repo, excluded.started_at, excluded.ended_at, excluded.transcript_bytes,
           excluded.skills, excluded.instructions)
       or not (sessions.meta @> excluded.meta)
returning (xmax = 0) as inserted
"""


def upsert(rows: list[SessionRow], *, db_url: str | None = None) -> tuple[int, int]:
    """(new, updated). Unchanged rows are not touched. One transaction."""
    new = updated = 0
    with store.connect(db_url) as conn:
        for r in rows:
            got = conn.execute(_UPSERT_SQL, (
                r.session_id, r.kind, r.repo, r.project_dir, r.started_at, r.ended_at, r.transcript_bytes,
                Jsonb(r.skills), Jsonb(r.instructions), Jsonb(r.meta),
            )).fetchone()
            if got is None:
                continue
            if got[0]:
                new += 1
            else:
                updated += 1
    return new, updated


def ingest(root: Path, *, days: int = DEFAULT_DAYS, now: datetime | None = None,
           db_url: str | None = None) -> IngestReport:
    rows, skipped = collect(root, days=days, now=now)
    report = IngestReport(files=len(rows) + skipped, skipped=skipped)
    for r in rows:
        report.kinds[r.kind] = report.kinds.get(r.kind, 0) + 1
    report.new, report.updated = upsert(rows, db_url=db_url)
    return report
