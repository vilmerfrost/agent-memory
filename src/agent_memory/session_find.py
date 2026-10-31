"""`amem sessions find --agent-id ID [--ingest]`: the sessions row of one subagent (worker)
transcript, for incident scans (docs/deviations.md, "Incident scans").

A subagent transcript lives at `<projects>/<project>/<parent session>/subagents/agent-<ID>.jsonl`
and `sessions_ingest` stores it as session_id `<parent session>/agent-<ID>`. Postgres only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from agent_memory import sessions_ingest, store

_AGENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def valid_agent_id(agent_id: str) -> bool:
    return _AGENT_ID_RE.fullmatch(agent_id) is not None


def _suffix(agent_id: str) -> str:
    return f"/agent-{agent_id}"


def find(agent_id: str, *, db_url: str | None = None) -> list[dict[str, Any]]:
    """Stored rows whose session id ends in `/agent-<agent_id>`, newest first."""
    if not valid_agent_id(agent_id):
        raise ValueError(f"not an agent id: {agent_id!r}")
    suffix = _suffix(agent_id)
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select session_id, kind, category, project, started_at, scanned_at
            from sessions where right(session_id, %s) = %s
            order by started_at desc nulls last, session_id
            """,
            (len(suffix), suffix),
        )
        return cur.fetchall()


def locate(agent_id: str, root: Path) -> list[Path]:
    """Transcript files `<root>/<project>/<parent>/subagents/agent-<agent_id>.jsonl`."""
    if not valid_agent_id(agent_id) or not root.is_dir():
        return []
    return sorted(p for p in root.glob(f"*/*/subagents/agent-{agent_id}.jsonl") if p.is_file())


def ingest(agent_id: str, root: Path | Sequence[Path], *, db_url: str | None = None) -> tuple[int, int, int]:
    """Ingest the transcript(s) of one subagent from one projects folder or several (the Claude
    roots). Returns (files, new, updated); a file with no timestamped event is skipped (files
    counts it, new / updated do not)."""
    rows = []
    paths = [p for r in ([root] if isinstance(root, Path) else root) for p in locate(agent_id, r)]
    for path in paths:
        project_dir = path.parent.parent.parent.name
        parent = path.parent.parent.name
        row = sessions_ingest.session_row(path, project_dir, parent_session=parent)
        if row.started_at is not None and not row.copy_only:   # a pure copy gets no row (session_resume)
            rows.append(row)
    if not isinstance(root, Path) and len(root) > 1:
        rows = sessions_ingest.one_per_session(rows)
    new, updated = sessions_ingest.upsert(rows, db_url=db_url) if rows else (0, 0)
    return len(paths), new, updated
