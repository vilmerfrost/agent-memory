"""Routine writers: record_best_effort and dataset_key (docs/data-use/coverage.md)."""

from __future__ import annotations

import psycopg
import pytest

from agent_memory import coverage as CV

from test_data_use_coverage import count, db, snap, use  # noqa: F401  (fixture and record builders)


def test_dataset_key_names_a_registered_family() -> None:
    assert CV.dataset_key("deviations", "deviations") == "deviations:deviations"
    with pytest.raises(KeyError):
        CV.dataset_key("no_such_family", "x")
    with pytest.raises(ValueError):
        CV.dataset_key("skills", "Bad Part")


def test_best_effort_writes_and_replays(db: dict[str, str]) -> None:
    with psycopg.connect(db["app"]) as conn:
        assert CV.record_best_effort(conn, [snap(), use()]) == []
        assert CV.record_best_effort(conn, [snap(), use()]) == []          # replay: same rows
    assert count(db["owner"], "coverage_snapshots") == 1 and count(db["owner"], "routine_consumption") == 1


def test_a_bad_record_never_breaks_the_job_transaction(db: dict[str, str]) -> None:
    with psycopg.connect(db["app"]) as conn:
        conn.execute("create temp table job_work (n int)")
        conn.execute("insert into job_work values (1)")
        bad = use(dataset="sessions", queried=1, considered=5, used=0)         # violates used <= considered <= queried
        problems = CV.record_best_effort(conn, [bad, snap(dataset="deviations")])
        assert len(problems) == 1 and "coverage sessions: ValueError" in problems[0]
        conn.execute("insert into job_work values (2)")                        # the job's transaction still works
        assert conn.execute("select count(*) from job_work").fetchone()[0] == 2
        conn.commit()
    assert count(db["owner"], "coverage_snapshots") == 1


def test_a_failing_insert_rolls_back_only_its_savepoint(db: dict[str, str]) -> None:
    with psycopg.connect(db["app"]) as conn:
        problems = CV.record_best_effort(conn, [snap(dataset="Not-Valid"), snap(dataset="skills:skill_calls")])
        assert len(problems) == 1 and "CheckViolation" in problems[0]
        conn.commit()
    assert count(db["owner"], "coverage_snapshots") == 1


def test_switch_off_writes_nothing(db: dict[str, str]) -> None:
    with psycopg.connect(db["app"]) as conn:
        assert CV.record_best_effort(conn, [snap(), use()], environ={"AMEM_COVERAGE_WRITE": "0"}) == []
        conn.commit()
    assert count(db["owner"], "coverage_snapshots") == 0 and count(db["owner"], "routine_consumption") == 0
