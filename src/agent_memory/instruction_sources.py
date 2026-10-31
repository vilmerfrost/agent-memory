"""Instruction sources in force for a session: which files, how sure we are, which version
(docs/data-use/instruction-sources.md; plan section 5, "Instruction sources").

Three steps per session:

1. `candidates(row)`: what was in force by name, resolved to files where a file exists.
   Claude Code: the user CLAUDE.md, the agent definition (plugin agents too), the routine prompt,
   PARENT.md for parents, every skill (user and project skills, plugin skills through
   `<claude home>/plugins/installed_plugins.json`), the repo's CLAUDE.md / .claude/CLAUDE.md /
   CLAUDE.local.md (worktree and main checkout) and AGENTS.md, the same memory files in every
   ancestor directory of the cwd up to (not including) the filesystem root, and the `@import`
   targets of every memory file (depth cap IMPORT_MAX_DEPTH, cycles stop, only text files under the
   allowed roots, never an env file). Codex: the global AGENTS.override.md / AGENTS.md, skills, the
   repo's AGENTS.override.md / AGENTS.md and those between the git root and the cwd.
   A name that resolves to nothing (a skill built into the app, an uninstalled plugin, a missing
   definition) is an explicit unresolved candidate with a reason, never dropped silently.
2. `evidence(events)`: what the transcript proves was loaded. Claude Code: the `instructions`
   attachment (path + content of every memory file loaded, imports included), a `Contents of <path>`
   system-reminder block (older transcripts), the skill bodies (`Base directory for this skill:`
   meta messages and `invoked_skills` attachments). Codex: the injected `# AGENTS.md instructions`
   blocks and `<skill>` tags.
3. `assess`: one Entry per file with an authority:
   observed       the transcript holds the text that was loaded (version = its content sha)
   reconstructed  the text at a verified version: the file is unchanged since the session started
                  (mtime), or git history gives the commit in force at the session start
   declared       named by the session metadata, but no version can be verified (no start time)
   unresolved     no text and no version; `reason` says why (built in, not installed, changed after
                  the session started and not in git, ...)
   A current file hash is never recorded as a historical version: a non-git file changed after the
   session started is unresolved.

Texts of observed and reconstructed entries are kept recoverable in `instruction_texts`
(content-addressed by the raw sha256, stored redacted, capped at TEXT_MAX_BYTES, only from the
allowed roots, never env files). `sessions.instruction_sources` holds the entries as JSON.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_memory import claude_roots, rule_versions
from agent_memory.redact import redact
from agent_memory.sessions_ingest import (
    SessionRow,
    agent_config_dir,
    claude_home,
    codex_home,
    main_checkout,
    text_of,
)

FORMAT = 1
AUTHORITIES = ("observed", "reconstructed", "declared", "unresolved")
RANK = {a: i for i, a in enumerate(AUTHORITIES)}   # lower is stronger
IMPORT_MAX_DEPTH = 5           # Claude Code follows @imports at most five hops deep
TEXT_MAX_BYTES = 100_000       # a longer text keeps its version but is not stored
ENTRIES_MAX = 80               # per session
HISTORY_MAX = 500              # git commits read per file
MEMORY_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
IMPORT_SUFFIXES = (".md", ".markdown", ".txt")
BUILTIN_NAMESPACES = {
    "anthropic-skills": "built into the Claude desktop app (anthropic-skills): no file on disk",
}
_NAME_RE = re.compile(r"[\w.-]+")
_NS_RE = re.compile(r"([\w.-]+):([\w.-]+)")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def home() -> Path:
    """The user's home as the Claude home's parent (tests point AMEM_CLAUDE_HOME at a tmp dir)."""
    return claude_home().parent


def label(path: Path | str) -> str:
    """`~/...` under the home, else the absolute path."""
    p = Path(path)
    try:
        rel = str(p.relative_to(home()))
    except ValueError:
        return str(p)
    return "~" if rel == "." else "~/" + rel


def unlabel(text: str) -> Path | None:
    """A stored `~/...` or absolute path back to a Path; None for a label (bundled:x, ns:skill)."""
    if text.startswith("~/"):
        return home() / text[2:]
    return Path(text) if text.startswith("/") else None


_REALPATHS: ContextVar[dict[str, str] | None] = ContextVar("instruction_sources_realpaths", default=None)


def _real(p: Path) -> str:
    cache = _REALPATHS.get()
    if cache is None:
        return os.path.realpath(p)
    key = os.fspath(p)
    if key not in cache:
        cache[key] = os.path.realpath(p)
    return cache[key]


@contextmanager
def _realpaths() -> Iterator[None]:
    """Hold realpath answers for one candidates() call. Each realpath costs one lstat per path part, and one
    call asks for the same ancestors and memory dirs many times; the files do not move during one call."""
    if _REALPATHS.get() is not None:
        yield   # nested: the outer call's cache already covers it
        return
    token = _REALPATHS.set({})
    try:
        yield
    finally:
        _REALPATHS.reset(token)


def _env_file(p: Path) -> bool:
    return p.name.lower().startswith(".env")


def _valid_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name)) and name not in (".", "..")


# --------------------------------------------------------------------------- candidates


@dataclass
class Candidate:
    name: str                          # source group: claude-md:global, skill:x, skill:p:x, import:~/..., ...
    kind: str                          # user | project | local | ancestor | import | skill | plugin-skill | agent
                                       # | routine | parent | codex-global | codex-project
    path: Path | None = None           # the file in force (None: nothing on disk)
    label: str | None = None           # what to store when there is no file (e.g. anthropic-skills:docs)
    reason: str | None = None          # set: unresolved, never read
    plugin: dict[str, Any] | None = None
    imported_by: Path | None = None
    memory: bool = False               # a memory file: the transcript's loaded list decides about it
    skill: str | None = None           # the skill name as the session names it (match against observed bodies)

    def where(self) -> str:
        return label(self.path) if self.path is not None else (self.label or self.name)


def installed_plugins() -> dict[str, list[dict[str, Any]]]:
    """`<claude home>/plugins/installed_plugins.json` -> {"<plugin>@<marketplace>": [install records]}."""
    path = claude_home() / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return {}
    return {str(k): [r for r in v if isinstance(r, dict)] for k, v in plugins.items() if isinstance(v, list)}


def plugin_install(plugin: str, dirs: list[Path]) -> tuple[str, dict[str, Any]] | None:
    """The install record of `plugin` in force for a session in `dirs`: a user-scope install, or a
    project/local install whose projectPath is one of the session's repo dirs."""
    real_dirs = {_real(d) for d in dirs}
    for key, recs in sorted(installed_plugins().items()):
        if key.split("@", 1)[0] != plugin:
            continue
        for rec in recs:
            scope = rec.get("scope")
            proj = rec.get("projectPath")
            if scope == "user" or (isinstance(proj, str) and _real(Path(proj)) in real_dirs):
                return key, rec
    return None


