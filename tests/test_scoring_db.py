"""scores/labels migration, RLS, and `amem label` (incl. relabel overwrite)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import psycopg
import pytest

from agent_memory.config import MIGRATIONS_DIR
from conftest import run_amem
from test_scoring_support import ScoringDb, add_run, add_score, sdb  # noqa: F401

MIGRATION = MIGRATIONS_DIR / "20261014020000_scores_labels.sql"
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_tables_and_constraints_exist(sdb: ScoringDb) -> None:
    with psycopg.connect(sdb.url) as conn:
        cols = dict(conn.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = 'public' and table_name = 'scores'"
        ).fetchall())
        assert cols == {
            "id": "uuid", "run_id": "uuid", "question": "text", "kind": "text", "value": "text",
            "value_num": "double precision", "confidence": "double precision",
            "probabilities": "jsonb", "jev_mode": "text", "created_at": "timestamp with time zone",
        }
        cols = dict(conn.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = 'public' and table_name = 'labels'"
        ).fetchall())
        assert cols == {"id": "uuid", "run_id": "uuid", "verdict": "text", "note": "text",
                        "labeled_at": "timestamp with time zone"}


def test_unique_checks_and_cascade(sdb: ScoringDb) -> None:
    run = add_run(sdb.url, "t-routine", "fail", "x", T0)
    add_score(sdb.url, run, "verdict", "choice", "ok", None, 0.9, {"ok": 0.9})
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_score(sdb.url, run, "verdict", "choice", "ok", None, 0.9)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_score(sdb.url, run, "other", "yesno", "1", 1.0, None)
    with pytest.raises(psycopg.errors.CheckViolation):
        add_score(sdb.url, run, "other", "choice", "ok", None, 1.5)
    with psycopg.connect(sdb.url) as conn:
        conn.execute("insert into labels (run_id, verdict) values (%s, 'up')", (run,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("insert into labels (run_id, verdict) values (%s, 'down')", (run,))
    with psycopg.connect(sdb.url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into labels (run_id, verdict) values (%s, 'meh')", (run,))
    with psycopg.connect(sdb.url) as conn:
        conn.execute("delete from runs where id = %s", (run,))
        assert conn.execute("select count(*) from scores").fetchone() == (0,)
        assert conn.execute("select count(*) from labels").fetchone() == (0,)


def test_rls_on_scores_and_labels(sdb: ScoringDb) -> None:
    with psycopg.connect(sdb.url) as conn:
        rows = dict(conn.execute(
            "select c.relname, c.relrowsecurity from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r'"
        ).fetchall())
    assert rows["scores"] is True and rows["labels"] is True
    assert all(rows.values()), rows


def test_revokes_api_roles_when_they_exist(sdb: ScoringDb) -> None:
    """On Supabase, anon/authenticated exist: the migration revokes their table rights."""
    created = []
    with psycopg.connect(sdb.url, autocommit=True) as conn:
        for role in ("anon", "authenticated"):
            if conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone() is None:
                conn.execute(f"create role {role} nologin")
                created.append(role)
            conn.execute(f"grant all on public.scores, public.labels to {role}")
        try:
            conn.execute(MIGRATION.read_text(encoding="utf-8"))  # idempotent re-apply
            for role in ("anon", "authenticated"):
                for table in ("public.scores", "public.labels"):
                    for priv in ("select", "insert", "update", "delete"):
                        got = conn.execute("select has_table_privilege(%s, %s, %s)", (role, table, priv)).fetchone()
                        assert got == (False,), (role, table, priv)
        finally:
            for role in created:
                conn.execute(f"drop owned by {role}")
                conn.execute(f"drop role {role}")


def test_label_then_relabel_overwrites(sdb: ScoringDb) -> None:
    run = add_run(sdb.url, "t-routine", "fail", "x", T0)
    first = run_amem("label", str(run), "down", "--note", "real problem", env=sdb.env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == f"labeled {run} down (new)"

    second = run_amem("label", str(run), "up", "--note", "was noise", env=sdb.env)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == f"labeled {run} up (relabeled)"

    third = run_amem("label", str(run), "up", env=sdb.env)  # no --note clears the note
    assert third.returncode == 0, third.stderr
    with psycopg.connect(sdb.url) as conn:
        rows = conn.execute("select verdict, note from labels where run_id = %s", (run,)).fetchall()
    assert rows == [("up", None)]


def test_label_rejects_bad_input(sdb: ScoringDb) -> None:
    bad_id = run_amem("label", "not-a-uuid", "up", env=sdb.env)
    assert bad_id.returncode == 2 and "not a run id" in bad_id.stderr
    bad_verdict = run_amem("label", str(uuid.uuid4()), "meh", env=sdb.env)
    assert bad_verdict.returncode == 2 and "invalid choice" in bad_verdict.stderr
    unknown = uuid.uuid4()
    missing = run_amem("label", str(unknown), "up", env=sdb.env)
    assert missing.returncode == 1
    assert f"no run with id {unknown}" in missing.stderr


def test_write_commands_respect_require_remote(sdb: ScoringDb) -> None:
    env = dict(sdb.env, AMEM_REQUIRE_REMOTE="1")
    for args in (("score",), ("label", str(uuid.uuid4()), "up")):
        result = run_amem(*args, env=env)
        assert result.returncode == 3, (args, result.stderr)
        assert "AMEM_REQUIRE_REMOTE=1" in result.stderr
