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
team repo's files. With the owner, `meta.remote_owner` and `meta.remote_host` (the remote's
host, lowercase) are stored; integrations/egress.py reads them for export ownership.

Tokens (session_tokens.py, docs/sessions-live.md): every row also gets its token totals,
per-model breakdown, turns and last activity; a parent's `meta.subagent_tokens` sums its
subagents. Pings the hook spooled (session_live.py) are flushed first.

Resumed sessions (session_resume.py, docs/sessions-live.md): a resumed transcript copies the
earlier transcript's lines. Its row counts only its own lines (tokens, turns, time, prompts, tool
calls, skills, commits, started_at / ended_at) and links the earlier row in `meta.resumed_from`.
A pure copy gets no row.

Commits and summaries (docs/sessions-live.md): `outcome_evidence.commits` from the git commit
output in the transcript (session_commits.py); the last step writes `summary` for finished
sessions with a cheap model (session_summary.py, off unless AMEM_SUMMARY_LIVE=1, time-boxed,
never fails the ingest).

Idempotent: one upsert per session_id. A re-ingest refreshes the facts and keeps
`scanned_at` and the scan keys in `meta`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from agent_memory import codex_sessions, config, session_commits, session_resume, session_tokens, store

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


_OWNER_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([^/:]+)(?::\d+)?[:/]+([\w.-]+)/[\w.-]+?(?:\.git)?/?$")
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def remote_owner(url: str | None) -> str | None:
    """`git@github-x:owner/repo.git`, `https://github.com/owner/repo` -> owner. None for a
    local path or anything else."""
    if not url:
        return None
    m = _OWNER_RE.match(url.strip())
    return m.group(2) if m else None


def remote_host(url: str | None) -> str | None:
    """The host of a remote whose owner remote_owner reads, lowercase, without user or port:
    `git@github.com:owner/repo.git` -> github.com, `https://u@gitlab.example.com:8443/o/r` ->
    gitlab.example.com, an ssh alias `git@github-x:owner/repo` -> github-x (an alias is not
    resolved). None when no owner is read or the host is not a plain host name (Mission 004C:
    egress.py counts a session as own only on a github.com remote)."""
    if not url:
        return None
    m = _OWNER_RE.match(url.strip())
    host = m.group(1).lower() if m else ""
    return host if _HOST_RE.fullmatch(host) else None


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
    """`team`, plus `remote_owner`, `remote_host` and `team_reason` when known. `url` (the
    remote a Codex transcript recorded) wins over the repo's git config.

    The host is stored with the owner (Mission 004C): the owner alone cannot tell
    `github.com/vilmerfrost/x` from `gitlab.example.com/vilmerfrost/x`, and egress.py counts a
    session as own only on a github.com remote. A row stored before the host was kept has no
    `remote_host` (egress: private) until a re-ingest or `amem sessions hosts` (session_hosts.py).

    No owner and no team profile -> {} (unknown): a re-ingest after the worktree was removed
    keeps what an earlier ingest stored, so a team flag is never lost to a cleanup."""
    url = url or git_remote_url(cwd)
    owner = remote_owner(url)
    if profile_mode(repo) == "team":
        out: dict[str, Any] = {"team": True, "team_reason": "profile"}
    elif owner:
        team = owner.lower() not in personal_owners()
        out = {"team": team, "team_reason": "remote-owner"} if team else {"team": False}
    else:
        return {}
    if owner:
        out["remote_owner"] = owner
        host = remote_host(url)
        if host:
            out["remote_host"] = host
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
    """The repo a cwd belongs to: `<repo>/.claude/worktrees/<name>` (Claude Code's own worktrees) -> repo,
    `<...>/worktrees/<repo>/<name>` -> repo, else the basename. None for no cwd or the home folder."""
    if not cwd:
        return None
    path = Path(cwd)
    parts = path.parts
    for i in range(len(parts) - 2, 1, -1):          # as project_of: before 2026-10-24 this gave `agent-<id>`
        if parts[i] == "worktrees" and parts[i - 1] == ".claude":
            return parts[i - 2]
    if "worktrees" in parts:
        i = len(parts) - 1 - parts[::-1].index("worktrees")
        if i + 1 < len(parts):
            return parts[i + 1]
    if path == Path.home() or path.name in ("", "/"):
        return None
    return path.name


# ---------------------------------------------------------------- project and category
# docs/sessions-live.md, "Project and category".

CATEGORIES = KINDS   # routine | chat | parent | worker | eval | headless: the session kind
UNKNOWN_PROJECT = "unknown"
_EVAL_PATH_RE = re.compile(r"/\.cache/(?:agent|skill)-evals(?:/|$)|/\.cache/sbx|(?:^|/)eval-jo-[^/]*(?:/|$)"
                           r"|(?:^|/)[^/]*junk-orchestra[^/]*-eval[^/]*(?:/|$)|(?:^|/)eval-[^/]*junk-orchestra")
_SCRATCH_PATH_RE = re.compile(r"/scratch-workspaces/|/Library/Application Support/Claude/"
                              r"|/Documents/Codex/\d{4}-\d{2}-\d{2}(?:/|$)|(?:^|/)scratchpad(?:/|$)"
                              r"|^/(?:private/)?tmp/claude-\d+/")
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def category_of(kind: str) -> str:
    return kind if kind in CATEGORIES else "chat"


