"""Handoff events (docs/handoffs.md): `amem handoffs log|resume|list`.

One row per session handoff in `public.handoffs`. Both backends: Postgres (psycopg) when
`config.backend()` is `postgres` or a `db_url` is passed, else PostgREST (`backend_rest`),
so a Stop hook in a cloud session can log too.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from agent_memory import backend_rest, config, store

TABLE = "handoffs"
REASONS = ("auto", "manual")
DEFAULT_DAYS = 7
COLUMNS = ("id", "session_id", "project", "handoff_path", "reason", "context_tokens", "context_pct", "title",
           "created_at", "resumed_session_id", "resumed_at")


def _rest(db_url: str | None) -> bool:
    return db_url is None and config.backend() == "rest"


@dataclass
class Resume:
    """Result of `resume`: `ok`, `missing` (no such id) or `taken` (resumed by another session)."""
    status: str
    resumed_session_id: str | None = None


def log(session_id: str, handoff_path: str, *, project: str | None = None, reason: str = "manual",
        context_tokens: int | None = None, context_pct: float | None = None, title: str | None = None,
        db_url: str | None = None) -> uuid.UUID:
    """Insert one handoff row and return its id."""
    if reason not in REASONS:
        raise ValueError(f"reason must be one of {REASONS}, got {reason!r}")
    row = {"session_id": session_id, "project": project, "handoff_path": handoff_path, "reason": reason,
           "context_tokens": context_tokens, "context_pct": context_pct, "title": title}
    if _rest(db_url):
        got = backend_rest._request("POST", [("select", "id")], [row], prefer="return=representation",
                                    table=TABLE)
        if not isinstance(got, list) or not got or "id" not in got[0]:
            raise backend_rest.RestError("POST /rest/v1/handoffs returned no id")
        return uuid.UUID(str(got[0]["id"]))
    cols = list(row)
    with store.connect(db_url) as conn:
        got = conn.execute(
            f"insert into handoffs ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) returning id",
            [row[c] for c in cols],
        ).fetchone()
    assert got is not None
    return got[0]


def resume(handoff_id: uuid.UUID, session_id: str, *, db_url: str | None = None) -> Resume:
    """Mark a handoff as resumed by `session_id`. Idempotent for the same session; a handoff
    already resumed by another session is left alone (`taken`)."""
    if _rest(db_url):
        got = backend_rest._request(
            "PATCH",
            [("id", f"eq.{handoff_id}"),
             ("or", f"(resumed_session_id.is.null,resumed_session_id.eq.{session_id})"),
             ("select", "id")],
            {"resumed_session_id": session_id, "resumed_at": datetime.now(UTC).isoformat()},
            prefer="return=representation", table=TABLE)
        if got:
            return Resume("ok", session_id)
        found = backend_rest._request("GET", [("select", "resumed_session_id"), ("id", f"eq.{handoff_id}")],
                                      table=TABLE)
        if not found:
            return Resume("missing")
        return Resume("taken", found[0].get("resumed_session_id"))
    with store.connect(db_url) as conn:
        got = conn.execute(
            """
            update handoffs set resumed_session_id = %s, resumed_at = coalesce(resumed_at, now())
            where id = %s and (resumed_session_id is null or resumed_session_id = %s)
            returning id
            """,
            (session_id, handoff_id, session_id),
        ).fetchone()
        if got is not None:
            return Resume("ok", session_id)
        found = conn.execute("select resumed_session_id from handoffs where id = %s", (handoff_id,)).fetchone()
    if found is None:
        return Resume("missing")
    return Resume("taken", found[0])


def recent(days: int = DEFAULT_DAYS, *, now: datetime | None = None, db_url: str | None = None) -> list[dict[str, Any]]:
    """Handoffs created in the last `days` days, newest first."""
    since = (now or datetime.now(UTC)) - timedelta(days=days)
    if _rest(db_url):
        got = backend_rest._request(
            "GET",
            [("select", ",".join(COLUMNS)), ("created_at", f"gte.{since.isoformat()}"),
             ("order", "created_at.desc")],
            table=TABLE)
        return list(got or [])
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"select {', '.join(COLUMNS)} from handoffs where created_at >= %s order by created_at desc",
                    (since,))
        return cur.fetchall()
