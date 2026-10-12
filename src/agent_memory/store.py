"""Run log storage (psycopg 3) and the SQL migration runner.

`insert_run`, `ingest_runs` and `recent_runs` go to `backend_rest` when
`config.backend()` is `rest` and no explicit `db_url` is passed. Migrations are
always Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import backend_rest, config
from agent_memory.routines_log import ParsedRun

STATUSES = ("pass", "fail", "skip", "error")
SOURCES = ("launchd", "scheduled-task", "cloud", "manual")


def _rest() -> bool:
    """True when the run functions below go over HTTPS (see backend_rest)."""
    return config.backend() == "rest"


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
    if db_url is None and _rest():
        return backend_rest.insert_run(
            routine, status, source=source, started_at=started_at, finished_at=finished_at,
            summary=summary, failure_reason=failure_reason, report_path=report_path, meta=meta,
        )
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


_INGEST_SQL = """
insert into runs (routine, source, started_at, status, summary, report_path, meta, source_key)
values (%s, %s, %s, %s, %s, %s, %s, %s)
on conflict (source_key) do update
    set meta = runs.meta || jsonb_build_object('failures', excluded.meta -> 'failures')
    where runs.meta -> 'failures' is distinct from excluded.meta -> 'failures'
returning (xmax = 0) as inserted
"""


def ingest_runs(rows: Iterable[ParsedRun], *, db_url: str | None = None) -> tuple[int, int]:
    """Insert parsed rows keyed by `source_key`, in one transaction.

    Returns (new, already_present). A row that is already present is left alone,
    except that its `meta.failures` is refreshed when the source now has more
    detail lines (an ingest that raced a routine still writing its details).
    Other `meta` keys are never overwritten.
    """
    if db_url is None and _rest():
        return backend_rest.ingest_runs(rows)
    new = present = 0
    with connect(db_url) as conn:
        for r in rows:
            got = conn.execute(
                _INGEST_SQL,
                (r.routine, r.source, r.started_at, r.status, r.summary,
                 r.report_path, Jsonb(r.meta), r.source_key),
            ).fetchone()
            if got is not None and got[0]:
                new += 1
            else:
                present += 1
    return new, present


def recent_runs(routine: str, limit: int = 10, *, db_url: str | None = None) -> list[dict[str, Any]]:
    """Newest runs first for one routine."""
    if db_url is None and _rest():
        return backend_rest.recent_runs(routine, limit)
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
