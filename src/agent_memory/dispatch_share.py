"""Token share of a dispatch run inside its own time window, from real local records.

A plan limit is per account, so a run that overlapped other sessions has a usage delta
that includes their work. This module measures what part of the window's tokens were
the run's own: Claude transcript messages and Codex rollout token_count events with a
timestamp inside [dispatched_at, briefed_at]. The run's part is its parent session and
that session's subagents/child threads. Use outside this Mac (claude.ai chat, Codex cloud,
other devices) is not in these files, so a share can overstate the run's part.

Refresh only: dispatch start/end never import this module.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_memory import dispatch_estimates as D

SHARE_METHOD = "token-share/1"
MAX_LINE = 1 << 20


def _fresh(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    vals = [usage.get(k) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens")]
    if not all(v is None or (isinstance(v, int) and not isinstance(v, bool) and v >= 0) for v in vals):
        return None
    return sum(v or 0 for v in vals)


def _claude_owner(path: Path, projects: Path) -> str | None:
    """The parent session a transcript belongs to: <proj>/<sid>.jsonl or <proj>/<sid>/subagents/*.jsonl."""
    try:
        parts = path.relative_to(projects).parts
    except ValueError:
        return None
    if len(parts) == 2:
        return path.stem
    if len(parts) == 4 and parts[2] == "subagents":
        return parts[1]
    return None


def _claude_files(projects: Path | Iterable[Path]) -> Iterator[tuple[Path, Path]]:
    """(transcript, its root) for one projects folder or several (the Claude roots)."""
    roots = [projects] if isinstance(projects, Path) else list(projects)
    if len(roots) == 1:
        for path in roots[0].glob("**/*.jsonl"):
            yield path, roots[0]
        return
    newest: dict[tuple[str | None, str], tuple[float, Path, Path]] = {}   # one session in two roots counts once
    for root in roots:
        for path in root.glob("**/*.jsonl"):
            try:
                when = path.stat().st_mtime
            except OSError:
                continue
            key = (_claude_owner(path, root), path.name)
            if key not in newest or when > newest[key][0]:
                newest[key] = (when, path, root)
    for _, path, root in newest.values():
        yield path, root


def claude_window(projects: Path | Iterable[Path], start: datetime, end: datetime, session: str) -> dict[str, Any]:
    """Fresh tokens (input + output + cache creation) inside the window: the run's and everyone's."""
    floor = start.timestamp()
    own = total = 0
    sessions: set[str] = set()
    files = 0
    for path, root in _claude_files(projects):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            if path.stat().st_mtime < floor:
                continue
        except OSError:
            continue
        owner = _claude_owner(path, root)
        if owner is None:
            continue
        latest: dict[str, int] = {}          # message id -> fresh; the latest line of a message wins
        try:
            with path.open("rb") as source:
                for raw in source:
                    if b'"usage"' not in raw or len(raw) > MAX_LINE:
                        continue
                    try:
                        rec = json.loads(raw)
                    except ValueError:
                        continue
                    msg = rec.get("message") if isinstance(rec, dict) else None
                    if not isinstance(msg, dict) or not isinstance(msg.get("id"), str):
                        continue
                    ts = D.parse_ts(rec.get("timestamp"))
                    fresh = _fresh(msg.get("usage"))
                    if ts is None or fresh is None or not start <= ts <= end:
                        continue
                    latest[msg["id"]] = fresh
        except OSError:
            continue
        if not latest:
            continue
        files += 1
        n = sum(latest.values())
        total += n
        sessions.add(owner)
        if owner == session:
            own += n
    return {"own": own, "total": total, "sessions": len(sessions), "files": files}


def _codex_meta(path: Path) -> tuple[str | None, str | None]:
    """(thread id, parent thread id) from the rollout's session_meta line."""
    try:
        with path.open("rb") as source:
            for i, raw in enumerate(source):
                if i > 5:
                    break
                if b'"session_meta"' not in raw[:200]:
                    continue
                payload = json.loads(raw).get("payload")
                if not isinstance(payload, dict):
                    return None, None
                src = payload.get("source")
                spawn = (src.get("subagent") or {}).get("thread_spawn") if isinstance(src, dict) \
                    and isinstance(src.get("subagent"), dict) else None
                parent = (spawn or {}).get("parent_thread_id") or payload.get("parent_thread_id")
                tid = payload.get("id") or payload.get("session_id")
                return (tid if isinstance(tid, str) else None, parent if isinstance(parent, str) else None)
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


def _codex_files(homes: Path | Iterable[Path]) -> list[Path]:
    """The rollouts of one Codex home or several. A file in two homes (a transition copy) counts once."""
    from agent_memory import dispatch_seed as S

    roots = [homes] if isinstance(homes, Path) else list(homes)
    if len(roots) == 1:
        return S.rollout_files(roots[0])
    newest: dict[str, tuple[float, Path]] = {}
    for root in roots:
        for path in S.rollout_files(root):
            try:
                when = path.stat().st_mtime
            except OSError:
                continue
            if path.name not in newest or when > newest[path.name][0]:
                newest[path.name] = (when, path)
    return [path for _, path in newest.values()]


def codex_window(home: Path | Iterable[Path], start: datetime, end: datetime, session: str) -> dict[str, Any]:
    """Fresh tokens (input - cached + output) of Codex rollouts inside the window."""
    from agent_memory import dispatch_seed as S

    thread = session[len("codex:"):] if session.startswith("codex:") else session
    floor = start.timestamp()
    own = total = 0
    threads: set[str] = set()
    files = 0
    for path in _codex_files(home):
        try:
            if path.stat().st_mtime < floor:
                continue
        except OSError:
            continue
        tid, parent = _codex_meta(path)
        deltas, _ = S.scan_rollout(path, path.name)
        n = sum(d[1] for d in deltas if start <= d[0] <= end)
        if not n:
            continue
        files += 1
        total += n
        threads.add(tid or path.name)
        if thread and (tid == thread or parent == thread):
            own += n
    return {"own": own, "total": total, "sessions": len(threads), "files": files}


def run_share(run: dict, *, projects: Path | Iterable[Path], codex_home: Path | Iterable[Path]) -> dict[str, Any] | None:
    """The run's token share for its own tool, or None when it cannot be measured."""
    start, end = D.parse_ts(run.get("dispatched_at")), D.parse_ts(run.get("briefed_at"))
    session, tool = run.get("session"), run.get("tool")
    if not start or not end or end <= start or not isinstance(session, str) or tool not in D.TOOLS:
        return None
    window = (claude_window(projects, start, end, session) if tool == "claude"
              else codex_window(codex_home, start, end, session))
    if not window["total"] or not window["own"]:
        return {**window, "share": None, "method_version": SHARE_METHOD,
                "why_none": "no own tokens in the window" if window["total"] else "no tokens in the window"}
    return {**window, "share": round(window["own"] / window["total"], 4), "method_version": SHARE_METHOD}