def _git_root(path: Path) -> Path | None:
    """The nearest folder (path or a parent, below the home folder) holding `.git`."""
    home = Path.home()
    for d in [path, *path.parents]:
        if d == home or d.parent == d:
            return None
        try:
            if (d / ".git").exists():
                return d
        except OSError:
            return None
    return None


def project_of(cwd: str, *, kind: str, routine: str | None = None, scratch_only: bool = False,
               project_dir: str = "", repo: str | None = None) -> str:
    """Where the work happened, normalized. First match wins:
    routine -> `routine:<name>`; eval clone -> `evals`; Desktop / Codex scratch folder ->
    `scratch`; a worktree (`<repo>/.claude/worktrees/<x>`, `~/worktrees/<repo>/<x>`, a git
    worktree checkout) -> its main repo; a git repo -> its folder name; the home folder ->
    `home`; else the folder's basename; never empty (`unknown`)."""
    path = cwd or ""
    if kind == "routine":
        name = routine or repo or (Path(path).name if path else "") or UNKNOWN_PROJECT
        return "routine:" + (_SLUG_RE.sub("-", name.strip().lower()).strip("-._") or UNKNOWN_PROJECT)
    if kind == "eval" or _EVAL_PATH_RE.search(path) or SKIP_PROJECT_RE.search(project_dir):
        return "evals"
    if scratch_only or _SCRATCH_PATH_RE.search(path) or "-scratch-workspaces-" in project_dir:
        return "scratch"
    if not path:
        return repo or UNKNOWN_PROJECT
    p = Path(path)
    parts = p.parts
    for i in range(len(parts) - 2, 0, -1):
        if parts[i] == "worktrees" and parts[i - 1] == ".claude" and i >= 2:
            return parts[i - 2]
    if "worktrees" in parts:
        i = len(parts) - 1 - parts[::-1].index("worktrees")
        if i + 1 < len(parts) and parts[i - 1] != ".codex":
            return parts[i + 1]
    main = main_checkout(p)
    if main is not None:
        return main.name
    root = _git_root(p)
    if root is not None:
        main = main_checkout(root)
        return (main or root).name
    if p == Path.home():
        return "home"
    return p.name or UNKNOWN_PROJECT


def place(row: SessionRow) -> tuple[str, str]:
    """(category, project) of a row."""
    routine = row.meta.get("routine") if isinstance(row.meta.get("routine"), str) else None
    return category_of(row.kind), project_of(row.cwd, kind=row.kind, routine=routine,
                                             scratch_only=row.meta.get("scratch_only") is True,
                                             project_dir=row.project_dir, repo=row.repo)


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
    tokens: session_tokens.Summary | None = None  # None: not measured (the token columns stay as they are)
    title: str | None = None   # the session's name; None: not known (the stored title stays)
    profile: dict[str, Any] | None = None   # Summary.profile(): model, effort, time (None: not known)
    signals: dict[str, int] | None = None   # outcome signals (None: not known, the stored outcome stays)
    end: str | None = None
    outcome: str | None = None   # the stored outcome (a row read back from the database), else None
    commits: list[dict[str, Any]] | None = None   # session_commits: {sha, message, repo, branch}; None: not read
    copy_only: bool = False   # a pure copy of an earlier transcript (session_resume): no row is stored
    timing_spans: list[Any] = field(default_factory=list)  # sanitized operation facts, never transcript text
    # docs/data-use/instruction-sources.md: the stored `sessions.instruction_sources` JSON (a row read
    # back from the database) and the entries captured at ingest (instruction_sources.Entry, with texts;
    # None: not captured, the stored value stays).
    instruction_sources: list[dict[str, Any]] | None = None
    instruction_entries: list[Any] | None = None

    def with_summary(self, s: session_tokens.Summary) -> SessionRow:
        """Title, profile and outcome signals from a full read."""
        self.title = self.title or s.title
        self.profile = s.profile()
        self.signals, self.end = dict(s.signals), s.end
        return self

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
    """One transcript file to a row (no database). A pure copy (session_resume) gets
    `copy_only` set: `collect` stores no row for it."""
    events = load_events(path)
    facts = scan_facts(events)
    subagent = parent_session is not None
    # Count only this session's lines: a resumed session's transcript starts with a copy of the
    # earlier transcript's lines (session_resume: the same uuids, or, older shape, the earlier
    # sessionId). A subagent's lines carry the parent's id. Kind, cwd and routine still read
    # every line (the chain is one conversation); counts, skills and times read only own lines.
    own = parent_session if subagent else path.stem
    resume = session_resume.resume_of(path)
    own_events = resume.own(events, own)
    mine = scan_facts(own_events) if len(own_events) != len(events) else facts
    cwd, scratch_only = work_cwd(facts.cwds, project_dir)
    kind = classify(facts, project_dir=project_dir, cwd=cwd, subagent=subagent)
    meta: dict[str, Any] = {
        "cwd": cwd or None,
        "cwds": facts.cwds,  # every cwd the session visited (skills.MATCH_SQL)
        "scratch_only": scratch_only,
        "entrypoints": sorted(facts.entrypoints),
        "prompts": mine.prompts,
        "tool_calls": mine.tool_calls,
        "spawned_workers": mine.spawned_workers,
    }
    if resume.resumed_from:
        meta["resumed_from"] = resume.resumed_from
    if mine is not facts:
        meta["copied_lines"] = len(events) - len(own_events)
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
    tokens = session_tokens.claude_summary(events, own_session=own, skip_uuids=resume.copied)
    start, end = (mine.start, mine.end) if mine.start is not None else (facts.start, facts.end)
    row = SessionRow(sid, kind, repo, project_dir, start, end, size,
                     mine.skills, [], meta, path, tokens=tokens).with_summary(tokens)
    row.copy_only = resume.pure_copy(events, own)
    from agent_memory.execution_timing import claude_spans
    row.timing_spans = claude_spans(own_events, sid)
    row.commits = confirm_commits(row, session_commits.claude_commits(own_events, cwd=cwd, own_session=own,
                                                                      fallback=commit_fallback(row)))
    return _with_instructions(row, events)


