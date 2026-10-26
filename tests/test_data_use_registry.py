"""Data-family registry (docs/data-use/data-families.md): every migration table has exactly one family or an
explicit exclusion, a new unassigned table fails the check, and the checked-in doc is the generated one."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import psycopg
import pytest

from agent_memory import config, data_families as DF
from conftest import base_env, run_amem

REPO = Path(__file__).resolve().parents[1]


def copy_migrations(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    shutil.copytree(config.MIGRATIONS_DIR, d)
    return d


def test_every_migration_table_and_view_has_exactly_one_home() -> None:
    assert DF.check() == []
    schema = DF.migration_schema()
    assert {"sessions", "runs", "test_timing_cases", "coverage_snapshots", "routine_consumption",
            "amem_private.dashboard_owner"} <= schema.tables
    assert {"session_groups", "rsi_decision_status", "session_timing_coverage"} <= schema.views
    claimed = DF.assignments()
    assert all(len(claimed[t]) == 1 for t in schema.tables | schema.views)


def test_a_new_unassigned_table_fails_the_check(tmp_path: Path) -> None:
    d = copy_migrations(tmp_path)
    (d / "29990101000000_fake.sql").write_text(
        "-- create table public.only_in_a_comment (x int);\n"
        "create table if not exists public.mystery_rows (id uuid primary key);\n"
        "create or replace view public.mystery_view as select 1 as x;\n")
    problems = DF.check(d)
    assert any(p.startswith("mystery_rows: created by a migration but assigned to no family") for p in problems)
    assert any(p.startswith("mystery_view: created by a migration") for p in problems)
    assert not any("only_in_a_comment" in p for p in problems)          # comments are not schema
    assert DF.check() == []                                              # the real migrations stay clean


def test_rename_and_drop_follow_the_migrations(tmp_path: Path) -> None:
    d = copy_migrations(tmp_path)
    (d / "29990101000000_rename.sql").write_text("alter table public.handoffs rename to handoffs_v2;\n"
                                                  "drop table if exists public.labels;\n")
    problems = DF.check(d)
    assert "handoffs_v2: created by a migration but assigned to no family and not excluded" in problems
    assert "handoffs: assigned to coordination but no migration creates it" in problems
    assert "labels: assigned to routine_runs but no migration creates it" in problems


def test_an_exclusion_needs_a_reason_and_no_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = copy_migrations(tmp_path)
    (d / "29990101000000_scratch.sql").write_text("create table public.scratch_import (x int);\n")
    monkeypatch.setattr(DF, "EXCLUDED", {"scratch_import": "one-off import buffer, dropped after use"})
    assert DF.check(d) == []
    monkeypatch.setattr(DF, "EXCLUDED", {"scratch_import": " "})
    assert DF.check(d) == ["scratch_import: excluded without a reason"]
    monkeypatch.setattr(DF, "EXCLUDED", {"runs": "would hide a learning table"})
    assert "runs: excluded but also assigned to routine_runs" in DF.check()


def test_a_table_claimed_twice_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    dup = DF.Family(key="dup", title="Dup", purpose="p", producers=("x",), owner_job="j", tables=("runs",),
                    retention="r", exclusion="e")
    monkeypatch.setattr(DF, "FAMILIES", (*DF.FAMILIES, dup))
    assert "runs: assigned to more than one family (routine_runs, dup)" in DF.check()


def test_every_family_is_documented_and_points_at_real_code() -> None:
    plan = {"sessions", "models_costs", "command_timings", "test_timings", "skills", "deviations", "routine_runs",
            "coordination", "lessons", "experiments", "decisions", "reports_checkpoints", "evidence_links",
            "auth_recovery", "config_assets"}                              # the 15 families of plan section 3
    assert plan <= {f.key for f in DF.FAMILIES}
    for f in DF.FAMILIES:
        assert f.producers and f.owner_job and f.retention and f.exclusion, f.key
        for c in f.consumers:
            assert (REPO / c.where).exists(), (f.key, c.where)
    auth = DF.family("auth_recovery")
    assert not auth.learning_input and "rsi_totp_used" in auth.tables and "Never a learning input" in auth.exclusion
    assert [f.key for f in DF.gaps()] == ["models_costs", "command_timings", "test_timings", "lessons",
                                          "reports_checkpoints", "eval_drafts"]


def test_the_doc_is_the_generated_registry() -> None:
    doc = REPO / "docs" / "data-use" / "data-families.md"
    assert doc.read_text() == DF.render_markdown(), "regenerate: amem coverage families --markdown"


def test_cli_families_json_and_text() -> None:
    env = base_env()
    out = run_amem("coverage", "families", "--json", env=env)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["problems"] == [] and data["excluded"] == {}
    assert len(data["families"]) == len(DF.FAMILIES)
    tt = next(f for f in data["families"] if f["key"] == "test_timings")
    assert tt["stage_e_gap"] and tt["tables"] == ["test_timing_runs", "test_timing_cases"]
    text = run_amem("coverage", "families", env=env)
    assert text.returncode == 0 and "test_timings" in text.stdout and "[no analysis consumer]" in text.stdout


def test_the_migrated_database_has_no_unclassified_table(pg_url: str) -> None:
    """The parser could miss a table created some other way: the real catalog must match the registry too."""
    with psycopg.connect(pg_url) as conn:
        tables = {r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")}
        views = {r[0] for r in conn.execute("select viewname from pg_views where schemaname = 'public'")}
        private = {f"amem_private.{r[0]}" for r in
                   conn.execute("select tablename from pg_tables where schemaname = 'amem_private'")}
    claimed = DF.assignments()
    assert {t for t in tables | views | private if t not in claimed and t not in DF.EXCLUDED} == set()
