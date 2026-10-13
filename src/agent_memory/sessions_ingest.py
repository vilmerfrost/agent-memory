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
3. routine   a scheduled task: the first user message holds a `<scheduled-task ...>` block,
             or a turn has `turnOrigin = scheduled`
4. headless  `claude -p` / SDK: `entrypoint` starts with `sdk` (`sdk-cli`, `sdk-ts`)
5. parent    the session ran `parent-lock`, the `dispatch` skill, or spawned a `worker`
6. chat      everything else

Codex sessions (`tool = codex`, codex_sessions.py) come from `<codex home>/sessions` and
`<codex home>/archived_sessions`. Id `codex:<thread id>`, `project_dir` the folder relative
to the Codex home (`sessions/2026/10/13`), `meta.file` the file name. Kind, first match wins:
eval (cwd), worker (a subagent thread), headless (`codex_exec`), parent (the `dispatch`
skill), chat. Skills: `<skill>` blocks the user invoked plus SKILL.md files the agent read
from a skill home.

Team repos (both tools): `meta.team` is true when the repo's git remote owner is not a
personal owner (AMEM_PERSONAL_OWNERS, default `vilmerfrost`) or
`<claude home>/parent/<repo>/profile.md` says `mode: team`. The edit step never edits a
team repo's files.

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

from agent_memory import codex_sessions, config, store

KINDS = ("chat", "parent", "worker", "headless", "eval", "routine")
TOOLS = ("claude", "codex")
DEFAULT_DAYS = 7
PERSONAL_OWNERS_ENV = "AMEM_PERSONAL_OWNERS"
DEFAULT_PERSONAL_OWNERS = ("vilmerfrost",)

SCRATCH_CWD = "/scratch-workspaces/"
SKIP_PROJECT_RE = re.compile(r"-cache-(agent|skill)-evals|-cache-sbx|claude-mem|-private-tmp-claude-\d+-probe|stallprobe")
EVAL_CWD_RE = re.compile(r"/\.cache/(agent|skill)-evals(/|$)")
COMMAND_RE = re.compile(r"<command-name>/?([\w:.-]+)</command-name>")
# Searched in the first user message only: a <system-reminder> block may come before it.
SCHEDULED_RE = re.compile(r'<scheduled-task\b([^>]*)>')
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


def codex_home(arg: Path | None = None) -> Path:
    """--codex-home, else AMEM_CODEX_HOME, else `.codex` next to the Claude home (~/.codex).
    Following the Claude home means a test that points AMEM_CLAUDE_HOME at a tmp dir never
    reads the real ~/.codex."""
    if arg is not None:
        return arg.expanduser()
    config.load_env()
    env = os.environ.get("AMEM_CODEX_HOME", "").strip()
    return Path(env).expanduser() if env else claude_home().parent / ".codex"


# ---------------------------------------------------------------- team repos


def main_checkout(cwd: Path) -> Path | None:
    """For a git worktree (`.git` is a file `gitdir: <main>/.git/worktrees/<x>`), the main checkout."""
    dotgit = cwd / ".git"
    try:
        if not dotgit.is_file():
            return None
        line = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    m = re.match(r"gitdir:\s*(.+)", line)
    if not m:
        return None
    parts = Path(m.group(1).strip()).parts
    if "worktrees" in parts:
        i = parts.index("worktrees")
        main_git = Path(*parts[:i])
        if main_git.name == ".git":
            return main_git.parent
    return None


_REMOTE_RE = re.compile(r'^\s*\[remote\s+"([^"]+)"\]\s*$')
_URL_RE = re.compile(r"^\s*url\s*=\s*(\S+)\s*$")
_SECTION_RE = re.compile(r"^\s*\[")


def git_remote_url(cwd: str) -> str | None:
    """The `origin` url (else the first remote's) from the repo's `.git/config`: the cwd or
    the nearest parent with a `.git`, a worktree's main checkout. Reads no other file."""
    if not cwd:
        return None
    start = Path(cwd)
    home = Path.home()
    for d in [start, *start.parents]:
        if d == home or d.parent == d:
            break
        dotgit = d / ".git"
        try:
            if dotgit.is_dir():
                cfg = dotgit / "config"
            elif dotgit.is_file():
                main = main_checkout(d)
                if main is None:
                    return None
                cfg = main / ".git" / "config"
            else:
                continue
            lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        urls: dict[str, str] = {}
        remote = None
        for line in lines:
            m = _REMOTE_RE.match(line)
            if m:
                remote = m.group(1)
                continue
            if _SECTION_RE.match(line):
                remote = None
                continue
            u = _URL_RE.match(line)
            if remote is not None and u:
                urls.setdefault(remote, u.group(1))
        return urls.get("origin") or next(iter(urls.values()), None)
    return None