def own_events(path: Path, events: list[dict[str, Any]], session_id: str,
               parent_session: str | None = None) -> list[dict[str, Any]]:
    """A local Claude Code transcript's own lines (session_row's rule): no line copied from an
    earlier transcript of its resume chain. For digests and commit re-reads of stored rows."""
    return session_resume.resume_of(path).own(events, parent_session or session_id)


def commit_fallback(row: SessionRow) -> session_commits.Fallback:
    """The git-log fallback of a local transcript: its `commit` signal count and time window."""
    return session_commits.Fallback(int((row.signals or {}).get("commit") or 0), row.started_at,
                                    row.ended_at or row.started_at)


def confirm_commits(row: SessionRow, commits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`commits`, and a `commit` signal of at least 1 when they are not empty: a commit with a
    sha the outcome signals could not prove from the call's own result (a background commit
    the git-log fallback found, a `git log --oneline` line with the commit's subject)."""
    if commits and row.signals is not None and not row.signals.get("commit"):
        row.signals["commit"] = 1
    return commits


def _with_instructions(row: SessionRow, events: list[dict[str, Any]] | None = None) -> SessionRow:
    """`instructions` (source names with a file today) and the captured entries with authority and
    version (docs/data-use/instruction-sources.md). A capture failure never stops ingest: the entries
    stay None and the stored ones are kept."""
    from agent_memory import deviations  # local import: deviations imports this module
    from agent_memory import instruction_sources

    row.instructions = [src.name for src in deviations.instruction_sources(row)]
    try:
        row.instruction_entries = instruction_sources.capture(row, events, instruction_sources.resolver())
    except Exception as exc:  # noqa: BLE001 - best effort: the names above are still stored
        row.instruction_entries = None
        print(f"amem: instruction sources of {row.session_id} not captured: {type(exc).__name__}: {exc}",
              file=sys.stderr)
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


CODEX_INDEX = "session_index.jsonl"


def codex_titles(home: Path) -> dict[str, str]:
    """Thread id -> title from `<codex home>/session_index.jsonl` lines
    `{id, thread_name, updated_at}`. The line with the latest `updated_at` wins (the file
    order breaks ties: a later line wins). Titles redacted and capped."""
    best: dict[str, tuple[datetime | None, str]] = {}
    for rec in load_events(home / CODEX_INDEX):
        tid, title = rec.get("id"), session_tokens.clean_title(rec.get("thread_name"))
        if not isinstance(tid, str) or not tid or not title:
            continue
        ts = parse_ts(rec.get("updated_at"))
        old = best.get(tid)
        if old is None or old[0] is None or (ts is not None and ts >= old[0]):
            best[tid] = (ts, title)
    return {tid: title for tid, (_ts, title) in best.items()}


def codex_row(path: Path, rel_dir: str, titles: dict[str, str] | None = None) -> SessionRow:
    """One Codex rollout file to a row (no database). `rel_dir`: its folder relative to the
    Codex home. `titles`: codex_titles() of that home."""
    events = load_events(path)
    f = codex_sessions.scan_facts(events, path.stem)
    cwd, scratch_only = work_cwd(f.cwds, "")
    kind = classify_codex(f, cwd)
    meta: dict[str, Any] = {
        "cwd": cwd or None,
        "cwds": f.cwds,  # every cwd the session visited (skills.MATCH_SQL)
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
    tokens = session_tokens.codex_summary(events)
    tokens.turns = f.prompts
    title = (titles or {}).get(f.thread_id or path.stem)
    row = SessionRow(sid, kind, repo, rel_dir, f.start, f.end, size, f.skills, [], meta, path, tool="codex",
                     tokens=tokens, title=title).with_summary(tokens)
    from agent_memory.execution_timing import codex_spans
    row.timing_spans = codex_spans(events, sid)
    durations = [s.elapsed_ms for s in row.timing_spans if s.category != "model_output" and s.elapsed_ms is not None]
    if durations:
        tokens.tool_ms = sum(durations)
        row.profile = tokens.profile()
    row.meta["timing_coverage"] = {"method": "codex_items_v1", "tool_spans": len(durations),
                                   "api": "requires_otel", "model_output_is_api": False}
    row.commits = confirm_commits(row, session_commits.codex_commits(events, cwd=cwd, fallback=commit_fallback(row)))
    return _with_instructions(row, events)


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
    skipped: int = 0  # empty transcripts, pure copies of a resumed one, or ones that ended before the window
    kinds: dict[str, int] = field(default_factory=dict)
    tools: dict[str, int] = field(default_factory=dict)
    team: int = 0
    cloud: int = 0      # cloud routine sessions ingested from cloud_digests in this run
    cloud_bad: int = 0  # cloud_digests rows that could not be ingested
    spool: int = 0      # hook pings flushed from the local spool file (docs/sessions-live.md)
    outcomes: int = 0   # stored rows whose outcome changed on the recompute pass (refresh_stored)
    places: int = 0     # stored rows whose null category / project was filled (refresh_stored)
    summaries: int = 0          # summaries written (session_summary; off unless AMEM_SUMMARY_LIVE=1)
    summary_failed: int = 0     # model calls that failed or answered junk
    summary_left: int = 0       # rows not looked at: the time budget ran out (the next run picks them up)
    summary_empty: int = 0      # rows skipped: the transcript digest is too short to describe
    summary_error: str | None = None   # the summary step itself failed (ingest still succeeds)
    summary_live: bool = False  # the summary step ran (AMEM_SUMMARY_LIVE=1)

    def line(self) -> str:
        kinds = " ".join(f"{k}={self.kinds.get(k, 0)}" for k in KINDS)
        tools = " ".join(f"{t}={self.tools.get(t, 0)}" for t in TOOLS)
        cloud = f" cloud={self.cloud}" if self.cloud or self.cloud_bad else ""
        bad = f" cloud_bad={self.cloud_bad}" if self.cloud_bad else ""
        spool = f" spool={self.spool}" if self.spool else ""
        fixed = (f" outcomes={self.outcomes}" if self.outcomes else "") + (f" places={self.places}" if self.places else "")
        # The summary step's state, always: `summary=off` (AMEM_SUMMARY_LIVE is not 1) or
        # `summaries=N`, even 0, so a launchd log tells "off" from "nothing eligible".
        summ = " summary=off" if not self.summary_live else f" summaries={self.summaries}"
        summ += "".join(f" {k}={v}" for k, v in (("summary_failed", self.summary_failed),
                                                 ("summary_left", self.summary_left),
                                                 ("summary_empty", self.summary_empty)) if v)
        summ += f" summary_error={self.summary_error}" if self.summary_error else ""
        return (f"sessions: {self.files} files, {self.new} new, {self.updated} updated, "
                f"{self.skipped} skipped ({kinds}) tools({tools}) team={self.team}{cloud}{bad}{spool}{fixed}{summ}")


def collect(root: Path | None, *, days: int = DEFAULT_DAYS, now: datetime | None = None,
            codex: Path | None = None, tools: tuple[str, ...] = TOOLS) -> tuple[list[SessionRow], int]:
    """Rows for every transcript in the window, plus the number skipped. `root`: the Claude
    projects folder; `codex`: the Codex home (default `codex_home()`)."""
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    from agent_memory import instruction_sources

    found: list[SessionRow] = []
    with instruction_sources.batch():   # one set of git caches for every row of this run
        if "claude" in tools and root is not None:
            found += [session_row(path, project, parent_session=parent)
                      for path, project, parent in transcripts(root, since)]
        if "codex" in tools:
            home = codex_home(codex)
            titles = codex_titles(home)
            found += [codex_row(path, rel, titles) for path, rel in codex_transcripts(home, since)]
    rows: list[SessionRow] = []
    skipped = 0
    for row in found:
        if row.copy_only or row.started_at is None or (row.ended_at is not None and row.ended_at < since):
            skipped += 1
            continue
        rows.append(row)
    spread_team(rows)
    rollup_subagents(rows)
    return rows, skipped


def rollup_subagents(rows: list[SessionRow]) -> None:
    """`meta.subagent_tokens` on each parent row: the summed tokens of its subagents
    ({input, output, cache_read, cache_creation, sessions}). Claude Code: every
    `<session>/subagents/agent-*.jsonl` next to the parent transcript (a subagent file not in
    this ingest is read for its tokens only; a pure copy, session_resume, adds nothing). Codex: the subagent rows of this ingest whose
    `meta.parent_session` is the parent. The subagents keep their own rows: a sum over all
    rows never adds this field."""
    by_id = {r.session_id: r for r in rows}
    sums: dict[str, tuple[session_tokens.Tokens, int]] = {}

    def add(parent: str, t: session_tokens.Tokens) -> None:
        total, n = sums.get(parent, (session_tokens.Tokens(), 0))
        total.add(t)
        sums[parent] = (total, n + 1)

    for r in rows:
        if r.tool == "claude" and r.path is not None and "parent_session" not in r.meta:
            sub_dir = r.path.parent / r.path.stem / "subagents"
            for f in sorted(sub_dir.glob("agent-*.jsonl")) if sub_dir.is_dir() else []:
                got = by_id.get(f"{r.session_id}/{f.stem}")
                if got is not None and got.tokens is not None:
                    add(r.session_id, got.tokens.tokens)
                    continue
                events = load_events(f)
                resume = session_resume.resume_of(f)
                if resume.pure_copy(events, r.session_id):
                    continue   # a copied subagent file: its tokens are the earlier session's
                add(r.session_id, session_tokens.claude_summary(events, own_session=r.session_id,
                                                                skip_uuids=resume.copied).tokens)
        elif r.tool == "codex" and r.meta.get("parent_session") and r.tokens is not None:
            add(str(r.meta["parent_session"]), r.tokens.tokens)
    for sid, (total, n) in sums.items():
        parent = by_id.get(sid)
        if parent is not None:
            parent.meta["subagent_tokens"] = {**total.as_dict(), "sessions": n}


# The token columns: a full read of the transcript is the truth, so ingest overwrites them
# (a row without tokens, `SessionRow.tokens is None`, keeps what is stored).
# `tokens_updated_at` moves only when a token column changed. `last_activity_at` never goes back
# (a hook ping may have stored a newer time).
_TOKEN_COLS = ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_creation", "models", "turns")

# Profile columns (docs/sessions-live.md): a full read wins when it knows the value; a None
# keeps what is stored. category and project are always known and always overwritten.
PROFILE_COLS = ("model", "model_last", "effort", "effort_last", "efforts", "duration_seconds", "active_seconds",
                "api_seconds", "tool_seconds", "lines_added", "lines_removed")
_KEEP_COLS = ("title", *PROFILE_COLS, "outcome", "outcome_evidence")
_ALL_COLS = ("session_id", "tool", "kind", "repo", "project_dir", "started_at", "ended_at", "transcript_bytes",
             "skills", "instructions", "meta", *_TOKEN_COLS, "last_activity_at", "category", "project", "env",
             "experiment_id", *_KEEP_COLS, "instruction_sources")
_FACT_COLS = ("tool", "kind", "repo", "project_dir", "started_at", "ended_at", "transcript_bytes", "skills",
              "instructions", "category", "project", "env", "experiment_id")
_FACT_ASSIGNMENTS = [
    (f"{c} = case when sessions.meta->>'rsi_native_observation'='true' then sessions.{c} else excluded.{c} end"
     if c in ("env", "experiment_id") else f"{c} = excluded.{c}") for c in _FACT_COLS]
_TOKENS_DIFFER = (f"({', '.join('sessions.' + c for c in _TOKEN_COLS)}) is distinct from "
                  f"({', '.join('excluded.' + c for c in _TOKEN_COLS)})")
_KEPT_DIFFER = (f"({', '.join(f'coalesce(excluded.{c}, sessions.{c})' for c in _KEEP_COLS)}) is distinct from "
                f"({', '.join('sessions.' + c for c in _KEEP_COLS)})")

_UPSERT_SQL = f"""
insert into sessions ({', '.join(_ALL_COLS)}, tokens_updated_at, title_updated_at, outcome_at)
values ({', '.join(f'%({c})s' for c in _ALL_COLS)},
        case when %(tokens_input)s::bigint is null then null else now() end,
        case when %(title)s::text is null then null else now() end,
        case when %(outcome)s::text is null then null else now() end)
on conflict (session_id) do update
    set {', '.join(_FACT_ASSIGNMENTS)},
        meta = sessions.meta || excluded.meta || case
          when sessions.meta->>'rsi_native_observation'='true' and excluded.transcript_bytes>0
          then jsonb_build_object('transcript_available',true) else '{{}}'::jsonb end,
        {', '.join(f'{c} = coalesce(excluded.{c}, sessions.{c})' for c in (*_TOKEN_COLS, *_KEEP_COLS))},
        instruction_sources = coalesce(excluded.instruction_sources, sessions.instruction_sources),
        last_activity_at = greatest(sessions.last_activity_at, excluded.last_activity_at),
        tokens_updated_at = case when excluded.tokens_input is not null and {_TOKENS_DIFFER}
                                 then now() else sessions.tokens_updated_at end,
        title_updated_at = case when excluded.title is not null and excluded.title is distinct from sessions.title
                                then now() else sessions.title_updated_at end,
        outcome_at = case when excluded.outcome is not null and excluded.outcome is distinct from sessions.outcome
                          then now() else sessions.outcome_at end
    where ({', '.join('sessions.' + c for c in _FACT_COLS)})
          is distinct from ({', '.join('excluded.' + c for c in _FACT_COLS)})
       or not (sessions.meta @> excluded.meta)
       or (excluded.tokens_input is not null and {_TOKENS_DIFFER})
       or {_KEPT_DIFFER}
       or (excluded.instruction_sources is not null
           and excluded.instruction_sources is distinct from sessions.instruction_sources)
       or excluded.last_activity_at > coalesce(sessions.last_activity_at, '-infinity'::timestamptz)
returning (xmax = 0) as inserted
"""


def token_values(t: session_tokens.Summary | None) -> tuple[Any, ...]:
    """(input, output, cache_read, cache_creation, models, turns) for the token columns, all None
    when not measured."""
    if t is None:
        return (None,) * len(_TOKEN_COLS)
    models = {k: dict(v) for k, v in sorted(t.models.items())}
    return (*t.tokens.as_list(), Jsonb(models), t.turns)


def _last_activity(r: SessionRow) -> datetime | None:
    last = (r.tokens.last_at if r.tokens is not None else None) or r.ended_at
    return last if isinstance(last, datetime) else None


def profile_values(profile: dict[str, Any] | None) -> dict[str, Any]:
    """The profile columns (None when not known); `efforts` as Jsonb."""
    p = profile or {}
    out = {c: p.get(c) for c in PROFILE_COLS}
    out["efforts"] = Jsonb(p["efforts"]) if isinstance(p.get("efforts"), dict) and p["efforts"] else None
    return out


def outcome_values(signals: dict[str, int] | None, end: str | None, last: datetime | None,
                   now: datetime | None, kind: str | None = None,
                   commits: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """`outcome` and `outcome_evidence`. The evidence gets `commits` (session_commits) when the
    session made any; the signal keys and `end` stay as outcome_of builds them."""
    if signals is None:
        return {"outcome": None, "outcome_evidence": None}
    name, evidence = session_tokens.outcome_of(signals, end, last, now, kind)
    if commits:
        evidence["commits"] = commits
    return {"outcome": name, "outcome_evidence": Jsonb(evidence)}


def rsi_sandbox_dir() -> Path:
    """The RSI experiment sandbox root (docs/sandbox.md): RSI_SANDBOX_DIR, default ~/sandbox."""
    return Path(os.environ.get("RSI_SANDBOX_DIR") or Path.home() / "sandbox").expanduser()


_EXPERIMENT_RE = re.compile(r"/experiments/([a-z0-9][a-z0-9-]{2,60})(/|$)")


def env_values(r: SessionRow) -> dict[str, Any]:
    """`env` and `experiment_id` (docs/rsi/spec.md). sandbox: the transcript or a cwd it visited is under the
    RSI sandbox root (or ingest was told so: meta.env); eval: kind eval; cloud: a cloud: id; else prod.
    Sandbox sessions never count in production evaluations (deviation scan, scoreboard, skill scores)."""
    root = str(rsi_sandbox_dir())
    places = [str(r.path or "")] + [c for c in (r.meta.get("cwds") or []) if isinstance(c, str)]
    places.append(str(r.meta.get("cwd") or ""))
    sandbox = r.meta.get("env") == "sandbox" or any(p == root or p.startswith(root + "/") for p in places if p)
    env = "sandbox" if sandbox else "eval" if r.kind == "eval" else "cloud" if r.session_id.startswith("cloud:") else "prod"
    exp = r.meta.get("experiment_id") if isinstance(r.meta.get("experiment_id"), str) else None
    if sandbox and not exp:
        for p in places:
            m = _EXPERIMENT_RE.search(p) if p.startswith(root) else None
            if m:
                exp = m.group(1)
                break
    return {"env": env, "experiment_id": exp if sandbox else None}


def _params(r: SessionRow, now: datetime | None = None,
            sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tv = token_values(r.tokens)
    last = _last_activity(r)
    category, project = place(r)
    return {"session_id": r.session_id, "tool": r.tool, "kind": r.kind, "repo": r.repo,
            "project_dir": r.project_dir, "started_at": r.started_at, "ended_at": r.ended_at,
            "transcript_bytes": r.transcript_bytes, "skills": Jsonb(r.skills),
            "instructions": Jsonb(r.instructions), "meta": Jsonb(r.meta), **dict(zip(_TOKEN_COLS, tv)),
            "last_activity_at": last, "category": category, "project": project, "title": r.title,
            "instruction_sources": Jsonb(sources) if sources is not None else None,
            **env_values(r),
            **profile_values(r.profile), **outcome_values(r.signals, r.end, last, now, r.kind, r.commits)}


def instruction_params(conn: Any, rows: list[SessionRow]) -> dict[str, tuple[list[dict[str, Any]], list[Any]]]:
    """session id -> (entries to store, texts to store) for the rows with captured entries: the stored
    entries merged with the new ones (instruction_sources.merge: a stronger authority wins, a tie keeps the
    earliest capture), so a replay changes nothing."""
    from agent_memory import instruction_sources

    ids = [r.session_id for r in rows if r.instruction_entries is not None]
    if not ids:
        return {}
    stored = dict(conn.execute("select session_id, instruction_sources from sessions where session_id = any(%s)",
                               (ids,)).fetchall())
    out: dict[str, tuple[list[dict[str, Any]], list[Any]]] = {}
    for r in rows:
        if r.instruction_entries is None:
            continue
        kept = instruction_sources.merge(stored.get(r.session_id), r.instruction_entries)
        out[r.session_id] = (kept, instruction_sources.texts_to_store(r.instruction_entries, kept))
    return out


def upsert(rows: list[SessionRow], *, db_url: str | None = None, now: datetime | None = None) -> tuple[int, int]:
    """(new, updated). Unchanged rows are not touched. One transaction."""
    from agent_memory import instruction_sources

    new = updated = 0
    with store.connect(db_url) as conn:
        sources = instruction_params(conn, rows)
        for r in rows:
            kept, texts = sources.get(r.session_id, (None, []))
            got = conn.execute(_UPSERT_SQL, _params(r, now, kept)).fetchone()
            instruction_sources.store_texts(conn, texts)
            if got is None:
                continue
            if got[0]:
                new += 1
            else:
                updated += 1
        from agent_memory import timing_store
        timing_store.upsert_conn(conn, (span for r in rows for span in r.timing_spans))
        timing_store.refresh_profiles(conn, (r.session_id for r in rows if r.tool=='codex'))
        # A native client transcript may arrive after its replay sidecar. The
        # join uses only the captured client id and sandbox experiment scope.
        from agent_memory import attempt_links
        attempt_links.reconcile_native_sessions(conn)
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
    if name == CLOUD_PROMPT:
        return f"routine:{routine}" if routine else None   # a chat has no routine prompt
    return f"cloud-file:{name}"


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


CLOUD_KINDS = ("routine", "chat")


def cloud_row(rec: dict[str, Any]) -> SessionRow | None:
    """One `cloud_digests` row to a `sessions` row (no database). Kind `routine` (a routine's
    own last step) or `chat` (the Stop / SessionEnd hook, docs/cloud.md "Cloud chats"). None
    for a row with a bad routine name or session id (it stays un-ingested and is reported)."""
    key, routine = str(rec.get("source_key") or ""), str(rec.get("routine") or "")
    kind = rec.get("kind") if rec.get("kind") in CLOUD_KINDS else "routine"
    if kind == "chat" and routine:
        kind = "routine"   # a routine name always makes it a routine session
    if not key.startswith(CLOUD_PREFIX) or (kind == "routine" and not _ROUTINE_NAME_RE.fullmatch(routine)):
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
        "prompts": _count(src.get("prompts")),
        "tool_calls": _count(src.get("tool_calls")),
        "script_version": str(src.get("script_version") or "") or None,
        "digest_sha": rec.get("digest_sha"),
    }
    if kind == "routine":
        meta["routine"] = routine
    size = _count(src.get("transcript_bytes")) or len(rec.get("digest") or "")
    row = SessionRow(key, kind, repo, CLOUD_PROJECT_DIR, rec.get("started_at"), rec.get("ended_at"),
                     size, skills, names, meta, tokens=cloud_tokens(src, rec.get("ended_at")),
                     title=session_tokens.clean_title(src.get("title")))
    from agent_memory import instruction_sources   # local import: it imports this module

    row.instruction_entries = instruction_sources.cloud_entries(routine, rec.get("instructions"))
    prof = src.get("profile") if isinstance(src.get("profile"), dict) else None
    if prof is not None:
        row.profile = cloud_profile(prof)
    out = src.get("outcome") if isinstance(src.get("outcome"), dict) else None
    if out is not None and isinstance(out.get("signals"), dict):
        row.signals = {str(k): v for k, v in out["signals"].items()
                       if isinstance(v, int) and not isinstance(v, bool) and v > 0 and re.fullmatch(r"[a-z_]{1,30}", str(k))}
        row.end = out.get("end") if out.get("end") in ("reply", "user", "tool", "result", "error", "interrupt") else None
    return row


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def cloud_profile(p: dict[str, Any]) -> dict[str, Any]:
    """The profile a digest carries (script_version 3+), validated: labels and numbers only."""
    def model(v: Any) -> str | None:
        return v.strip() if session_tokens.is_model(v) and len(v) <= 100 else None

    efforts = p.get("efforts") if isinstance(p.get("efforts"), dict) else {}
    lines = {k: (int(v) if (v := _num(p.get(k))) is not None else None) for k in ("lines_added", "lines_removed")}
    return {"model": model(p.get("model")), "model_last": model(p.get("model_last")),
            "effort": session_tokens.effort_of(p.get("effort")),
            "effort_last": session_tokens.effort_of(p.get("effort_last")),
            "efforts": {e: int(n) for k, n in efforts.items()
                        if (e := session_tokens.effort_of(k)) and _num(n) is not None},
            **{k: _num(p.get(k)) for k in ("duration_seconds", "active_seconds", "api_seconds", "tool_seconds")},
            **lines}


def cloud_tokens(src: dict[str, Any], ended_at: Any) -> session_tokens.Summary | None:
    """The token totals a digest carries in `meta.tokens` (script_version 2+), else None (an
    older digest: the token columns stay empty)."""
    raw = src.get("tokens")
    if not isinstance(raw, dict):
        return None
    t = session_tokens.Summary(tokens=session_tokens.Tokens.of(raw), turns=_count(src.get("prompts")))
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    for name, v in models.items():
        if isinstance(v, dict) and isinstance(name, str) and 0 < len(name) <= 100:
            t.models[name] = {**session_tokens.Tokens.of(v).as_dict(), "calls": _count(v.get("calls"))}
    t.last_at = ended_at if isinstance(ended_at, datetime) else None
    return t


_CLOUD_PENDING_SQL = """
select source_key, kind, routine, session_id, started_at, ended_at, digest, digest_sha, instructions, skills, meta
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


def ingest_cloud(*, db_url: str | None = None, now: datetime | None = None) -> CloudReport:
    """Turn new or updated `cloud_digests` rows (ingested_at null: a hook POST resets it) into
    `sessions` rows and set `ingested_at`, in one transaction. An updated row refreshes the
    existing sessions row (tokens, times, profile, outcome). A session stored as a routine is
    never turned into a chat. Idempotent; the sessions upsert keeps `scanned_at` and the scan keys."""
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
            if row.kind == "chat":
                stored = conn.execute("select kind, meta ->> 'routine', instructions from sessions "
                                      "where session_id = %s", (row.session_id,)).fetchone()
                if stored is not None and stored[0] == "routine" and stored[1]:
                    row.kind = "routine"
                    row.meta["routine"] = stored[1]
                    row.instructions = list(stored[2] or [])
            kept, _texts = instruction_params(conn, [row]).get(row.session_id, (None, []))
            got = conn.execute(_UPSERT_SQL, _params(row, now, kept)).fetchone()
            if got is not None:
                if got[0]:
                    report.new += 1
                else:
                    report.updated += 1
            conn.execute("update cloud_digests set ingested_at = now() where source_key = %s", (row.session_id,))
            report.rows.append(row)
    return report


# ---------------------------------------------------------------- stored rows
# Every ingest also fixes stored rows outside its window (docs/sessions-live.md, "Outcome"):
# the outcome is recomputed from the stored evidence (signal names and counts plus `end`) with
# today's rules and time, and a null category / project is filled from the stored kind and cwd.

_STORED_SQL = """
select session_id, kind, repo, project_dir, meta, category, project, outcome, outcome_evidence,
       coalesce(last_activity_at, ended_at) as last_at
from sessions
where outcome_evidence is not null or category is null or project is null
"""


def stored_outcome(evidence: Any, last_at: datetime | None, now: datetime | None,
                   kind: str | None) -> str | None:
    """The outcome of a stored row from its `outcome_evidence`, None when there is none. A
    non-empty `commits` list counts as a `commit` signal (confirm_commits: a hook ping counts
    no background commit, the ingest's commits list does)."""
    if not isinstance(evidence, dict):
        return None
    signals = {str(k): v for k, v in evidence.items()
               if k != "end" and isinstance(v, int) and not isinstance(v, bool)}
    if not signals.get("commit") and isinstance(evidence.get("commits"), list) and evidence["commits"]:
        signals["commit"] = 1
    end = evidence.get("end") if isinstance(evidence.get("end"), str) else None
    return session_tokens.outcome_of(signals, end, last_at, now, kind)[0]


def refresh_stored(*, db_url: str | None = None, now: datetime | None = None) -> tuple[int, int]:
    """(outcomes changed, places filled) over every stored row. Idempotent: a second run
    changes nothing (until the clock moves a session past the active / abandoned limits)."""
    from psycopg.rows import dict_row

    outcomes = places = 0
    with store.connect(db_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_STORED_SQL)
            recs = cur.fetchall()
        for rec in recs:
            name = stored_outcome(rec["outcome_evidence"], rec["last_at"], now, rec["kind"])
            if name is not None and name != rec["outcome"]:
                conn.execute("update sessions set outcome = %s, outcome_at = now() where session_id = %s",
                             (name, rec["session_id"]))
                outcomes += 1
            if rec["category"] is None or rec["project"] is None:
                row = SessionRow(rec["session_id"], rec["kind"], rec["repo"], rec["project_dir"] or "", None, None,
                                 0, [], [], dict(rec["meta"] or {}))
                category, project = place(row)
                conn.execute("update sessions set category = coalesce(category, %s), "
                             "project = coalesce(project, %s) where session_id = %s",
                             (category, project, rec["session_id"]))
                places += 1
    return outcomes, places


def ingest(root: Path | None, *, days: int = DEFAULT_DAYS, now: datetime | None = None,
           db_url: str | None = None, codex: Path | None = None,
           tools: tuple[str, ...] = TOOLS) -> IngestReport:
    from agent_memory import session_live  # local import: session_live imports this module

    rows, skipped = collect(root, days=days, now=now, codex=codex, tools=tools)
    report = IngestReport(files=len(rows) + skipped, skipped=skipped)
    # Pings spooled while the database was unreachable go first: the full read below wins.
    report.spool = session_live.flush_spool(db_url=db_url)
    report.new, report.updated = upsert(rows, db_url=db_url, now=now)
    # Parent command measurements are locally spooled without credentials. Ingest
    # them through this existing authorized writer, after session identities exist.
    from agent_memory import timing_store
    if os.environ.get("AMEM_TIMING_INGEST") == "1":
        timing_store.ingest_spools(db_url=db_url)
    if "claude" in tools:  # cloud sessions (routines and chats) are Claude Code sessions
        cloud = ingest_cloud(db_url=db_url, now=now)
        rows = rows + cloud.rows
        report.files += len(cloud.rows)
        report.new += cloud.new
        report.updated += cloud.updated
        report.cloud = len(cloud.rows)
        report.cloud_bad = cloud.bad
    report.outcomes, report.places = refresh_stored(db_url=db_url, now=now)
    for r in rows:
        report.kinds[r.kind] = report.kinds.get(r.kind, 0) + 1
        report.tools[r.tool] = report.tools.get(r.tool, 0) + 1
        report.team += r.team
    summarize_step(report, days=days, now=now, db_url=db_url)
    return report


def summarize_step(report: IngestReport, *, days: int, now: datetime | None, db_url: str | None) -> None:
    """Summaries of finished sessions active in the window (session_summary), inside the time
    budget. Off unless AMEM_SUMMARY_LIVE=1. Never fails the ingest: an error is one field of
    the report line."""
    from agent_memory import session_summary  # local import: session_summary imports this module

    if not session_summary.live():
        return
    report.summary_live = True
    try:
        got = session_summary.summarize(days=days, now=now, db_url=db_url, budget=session_summary.budget_seconds(),
                                        workers=session_summary.concurrency(), window="active")
    except Exception as exc:  # the step is best effort; the next run tries again
        report.summary_error = type(exc).__name__
        return
    report.summaries, report.summary_failed, report.summary_left = got.done, got.failed, got.left
    report.summary_empty = got.skipped.get("empty", 0)
