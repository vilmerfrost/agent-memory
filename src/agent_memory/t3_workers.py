"""T3 workers: agents T3 Code started with delegate_task, which the root registered (agent-config bin/parent-register).

A T3 worker is its own top-level session, unlike a subagent whose lines live inside its parent's. Registration leaves
one small record per worker: <claude home>/parent/<repo>/t3-workers/<t3-id>.json. Ingest reads it to save the session
as kind `worker`, with the parent link in meta like any worker, so the existing joins, token rollups and dispatch
estimates count it. No new column.

`meta.parent_session` on a subagent also means "my lines carry that id". A T3 worker's lines carry its own id, so code
that filters a transcript or finds its file asks `own_session` and `in_parent_folder` instead of reading the key.

`amem sessions t3-workers` writes these records for children from before registration existed, from T3's own state.
The next ingest then fixes their rows. It writes only local records, never the database.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

AGENT_TYPE = "t3-worker"
CHILD_PREFIX = "thread:delegated-task:"
WORKTREE_RE = re.compile(r"/worktrees/([^/]+)/(t3-[0-9A-Za-z]+)(?:/|$)")
_UUID_END = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_META_KEYS = ("name", "provider", "model", "effort", "thread", "why", "branch")


def record_path(parent_dir: Path, repo: str, agent: str) -> Path:
    return parent_dir / repo / "t3-workers" / f"{agent}.json"


def lookup(parent_dir: Path, cwds: Iterable[str | None]) -> dict[str, Any] | None:
    """The registration record of the T3 worktree a session worked in, or None."""
    for cwd in cwds:
        m = WORKTREE_RE.search(cwd or "")
        if not m:
            continue
        try:
            data = json.loads(record_path(parent_dir, m.group(1), m.group(2)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def apply(meta: dict[str, Any], record: dict[str, Any]) -> None:
    """Mark a session's meta as this T3 worker. A worker with no known parent gets no parent key."""
    meta["agent_type"] = AGENT_TYPE
    meta["t3_worker"] = {k: record.get(k) for k in _META_KEYS if record.get(k)}
    if record.get("parent_session"):
        meta["parent_session"] = record["parent_session"]


def is_worker(meta: dict[str, Any] | None) -> bool:
    return (meta or {}).get("agent_type") == AGENT_TYPE


def own_session(meta: dict[str, Any] | None, session_id: str) -> str:
    """The id on this session's own transcript lines."""
    meta = meta or {}
    return session_id if is_worker(meta) else str(meta.get("parent_session") or session_id)


def in_parent_folder(meta: dict[str, Any] | None) -> bool:
    """True for a subagent, whose transcript lives under its parent's. A T3 worker has its own file."""
    return bool((meta or {}).get("parent_session")) and not is_worker(meta)


# --------------------------------------------------------------------------- older children


@dataclass
class Child:
    thread: str
    worktree: str
    provider: str
    model: str | None
    effort: str | None
    title: str
    created_at: str
    parent_thread: str | None = None
    parent_session: str | None = None


@dataclass
class Plan:
    children: list[Child] = field(default_factory=list)
    skipped: int = 0

    def line(self) -> str:
        linked = sum(1 for c in self.children if c.parent_session)
        return (f"sessions t3-workers: {len(self.children)} older child(ren) without a record, {linked} with a parent "
                f"session found, {self.skipped} already recorded")


def read_children(db: Path) -> list[Child]:
    """Delegated threads that had a worktree, from T3's state, read-only."""
    con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("select thread_id, default_provider, title, created_at, payload_json "
                           "from orchestration_v2_projection_threads where thread_id like ? order by created_at",
                           (CHILD_PREFIX + "%",)).fetchall()
    finally:
        con.close()
    out = []
    for tid, provider, title, created, payload in rows:
        p = json.loads(payload)
        model = p.get("modelSelection") or {}
        if not p.get("worktreePath"):
            continue
        effort = next((o.get("value") for o in model.get("options") or [] if o.get("id") in ("effort", "reasoningEffort")), None)
        out.append(Child(tid, p["worktreePath"], provider, model.get("model"), effort, title, created,
                         (p.get("lineage") or {}).get("parentThreadId")))
    return out


def session_of(path: Path) -> str:
    """A transcript file's session id as stored: Claude's file stem, or `codex:` + the rollout's thread."""
    if path.name.startswith("rollout-"):
        m = _UUID_END.search(path.stem)
        return "codex:" + m.group(0) if m else ""
    return path.stem


def find_parents(children: list[Child], files: Iterable[Path]) -> None:
    """Fill parent_session from the transcript whose delegate_task result names the child's thread."""
    wanted = {c.thread: c for c in children}
    for path in files:
        try:
            with path.open("rb") as fh:
                for raw in fh:
                    if b"childThreadId" not in raw:
                        continue
                    line = raw.decode("utf-8", "replace")
                    for thread, child in wanted.items():
                        if child.parent_session is None and thread in line:
                            child.parent_session = session_of(path) or None
        except OSError:
            continue


def plan(db: Path, parent_dir: Path, files: Iterable[Path]) -> Plan:
    result = Plan()
    for child in read_children(db):
        m = WORKTREE_RE.search(child.worktree)
        if m and record_path(parent_dir, m.group(1), m.group(2)).exists():
            result.skipped += 1
        elif m:
            result.children.append(child)
    find_parents(result.children, files)
    return result


def apply_plan(result: Plan, parent_dir: Path, now: datetime | None = None) -> int:
    """Write one record per older child. Never overwrites a record, so running it twice changes nothing."""
    now = now or datetime.now(UTC)
    n = 0
    for c in result.children:
        m = WORKTREE_RE.search(c.worktree)
        if not m:
            continue
        path = record_path(parent_dir, m.group(1), m.group(2))
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agent_id": m.group(2), "repo": m.group(1), "worktree": c.worktree, "thread": c.thread,
                                    "provider": c.provider, "model": c.model, "effort": c.effort, "name": c.title,
                                    "parent_thread": c.parent_thread, "parent_session": c.parent_session,
                                    "backfilled_at": now.isoformat(timespec="seconds")}, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
        n += 1
    return n


def transcripts_since(roots: Iterable[Path], since: datetime) -> list[Path]:
    """Transcript files touched since `since`. A parent's file changed after its child started."""
    cut = (since - timedelta(minutes=5)).timestamp()
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime >= cut:
                    found.append(path)
            except OSError:
                continue
    return found
