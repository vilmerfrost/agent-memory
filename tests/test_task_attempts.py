"""Ordinary task attempt identity and observations, without another table."""
from __future__ import annotations

import json
from pathlib import Path

import psycopg
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from agent_memory import tasks_ingest as T
from test_scoring_support import ScoringDb, sdb  # noqa: F401


def write(parent: Path, repo: str, *items):
    p = parent / repo / "tasks.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x) + "\n" for x in items))


def task(attempt="a", **extra):
    return {"id": "logical-1", "attempt_id": attempt, "producer_id": "host-A",
            "repo": "r", "session": "parent-1", "final_status": "PARTIAL",
            "started": "2026-10-26T10:00:00Z", "ended": "2026-10-26T10:01:00Z",
            "recorded": "2026-10-26T10:01:01Z", **extra}


def test_retries_resumes_and_repositories_have_distinct_attempts(tmp_path):
    a = task()
    b = task("b", retry_of="a", resumed_from="parent-1", session="parent-2", final_status="DONE")
    write(tmp_path, "r", a, b)
    write(tmp_path, "other", {**a, "repo": "other"})
    parsed = T.parse_parent(tmp_path)
    assert len(parsed.runs) == 3
    assert len({r.source_key for r in parsed.runs}) == 3
    assert {r.meta["logical_task_id"] for r in parsed.runs} == {"logical-1"}
    retry = next(r for r in parsed.runs if r.meta["attempt_id"] == "b")
    assert retry.meta["retry_of"] == "a" and retry.meta["resumed_from"] == "parent-1"
    assert retry.meta["acceptance"] == "unknown" and retry.status == "pass"


def test_source_execution_boundaries_are_preserved_without_guessing(tmp_path):
    a = task()
    a.pop("attempt_id")
    b = {**a, "session": "parent-2", "started": "2026-10-26T11:00:00Z"}
    write(tmp_path, "r", a, b, {"id": "unknown"})
    runs = T.parse_parent(tmp_path).runs
    assert len(runs) == 3
    assert sum(r.meta["attempt_identity_basis"] == "source-execution" for r in runs) == 2
    unknown = next(r for r in runs if r.meta["task_id"] == "unknown")
    assert unknown.meta["attempt_identity_basis"] == "unresolved"
    assert unknown.meta["session"] is None and unknown.meta["retry_of"] is None


def test_out_of_order_observations_do_not_downgrade_projection(tmp_path):
    partial = task()
    done = task(final_status="DONE", ended="2026-10-26T10:04:00Z", recorded="2026-10-26T10:04:01Z")
    write(tmp_path, "r", done, partial, done)
    parsed = T.parse_parent(tmp_path)
    assert parsed.duplicates == 2 and len(parsed.runs) == 1
    current = parsed.runs[0]
    assert current.status == "pass" and len(current.meta["observations"]) == 2
    assert {o["reported_result"] for o in current.meta["observations"].values()} == {"DONE", "PARTIAL"}


def test_worker_lifecycle_fields_are_preserved_not_reconstructed(tmp_path):
    worker = {"name": "w", "agent_id": "agent-1", "spawn_id": "call-1",
              "spawned": "2026-10-26T10:00:00Z", "last_handback": "2026-10-26T10:01:00Z",
              "final_status": "PARTIAL", "span_ids": ["span-1"], "info_pushes": 2,
              "denials_by": {"hook": 1}}
    write(tmp_path, "r", task(workers=[worker, {"name": "unknown"}]))
    first, unknown = T.parse_parent(tmp_path).runs[0].meta["workers"]
    assert first["spawn_id"] == "call-1" and first["span_ids"] == ["span-1"]
    assert first["spawned_at"] == "2026-10-26T10:00:00+00:00"
    assert first["reported_at"] == "2026-10-26T10:01:00+00:00"
    assert first["completed_at"] is None  # a report is not an independently observed completion
    assert first["info_pushes"] == 2 and first["denials_by"] == {"hook": 1}
    assert unknown["spawned_at"] is None and unknown["reported_at"] is None


def test_concurrent_first_insert_keeps_both_source_observations(sdb: ScoringDb,tmp_path):
    repo=tmp_path/'r';repo.mkdir()
    partial=T.task_run(task('concurrent'),repo,repo/'tasks.jsonl',1)
    done=T.task_run(task('concurrent',final_status='DONE',recorded='2026-10-26T10:05:00Z'),repo,repo/'tasks.jsonl',2)
    barrier=Barrier(2)
    def ingest(row):
        barrier.wait(timeout=10)
        return T.ingest([row],db_url=sdb.url)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(ingest,[partial,done]))
    with psycopg.connect(sdb.url) as c:
        records=c.execute("select status,meta from runs where source_key=%s",(partial.source_key,)).fetchall()
    assert len(records)==1 and records[0][0]=='pass'
    assert len(records[0][1]['observations'])==2
    assert records[0][1]['acceptance']=='unknown'


def test_ingest_preserves_attempt_history_and_is_idempotent(sdb: ScoringDb, tmp_path):  # noqa: F811
    write(tmp_path, "r", task(), task("b", retry_of="a", final_status="DONE"))
    parsed = T.parse_parent(tmp_path)
    assert T.ingest(parsed.runs).new == 2
    assert T.ingest(parsed.runs).unchanged == 2
    # A later delivery of an old observation does not erase DONE or another attempt.
    write(tmp_path, "r", task("b", final_status="PARTIAL", recorded="2026-10-26T09:00:00Z"))
    T.ingest(T.parse_parent(tmp_path).runs)
    with psycopg.connect(sdb.url) as conn:
        rows = conn.execute("select status,meta from runs order by meta->>'attempt_id'").fetchall()
    assert len(rows) == 2 and rows[1][0] == "pass"
    assert len(rows[1][1]["observations"]) == 2


def test_exact_legacy_row_is_adopted_without_changing_its_uuid(sdb: ScoringDb, tmp_path):  # noqa: F811
    write(tmp_path, "r", task())
    r = T.parse_parent(tmp_path).runs[0]
    with psycopg.connect(sdb.url) as conn:
        old_id = conn.execute("insert into runs (routine,source,source_key,status,started_at,meta) "
                              "values ('dispatch','dispatch',%s,'fail',%s,%s) returning id",
                              (r.meta["legacy_source_key"], r.started_at,
                               psycopg.types.json.Jsonb({"repo": "r", "task_id": "logical-1", "session": "parent-1"}))).fetchone()[0]
    T.ingest([r])
    with psycopg.connect(sdb.url) as conn:
        rows = conn.execute("select id,source_key,meta from runs").fetchall()
    assert len(rows) == 1 and rows[0][0] == old_id
    assert rows[0][1] == "dispatch:logical-1"
    assert rows[0][2]["canonical_source_key"] == r.source_key
    # Another explicit attempt can share session/start. It must not adopt and
    # overwrite the already-bound legacy row.
    write(tmp_path, "r", task("second", final_status="DONE"))
    T.ingest(T.parse_parent(tmp_path).runs)
    with psycopg.connect(sdb.url) as conn:
        rows = conn.execute("select meta->>'attempt_id' from runs order by 1").fetchall()
    assert rows == [("a",), ("second",)]
