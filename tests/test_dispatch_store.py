"""dispatch-events.jsonl upload into dispatch_runs / usage_snapshots / dispatch_estimates."""
from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import uuid

import psycopg
import pytest

from agent_memory import dispatch_estimates as D, dispatch_store, timing_collector, timing_store
from synth_support import fresh_db
from test_dispatch_estimates import T0, add_run, home, write_vitals  # noqa: F401  (fixtures)


@pytest.fixture(scope="module")
def db(pg_url: str):
    with fresh_db(pg_url) as url:
        yield url


def q(db: str, sql: str, *args):
    with psycopg.connect(db) as conn:
        return conn.execute(sql, args).fetchall()


def test_tables_have_rls_and_no_public_grants(db: str) -> None:
    for table in ("dispatch_runs", "usage_snapshots", "dispatch_estimates"):
        assert q(db, "select relrowsecurity from pg_class where oid=%s::regclass", f"public.{table}") == [(True,)]
        assert q(db, "select count(*) from information_schema.role_table_grants "
                     "where table_name=%s and grantee='PUBLIC'", table) == [(0,)]
    assert q(db, "select count(*) from pg_views where viewname='dispatch_run_actuals'") == [(1,)]


def test_real_start_end_cycle_uploads_idempotently(db: str, home: Path, tmp_path: Path) -> None:
    started = D.start("demo", 2, brief_chars=1200, files_claimed=4, session="parent-session")
    write_vitals(tmp_path / "vitals", claude_session=31)
    D.end(session="parent-session", integration_test_ms=93_000)
    cursor = home / "parent" / "dispatch-cursors.json"
    report = timing_store.ingest_spools(root=home / "parent", kind="dispatch", db_url=db, cursor_path=cursor)
    assert report.invalid == 0 and report.records == 1 + 4 + 1 + 1 + 4
    assert report.new == 1 + 4 + 1 + 4 and report.updated == 1     # end updates the started run
    run = q(db, "select repo,parent_tool,n_workers,brief_chars,files_claimed,status,integration_test_ms,"
                "concurrent_sessions,duration_s is not null,meta->'concurrent_by_tool' "
                "from dispatch_runs where run_id=%s", started["run_id"])
    assert run == [("demo", "claude", 2, 1200, 4, "done", 93_000, 0, True, {"claude": 0, "codex": 0})]
    rows = q(db, "select limit_tool,limit_name,usage_delta,clean from dispatch_run_actuals where run_id=%s "
                 "order by limit_tool,limit_name", started["run_id"])
    assert ("claude", "5-hour session", 6, True) in rows and len(rows) == 4
    assert q(db, "select est_minutes,basis_runs,method_version from dispatch_estimates where run_id=%s",
             started["run_id"]) == [(None, 0, D.METHOD)]
    # Replay from scratch (cursor lost): nothing new, nothing changed.
    cursor.unlink()
    again = timing_store.ingest_spools(root=home / "parent", kind="dispatch", db_url=db, cursor_path=cursor)
    assert (again.new, again.updated) == (0, 0)