_OWNER_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?[^/:]+(?::\d+)?[:/]+([\w.-]+)/[\w.-]+?(?:\.git)?/?$")


def remote_owner(url: str | None) -> str | None:
    """`git@github-x:owner/repo.git`, `https://github.com/owner/repo` -> owner. None for a
    local path or anything else."""
    if not url:
        return None
    m = _OWNER_RE.match(url.strip())
    return m.group(1) if m else None


def personal_owners() -> set[str]:
    config.load_env()
    env = os.environ.get(PERSONAL_OWNERS_ENV, "").strip()
    names = env.split(",") if env else DEFAULT_PERSONAL_OWNERS
    return {n.strip().lower() for n in names if n.strip()}


_MODE_RE = re.compile(r"^\s*[-*]?\s*mode:\s*([\w-]+)", re.M | re.I)


def profile_mode(repo: str | None) -> str | None:
    """`mode:` from `<claude home>/parent/<repo>/profile.md` (personal | team | off), or None."""
    if not repo or not re.fullmatch(r"[\w][\w .-]{0,100}", repo) or ".." in repo:
        return None
    path = claude_home() / "parent" / repo / "profile.md"
    try:
        m = _MODE_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1).lower() if m else None


def team_info(cwd: str, repo: str | None, url: str | None = None) -> dict[str, Any]:
    """`team`, plus `remote_owner` and `team_reason` when known. `url` (the remote a Codex
    transcript recorded) wins over the repo's git config.

    No owner and no team profile -> {} (unknown): a re-ingest after the worktree was removed
    keeps what an earlier ingest stored, so a team flag is never lost to a cleanup."""
    owner = remote_owner(url or git_remote_url(cwd))
    if profile_mode(repo) == "team":
        out: dict[str, Any] = {"team": True, "team_reason": "profile"}
    elif owner:
        team = owner.lower() not in personal_owners()
        out = {"team": team, "team_reason": "remote-owner"} if team else {"team": False}
    else:
        return {}
    if owner:
        out["remote_owner"] = owner
    return out


def spread_team(rows: list[SessionRow]) -> None:
    """A session whose remote is unknown (its worktree is gone) is a team session when another
    session of the same repo in this ingest is one (`team_reason = same-repo`). Only ever
    turns the flag on: the safe direction."""
    team_repos = {r.repo for r in rows if r.team and r.repo}
    for r in rows:
        if "team" not in r.meta and r.repo in team_repos:
            r.meta.update(team=True, team_reason="same-repo")


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
    tool: str = "claude"   # claude | codex

    @property
    def cwd(self) -> str:
        return str(self.meta.get("cwd") or "")

    @property
    def team(self) -> bool:
        return self.meta.get("team") is True


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
                tag = SCHEDULED_RE.search(text)
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
    repo = repo_of(cwd)
    meta.update(team_info(cwd, repo))
    row = SessionRow(sid, kind, repo, project_dir, facts.start, facts.end, size,
                     facts.skills, [], meta, path)
    return _with_instructions(row)


def _with_instructions(row: SessionRow) -> SessionRow:
    from agent_memory import deviations  # local import: deviations imports this module

    row.instructions = [src.name for src in deviations.instruction_sources(row)]
    return row


def classify_codex(f: codex_sessions.CodexFacts, cwd: str) -> str:
    if (cwd and EVAL_CWD_RE.search(cwd)) or any(EVAL_CWD_RE.search(c) for c in f.cwds):
        return "eval"
    if f.subagent:
        return "worker"
    if f.headless:
        return "headless"
    if "dispatch" in f.skills:
        return "parent"
    return "chat"


def codex_row(path: Path, rel_dir: str) -> SessionRow:
    """One Codex rollout file to a row (no database). `rel_dir`: its folder relative to the
    Codex home."""
    events = load_events(path)
    f = codex_sessions.scan_facts(events, path.stem)
    cwd, scratch_only = work_cwd(f.cwds, "")
    kind = classify_codex(f, cwd)
    meta: dict[str, Any] = {
        "cwd": cwd or None,
        "scratch_only": scratch_only,
        "file": path.name,
        "originator": f.originator,
        "source": f.source,
        "thread_source": f.thread_source,
        "prompts": f.prompts,
        "tool_calls": f.tool_calls,
        "spawned_workers": f.spawned_workers,
    }
    if f.subagent:
        meta["parent_session"] = codex_sessions.PREFIX + f.parent_thread if f.parent_thread else None
        meta["agent_type"] = f.agent_role
    # Codex app worktrees (~/.codex/worktrees/<task>) are named after the task: the main
    # checkout names the repo.
    main = main_checkout(Path(cwd)) if cwd else None
    repo = main.name if main is not None else repo_of(cwd)
    meta.update(team_info(cwd, repo, f.repo_url))
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    sid = codex_sessions.PREFIX + (f.thread_id or path.stem)
    row = SessionRow(sid, kind, repo, rel_dir, f.start, f.end, size, f.skills, [], meta, path, tool="codex")
    return _with_instructions(row)


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


