from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from agent_memory import store
from agent_memory.config import MIGRATIONS_DIR
from conftest import run_amem


def _routine() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def test_migrate_is_idempotent(db_env: dict[str, str], pg_url: str) -> None:
    # The session fixture already migrated once; running again applies nothing.
    first = run_amem("db", "migrate", env=db_env)
    second = run_amem("db", "migrate", env=db_env)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "(0 applied now)" in first.stdout
    assert "(0 applied now)" in second.stdout
    with psycopg.connect(pg_url) as conn:
        rows = conn.execute("select filename from schema_migrations order by filename").fetchall()
        expected = sorted(f.name for f in MIGRATIONS_DIR.glob("*.sql"))
        assert [r[0] for r in rows] == expected
        assert conn.execute("select to_regclass('public.runs') is not null").fetchone() == (True,)


def test_log_run_round_trip(db_env: dict[str, str]) -> None:
    routine = _routine()
    result = run_amem(
        "log-run", "--routine", routine, "--status", "fail",
        "--source", "launchd", "--summary", "2 of 3 sections", "--failure-reason", "timeout",
        "--report-path", "/tmp/report.md",
        "--started-at", "2026-10-12T06:00:00+02:00", "--finished-at", "2026-10-12T06:05:00+02:00",
        env=db_env,
    )
    assert result.returncode == 0, result.stderr
    run_id = uuid.UUID(result.stdout.strip())

    rows = store.recent_runs(routine, 5)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == run_id
    assert row["status"] == "fail"
    assert row["source"] == "launchd"
    assert row["summary"] == "2 of 3 sections"
    assert row["failure_reason"] == "timeout"
    assert row["report_path"] == "/tmp/report.md"
    assert row["started_at"] == datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
    assert row["finished_at"] == datetime(2026, 10, 12, 4, 5, tzinfo=timezone.utc)
    assert row["meta"] == {}


def test_recent_runs_newest_first_and_limited(db_env: dict[str, str]) -> None:
    routine = _routine()
    ids = [
        store.insert_run(routine, "pass", started_at=datetime(2026, 10, d, tzinfo=timezone.utc))
        for d in (1, 3, 2)
    ]
    rows = store.recent_runs(routine, 2)
    assert [r["id"] for r in rows] == [ids[1], ids[2]]


def test_status_check_rejects_junk(db_env: dict[str, str]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        store.insert_run(_routine(), "maybe")
    # The CLI refuses it before reaching the db.
    result = run_amem("log-run", "--routine", _routine(), "--status", "maybe", env=db_env)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_log_run_db_error_exits_nonzero(db_env: dict[str, str]) -> None:
    env = dict(db_env, AMEM_DB_URL="postgresql://postgres:postgres@127.0.0.1:1/postgres")
    result = run_amem("log-run", "--routine", _routine(), "--status", "pass", env=env)
    assert result.returncode == 1
    assert "log-run failed" in result.stderr


def test_rls_enabled_on_all_public_tables(pg_url):
    import psycopg

    with psycopg.connect(pg_url) as conn:
        rows = conn.execute(
            "select c.relname, c.relrowsecurity from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r'"
        ).fetchall()
    assert rows, "no public tables found"
    assert all(rls for _, rls in rows), f"RLS off on: {[n for n, rls in rows if not rls]}"
