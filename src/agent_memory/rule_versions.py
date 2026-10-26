"""When did a rule's own text last change? From git history (docs/rsi/hand-edits.md).

A deviation is evidence against the wording that was in force when its session ran. When Vilmer (or the loop)
rewrites a rule, the deviations recorded against the old wording say nothing about the new one, so the night counts
only deviations from sessions that started after the rule's section last changed (`since`). The rule's section is
`edits_sections.section_text`, the same span the writer edits; a change elsewhere in the file does not reset it.

Only files in a git repository have a history here; any other file (e.g. ~/.claude/CLAUDE.md) gives None, which
means "no filter". The newest MAX_COMMITS commits of the file are read; a rule unchanged in all of them gives None.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent_memory import edits_sections

MAX_COMMITS = 40
GIT_TIMEOUT_S = 10


def _git(cwd: Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


def _rule_text(text: str | None, rule_id: str) -> str | None:
    if text is None:
        return None
    try:
        return edits_sections.section_text(text, rule_id).rstrip()
    except (edits_sections.SectionError, ValueError):
        return None


def changes(path: Path, rule_id: str) -> list[datetime]:
    """Times (newest first) at which the rule's text changed, from the file's git history and its uncommitted state.
    Empty when the file is not in git, the rule is not in the file, or nothing changed in MAX_COMMITS commits."""
    path = Path(path).expanduser()
    if not path.is_file():
        return []
    top = _git(path.parent, "rev-parse", "--show-toplevel")
    if not top:
        return []
    root = Path(top.strip())
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return []
    log = _git(root, "log", f"-n{MAX_COMMITS}", "--format=%H %ct", "--", rel) or ""
    commits = [(h, int(t)) for h, _, t in (line.partition(" ") for line in log.splitlines()) if t.strip().isdigit()]
    try:
        current = _rule_text(path.read_text(encoding="utf-8"), rule_id)
    except (OSError, UnicodeDecodeError):
        return []
    if current is None:
        return []
    out: list[datetime] = []
    newer_text, newer_time = current, None   # None: the working tree (uncommitted)
    for h, t in commits:
        text = _rule_text(_git(root, "show", f"{h}:{rel}"), rule_id)
        if text != newer_text:
            out.append(datetime.fromtimestamp(newer_time, UTC) if newer_time is not None
                       else datetime.fromtimestamp(path.stat().st_mtime, UTC))
        newer_text, newer_time = text, t
    return out


def changed_at(path: Path, rule_id: str) -> datetime | None:
    """The newest change of the rule's text, or None (no history, or unchanged in MAX_COMMITS commits)."""
    c = changes(path, rule_id)
    return c[0] if c else None


def since(target_paths: list[str], rule_id: str | None) -> datetime | None:
    """The newest change of the rule over the source's files: deviations from sessions before it are old evidence."""
    if not rule_id:
        return None
    times = [t for p in target_paths if (t := changed_at(Path(p).expanduser(), rule_id)) is not None]
    return max(times) if times else None