CODEX_DIRS = ("sessions", "archived_sessions")


def codex_transcripts(home: Path, since: datetime) -> Iterator[tuple[Path, str]]:
    """(file, folder relative to the Codex home) for rollout files touched since `since`."""
    for sub in CODEX_DIRS:
        base = home / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("rollout-*.jsonl")):
            try:
                if f.is_file() and _mtime(f) >= since:
                    yield f, str(f.parent.relative_to(home))
            except (OSError, ValueError):
                continue


@dataclass
class IngestReport:
    files: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0  # empty transcripts or ones that ended before the window
    kinds: dict[str, int] = field(default_factory=dict)
    tools: dict[str, int] = field(default_factory=dict)
    team: int = 0
    cloud: int = 0      # cloud routine sessions ingested from cloud_digests in this run
    cloud_bad: int = 0  # cloud_digests rows that could not be ingested

    def line(self) -> str:
        kinds = " ".join(f"{k}={self.kinds.get(k, 0)}" for k in KINDS)
        tools = " ".join(f"{t}={self.tools.get(t, 0)}" for t in TOOLS)
        cloud = f" cloud={self.cloud}" if self.cloud or self.cloud_bad else ""
        bad = f" cloud_bad={self.cloud_bad}" if self.cloud_bad else ""
        return (f"sessions: {self.files} files, {self.new} new, {self.updated} updated, "
                f"{self.skipped} skipped ({kinds}) tools({tools}) team={self.team}{cloud}{bad}")


def collect(root: Path | None, *, days: int = DEFAULT_DAYS, now: datetime | None = None,
            codex: Path | None = None, tools: tuple[str, ...] = TOOLS) -> tuple[list[SessionRow], int]:
    """Rows for every transcript in the window, plus the number skipped. `root`: the Claude
    projects folder; `codex`: the Codex home (default `codex_home()`)."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    found: list[SessionRow] = []
    if "claude" in tools and root is not None:
        found += [session_row(path, project, parent_session=parent)
                  for path, project, parent in transcripts(root, since)]
    if "codex" in tools:
        found += [codex_row(path, rel) for path, rel in codex_transcripts(codex_home(codex), since)]
    rows: list[SessionRow] = []
    skipped = 0
    for row in found:
        if row.started_at is None or (row.ended_at is not None and row.ended_at < since):
            skipped += 1
            continue
        rows.append(row)
    spread_team(rows)
    return rows, skipped


_UPSERT_SQL = """
insert into sessions (session_id, tool, kind, repo, project_dir, started_at, ended_at, transcript_bytes,
                      skills, instructions, meta)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (session_id) do update
    set tool = excluded.tool, kind = excluded.kind, repo = excluded.repo, project_dir = excluded.project_dir,
        started_at = excluded.started_at, ended_at = excluded.ended_at,
        transcript_bytes = excluded.transcript_bytes, skills = excluded.skills,
        instructions = excluded.instructions, meta = sessions.meta || excluded.meta
    where (sessions.tool, sessions.kind, sessions.repo, sessions.project_dir, sessions.started_at,
           sessions.ended_at, sessions.transcript_bytes, sessions.skills, sessions.instructions)
          is distinct from
          (excluded.tool, excluded.kind, excluded.repo, excluded.project_dir, excluded.started_at,
           excluded.ended_at, excluded.transcript_bytes, excluded.skills, excluded.instructions)
       or not (sessions.meta @> excluded.meta)