def _plugin_info(key: str, rec: dict[str, Any]) -> dict[str, Any]:
    info = {"id": key, "version": rec.get("version"), "git_commit_sha": rec.get("gitCommitSha"),
            "installed_at": rec.get("installedAt"), "last_updated": rec.get("lastUpdated"), "scope": rec.get("scope")}
    return {k: str(v) for k, v in info.items() if isinstance(v, str) and v}


def _plugin_file(namespace: str, item: str, sub: str, dirs: list[Path], name: str, kind: str) -> Candidate:
    """A namespaced skill (`sub` skills) or agent (`sub` agents) of a plugin."""
    if namespace in BUILTIN_NAMESPACES:
        return Candidate(name, kind, label=f"{namespace}:{item}", reason=BUILTIN_NAMESPACES[namespace], skill=item)
    got = plugin_install(namespace, dirs)
    if got is None:
        return Candidate(name, kind, label=f"{namespace}:{item}", skill=item,
                         reason=f"plugin {namespace} is not installed now (installed_plugins.json)")
    key, rec = got
    info = _plugin_info(key, rec)
    root = Path(str(rec.get("installPath") or "")).expanduser()
    files = ([root / "skills" / item / "SKILL.md", root / "commands" / f"{item}.md"] if sub == "skills"
             else [root / "agents" / f"{item}.md"])
    for f in files:
        if root.is_absolute() and f.is_file() and not _env_file(f):
            return Candidate(name, kind, f, plugin=info, skill=item)
    return Candidate(name, kind, label=f"{namespace}:{item}", plugin=info, skill=item,
                     reason=f"{item} is not in plugin {key} {info.get('version', '')}".rstrip())


def repo_dirs(row: SessionRow) -> list[Path]:
    """The cwd and, for a git worktree, its main checkout."""
    if not row.cwd:
        return []
    base = Path(row.cwd)
    main = main_checkout(base)
    return [base] if main is None else [base, main]


def _repo_name(row: SessionRow) -> str:
    return row.repo or Path(row.cwd).name


def _git_root(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if (d / ".git").exists():
            return d
        if d.parent == d:
            break
    return None


def _ancestors(start: Path) -> list[Path]:
    """Parents of `start` up to, not including, the filesystem root."""
    return [d for d in start.parents if d.parent != d]


class _Names:
    """Source names by path, so a file has one name however it was found (directly or by @import)."""

    def __init__(self, row: SessionRow, tool: str) -> None:
        self.row, self.tool = row, tool
        self.repo = {_real(d) for d in repo_dirs(row)}
        # The configured homes, and the default ones a transcript names (the same files in production).
        self.user = {_real(claude_home() / "CLAUDE.md"), _real(Path.home() / ".claude" / "CLAUDE.md")}
        self.codex_global = {_real(d / n) for d in (codex_home(), Path.home() / ".codex")
                             for n in ("AGENTS.override.md", "AGENTS.md")}
        self.parent = {_real(agent_config_dir() / "parent" / "PARENT.md"),
                       _real(Path.home() / "agent-config" / "parent" / "PARENT.md")}

    def of(self, path: Path, memory_dir: Path | None = None) -> str:
        real = _real(path)
        if real in self.user:
            return "claude-md:global"
        if real in self.codex_global:
            return "agents-md:global"
        if real in self.parent:
            return "parent-md"
        fname = path.name
        # A listed home's own file: one name for all guarded homes, whose hash differs per thread.
        folder = Path(real).parent
        if fname == "CLAUDE.md" and claude_roots.label_of(folder / "projects") is not None:
            return "claude-md:guarded"
        if fname in ("AGENTS.md", "AGENTS.override.md") and claude_roots.codex_label_of(folder) is not None:
            return "agents-md:guarded"
        if memory_dir is None:
            memory_dir = path.parent.parent if path.parent.name == ".claude" and fname == "CLAUDE.md" else path.parent
        is_agents = fname in ("AGENTS.md", "AGENTS.override.md")
        is_claude = fname in ("CLAUDE.md", "CLAUDE.local.md")
        if is_agents or is_claude:
            prefix = "agents-md" if is_agents else "claude-md"
            if _real(memory_dir) in self.repo:
                return f"{prefix}:{_repo_name(self.row)}"
            if path.parent == memory_dir or (path.parent.name == ".claude" and path.parent.parent == memory_dir):
                return f"{prefix}:{label(memory_dir)}"
        return f"import:{label(path)}"


_FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)
_SPAN_RE = re.compile(r"`[^`\n]*`")
_IMPORT_RE = re.compile(r"(?<![\w@/.`])@((?:~/|\.{1,2}/|/)?[\w.+-][\w./+~-]*)")


