"""Run log storage (psycopg 3) and the SQL migration runner."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import config

STATUSES = ("pass", "fail", "skip", "error")
SOURCES = ("launchd", "scheduled-task", "cloud", "manual")


def connect(db_url: str | None = None, **kwargs: Any) -> psycopg.Connection:
    return psycopg.connect(db_url or config.db_url(), **kwargs)


def insert_run(
    routine: str,
    status: str,
    *,
    source: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    summary: str | None = None,
    failure_reason: str | None = None,
    report_path: str | None = None,
    meta: dict[str, Any] | None = None,
    db_url: str | None = None,
) -> uuid.UUID:
    """Insert one run row and return its id. started_at defaults to now()."""
    with connect(db_url) as conn:
        row = conn.execute(
            """
            insert into runs (routine, source, started_at, finished_at, status,
                              summary, failure_reason, report_path, meta)
            values (%s, %s, coalesce(%s, now()), %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (routine, source, started_at, finished_at, status,
             summary, failure_reason, report_path, Jsonb(meta or {})),
        ).fetchone()
    assert row is not None
    return row[0]


def recent_runs(routine: str, limit: int = 10, *, db_url: str | None = None) -> list[dict[str, Any]]:
    """Newest runs first for one routine."""
    with connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            select * from runs where routine = %s
            order by started_at desc nulls last, created_at desc
            limit %s
            """,
            (routine, limit),
        )
        return cur.fetchall()


def migrate(db_url: str | None = None, migrations_dir: Path | None = None) -> list[str]:
    """Apply `*.sql` files in name order, each once. Returns the files applied now.

    Applied files are tracked in `schema_migrations`. Each file runs in its own
    transaction together with its tracking row. An advisory lock serializes
    concurrent migrators.
    """
    directory = migrations_dir or config.MIGRATIONS_DIR
    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    applied_now: list[str] = []
    with connect(db_url, autocommit=True) as conn:
        conn.execute(
            """
            create table if not exists schema_migrations (
                filename   text primary key,
                applied_at timestamptz not null default now()
            )
            """
        )
        for path in files:
            with conn.transaction():
                conn.execute("select pg_advisory_xact_lock(hashtext('amem_schema_migrations'))")
                done = conn.execute(
                    "select 1 from schema_migrations where filename = %s", (path.name,)
                ).fetchone()
                if done:
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("insert into schema_migrations (filename) values (%s)", (path.name,))
                applied_now.append(path.name)
    return applied_now
