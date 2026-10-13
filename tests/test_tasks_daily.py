"""The `tasks` step of `amem daily`: runs after the git-ref ingest, a failure is a
failed step that never stops later steps, `tasks=N` in the house log line.

The daily_io fixture points the routines log into tmp_path/parent, so the step
reads tasks from there, never from the real ~/.claude/parent.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from agent_memory import daily, tasks_ingest
from test_daily_support import LOG_LINE_RE, DailyIO, daily_io  # noqa: F401
from test_scoring_support import ScoringDb, add_run, sdb  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tasks"
NOW = datetime(2026, 10, 13, 9, 20, 4, tzinfo=timezone(timedelta(hours=2)))
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _dispatch_count(url: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute("select count(*) from runs where routine = 'dispatch'").fetchone()
    assert row is not None
    return int(row[0])


def _seed_parent(daily_io: DailyIO) -> Path:  # noqa: F811
    parent = daily_io.routines_log.parent
    shutil.copytree(FIXTURES, parent, dirs_exist_ok=True)
    return parent


def test_tasks_step_ingests_and_logs_tasks_count(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    _seed_parent(daily_io)
    add_run(sdb.url, "t-routine", "fail", "edge below", T0)  # scoring still runs after the tasks step

    r = daily.run_daily(push=False, now=NOW)
    assert r.failures == [] and r.verdict == "PASS"
    assert r.tasks == 41 and r.scored == 1
    assert _dispatch_count(sdb.url) == 41
    (line,) = daily_io.log_lines()
    assert re.fullmatch(LOG_LINE_RE, line)
    assert " amem-daily PASS scored=1 escalations=1 new=1 push=none tasks=41 " in line
    err = capsys.readouterr().err
    assert "tasks: ingested 41 new, 0 already present, 0 unparsed (3 repos)" in err
    # The step sits right after the git-ref ingest and before scoring.
    assert err.index("daily: ingest: 0 new, 0 already present") < err.index("daily: tasks: ")

    r = daily.run_daily(push=False, now=NOW)
    assert (r.verdict, r.tasks) == ("PASS", 0)
    assert daily_io.log_lines()[-1].split()[-2] == "tasks=0"
    assert _dispatch_count(sdb.url) == 41


def test_tasks_step_failure_is_recorded_and_later_steps_run(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_parent(daily_io)
    add_run(sdb.url, "t-routine", "fail", "edge below", T0)

    def boom(*_a: object, **_k: object) -> tasks_ingest.IngestReport:
        raise RuntimeError("connection lost\nmid-write")

    monkeypatch.setattr(tasks_ingest, "ingest", boom)
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL" and r.tasks == 0
    assert r.failures == ["tasks: RuntimeError: connection lost mid-write"]
    assert r.scored == 1 and r.escalations == 1  # score and escalations still ran
    head, fail = daily_io.log_lines()
    assert re.fullmatch(LOG_LINE_RE, head) and " tasks=0 " in head
    assert fail.endswith(" amem-daily-fail tasks: RuntimeError: connection lost mid-write")


def test_no_tasks_files_is_not_a_failure(sdb: ScoringDb, daily_io: DailyIO) -> None:  # noqa: F811
    r = daily.run_daily(push=False, now=NOW)
    assert (r.verdict, r.tasks) == ("PASS", 0)
    assert _dispatch_count(sdb.url) == 0


def test_tasks_step_needs_postgres(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_parent(daily_io)
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")  # AMEM_DB_URL is the local test container
    r = daily.run_daily(push=False, now=NOW)
    assert "tasks: AMEM_REQUIRE_REMOTE=1 refused a local or empty database" in r.failures
    assert _dispatch_count(sdb.url) == 0


def test_explicit_parent_dir(sdb: ScoringDb, daily_io: DailyIO, tmp_path: Path) -> None:  # noqa: F811
    other = tmp_path / "elsewhere"
    shutil.copytree(FIXTURES / "pipeline-poc", other / "pipeline-poc")
    r = daily.run_daily(push=False, now=NOW, parent_dir=other)
    assert (r.verdict, r.tasks) == ("PASS", 5)