def imports_of(text: str) -> list[str]:
    """`@path` references outside code blocks and code spans (Claude Code's import syntax), in order."""
    body = _SPAN_RE.sub(" ", _FENCE_RE.sub(" ", text))
    out: list[str] = []
    for raw in _IMPORT_RE.findall(body):
        raw = raw.rstrip(".,;:)")
        if ("/" in raw or "." in raw) and raw not in out:
            out.append(raw)
    return out


def allowed_roots(row: SessionRow) -> list[Path]:
    """Where an instruction text may be read from by @import and stored from at all."""
    return [claude_home(), codex_home(), agent_config_dir(), *repo_dirs(row)]


def _under(path: Path, roots: list[Path]) -> bool:
    return _under_real(path, [_real(r) for r in roots])


def _under_real(path: Path, real_roots: list[str]) -> bool:
    real = _real(path)
    return any(real == r or real.startswith(r + os.sep) for r in real_roots)


def candidates(row: SessionRow) -> list[Candidate]:
    """Every instruction source in force by name, in reading order. Reads only names and imports."""
    with _realpaths():
        return _candidates(row)


def _candidates(row: SessionRow) -> list[Candidate]:
    from agent_memory.deviations import is_cloud  # local import: deviations imports this module

    if is_cloud(row):
        return []   # a cloud routine ran in a VM: its texts come from cloud_digests (cloud_entries)
    names = _Names(row, row.tool)
    out: list[Candidate] = []
    seen: set[str] = set()
    dirs = repo_dirs(row)
    roots = [_real(r) for r in allowed_roots(row)]   # resolved once per call, not per @import check

    def take(c: Candidate) -> bool:
        if c.path is None:
            out.append(c)
            return True
        if _env_file(c.path) or not c.path.is_file():
            return False
        real = _real(c.path)
        if real in seen:
            return False
        seen.add(real)
        out.append(c)
        return True

    def memory(path: Path, kind: str, memory_dir: Path) -> None:
        if not path.is_file():
            return   # take() would drop it; naming it first costs two realpaths (most ancestors have none)
        c = Candidate(names.of(path, memory_dir), kind, path, memory=True)
        if take(c) and row.tool != "codex":
            follow(path, 1)

    def follow(importer: Path, depth: int) -> None:
        if depth > IMPORT_MAX_DEPTH:
            return
        try:
            text = importer.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for raw in imports_of(text):
            target = (home() / raw[2:]) if raw.startswith("~/") else Path(raw) if raw.startswith("/") \
                else importer.parent / raw
            target = Path(os.path.normpath(target))
            if not target.is_file() or _real(target) in seen:
                continue   # Claude Code ignores a missing target; a cycle stops here
            name = names.of(target)
            if _env_file(target):
                seen.add(_real(target))
                out.append(Candidate(name, "import", label=label(target), imported_by=importer,
                                     reason="env file: never read"))
                continue
            if not _under_real(target, roots) or target.suffix.lower() not in IMPORT_SUFFIXES:
                seen.add(_real(target))
                why = ("outside the allowed roots" if not _under_real(target, roots) else "not a text instruction file")
                out.append(Candidate(name, "import", label=label(target), imported_by=importer,
                                     reason=f"import {why}: not read"))
                continue
            if take(Candidate(name, "import", target, imported_by=importer, memory=True)):
                follow(target, depth + 1)

    if row.tool == "codex":
        cx = codex_home()
        for f in (cx / "AGENTS.override.md", cx / "AGENTS.md"):
            if f.is_file():
                take(Candidate("agents-md:global", "codex-global", f, memory=True))
                break
        for skill in row.skills:
            if _valid_name(skill):
                found = next((f for f in _codex_skill_files(skill) if f.is_file()), None)
                take(Candidate(f"skill:{skill}", "skill", found, skill=skill) if found else
                     Candidate(f"skill:{skill}", "skill", label=skill, skill=skill,
                               reason="no SKILL.md under the Codex or shared skills (built in or removed)"))
            elif _NS_RE.fullmatch(skill):
                take(Candidate(f"skill:{skill}", "plugin-skill", label=skill, skill=skill.split(":", 1)[1],
                               reason="Codex plugin skills are not resolved to files"))
        if row.cwd:
            for d in dirs:
                f = next((d / n for n in ("AGENTS.override.md", "AGENTS.md") if (d / n).is_file()), None)
                if f is not None:
                    take(Candidate(names.of(f), "codex-project", f, memory=True))
            root = _git_root(Path(row.cwd))
            if root is not None:
                between = [d for d in _ancestors(Path(row.cwd)) if d == root or root in d.parents]
                for d in reversed(between):
                    f = next((d / n for n in ("AGENTS.override.md", "AGENTS.md") if (d / n).is_file()), None)
                    if f is not None:
                        take(Candidate(names.of(f), "codex-project", f, memory=True))
        return out[:ENTRIES_MAX]

    ch = claude_home()
    memory(ch / "CLAUDE.md", "user", ch)
    if row.kind == "worker" and row.meta.get("agent_type"):
        agent = str(row.meta["agent_type"])
        if _valid_name(agent):
            f = ch / "agents" / f"{agent}.md"
            take(Candidate(f"agent:{agent}", "agent", f) if f.is_file() else
                 Candidate(f"agent:{agent}", "agent", label=agent,
                           reason="no agent definition file (a built-in agent type, or removed)"))
        elif _NS_RE.fullmatch(agent):
            ns, item = agent.split(":", 1)
            take(_plugin_file(ns, item, "agents", dirs, f"agent:{agent}", "agent"))
    if row.kind == "routine" and row.meta.get("routine"):
        name = str(row.meta["routine"])
        if _valid_name(name):
            alt = Path(str(row.meta.get("routine_file") or "")).expanduser()
            alt_ok = alt.name == "SKILL.md" and alt.is_absolute() and ch in alt.parents
            standard = ch / "scheduled-tasks" / name / "SKILL.md"
            f = standard if standard.is_file() or not alt_ok else alt
            take(Candidate(f"routine:{name}", "routine", f) if f.is_file() else
                 Candidate(f"routine:{name}", "routine", label=name, reason="routine prompt file not found"))
    if row.kind == "parent":
        take(Candidate("parent-md", "parent", agent_config_dir() / "parent" / "PARENT.md"))
    for skill in row.skills:
        if _valid_name(skill):
            files = [ch / "skills" / skill / "SKILL.md", *[d / ".claude" / "skills" / skill / "SKILL.md" for d in dirs]]
            found = next((f for f in files if f.is_file()), None)
            take(Candidate(f"skill:{skill}", "skill", found, skill=skill) if found else
                 Candidate(f"skill:{skill}", "skill", label=skill, skill=skill,
                           reason="no SKILL.md under the Claude home or the repo (bundled with Claude Code, or removed)"))
        elif _NS_RE.fullmatch(skill):
            ns, item = skill.split(":", 1)
            take(_plugin_file(ns, item, "skills", dirs, f"skill:{skill}", "plugin-skill"))
    for d in dirs:
        for fn in MEMORY_FILES:
            memory(d / fn, "local" if fn == "CLAUDE.local.md" else "project", d)
    for d in dirs:
        f = d / "AGENTS.md"
        if f.is_file():
            take(Candidate(names.of(f, d), "project", f, memory=True))
    user_files = {_real(ch / "CLAUDE.md"), _real(Path.home() / ".claude" / "CLAUDE.md")}
    for chain in dirs:
        for d in _ancestors(chain):
            for fn in MEMORY_FILES:
                if (d / fn).is_file() and _real(d / fn) not in user_files:   # <home>/.claude/CLAUDE.md is the user memory, read above
                    memory(d / fn, "ancestor", d)
    return out[:ENTRIES_MAX]


