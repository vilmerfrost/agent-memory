from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from agent_memory import test_timings as T
from test_execution_timing import db  # noqa: F401
from test_dashboard import owner_db, identity, OWNER, OTHER  # noqa: F401
from conftest import base_env, run_amem


def phase(capture, when, *, nodeid="tests/test_a.py::test_a", duration=1.0,
          outcome="passed", xfail=False):
    r = SimpleNamespace(nodeid=nodeid, when=when, duration=duration,
                        failed=outcome == "failed", skipped=outcome == "skipped", worker_id="gw0")
    if xfail:
        r.wasxfail = "reason must not be saved"
    capture.report(r)


def batch():
    c = T.Capture(repo="agent-memory", head="a"*40, dirty=False, workers=4,
                  selection="full", log_ref="pytest-example.log")
    c.collected()
    for when, duration in (("setup", 2), ("call", 3), ("teardown", 1)):
        phase(c, when, duration=duration)
    return c.finish(0)


def test_capture_separates_phases_hashes_parameter_values_and_keeps_errors():
    c = T.Capture(repo="tests", head=None, dirty=None, workers=0, selection="custom", log_ref="a.log")
    for when in ("setup", "call", "teardown"):
        phase(c, when, nodeid="tests/test_a.py::test_a[PRIVATE-TOKEN]", outcome="failed" if when == "teardown" else "passed")
    result = c.finish(1)
    assert "PRIVATE-TOKEN" not in json.dumps(result)
    row, = result["tests"]
    assert row["outcome"] == "error" and row["complete"]
    assert row["total_ms"] == 3000
    assert result["run"]["outcomes"] == {"error": 1}


def test_skipped_and_interrupted_tests_do_not_gain_missing_phases():
    c = T.Capture(repo="tests", head=None, dirty=None, workers=0, selection="custom", log_ref="a.log")
    phase(c, "setup", outcome="skipped", xfail=True)
    phase(c, "teardown")
    phase(c, "setup", nodeid="tests/test_a.py::test_incomplete")
    result = T.validate(c.finish(2))
    a, b = result["tests"]
    assert a["call_ms"] is None and a["outcome"] == "xfailed" and a["complete"]
    assert b["outcome"] == "incomplete" and b["teardown_ms"] is None and not b["complete"]
    assert result["run"]["tests_complete"] == 1


@pytest.mark.parametrize("mutation", ["nan", "negative", "duplicate", "wrong_sum", "bad_counts"])
def test_invalid_batch_is_rejected_before_database_connection(monkeypatch, mutation):
    value = batch()
    if mutation == "nan": value["tests"][0]["call_ms"] = float("nan")
    if mutation == "negative": value["run"]["elapsed_ms"] = -1
    if mutation == "duplicate": value["tests"].append(deepcopy(value["tests"][0]))
    if mutation == "wrong_sum": value["tests"][0]["total_ms"] = 1
    if mutation == "bad_counts": value["run"]["tests_reported"] = 0
    monkeypatch.setattr(T.store, "connect", lambda *a, **k: pytest.fail("invalid input opened a DB connection"))
    with pytest.raises(ValueError): T.upload(value)


def test_upload_replay_phase_queries_and_session_link(db):
    value = batch()
    value["run"].update(session_id="codex:case-timing", parent_span_id="full-suite-command")
    value["tests"][0]["raw_output"] = "NEVER-SAVE"
    value["run"]["command"] = "NEVER-SAVE"
    T.upload(value, db_url=db)
    T.upload(value, db_url=db)
    result = T.report(session_id="codex:case-timing", db_url=db)
    assert result["run"]["run_id"] == __import__('uuid').UUID(value["run"]["run_id"])
    assert result["run"]["parent_span_id"] == "full-suite-command"
    assert len(result["slowest"]) == 1
    assert result["slowest"][0]["setup_ms"] == 2000
    assert result["slowest"][0]["call_ms"] == 3000
    assert "NEVER-SAVE" not in json.dumps(result, default=str)
    cli = run_amem("timing", "tests", "--run", value["run"]["run_id"], "--limit", "1",
                   env=base_env(AMEM_DB_URL=db))
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout)["slowest"][0]["call_ms"] == 3000


def test_failed_upload_retains_batch_and_replay_is_idempotent(db, tmp_path, monkeypatch):
    queue = tmp_path/"queue"
    value = batch()
    path = queue/f"run-{value['run']['run_id']}.json"
    T.atomic_json(path, value)
    assert path.stat().st_mode & 0o777 == 0o600
    original = T.upload
    def fail(*args, **kwargs): raise psycopg.OperationalError("offline")
    monkeypatch.setattr(T, "upload", fail)
    with pytest.raises(psycopg.OperationalError): T.flush_queue(queue, db_url=db)
    assert path.exists()
    monkeypatch.setattr(T, "upload", original)
    assert T.flush_queue(queue, db_url=db)["uploaded"] == 1
    assert not path.exists()
    T.atomic_json(path, value)  # crash after commit, before queue unlink
    assert T.flush_queue(queue, db_url=db)["uploaded"] == 1
    assert len(T.report(run_id=value["run"]["run_id"], db_url=db)["slowest"]) == 1


