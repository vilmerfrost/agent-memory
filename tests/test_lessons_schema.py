"""Migration 20261014030000_lessons.sql: tables, generated tsv + GIN, pgvector column, RLS."""

from __future__ import annotations

import uuid

import psycopg
import pytest

from agent_memory.config import MIGRATIONS_DIR

MIGRATION = MIGRATIONS_DIR / "20261014030000_lessons.sql"


def _slug() -> str:
    return f"schema-{uuid.uuid4().hex[:10]}"


def test_tables_and_columns_exist(pg_url: str) -> None:
    with psycopg.connect(pg_url) as conn:
        cols = dict(conn.execute(
            "select column_name, udt_name from information_schema.columns "
            "where table_schema = 'public' and table_name = 'lessons'"
        ).fetchall())
        uses = dict(conn.execute(
            "select column_name, udt_name from information_schema.columns "
            "where table_schema = 'public' and table_name = 'lesson_uses'"
        ).fetchall())
    for name in ("id", "slug", "title", "rule", "scope_repo", "scope_task", "status", "source_run_ids",
                 "evidence", "since", "tsv", "embedding", "created_at", "updated_at"):
        assert name in cols, name
    assert cols["source_run_ids"] == "_uuid"
    assert cols["tsv"] == "tsvector"
    assert cols["embedding"] == "vector"
    assert set(uses) >= {"id", "lesson_id", "run_id", "injected_at", "context", "outcome"}


def test_tsv_is_generated_and_gin_indexed(pg_url: str) -> None:
    slug = _slug()
    with psycopg.connect(pg_url) as conn:
        conn.execute(
            "insert into lessons (slug, title, rule, evidence) values (%s, %s, %s, %s)",
            (slug, "Workers refused compound commands", "split them", "the isolation check denied it"),
        )
        hit = conn.execute(
            "select tsv @@ websearch_to_tsquery('english', 'refusing isolation') from lessons where slug = %s",
            (slug,),
        ).fetchone()
        assert hit == (True,)
        idx = conn.execute(
            "select indexdef from pg_indexes where schemaname = 'public' and indexname = 'lessons_tsv_idx'"
        ).fetchone()
        assert idx is not None and "gin" in idx[0].lower()
        conn.rollback()


def test_status_and_outcome_checks(pg_url: str) -> None:
    with psycopg.connect(pg_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into lessons (slug, title, rule, status) values (%s, 't', 'r', 'reverted')",
                         (_slug(),))
    with psycopg.connect(pg_url) as conn:
        lid = conn.execute("insert into lessons (slug, title, rule) values (%s, 't', 'r') returning id",
                           (_slug(),)).fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into lesson_uses (lesson_id, outcome) values (%s, 'maybe')", (lid,))


def test_defaults_and_cascade(pg_url: str) -> None:
    slug = _slug()
    with psycopg.connect(pg_url) as conn:
        row = conn.execute(
            "insert into lessons (slug, title, rule) values (%s, 't', 'r') "
            "returning id, status, source_run_ids, embedding",
            (slug,),
        ).fetchone()
        lid, status, runs, embedding = row
        assert (status, runs, embedding) == ("trial", [], None)
        conn.execute("insert into lesson_uses (lesson_id, context) values (%s, 'ctx')", (lid,))
        conn.execute("delete from lessons where id = %s", (lid,))
        left = conn.execute("select count(*) from lesson_uses where lesson_id = %s", (lid,)).fetchone()
        assert left == (0,)


def test_rls_on_for_lesson_tables(pg_url: str) -> None:
    with psycopg.connect(pg_url) as conn:
        rows = dict(conn.execute(
            "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relname in ('lessons', 'lesson_uses')"
        ).fetchall())
    assert rows == {"lessons": True, "lesson_uses": True}


def test_migration_file_is_rerunnable(pg_url: str) -> None:
    # Every statement is guarded (if not exists / conditional do-blocks), so a second run is a no-op.
    with psycopg.connect(pg_url) as conn:
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        conn.rollback()


def test_revokes_run_when_supabase_roles_exist(pg_url: str) -> None:
    # Simulate Supabase's API roles inside a rolled-back transaction.
    with psycopg.connect(pg_url) as conn:
        for role in ("anon", "authenticated"):
            exists = conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone()
            if not exists:
                conn.execute(f"create role {role}")
            conn.execute(f"grant select on public.lessons, public.lesson_uses to {role}")
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        granted = conn.execute(
            "select count(*) from information_schema.role_table_grants "
            "where table_name in ('lessons', 'lesson_uses') and grantee in ('anon', 'authenticated')"
        ).fetchone()
        assert granted == (0,)
        conn.rollback()