def _codex_skill_files(skill: str) -> list[Path]:
    cx = codex_home()
    return [claude_home() / "skills" / skill / "SKILL.md", cx / "skills" / skill / "SKILL.md",
            cx / "skills" / ".system" / skill / "SKILL.md"]


# --------------------------------------------------------------------------- transcript evidence


@dataclass
class Evidence:
    files: dict[str, str] = field(default_factory=dict)        # realpath -> text loaded (first seen)
    later: dict[str, set[str]] = field(default_factory=dict)   # realpath -> other shas seen later
    paths: dict[str, str] = field(default_factory=dict)        # realpath -> the path as the transcript wrote it
    kinds: dict[str, str] = field(default_factory=dict)        # realpath -> User | Project | Local | ...
    listed: bool = False                                       # the transcript lists the loaded memory files
    skills: list[tuple[str, str]] = field(default_factory=list)   # (skill base dir, body)
    blocks: list[str] = field(default_factory=list)            # Codex: injected AGENTS.md blocks

    def add_file(self, path: str, text: str, kind: str = "") -> None:
        real = _real(Path(path))
        if real not in self.files:
            self.files[real], self.paths[real], self.kinds[real] = text, path, kind
        elif sha256(text) != sha256(self.files[real]):
            self.later.setdefault(real, set()).add(sha256(text))


_BASE_RE = re.compile(r"\A\s*Base directory for this skill: ([^\n]+)\n?(.*)\Z", re.S)
_CONTENTS_RE = re.compile(r"Contents of (/[^\n]+?|~/[^\n]+?) \(([^)\n]*)\):\n\n(.*?)(?=\n\nContents of [/~][^\n]* \(|\n</system-reminder>|\Z)",
                          re.S)
_CODEX_SKILL_RE = re.compile(r"<skill>\s*<name>\s*([^<]{1,80}?)\s*</name>\s*<path>\s*([^<]{1,400}?)\s*</path>(.*?)</skill>",
                             re.S)


