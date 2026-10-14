"""`amem sessions dedupe-resumes [--apply] [--log FILE] | --revert FILE` (docs/sessions-live.md,
"Resumed sessions"): re-read the stored rows of resumed Claude Code transcripts with the
session_resume rule. Postgres only.

Before the rule, a resumed transcript's row counted the copied lines again (tokens, turns, time,
skills, prompts, commits, and its started_at was the earlier session's). This command:

- finds every local transcript (main and subagent files, any age) that copies an earlier one: a
  resume chain (session_resume.resume_of) or, older shape, a first line with another `sessionId`;
- re-reads its row (sessions_ingest.session_row) and writes it with the ingest upsert, so the row
  is exactly what `sessions ingest` stores today;
- deletes the row of a pure copy (no own message). Its `skill_calls` move to the row it copied
  (`meta.resumed_from` when stored, else session_id null: the next skills ingest matches them again).

Without `--apply` it prints the plan and writes nothing. `--apply` first writes a JSON log of
every stored row it touches (all columns, before the change), of every skill call it moves and
of every deviation a delete would cascade to, then makes the change in one transaction.
`--revert FILE` puts the logged rows, deviations and skill calls back. A second `--apply` finds nothing to change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import session_resume, sessions_ingest, store

LOG_VERSION = 1
_COMPARE = ("kind", "started_at", "ended_at", "skills", "tokens_input", "tokens_output", "tokens_cache_read",
            "tokens_cache_creation", "turns", "duration_seconds", "active_seconds")
_TOKENS = ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_creation")


def default_log(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return sessions_ingest.claude_home() / "parent" / f"amem-dedupe-resumes-{stamp}.json"


def _first_session_id(path: Path) -> str | None:
    for e in session_resume.read_lines(path, session_resume.FIRST_UUID_LINES):
        sid = e.get("sessionId")
        if isinstance(sid, str) and sid:
            return sid
    return None


def candidates(root: Path) -> list[tuple[Path, str, str | None]]:
    """(file, project folder, parent session or None) of every transcript that copies an earlier one."""
    out = []
    for path, project, parent in sessions_ingest.transcripts(root, datetime.fromtimestamp(0, UTC)):
        own = parent or path.stem
        first = _first_session_id(path)
        if session_resume.resume_of(path) != session_resume.NONE or (first is not None and first != own):
            out.append((path, project, parent))
    return out


@dataclass
class Change:
    session_id: str
    action: str                         # update | delete
    before: dict[str, Any]
    row: sessions_ingest.SessionRow
    target: str | None = None           # delete: the row its skill calls move to


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    unchanged: int = 0
    not_stored: int = 0
    files: int = 0

    def tokens(self, after: bool) -> int:
        total = 0
        for c in self.changes:
            if not after:
                total += sum(int(c.before.get(k) or 0) for k in _TOKENS)
            elif c.action == "update" and c.row.tokens is not None:
                total += c.row.tokens.tokens.total
        return total


def _differs(before: dict[str, Any], row: sessions_ingest.SessionRow) -> bool:
    p = sessions_ingest._params(row)
    for k in _COMPARE:
        new = p.get(k)
        new = new.obj if isinstance(new, Jsonb) else new
        old = before.get(k)
        if isinstance(old, Decimal):
            old = float(old)
        if old != new:
            return True
    return before.get("meta", {}).get("resumed_from") != row.meta.get("resumed_from")


def plan(root: Path, *, db_url: str | None = None) -> Plan:
    out = Plan()
    found = candidates(root)
    out.files = len(found)
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        for path, project, parent in found:
            row = sessions_ingest.session_row(path, project, parent_session=parent)
            cur.execute("select * from sessions where session_id = %s", (row.session_id,))
            before = cur.fetchone()
            if before is None:
                out.not_stored += 1
                continue
            if row.copy_only:
                target = row.meta.get("resumed_from")
                if target is None and parent is None:
                    target = _first_session_id(path)
                elif target is None:
                    target = f"{_first_session_id(path)}/{path.stem}"
                cur.execute("select 1 from sessions where session_id = %s", (target,))
                out.changes.append(Change(row.session_id, "delete", before, row,
                                          target if cur.fetchone() is not None else None))
            elif row.started_at is not None and _differs(before, row):
                out.changes.append(Change(row.session_id, "update", before, row))
            else:
                out.unchanged += 1
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def apply(p: Plan, log: Path, *, db_url: str | None = None, now: datetime | None = None) -> dict[str, int]:
    """Write the log, then the changes (one transaction). Returns counts."""
    with store.connect(db_url) as conn:
        deletes = [c for c in p.changes if c.action == "delete"]
        moved: list[dict[str, Any]] = []
        cascaded: list[dict[str, Any]] = []
        if deletes:
            ids = [c.session_id for c in deletes]
            moved = [{"id": r[0], "session_id": r[1]} for r in conn.execute(
                "select id::text, session_id from skill_calls where session_id = any(%s)", (ids,)).fetchall()]
            with conn.cursor(row_factory=dict_row) as cur:   # deleted with the row (on delete cascade)
                cur.execute("select * from deviations where session_id = any(%s)", (ids,))
                cascaded = [{k: _plain(v) for k, v in r.items()} for r in cur.fetchall()]
        doc = {"v": LOG_VERSION, "at": (now or datetime.now(UTC)).isoformat(),
               "rows": [{k: _plain(v) for k, v in c.before.items()} for c in p.changes],
               "actions": {c.session_id: c.action for c in p.changes}, "skill_calls": moved,
               "deviations": cascaded}
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        for c in p.changes:
            if c.action == "update":
                conn.execute(sessions_ingest._UPSERT_SQL, sessions_ingest._params(c.row, now))
            else:
                conn.execute("update skill_calls set session_id = %s where session_id = %s", (c.target, c.session_id))
                conn.execute("delete from sessions where session_id = %s", (c.session_id,))
    return {"updated": len(p.changes) - len(deletes), "deleted": len(deletes), "skill_calls": len(moved)}


def _put(conn: Any, table: str, rec: dict[str, Any], key: str, *, replace: bool) -> None:
    """Insert one logged row of `table` (all its logged columns); `replace`: overwrite a stored one."""
    types = dict(conn.execute("select column_name, data_type from information_schema.columns "
                              "where table_schema = 'public' and table_name = %s", (table,)).fetchall())
    cols = [k for k in rec if k in types]
    vals = [Jsonb(rec[k]) if types[k] == "jsonb" and rec[k] is not None else rec[k] for k in cols]
    action = (sql.SQL("do update set {}").format(sql.SQL(", ").join(
        sql.SQL("{c} = excluded.{c}").format(c=sql.Identifier(c)) for c in cols if c != key))
        if replace else sql.SQL("do nothing"))
    conn.execute(sql.SQL("insert into {t} ({cols}) values ({vals}) on conflict ({key}) {action}").format(
        t=sql.Identifier(table), cols=sql.SQL(", ").join(map(sql.Identifier, cols)),
        vals=sql.SQL(", ").join(sql.Placeholder() * len(cols)), key=sql.Identifier(key), action=action), vals)


def revert(log: Path, *, db_url: str | None = None) -> dict[str, int]:
    """Put the logged rows and skill calls back (one transaction)."""
    doc = json.loads(log.read_text(encoding="utf-8"))
    if doc.get("v") != LOG_VERSION:
        raise ValueError(f"not a dedupe-resumes log (v {doc.get('v')!r}): {log}")
    with store.connect(db_url) as conn:
        for rec in doc["rows"]:
            _put(conn, "sessions", rec, "session_id", replace=True)
        for rec in doc.get("deviations") or []:
            _put(conn, "deviations", rec, "id", replace=False)
        for call in doc.get("skill_calls") or []:
            conn.execute("update skill_calls set session_id = %s where id::text = %s", (call["session_id"], call["id"]))
    return {"rows": len(doc["rows"]), "skill_calls": len(doc.get("skill_calls") or []),
            "deviations": len(doc.get("deviations") or [])}


def lines(p: Plan) -> list[str]:
    out = [f"dedupe-resumes: {p.files} resumed transcripts, {len(p.changes)} rows to fix "
           f"({sum(c.action == 'update' for c in p.changes)} update, {sum(c.action == 'delete' for c in p.changes)} "
           f"delete), {p.unchanged} already right, {p.not_stored} not stored"]
    for c in p.changes:
        before = sum(int(c.before.get(k) or 0) for k in _TOKENS)
        after = c.row.tokens.tokens.total if c.action == "update" and c.row.tokens is not None else 0
        start = c.row.started_at.isoformat() if c.action == "update" and c.row.started_at else "-"
        out.append(f"  {c.action:6} {c.session_id} tokens {before} -> {after} started_at "
                   f"{_plain(c.before.get('started_at'))} -> {start}"
                   + (f" resumed_from {c.row.meta.get('resumed_from')}" if c.row.meta.get("resumed_from") else "")
                   + (f" skill_calls -> {c.target}" if c.action == "delete" else ""))
    out.append(f"  tokens of these rows: {p.tokens(False)} -> {p.tokens(True)}")
    return out