def test_invalid_queue_does_not_block_later_valid_batch(db, tmp_path):
    T.atomic_json(tmp_path/"run-000.json", {"schema": 999})
    value = batch()
    T.atomic_json(tmp_path/"run-100.json", value)
    assert T.flush_queue(tmp_path, db_url=db) == {"uploaded": 1, "invalid": 1, "pending": 0}
    assert (tmp_path/"run-000.invalid").exists()


def test_retention_removes_only_old_details_and_keeps_summary_on_replay(db):
    value = batch()
    T.upload(value, db_url=db)
    old = (datetime.now(UTC)-timedelta(days=31)).isoformat()
    with psycopg.connect(db) as conn:
        conn.execute("update test_timing_runs set started_at=%s,ended_at=%s where run_id=%s",
                     (old, old, value["run"]["run_id"]))
    assert T.prune(db_url=db) >= 1
    result = T.report(run_id=value["run"]["run_id"], db_url=db)
    assert result["run"]["tests_reported"] == 1 and not result["run"]["details_retained"]
    assert result["slowest"] == []
    T.upload(value, db_url=db)
    assert T.report(run_id=value["run"]["run_id"], db_url=db)["slowest"] == []


def test_retention_and_upload_work_with_least_privilege_role(db):
    value = batch()
    # Roles are cluster-global. Use the production role only inside a transaction
    # on this disposable database; it has no credentials and cannot log in here.
    with psycopg.connect(db) as conn:
        conn.execute("do $$ begin if not exists(select 1 from pg_roles where rolname='amem_app') "
                     "then create role amem_app nologin; end if; end $$")
        migration = Path(T.config.MIGRATIONS_DIR)/"20261026134831_test_timings.sql"
        # Apply the conditional grants/policies from the same migration, after
        # creating a role absent from the normal local Supabase-free fixture.
        sql = migration.read_text().split("do $$", 1)[1]
        if not conn.execute("select has_table_privilege('amem_app','test_timing_runs','insert')").fetchone()[0]:
            conn.execute("do $$"+sql)
    from psycopg.conninfo import make_conninfo
    role_db = make_conninfo(db, options="-c role=amem_app")
    T.upload(value, db_url=role_db)
    with psycopg.connect(db) as conn:
        assert conn.execute("select relrowsecurity from pg_class where oid='public.test_timing_cases'::regclass").fetchone() == (True,)
        assert conn.execute("select has_table_privilege('amem_app','test_timing_cases','delete')").fetchone() == (True,)
    assert T.report(run_id=value["run"]["run_id"], db_url=role_db)["slowest"]
    old = (datetime.now(UTC)-timedelta(days=31)).isoformat()
    with psycopg.connect(db) as conn:
        conn.execute("update test_timing_runs set started_at=%s,ended_at=%s where run_id=%s",
                     (old, old, value["run"]["run_id"]))
    T.prune(db_url=role_db)
    assert T.report(run_id=value["run"]["run_id"], db_url=role_db)["slowest"] == []


def test_owner_reads_timings_other_users_and_anon_cannot(owner_db):
    conn, _ = owner_db
    # Recreate only these new tables in the fixture's rolled-back transaction,
    # now that its Supabase auth stubs exist. This tests the real migration.
    conn.execute("drop table public.test_timing_cases,public.test_timing_runs")
    conn.execute((Path(T.config.MIGRATIONS_DIR)/"20261026134831_test_timings.sql").read_text())
    value = batch()["run"]
    conn.execute("insert into test_timing_runs ("+",".join(T.RUN_FIELDS)+") values ("+
                 ",".join(["%s"]*len(T.RUN_FIELDS))+")",
                 [T.Jsonb(value[k]) if k == "outcomes" else value.get(k) for k in T.RUN_FIELDS])
    conn.execute("insert into test_timing_cases(run_id,test_key,nodeid,outcome,complete) "
                 "values (%s,%s,'tests/test_a.py::test_a','passed',true)", (value["run_id"], "a"*64))
    for table in ("test_timing_runs", "test_timing_cases"):
        identity(conn, OWNER)
        assert conn.execute(f"select count(*) from {table}").fetchone() == (1,)
        identity(conn, OTHER)
        assert conn.execute(f"select count(*) from {table}").fetchone() == (0,)
        identity(conn, "", "anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(f"select * from {table}")
        identity(conn, OWNER)
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(f"delete from {table}")