def _skill_body(text: str) -> tuple[str, str] | None:
    m = _BASE_RE.match(text)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).lstrip("\n")


def evidence(events: list[dict[str, Any]], tool: str = "claude") -> Evidence:
    """What the transcript itself shows was loaded (module docstring, step 2)."""
    ev = Evidence()
    if tool == "codex":
        from agent_memory.codex_sessions import message_text, payload

        for e in events:
            p = payload(e)
            if e.get("type") != "response_item" or p.get("type") != "message" or p.get("role") != "user":
                continue
            text = message_text(p.get("content"))
            # Codex names no file per AGENTS.md text and a skill tag's body may be rewritten, so these
            # blocks only confirm a resolved file whose text appears in them (assess).
            if "# AGENTS.md instructions" in text or _CODEX_SKILL_RE.search(text):
                ev.blocks.append(text)
        return ev
    seen_assistant = False
    for e in events:
        etype = e.get("type")
        if etype == "assistant":
            seen_assistant = True
        if etype == "attachment":
            a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
            if a.get("type") == "instructions" and isinstance(a.get("files"), list):
                ev.listed = True
                for f in a["files"]:
                    if isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("content"), str):
                        ev.add_file(f["path"], f["content"], str(f.get("type") or ""))
            elif a.get("type") == "invoked_skills" and isinstance(a.get("skills"), list):
                for s in a["skills"]:
                    got = _skill_body(str(s.get("content") or "")) if isinstance(s, dict) else None
                    if got:
                        ev.skills.append(got)
            continue
        if etype != "user":
            continue
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = m.get("content")
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue
        text = text_of(content)
        got = _skill_body(text)
        if got:
            ev.skills.append(got)
            continue
        if not seen_assistant and "Contents of " in text and "<system-reminder>" in text:
            for path, _desc, body in _CONTENTS_RE.findall(text):
                p = str(home() / path[2:]) if path.startswith("~/") else path
                ev.listed = True
                ev.add_file(p, body)
    return ev


# --------------------------------------------------------------------------- versions


class Resolver:
    """Git lookups with caches for one run (an ingest batch, a reconcile, a scan)."""

    def __init__(self) -> None:
        self._top: dict[str, Path | None] = {}
        self._tracked: dict[tuple[str, str], bool] = {}
        self._hist: dict[tuple[str, str], list[tuple[str, int]]] = {}
        self._show: dict[tuple[str, str, str], tuple[str, str] | None] = {}

    def top(self, d: Path) -> Path | None:
        key = _real(d)
        if key not in self._top:
            out = rule_versions._git(d, "rev-parse", "--show-toplevel") if d.is_dir() else None
            self._top[key] = Path(out.strip()) if out and out.strip() else None
        return self._top[key]

    def tracked(self, path: Path) -> tuple[Path, str] | None:
        """(repo top, path relative to it) for a file git tracks, else None. A symlink counts as its
        target (~/.claude/skills/x -> ~/agent-config/skills/x: the target's repo has the history)."""
        path = Path(_real(path))
        top = self.top(path.parent)
        if top is None:
            return None
        try:
            rel = str(Path(_real(path)).relative_to(_real(top)))
        except ValueError:
            return None
        key = (str(top), rel)
        if key not in self._tracked:
            out = rule_versions._git(top, "ls-files", "--", rel)
            self._tracked[key] = bool(out and out.strip())
        return (top, rel) if self._tracked[key] else None

    def history(self, top: Path, rel: str) -> list[tuple[str, int]]:
        """(commit, commit time) newest first, the commits that touched the file."""
        key = (str(top), rel)
        if key not in self._hist:
            log = rule_versions._git(top, "log", f"-n{HISTORY_MAX}", "--format=%H %ct", "--", rel) or ""
            self._hist[key] = [(h, int(t)) for h, _, t in (ln.partition(" ") for ln in log.splitlines())
                               if t.strip().isdigit()]
        return self._hist[key]

    def show(self, top: Path, commit: str, rel: str) -> tuple[str, str] | None:
        """(blob sha, text) of the file at `commit`."""
        key = (str(top), commit, rel)
        if key not in self._show:
            blob = rule_versions._git(top, "rev-parse", f"{commit}:{rel}")
            text = rule_versions._git(top, "show", f"{commit}:{rel}")
            self._show[key] = (blob.strip(), text) if blob and text is not None else None
        return self._show[key]

    def blob_text(self, repo: Path, blob: str) -> str | None:
        if not re.fullmatch(r"[0-9a-f]{40,64}", blob or ""):
            return None
        return rule_versions._git(repo, "cat-file", "-p", blob)


_ACTIVE: ContextVar[Resolver | None] = ContextVar("instruction_sources_resolver", default=None)


@contextmanager
def batch() -> Iterator[Resolver]:
    """One Resolver (one set of git caches) for every row built inside the block."""
    r = Resolver()
    token = _ACTIVE.set(r)
    try:
        yield r
    finally:
        _ACTIVE.reset(token)


def resolver() -> Resolver:
    return _ACTIVE.get() or Resolver()


