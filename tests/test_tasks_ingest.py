"""`amem ingest tasks` against a fresh database (sdb, test_scoring_support): idempotency,
refresh on change, the source check, the CLI line, REST and AMEM_REQUIRE_REMOTE refusals."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import tasks_ingest
from conftest import base_env, run_amem
from test_scoring_support import ScoringDb, sdb  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tasks"


def _rows(url: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "select source_key, routine, source, status, started_at, finished_at, summary, failure_reason, meta "
            "from runs order by source_key").fetchall()


def _copy_fixtures(tmp_path: Path) -> Path:
    parent = tmp_path / "parent"
    shutil.copytree(FIXTURES, parent)
    return parent


def test_cli_ingests_real_fixtures_twice(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    parent = _copy_fixtures(tmp_path)
    first = run_amem("ingest", "tasks", "--parent-dir", str(parent), env=sdb.env)
    assert first.returncode == 0, first.stderr
    assert first.stdout == "ingested 41 new, 0 already present, 0 unparsed (3 repos)\n"
    second = run_amem("ingest", "tasks", "--parent-dir", str(parent), env=sdb.env)
    assert second.returncode == 0, second.stderr
    assert second.stdout == "ingested 0 new, 41 already present, 0 unparsed (3 repos)\n"
    assert "refreshed" not in second.stderr

    rows = _rows(sdb.url)
    assert len(rows) == 41
    assert {(r[1], r[2]) for r in rows} == {("dispatch", "dispatch")}
    by_key = {r[8]["legacy_source_key"]: r for r in rows}
    key, routine, source, status, started, finished, summary, reason, meta = by_key["dispatch:t-d2dce9a006"]
    assert status == "fail" and reason == "denied-command"
    assert summary == ("junk-orchestra: BLOCKED, 1 worker, dod_first_pass=false, denials=1 (heredoc), "
                       "reasons=denied-command")
    assert started.isoformat() == "2026-10-10T16:28:36.176000+00:00"
    assert finished.isoformat() == "2026-10-10T16:28:56.128000+00:00"
    assert meta["repo"] == "junk-orchestra" and meta["task_id"] == "t-d2dce9a006"
    assert meta["log_source"] == "tasks.jsonl" and meta["workers"][0]["denied_shapes"] == {"heredoc": 1}
    assert all(r[8]["repo"] for r in rows)

    # `amem runs --routine dispatch` shows them, newest first.
    shown = run_amem("runs", "--routine", "dispatch", "--limit", "2", env=sdb.env)
    assert shown.returncode == 0, shown.stderr
    lines = [json.loads(line) for line in shown.stdout.splitlines()]
    assert len(lines) == 2 and lines[0]["started_at"] >= lines[1]["started_at"]


def test_reingest_changes_nothing_unless_the_line_or_excerpt_changed(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    parent = tmp_path / "parent"
    repo = parent / "r"
    (repo / "checkpoints").mkdir(parents=True)
    tasks = repo / "tasks.jsonl"
    task = {"id": "t-1", "repo": "r", "final_status": "PARTIAL", "started": "2026-10-10T10:00:00Z",
            "ended": "2026-10-10T10:05:00Z", "blocked_partial_reasons": ["tests-failing"],
            "workers": [{"name": "w1", "final_status": "PARTIAL"}]}
    tasks.write_text(json.dumps(task) + "\n")

    parsed = tasks_ingest.parse_parent(parent)
    report = tasks_ingest.ingest(parsed.runs)
    assert (report.new, report.updated, report.unchanged) == (1, 0, 0)
    with psycopg.connect(sdb.url) as conn:  # a key someone else added to meta (e.g. a distill mark)
        conn.execute("update runs set meta = meta || '{\"distilled\": true}'::jsonb")
    (before,) = _rows(sdb.url)

    report = tasks_ingest.ingest(tasks_ingest.parse_parent(parent).runs)
    assert (report.new, report.updated, report.unchanged) == (0, 0, 1)
    assert _rows(sdb.url) == [before]

    # The worker's checkpoint appears: the excerpt changes, so the row is refreshed.
    (repo / "checkpoints" / "w1.md").write_text("### 10:04 — stuck\nDid: tests red\nOpen: flaky test\n")
    report = tasks_ingest.ingest(tasks_ingest.parse_parent(parent).runs)
    assert (report.new, report.updated, report.unchanged) == (0, 1, 0)
    (row,) = _rows(sdb.url)
    assert row[8]["workers"][0]["checkpoint_excerpt"] == "Open: flaky test"
    assert row[8]["distilled"] is True  # other meta keys are kept

    # The task line is rewritten with a new status: columns and meta follow.
    task.update(final_status="DONE", blocked_partial_reasons=[])
    tasks.write_text(json.dumps(task) + "\n")
    result = run_amem("ingest", "tasks", "--parent-dir", str(parent), env=sdb.env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ingested 0 new, 1 already present, 0 unparsed (1 repos)\n"
    assert "refreshed 1 task(s)" in result.stderr
    (row,) = _rows(sdb.url)
    assert (row[3], row[7], row[8]["final_status"]) == ("pass", None, "DONE")
    assert row[6] == "r: DONE, 1 worker, dod_first_pass=null, denials=0"


def test_cli_reports_unparsed_lines(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    parent = tmp_path / "parent"
    (parent / "a").mkdir(parents=True)
    (parent / "a" / "tasks.jsonl").write_text('{"id": "t-1"}\nnot json\n{"no": "id"}\n')
    result = run_amem("ingest", "tasks", "--parent-dir", str(parent), env=sdb.env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ingested 1 new, 0 already present, 2 unparsed (1 repos)\n"
    assert "unparsed task lines: a/tasks.jsonl:2, a/tasks.jsonl:3" in result.stderr
    ((_, _, _, status, *_rest, meta),) = _rows(sdb.url)
    assert status == "error" and meta["raw_status"] is None and meta["repo"] == "a"


def test_cli_missing_parent_dir_exits_1(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    result = run_amem("ingest", "tasks", "--parent-dir", str(tmp_path / "nope"), env=sdb.env)
    assert result.returncode == 1
    assert "no such directory" in result.stderr and result.stdout == ""


def test_rest_backend_is_refused(tmp_path: Path) -> None:
    parent = _copy_fixtures(tmp_path)
    env = base_env(AMEM_BACKEND="rest", AMEM_SUPABASE_URL="http://127.0.0.1:9", AMEM_SUPABASE_KEY="stub")
    result = run_amem("ingest", "tasks", "--parent-dir", str(parent), env=env)
    assert result.returncode == 1
    assert "needs the Postgres backend" in result.stderr and result.stdout == ""


def test_require_remote_refuses_a_local_db(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    parent = _copy_fixtures(tmp_path)
    result = run_amem("ingest", "tasks", "--parent-dir", str(parent), env={**sdb.env, "AMEM_REQUIRE_REMOTE": "1"})
    assert result.returncode == 3
    assert "AMEM_REQUIRE_REMOTE=1" in result.stderr
    assert _rows(sdb.url) == []


def test_source_check_allows_dispatch_only_as_a_new_value(sdb: ScoringDb) -> None:  # noqa: F811
    with psycopg.connect(sdb.url) as conn:
        for source in ("launchd", "scheduled-task", "cloud", "manual", "dispatch", None):
            conn.execute("insert into runs (routine, source, status) values ('x', %s, 'pass')", (source,))
    with pytest.raises(psycopg.errors.CheckViolation), psycopg.connect(sdb.url) as conn:
        conn.execute("insert into runs (routine, source, status) values ('x', 'bogus', 'pass')")
    with psycopg.connect(sdb.url) as conn:
        rls = conn.execute("select relrowsecurity from pg_class where relname = 'runs'").fetchone()
    assert rls == (True,)