returning (xmax = 0) as inserted
"""


def upsert(rows: list[SessionRow], *, db_url: str | None = None) -> tuple[int, int]:
    """(new, updated). Unchanged rows are not touched. One transaction."""
    new = updated = 0
    with store.connect(db_url) as conn:
        for r in rows:
            got = conn.execute(_UPSERT_SQL, (
                r.session_id, r.tool, r.kind, r.repo, r.project_dir, r.started_at, r.ended_at,
                r.transcript_bytes, Jsonb(r.skills), Jsonb(r.instructions), Jsonb(r.meta),
            )).fetchone()
            if got is None:
                continue
            if got[0]:
                new += 1
            else:
                updated += 1
    return new, updated


# ---------------------------------------------------------------- cloud routines

CLOUD_PREFIX = "cloud:"          # session ids of cloud routine sessions: cloud:<session id>
CLOUD_PROJECT_DIR = "cloud"
CLOUD_PROMPT = "prompt"          # the instruction named `prompt` is the routine prompt
_CLOUD_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")


def cloud_source_name(routine: str, name: str) -> str | None:
    """The instruction source of a stored cloud instruction: `routine:<routine>` for the
    prompt, `cloud-file:<name>` for a file (NIGHTLY.md -> cloud-file:nightly-md)."""
    if not _CLOUD_NAME_RE.fullmatch(name or ""):
        return None
    return f"routine:{routine}" if name == CLOUD_PROMPT else f"cloud-file:{name}"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def cloud_row(rec: dict[str, Any]) -> SessionRow | None:
    """One `cloud_digests` row to a `sessions` row (no database). None for a row with a bad
    routine name or session id (it stays un-ingested and is reported)."""
    key, routine = str(rec.get("source_key") or ""), str(rec.get("routine") or "")
    if not key.startswith(CLOUD_PREFIX) or not _ROUTINE_NAME_RE.fullmatch(routine):
        return None
    src = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
    names: list[str] = []
    for item in rec.get("instructions") or []:
        name = cloud_source_name(routine, str(item.get("name") or "")) if isinstance(item, dict) else None
        if name and name not in names:
            names.append(name)
    skills = [s for s in (skill_name(x) for x in rec.get("skills") or []) if s]
    repo = src.get("repo") if isinstance(src.get("repo"), str) and src.get("repo") else None
    meta: dict[str, Any] = {
        "cloud": True,
        "routine": routine,
        "prompts": _count(src.get("prompts")),
        "tool_calls": _count(src.get("tool_calls")),
        "script_version": str(src.get("script_version") or "") or None,
        "digest_sha": rec.get("digest_sha"),
    }
    size = _count(src.get("transcript_bytes")) or len(rec.get("digest") or "")
    return SessionRow(key, "routine", repo, CLOUD_PROJECT_DIR, rec.get("started_at"), rec.get("ended_at"),
                      size, skills, names, meta)


_CLOUD_PENDING_SQL = """
select source_key, routine, session_id, started_at, ended_at, digest, digest_sha, instructions, skills, meta
from cloud_digests where ingested_at is null
order by created_at, source_key
for update skip locked
"""


@dataclass
class CloudReport:
    rows: list[SessionRow] = field(default_factory=list)
    new: int = 0
    updated: int = 0
    bad: int = 0      # rows left un-ingested (bad routine name or key)


def ingest_cloud(*, db_url: str | None = None) -> CloudReport:
    """Turn new `cloud_digests` rows into `sessions` rows and set `ingested_at`, in one
    transaction. Idempotent: an ingested row is not read again, and the sessions upsert keeps
    `scanned_at` and the scan keys."""
    from psycopg.rows import dict_row

    report = CloudReport()
    with store.connect(db_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_CLOUD_PENDING_SQL)
            recs = cur.fetchall()
        for rec in recs:
            row = cloud_row(rec)
            if row is None:
                report.bad += 1
                continue
            got = conn.execute(_UPSERT_SQL, (
                row.session_id, row.tool, row.kind, row.repo, row.project_dir, row.started_at, row.ended_at,
                row.transcript_bytes, Jsonb(row.skills), Jsonb(row.instructions), Jsonb(row.meta),
            )).fetchone()
            if got is not None:
                if got[0]:
                    report.new += 1
                else:
                    report.updated += 1
            conn.execute("update cloud_digests set ingested_at = now() where source_key = %s", (row.session_id,))
            report.rows.append(row)
    return report


def ingest(root: Path | None, *, days: int = DEFAULT_DAYS, now: datetime | None = None,
           db_url: str | None = None, codex: Path | None = None,
           tools: tuple[str, ...] = TOOLS) -> IngestReport:
    rows, skipped = collect(root, days=days, now=now, codex=codex, tools=tools)
    report = IngestReport(files=len(rows) + skipped, skipped=skipped)
    report.new, report.updated = upsert(rows, db_url=db_url)
    if "claude" in tools:  # cloud routine sessions are Claude Code sessions
        cloud = ingest_cloud(db_url=db_url)
        rows = rows + cloud.rows
        report.files += len(cloud.rows)
        report.new += cloud.new
        report.updated += cloud.updated
        report.cloud = len(cloud.rows)
        report.cloud_bad = cloud.bad
    for r in rows:
        report.kinds[r.kind] = report.kinds.get(r.kind, 0) + 1
        report.tools[r.tool] = report.tools.get(r.tool, 0) + 1
        report.team += r.team
    return report