@dataclass
class Entry:
    name: str
    kind: str
    path: str                          # ~-relative path, absolute path, or a label (anthropic-skills:docs)
    authority: str
    reason: str | None = None
    version: dict[str, Any] | None = None   # {kind: git|content, id, blob?, repo?, basis}
    sha256: str | None = None          # sha256 of the raw text in force, when known
    plugin: dict[str, Any] | None = None
    imported_by: str | None = None
    changed_during_session: bool = False
    stored: bool = False               # its text is in instruction_texts
    text: str | None = field(default=None, repr=False, compare=False)

    def key(self) -> tuple[str, str]:
        return (self.name, self.path)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "kind": self.kind, "path": self.path, "authority": self.authority}
        for k in ("reason", "version", "sha256", "plugin", "imported_by"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if self.changed_during_session:
            out["changed_during_session"] = True
        if self.stored:
            out["stored"] = True
        return out

    @classmethod
    def from_json(cls, d: Any) -> Entry | None:
        if not isinstance(d, dict) or d.get("authority") not in AUTHORITIES:
            return None
        if not all(isinstance(d.get(k), str) for k in ("name", "kind", "path")):
            return None
        return cls(d["name"], d["kind"], d["path"], d["authority"], d.get("reason"),
                   d.get("version") if isinstance(d.get("version"), dict) else None,
                   d.get("sha256") if isinstance(d.get("sha256"), str) else None,
                   d.get("plugin") if isinstance(d.get("plugin"), dict) else None,
                   d.get("imported_by") if isinstance(d.get("imported_by"), str) else None,
                   d.get("changed_during_session") is True, d.get("stored") is True)


def _ts(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def _iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def version_at(path: Path, start: datetime | None, r: Resolver,
               plugin: dict[str, Any] | None = None) -> tuple[str, dict[str, Any] | None, str | None, str | None]:
    """(authority, version, text, reason) of a file at the session start (module docstring, step 3)."""
    try:
        st = path.stat()
    except OSError:
        return "unresolved", None, None, "file not readable now"
    t0 = _ts(start)
    if t0 is None:
        return "declared", None, None, "session start unknown: the version in force cannot be verified"
    git = r.tracked(path)
    if st.st_mtime <= t0:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "unresolved", None, None, "file not readable now"
        version: dict[str, Any] = {"kind": "content", "id": sha256(text), "basis": "mtime"}
        if git is not None:
            top, rel = git
            hist = r.history(top, rel)
            got = r.show(top, hist[0][0], rel) if hist else None
            if got is not None and got[1] == text:
                version = {"kind": "git", "id": hist[0][0], "blob": got[0], "repo": label(top), "basis": "mtime"}
        return "reconstructed", version, text, None
    if git is not None:
        top, rel = git
        commit = next((h for h, t in r.history(top, rel) if t <= t0), None)
        if commit is None:
            return "unresolved", None, None, "changed after the session started; no git commit of it before then"
        got = r.show(top, commit, rel)
        if got is None:
            return "unresolved", None, None, "git could not read the version in force"
        return ("reconstructed", {"kind": "git", "id": commit, "blob": got[0], "repo": label(top),
                                  "basis": "git-history"}, got[1], None)
    if plugin is not None:
        updated = _iso(plugin.get("last_updated"))
        if updated is not None and _ts(updated) is not None and (_ts(updated) or 0) > t0:
            return ("unresolved", None, None, f"plugin {plugin.get('id')} was installed or updated after the session started "
                                              f"(now {plugin.get('version', '?')}): the version in force is unknown")
    return "unresolved", None, None, "changed after the session started and not in git: the version in force is unknown"


def _match_skill(c: Candidate, ev: Evidence) -> tuple[str, str] | None:
    """The observed (base dir, body) of a skill candidate, or None."""
    if c.skill is None:
        return None
    own_dir = _real(c.path.parent) if c.path is not None else None
    ns = c.name.split(":")[1] if c.name.count(":") == 2 else None
    for base, body in ev.skills:
        b = Path(base)
        if own_dir is not None and _real(b) == own_dir:
            return base, body
        if b.name != c.skill:
            continue
        if ns is None:
            return base, body
        if ns in BUILTIN_NAMESPACES and ("skills-plugin" in base or "local-agent-mode-sessions" in base):
            return base, body
        if f"/plugins/cache/" in base and f"/{ns}/" in base:
            return base, body
    return None


def _observed(c: Candidate, path_label: str, text: str, basis: str = "transcript") -> Entry:
    return Entry(c.name, c.kind, path_label, "observed", version={"kind": "content", "id": sha256(text), "basis": basis},
                 sha256=sha256(text), plugin=c.plugin,
                 imported_by=label(c.imported_by) if c.imported_by else None, text=text)


def assess(row: SessionRow, cands: list[Candidate], ev: Evidence | None = None,
           r: Resolver | None = None) -> list[Entry]:
    """One Entry per candidate (plus files the transcript shows loaded that resolution did not
    predict). Memory candidates absent from the transcript's loaded list were not in force: dropped."""
    ev = ev or Evidence()
    r = r or resolver()
    names = _Names(row, row.tool)
    out: list[Entry] = []
    matched: set[str] = set()
    for c in cands:
        if c.path is not None:
            real = _real(c.path)
            if real in ev.files:
                matched.add(real)
                e = _observed(c, label(c.path), ev.files[real])
                e.changed_during_session = real in ev.later
                out.append(e)
                continue
            if ev.listed and c.memory and row.tool != "codex":
                continue   # the transcript lists what was loaded, and this file is not in it
        obs = _match_skill(c, ev)
        if obs is not None:
            base, body = obs
            where = label(Path(base) / "SKILL.md") if Path(base).is_absolute() else base
            out.append(_observed(c, where, body))
            continue
        if c.path is None:
            out.append(Entry(c.name, c.kind, c.where(), "unresolved", c.reason or "not resolved", plugin=c.plugin,
                             imported_by=label(c.imported_by) if c.imported_by else None))
            continue
        if row.tool == "codex" and ev.blocks:
            try:
                now_text = c.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                now_text = ""
            if now_text.strip() and any(now_text.strip() in b for b in ev.blocks):
                out.append(_observed(c, label(c.path), now_text))
                continue
        authority, version, text, reason = version_at(c.path, row.started_at, r, c.plugin)
        out.append(Entry(c.name, c.kind, label(c.path), authority, reason, version,
                         sha256(text) if text is not None else None, c.plugin,
                         label(c.imported_by) if c.imported_by else None, text=text))
    # Memory files the transcript shows loaded that resolution did not predict (an ancestor or an
    # import outside what we follow): observed all the same.
    for real, text in ev.files.items():
        if real in matched:
            continue
        p = Path(ev.paths[real])
        if _env_file(p):
            continue
        kind = {"User": "user", "Project": "project", "Local": "local"}.get(ev.kinds.get(real, ""), "import")
        e = _observed(Candidate(names.of(p), kind, p), label(p), text)
        e.changed_during_session = real in ev.later
        out.append(e)
    return out[:ENTRIES_MAX]


def storable(e: Entry, row: SessionRow) -> bool:
    """May this entry's text go into instruction_texts? Observed or reconstructed, under the size
    cap, a file under the allowed roots (or a memory file of the session's directories), never env."""
    if e.authority not in ("observed", "reconstructed") or e.text is None or e.sha256 is None:
        return False
    if len(e.text.encode("utf-8", errors="replace")) > TEXT_MAX_BYTES:
        return False
    p = unlabel(e.path)
    if p is None or _env_file(p):
        return False
    if _under(p, allowed_roots(row)):
        return True
    if p.name not in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "AGENTS.override.md") or not row.cwd:
        return False
    chain = {_real(d) for d in [Path(row.cwd), *Path(row.cwd).parents]}
    owner = p.parent.parent if p.parent.name == ".claude" else p.parent
    return _real(owner) in chain


def capture(row: SessionRow, events: list[dict[str, Any]] | None = None, r: Resolver | None = None) -> list[Entry]:
    """Resolve, read the transcript evidence and assess: the entries for `sessions.instruction_sources`.
    Entries whose text may be kept get `stored` set (the caller writes their text, `store_texts`)."""
    entries = assess(row, candidates(row), evidence(events or [], row.tool), r)
    for e in entries:
        e.stored = storable(e, row)
    return entries


def cloud_entries(routine: str, instructions: Any) -> list[Entry]:
    """A cloud routine's sources: the texts it sent (cloud_digests.instructions) are what was loaded."""
    from agent_memory.sessions_ingest import cloud_source_name

    out: list[Entry] = []
    for item in instructions or []:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        name = cloud_source_name(routine, str(item.get("name") or ""))
        if name is None or any(e.name == name for e in out):
            continue
        sha = sha256(item["text"])
        out.append(Entry(name, "cloud", f"cloud:{item.get('name')}", "observed",
                         version={"kind": "content", "id": sha, "basis": "cloud"}, sha256=sha))
    return out


# --------------------------------------------------------------------------- storage


def merge(stored: Any, new: list[Entry]) -> list[dict[str, Any]]:
    """Stored entries (JSON) plus new ones. Per (name, path) the stronger authority wins and a tie
    keeps the stored one (the earliest capture is the closest to the session start). Entries never
    disappear: once recorded in force, a source stays recorded."""
    old = [e for e in (Entry.from_json(d) for d in (stored if isinstance(stored, list) else [])) if e is not None]
    by_key = {e.key(): i for i, e in enumerate(old)}
    out = list(old)
    for e in new:
        i = by_key.get(e.key())
        if i is None:
            by_key[e.key()] = len(out)
            out.append(e)
        elif RANK[e.authority] < RANK[out[i].authority]:
            out[i] = e
    return [e.to_json() for e in out[:ENTRIES_MAX]]


def texts_to_store(entries: list[Entry], kept: list[dict[str, Any]]) -> list[tuple[str, str, int, bool, str]]:
    """(sha256, redacted text, raw bytes, redacted?, path) for each kept entry that came from `entries`
    with `stored` set."""
    keep = {(d["name"], d["path"], d.get("sha256")) for d in kept if d.get("stored")}
    out: dict[str, tuple[str, str, int, bool, str]] = {}
    for e in entries:
        if e.stored and e.text is not None and e.sha256 and (e.name, e.path, e.sha256) in keep:
            red = redact(e.text)
            out.setdefault(e.sha256, (e.sha256, red, len(e.text.encode("utf-8", errors="replace")), red != e.text,
                                      e.path))
    return list(out.values())


_TEXT_SQL = """
insert into instruction_texts (sha256, text, bytes, redacted, path) values (%s, %s, %s, %s, %s)
on conflict (sha256) do nothing
"""


def store_texts(conn: Any, rows: list[tuple[str, str, int, bool, str]]) -> int:
    n = 0
    for t in rows:
        cur = conn.execute(_TEXT_SQL, t)
        n += cur.rowcount or 0
    return n


def fetch_texts(conn: Any, shas: list[str]) -> dict[str, str]:
    if not shas:
        return {}
    return {s: t for s, t in conn.execute("select sha256, text from instruction_texts where sha256 = any(%s)",
                                          (list(set(shas)),)).fetchall()}


def entry_text(e: Entry, texts: dict[str, str], r: Resolver | None = None) -> str | None:
    """The text of a recorded version: instruction_texts, else git (blob of a git version), else
    today's file when its sha equals the recorded one. None: not recoverable."""
    if e.authority not in ("observed", "reconstructed") or not e.sha256:
        return None
    if e.sha256 in texts:
        return texts[e.sha256]
    v = e.version or {}
    if v.get("kind") == "git" and isinstance(v.get("repo"), str) and isinstance(v.get("blob"), str):
        repo = unlabel(v["repo"])
        if repo is not None and repo.is_dir():
            text = (r or Resolver()).blob_text(repo, v["blob"])
            if text is not None and sha256(text) == e.sha256:
                return text
    p = unlabel(e.path)
    if p is not None and not _env_file(p) and p.is_file():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if sha256(text) == e.sha256:
            return text
    return None


# --------------------------------------------------------------------------- reconcile (historical rows)


@dataclass
class ReconcileReport:
    apply: bool = False
    considered: int = 0
    written: int = 0
    texts: int = 0
    with_transcript: int = 0
    authorities: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    kinds: Counter[str] = field(default_factory=Counter)
    remaining: int = 0

    def line(self) -> str:
        a = self.authorities
        head = (f"instruction-sources: sessions={self.considered} with_transcript={self.with_transcript} "
                f"entries={sum(a.values())} observed={a['observed']} reconstructed={a['reconstructed']} "
                f"declared={a['declared']} unresolved={a['unresolved']}")
        if self.apply:
            return head + f" written={self.written} texts={self.texts} remaining={self.remaining}"
        return head + f" remaining={self.remaining} (dry run, nothing written; --apply to write)"

    def lines(self) -> list[str]:
        out = [self.line()]
        out += [f"  unresolved: {n} x {why}" for why, n in self.reasons.most_common()]
        out += [f"  kind {k}: {n}" for k, n in sorted(self.kinds.items())]
        return out

    def to_json(self) -> dict[str, Any]:
        return {"apply": self.apply, "considered": self.considered, "written": self.written, "texts": self.texts,
                "with_transcript": self.with_transcript, "remaining": self.remaining,
                "authorities": dict(self.authorities), "unresolved_reasons": dict(self.reasons),
                "kinds": dict(self.kinds)}


_PENDING_SQL = """
select * from sessions
where instruction_sources is null
  and (%(since)s::timestamptz is null or coalesce(started_at, created_at) >= %(since)s)
order by coalesce(started_at, created_at) desc nulls last, session_id
limit %(limit)s
"""
_REMAINING_SQL = """
select count(*) from sessions where instruction_sources is null
  and (%(since)s::timestamptz is null or coalesce(started_at, created_at) >= %(since)s)
"""


def reconcile(*, apply: bool = False, limit: int = 200, days: int | None = None, now: datetime | None = None,
              db_url: str | None = None) -> ReconcileReport:
    """Historical sessions without `instruction_sources`: up to `limit` rows (newest first) get their
    entries from today's resolution, their transcript (when it still exists) and git history at their
    start. Dry run by default: nothing written. `apply` writes only rows still null (a concurrent
    ingest wins), so a second run changes nothing and an interrupted run resumes where it stopped."""
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from agent_memory import deviations, store
    from agent_memory.sessions_ingest import load_events

    now = now or datetime.now(UTC)
    since = now - timedelta(days=days) if days else None
    report = ReconcileReport(apply=apply)
    r = Resolver()
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_PENDING_SQL, {"since": since, "limit": limit})
        recs = cur.fetchall()
    planned: list[tuple[str, list[dict[str, Any]], list[tuple[str, str, int, bool, str]]]] = []
    for rec in recs:
        row = deviations._row(rec)
        report.considered += 1
        report.kinds[row.kind] += 1
        if deviations.is_cloud(row):
            cloud = deviations.cloud_record(row.session_id, db_url)
            entries = cloud_entries(str((cloud or {}).get("routine") or ""), (cloud or {}).get("instructions"))
        else:
            path = deviations.transcript_path(row)
            events = load_events(path) if path.is_file() else []
            report.with_transcript += bool(events)
            entries = capture(row, events, r)
        for e in entries:
            report.authorities[e.authority] += 1
            if e.authority == "unresolved":
                report.reasons[e.reason or "?"] += 1
        kept = [e.to_json() for e in entries]
        planned.append((row.session_id, kept, texts_to_store(entries, kept)))
    if apply and planned:
        with store.connect(db_url) as conn:
            for sid, kept, texts in planned:
                cur2 = conn.execute("update sessions set instruction_sources = %s "
                                    "where session_id = %s and instruction_sources is null", (Jsonb(kept), sid))
                if cur2.rowcount:
                    report.written += 1
                    report.texts += store_texts(conn, texts)
    with store.connect(db_url) as conn:
        report.remaining = int(conn.execute(_REMAINING_SQL, {"since": since}).fetchone()[0])
    return report
