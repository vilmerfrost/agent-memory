"""Which file an edit may change, per instruction source (docs/edits.md, "Targets").

| Source | File | Action |
|---|---|---|
| `skill:<name>` | the real SKILL.md under ~/.agents/skills (symlinks resolved) | edit |
| `claude-md:global` | ~/.claude/CLAUDE.md | edit |
| `parent-md` | ~/agent-config/parent/PARENT.md | edit |
| `agent:<type>` | ~/agent-config/agents/<type>.md | edit |
| `routine:<name>` | ~/.claude/scheduled-tasks/<name>/SKILL.md | edit |
| `agents-md:global` | ~/.codex/AGENTS.md | edit |
| `claude-md:<repo>`, `agents-md:<repo>` | a file inside a repo | suggest (never edited) |

An "edit" target outside improve's allowed targets (`allowed_targets` in
~/agent-config/improve/criteria.json, read only) becomes a suggestion too. A missing file
or an unknown source is skipped. Reads names and the criteria file only; never writes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agent_memory import config
from agent_memory.sessions_ingest import agent_config_dir, claude_home

EDIT, SUGGEST, SKIP = "edit", "suggest", "skip"

# A copy of improve's allowed_targets, used only when criteria.json cannot be read.
DEFAULT_ALLOWED = ["@/parent/PARENT.md", "@/agents/worker.md", "@/lessons.md", "@/hooks/",
                   "~/.agents/skills/", "~/.claude/CLAUDE.md"]
_NAME_RE = re.compile(r"[\w.-]+")


def agents_home() -> Path:
    """AMEM_AGENTS_HOME (tests), else ~/.agents."""
    config.load_env()
    env = os.environ.get("AMEM_AGENTS_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".agents"


def codex_home() -> Path:
    """AMEM_CODEX_HOME (tests), else ~/.codex."""
    config.load_env()
    env = os.environ.get("AMEM_CODEX_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".codex"


def short(p: Path | None) -> str:
    """`~`-relative path for display and the edits row."""
    if p is None:
        return "-"
    for home in (Path.home().resolve(), Path.home()):
        try:
            return "~/" + str(p.relative_to(home))
        except ValueError:
            continue
    return str(p)


def _expand(t: str) -> Path:
    """An allowed_targets entry: `@/` is agent-config, `~/.claude|.agents|.codex` follow the
    test overrides, any other `~/` is the home folder."""
    t = t.rstrip("/")
    if t.startswith("@/"):
        return agent_config_dir() / t[2:]
    if t.startswith("~/"):
        rest = t[2:]
        for prefix, base in ((".claude", claude_home()), (".agents", agents_home()), (".codex", codex_home())):
            if rest == prefix or rest.startswith(prefix + "/"):
                return base / rest[len(prefix):].lstrip("/")
        return Path.home() / rest
    return Path(t)


def allowed_targets() -> list[tuple[Path, bool]]:
    """(path, is_dir_entry) per improve allowed target. criteria.json wins over the default."""
    raw: list[str] = DEFAULT_ALLOWED
    try:
        data = json.loads((agent_config_dir() / "improve" / "criteria.json").read_text(encoding="utf-8"))
        if isinstance(data.get("allowed_targets"), list) and all(isinstance(x, str) for x in data["allowed_targets"]):
            raw = data["allowed_targets"]
    except (OSError, ValueError, AttributeError):
        pass
    return [(_expand(t), t.endswith("/")) for t in raw]


def is_allowed(path: Path) -> bool:
    rp = path.resolve()
    for a, is_dir in allowed_targets():
        try:
            ra = a.resolve()
        except OSError:
            continue
        if rp == ra or ((is_dir or a.is_dir()) and ra in rp.parents):
            return True
    return False


@dataclass(frozen=True)
class Target:
    action: str            # edit | suggest | skip
    path: Path | None      # the real file (edit), or the best-known file (suggest)
    reason: str = ""


def _skill(name: str) -> Target:
    agents_skills = (agents_home() / "skills").resolve()
    for cand in (claude_home() / "skills" / name / "SKILL.md", agents_home() / "skills" / name / "SKILL.md"):
        if cand.is_file():
            real = cand.resolve()
            if agents_skills not in real.parents:
                return Target(SUGGEST, real, "skill file is not under ~/.agents/skills")
            return Target(EDIT, real)
    return Target(SKIP, None, "skill file not found")


def _file(path: Path) -> Target:
    if not path.is_file():
        return Target(SKIP, None, f"target file not found ({short(path)})")
    return Target(EDIT, path.resolve())


def resolve(source: str, target_paths: list[str] | None = None) -> Target:
    """Map an instruction source to its target. `target_paths` (from the candidates file)
    only names the file of a repo source in the suggestion."""
    kind, _, name = source.partition(":")
    if source.startswith(("claude-md:", "agents-md:")) and name != "global":
        hint = Path(os.path.expanduser(target_paths[0])) if target_paths else None
        return Target(SUGGEST, hint, "repo file: never edited, suggested instead")
    if source == "parent-md":
        t = _file(agent_config_dir() / "parent" / "PARENT.md")
    elif source == "claude-md:global":
        t = _file(claude_home() / "CLAUDE.md")
    elif source == "agents-md:global":
        t = _file(codex_home() / "AGENTS.md")
    elif kind == "skill" and _NAME_RE.fullmatch(name):
        t = _skill(name)
    elif kind == "agent" and _NAME_RE.fullmatch(name):
        primary = agent_config_dir() / "agents" / f"{name}.md"
        t = _file(primary if primary.is_file() else claude_home() / "agents" / f"{name}.md")
    elif kind == "routine" and _NAME_RE.fullmatch(name):
        t = _file(claude_home() / "scheduled-tasks" / name / "SKILL.md")
    else:
        return Target(SKIP, None, f"unknown instruction source {source!r}")
    if t.action == EDIT and t.path is not None and not is_allowed(t.path):
        return Target(SUGGEST, t.path, "outside improve's allowed targets")
    return t