def test_end_before_start_and_orphans_do_not_break_ingest(db: str, tmp_path: Path) -> None:
    run_id = str(uuid.uuid4())
    orphan = str(uuid.uuid4())
    at = D.iso(T0)
    lines = [
        dict(v=1, type="snapshot", run_id=orphan, phase="start", snapshot_id=str(uuid.uuid4()), tool="claude",
             account_hash="cd" * 8, limit_kind="session", limit_name="5-hour session", used_percent=4,
             resets_at=None, taken_at=at),
        dict(v=1, type="estimate", run_id=orphan, at=at, est_minutes=5, est_slow_minutes=9, est_usage={},
             basis_runs=0, method_version=D.METHOD),
        dict(v=1, type="end", run_id=run_id, repo="r", session="s", tool="codex", dispatched_at=at,
             at=D.iso(T0 + timedelta(minutes=9)), n_workers=1, status="partial", concurrent=1,
             concurrent_by_tool={"claude": 0, "codex": 1}),
        dict(v=1, type="start", run_id=run_id, repo="r", session="s", tool="codex", at=at, n_workers=1,
             concurrent=0, concurrent_by_tool={"claude": 0, "codex": 0}),
        dict(v=1, type="tokens", run_id=run_id, tokens_fresh=1000, tokens_cache_read=50),
        dict(v=2, type="start", run_id=run_id),                                   # unknown schema
        dict(v=1, type="snapshot", run_id=run_id, phase="start", snapshot_id=str(uuid.uuid4()), tool="claude",
             account_hash="raw@example.invalid", limit_kind="session", limit_name="x", used_percent=1, taken_at=at),
        dict(v=1, type="end", run_id=run_id, repo="r", tool="codex", dispatched_at=at,
             at=D.iso(T0 - timedelta(minutes=1)), n_workers=1, status="done"),     # briefed before dispatch
    ]
    path = tmp_path / "r" / D.SPOOL
    path.parent.mkdir()
    path.write_text("".join(json.dumps(r) + "\n" for r in lines) + "x" * 40000 + "\n")
    report = timing_store.ingest_spools([path], kind="dispatch", db_url=db)
    assert report.invalid == 4 and report.records == 5
    assert q(db, "select status,briefed_at is not null,concurrent_sessions,tokens_fresh,tokens_cache_read "
                 "from dispatch_runs where run_id=%s", run_id) == [("partial", True, 1, 1000, 50)]
    assert q(db, "select count(*) from usage_snapshots where run_id=%s", orphan) == [(0,)]
    assert q(db, "select count(*) from dispatch_estimates where run_id=%s", orphan) == [(0,)]


def test_parse_record_rejects_bad_shapes() -> None:
    good = dict(v=1, type="start", run_id=str(uuid.uuid4()), repo="r", tool="claude", at=D.iso(T0), n_workers=1)
    assert dispatch_store.parse_record(good)[0] == "start"
    for change in ({"tool": "gpt"}, {"n_workers": -1}, {"n_workers": True}, {"repo": ""}, {"run_id": "nope"},
                   {"at": "yesterday"}, {"brief_chars": "9"}):
        assert dispatch_store.parse_record({**good, **change}) is None, change


def test_db_source_reads_worker_tokens_in_the_run_window(db: str) -> None:
    with psycopg.connect(db) as conn:
        for sid, parent, offset, tokens in (("w1", "par", 1, 100), ("w2", "par", 5, 200),
                                             ("late", "par", 300, 999), ("other", "x", 1, 999)):
            conn.execute("insert into sessions(session_id,kind,tool,project_dir,started_at,meta,tokens_input,"
                         "tokens_output,tokens_cache_creation,tokens_cache_read,tokens_updated_at) "
                         "values (%s,'worker','claude','p',%s,%s::jsonb,%s,1,2,1000,now())",
                         (sid, T0 + timedelta(minutes=offset), json.dumps({"parent_session": parent}), tokens))
        source = dispatch_store.DbSource(conn)
        found = source.tokens({"session": "par", "dispatched_at": D.iso(T0),
                               "briefed_at": D.iso(T0 + timedelta(minutes=30))})
    assert found == (100 + 3 + 200 + 3, 2000, 2)


def test_collector_flush_uploads_dispatch_spools_and_retries_after_failure(db: str, home: Path) -> None:
    run_id = add_run(home / "parent", minutes=12)
    parent = home / "parent"
    with pytest.raises(psycopg.OperationalError):
        timing_collector.flush_dispatch(parent, {}, db_url="postgresql://nobody@127.0.0.1:1/none")
    signatures = timing_collector.flush_dispatch(parent, {}, db_url=db)
    assert list(signatures) == [parent / "demo" / D.SPOOL]
    assert q(db, "select status from dispatch_runs where run_id=%s", run_id) == [("done",)]
    assert timing_collector.flush_dispatch(parent, signatures, db_url=db) == signatures
    assert (parent / "dispatch-cursors.json").exists()
