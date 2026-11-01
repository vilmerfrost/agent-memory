"""`amem sessions interface`: fill `sessions.interface` for rows stored before ingest wrote it.

    candidates  rows where interface is null
    evidence    stored meta only: no transcript read, no git, no network (session_interface.derive)
    parents     resolved before their subagents, so a subagent can use the parent's value

Dry run unless --apply; the dry run reads the database in a read-only transaction. --apply writes in
one transaction, only the `interface` column, only while it is still null. `sessions` has no update
trigger and no updated_at column, so nothing else moves. It deletes nothing.

--apply may not touch any other column, so a row does not record who wrote its value. A local log
does: `--revert LOG` sets interface back to null for the logged rows that still hold the logged value.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_memory import session_interface, sessions_ingest, store

LOG_VERSION = 1

_ROWS_SQL = "select session_id, tool, interface, meta from sessions order by session_id"

_APPLY_SQL = """
update sessions s set interface = x.value
from unnest(%s::text[], %s::text[]) as x(id, value)
where s.session_id = x.id and s.interface is null
returning s.session_id, s.interface
"""

_REVERT_SQL = """
update sessions s set interface = null
from unnest(%s::text[], %s::text[]) as x(id, value)
where s.session_id = x.id and s.interface = x.value
"""


@dataclass(frozen=True)
class Stored:
    session_id: str
    tool: str | None
    interface: str | None
    meta: Mapping[str, Any]


@dataclass(frozen=True)
class Row:
    session_id: str
    interface: str


@dataclass
class Plan:
    rows: list[Row] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = Counter(r.interface for r in self.rows)
        known = {v: c.pop(v, 0) for v in session_interface.VALUES}
        return {"candidates": len(self.rows), **known, **dict(c)}

    def to_json(self) -> dict[str, Any]:
        return {"counts": self.counts(),
                "rows": [{"session_id": r.session_id, "interface": r.interface} for r in self.rows]}

    def line(self) -> str:
        n = self.counts()
        values = " ".join(f"{k}={v}" for k, v in n.items() if k != "candidates")
        return f"sessions interface: {n['candidates']} without a value: {values}"


def resolve(stored: Iterable[Stored]) -> list[Row]:
    """The value of every row without one. Pure. A parent is resolved first, stored or not."""
    by_id = {s.session_id: s for s in stored}
    done: dict[str, str | None] = {}

    def value(sid: str, path: tuple[str, ...]) -> str | None:
        s = by_id[sid]
        if s.interface is not None:
            return s.interface
        if sid not in done:
            up = s.meta.get("parent_session")
            parent = value(up, (*path, sid)) if up in by_id and up not in (*path, sid) else None
            done[sid] = session_interface.derive(s.tool, s.meta, session_id=sid, parent=parent)
        return done[sid]

    return [Row(sid, got) for sid in sorted(by_id) if by_id[sid].interface is None
            and (got := value(sid, ())) is not None]


def plan(*, db_url: str | None = None) -> Plan:
    """Every candidate row and its value. Reads the database in a read-only transaction; writes nothing."""
    with store.connect(db_url) as conn:
        conn.read_only = True
        try:
            rows = conn.execute(_ROWS_SQL).fetchall()
        finally:
            conn.rollback()
    return Plan(resolve(Stored(sid, tool, value, meta if isinstance(meta, dict) else {})
                        for sid, tool, value, meta in rows))


def default_log(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return sessions_ingest.claude_home() / "parent" / f"amem-sessions-interface-{stamp}.json"


def apply(p: Plan, log: Path, *, db_url: str | None = None, now: datetime | None = None) -> int:
    """Write the values (one transaction). Returns the rows written; a row that gained a value since the
    plan is left alone. The log holds the rows really written: a failed log write rolls everything back."""
    with store.connect(db_url) as conn:
        written = conn.execute(_APPLY_SQL, ([r.session_id for r in p.rows], [r.interface for r in p.rows])).fetchall()
        doc = {"v": LOG_VERSION, "at": (now or datetime.now(UTC)).isoformat(), "rows": dict(written)}
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    return len(written)


def revert(log: Path, *, db_url: str | None = None) -> int:
    """Set interface back to null for the rows of an --apply log that still hold the logged value.
    A row that ingest or a ping changed since is left alone. Returns the rows changed."""
    doc = json.loads(log.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("v") != LOG_VERSION or not isinstance(doc.get("rows"), dict):
        raise ValueError(f"not a sessions interface log: {log}")
    with store.connect(db_url) as conn:
        return conn.execute(_REVERT_SQL, (list(doc["rows"]), list(doc["rows"].values()))).rowcount
