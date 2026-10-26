"""Relationship contract (docs/data-use/relationships.md): `amem coverage links` counts linked, unlinked and
unresolvable rows per relationship from the existing link columns, read-only, on a fresh database seeded with
both kinds of rows."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from agent_memory import coverage_links as CL
from conftest import base_env, run_amem

REPO = Path(__file__).resolve().parents[1]
DB = "data_use_links"

ART = """insert into rsi_artifacts (hash, manifest_canonical, manifest, files, baseline_id)
         select encode(sha256(convert_to(m, 'UTF8')), 'hex'), m, m::jsonb, '{}', 'bl-1'
         from (select '{"format":"rsi-artifact/1","n":%s}'::text as m) x returning hash"""

SEED = [
    # parent <-> worker sessions: 1 linked, 1 unlinked, 1 unresolvable (parent not ingested yet); a chat is not eligible
    "insert into sessions (session_id, kind, project_dir) values ('p1', 'parent', 'x')",
    "insert into sessions (session_id, kind, project_dir, meta) values ('w1', 'worker', 'x', '{\"parent_session\": \"p1\"}')",
    "insert into sessions (session_id, kind, project_dir) values ('w2', 'worker', 'x')",
    "insert into sessions (session_id, kind, project_dir, meta) values ('w3', 'worker', 'x', '{\"parent_session\": \"gone\"}')",
    "insert into sessions (session_id, kind, project_dir) values ('c1', 'chat', 'x')",
    # sessions <-> commands
    "insert into execution_spans (span_id, session_id, tool, category, operation, status, measurement_kind, source) values"
    " ('sp1', 'p1', 'claude', 'test_full', 'pytest', 'passed', 'measured', 'claude.tool'),"
    " ('sp2', null, 'claude', 'command', 'git', 'passed', 'measured', 'hook'),"
    " ('sp3', 'late', 'codex', 'command', 'npm', 'failed', 'observed', 'codex.item')",
    # commands <-> test runs and sessions <-> test runs; cases (FK) with one run missing case rows
    "insert into test_timing_runs (run_id, session_id, parent_span_id, repo, selection, workers, started_at, ended_at,"
    " elapsed_ms, exit_code, collection_errors, worker_errors, tests_reported, tests_complete, outcomes, log_ref) values"
    " ('00000000-0000-4000-8000-000000000001', 'p1', 'sp1', 'r', 'full', 4, now(), now(), 1, 0, 0, 0, 2, 2, '{}', 'l'),"
    " ('00000000-0000-4000-8000-000000000002', null, null, 'r', 'custom', 1, now(), now(), 1, 0, 0, 0, 3, 3, '{}', 'l'),"
    " ('00000000-0000-4000-8000-000000000003', 'nope', 'sp-gone', 'r', 'custom', 1, now(), now(), 1, 1, 0, 0, 0, 0, '{}', 'l')",
    "insert into test_timing_cases (run_id, test_key, nodeid, outcome, complete) values"
    " ('00000000-0000-4000-8000-000000000001', repeat('a', 64), 't::a', 'passed', true),"
    " ('00000000-0000-4000-8000-000000000001', repeat('b', 64), 't::b', 'failed', true),"
    " ('00000000-0000-4000-8000-000000000002', repeat('c', 64), 't::c', 'passed', true)",
    # experiments <-> attempts (sandbox sessions), eval results, traces, scores
    "insert into rsi_experiments (id, kind) values ('exp-20261025-01', 'edit')",
    "insert into sessions (session_id, kind, project_dir, env, experiment_id) values"
    " ('s1', 'eval', 'x', 'sandbox', 'exp-20261025-01'), ('s2', 'eval', 'x', 'sandbox', null),"
    " ('s3', 'eval', 'x', 'eval', 'exp-20261025-99'),"
    " ('e1', 'eval', '-Users-v--cache-agent-evals-eval-jo-1', 'eval', null), ('e2', 'eval', 'x', 'eval', null)",
    # attempts (R1): linked and covering s1; linked to a missing experiment; unresolved with a reason
    "insert into rsi_attempts (id, status, experiment_id, arm, case_id, rep, attempt_session_id, session_id, source,"
    " reason) values ('exp-20261025-01/A/fix-add/0', 'linked', 'exp-20261025-01', 'A', 'fix-add', 0, 'u1', 's1',"
    " 'runner', null), ('exp-20261025-98/B/fix-add/0', 'linked', 'exp-20261025-98', 'B', 'fix-add', 0, 'u2', null,"
    " 'repair', null), ('unresolved:run:exp-20261025-01/r9', 'unresolved', 'exp-20261025-01', null, null, null,"
    " null, null, 'repair', 'run log without a sidecar: case and rep are not explicit')",
    "insert into rsi_evidence (system, kind, subject_refs, external_ref) values"
    " ('langfuse', 'trace', '{amem:experiment/exp-20261025-01}', 'lf:trace/' || repeat('1', 32)),"
    " ('langfuse', 'trace', '{}', 'lf:trace/' || repeat('2', 32)),"
    " ('langfuse', 'trace', '{amem:session/s1,amem:experiment/exp-20261025-77}', 'lf:trace/' || repeat('3', 32)),"
    " ('langfuse', 'score', '{amem:experiment/exp-20261025-01}', 'lf:dataset_run/exp-20261025-01-A'),"
    " ('langfuse', 'score', '{amem:session/s2}', 'lf:dataset_run/exp-x-B'),"
    " ('clickhouse', 'trace', '{}', 'ch:q.x/1')",
    # proposals, decisions, results; staged edits; undo requests; outcome checks
    "insert into edits (id, instruction_source, rule_id, status, meta) values"
    " ('00000000-0000-4000-8000-0000000000e1', 's', 'r', 'kept', '{}'),"
    " ('00000000-0000-4000-8000-0000000000e2', 's', 'r', 'staged', '{\"mode\": \"stage\"}'),"
    " ('00000000-0000-4000-8000-0000000000e3', 's', 'r', 'staged', '{\"mode\": \"stage\", \"proposal_id\": \"00000000-0000-4000-8000-0000000000ff\"}')",
    "insert into undo_requests (edit_id) values ('00000000-0000-4000-8000-0000000000e1')",
    "insert into undo_requests (edit_id, handled_at, result) values ('00000000-0000-4000-8000-0000000000e2', now(), 'reverted')",
]


@pytest.fixture(scope="module")
def links_db(pg_url: str) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"drop database if exists {DB} with (force)")
        conn.execute(f"create database {DB}")
    url = pg_url.rsplit("/", 1)[0] + f"/{DB}"
    try:
        assert run_amem("db", "migrate", env=base_env(AMEM_DB_URL=url)).returncode == 0
        with psycopg.connect(url) as conn:
            for sql in SEED:
                conn.execute(sql)
            h1 = conn.execute(ART % 1).fetchone()[0]
            h2 = conn.execute(ART % 2).fetchone()[0]
            p1 = conn.execute("insert into rsi_proposals (artifact_hash, title, targets, experiment_id) "
                              "values (%s, 'one', '{a}', 'exp-20261025-01') returning id", (h1,)).fetchone()[0]
            p2 = conn.execute("insert into rsi_proposals (artifact_hash, title, targets) values (%s, 'two', '{b}') "
                              "returning id", (h2,)).fetchone()[0]
            conn.execute("update edits set meta = jsonb_build_object('mode', 'stage', 'proposal_id', %s::text) "
                         "where id = '00000000-0000-4000-8000-0000000000e1'", (str(p1),))
            for pid, h, action in ((p1, h1, "reject"), (p2, h2, "apply")):
                conn.execute("insert into rsi_decisions (proposal_id, action, artifact_hash, source, verified, "
                             "expires_at) values (%s, %s, %s, 'cli', 'cli-tty', now() + interval '1 day')",
                             (pid, action, h))
            conn.execute("insert into rsi_decision_results (decision_id, result, handled_at) "
                         "select id, 'rejected', now() from rsi_decisions where proposal_id = %s", (p1,))
            for i, eval_exp in enumerate(("exp-20261025-01", None, "exp-20261025-55")):
                rec = json.dumps({"format": "rsi-eval/1", "i": i})
                conn.execute("insert into rsi_eval_results (artifact_hash, experiment_id, baseline_id, suite, "
                             "suite_version, verdict, record, record_sha) values (%s, %s, 'bl-1', 'replay', '1', "
                             "'pass', %s, %s)", (h1, eval_exp, rec, str(i) * 64))
            conn.execute("insert into rsi_outcomes (change_kind, change_id, rule_key, instruction_source, rule_id, "
                         "applied_at, window_days, verdict, reason) values "
                         "('proposal', %s, 's:r', 's', 'r', now(), 7, 'too-early', 'x'), "
                         "('edit', '00000000-0000-4000-8000-0000000000e1', 's:r', 's', 'r', now(), 7, 'helped', 'x'), "
                         "('proposal', '00000000-0000-4000-8000-00000000dead', 's:r', 's', 'r', now(), 7, 'mixed', 'x')",
                         (str(p1),))
        yield url
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f"drop database if exists {DB} with (force)")


EXPECTED = {   # key: (eligible, linked, unlinked, unresolvable)
    "parent_worker_sessions": (3, 1, 1, 1),
    "session_commands": (3, 1, 1, 1),
    "command_test_runs": (3, 1, 1, 1),
    "session_test_runs": (3, 1, 1, 1),
    "test_run_cases": (3, 3, 0, 0),
    "experiment_attempt_sessions": (5, 1, 3, 1),     # 3 attempt rows + s2 + s3; eval sessions excluded
    "experiment_eval_results": (3, 1, 1, 1),
    "experiment_traces": (3, 1, 1, 1),
    "experiment_scores": (2, 1, 1, 0),
    "decision_proposals": (2, 2, 0, 0),
    "decision_results": (2, 1, 1, 0),
    "proposal_edits": (3, 1, 1, 1),
    "edit_undo_requests": (2, 2, 0, 0),
    "change_outcomes": (3, 2, 0, 1),
}


def as_tuple(r: dict) -> tuple[int, int, int, int]:
    return (r["eligible"], r["linked"], r["unlinked"], r["unresolvable"])


def test_counts_on_mixed_linked_and_unlinked_rows(links_db: str) -> None:
    with psycopg.connect(links_db, autocommit=True) as conn:
        rows = {r["key"]: r for r in CL.counts(conn)}
    assert {k: as_tuple(r) for k, r in rows.items()} == EXPECTED
    assert rows["test_run_cases"]["runs_missing_cases"] == 1              # run 2 reported 3 tests, stored 1
    assert rows["edit_undo_requests"]["outcome_pending"] == 1
    att = rows["experiment_attempt_sessions"]                             # eval sessions are not attempts
    assert (att["excluded_eval_harness"], att["excluded_eval_other"]) == (1, 1) and "eval" in att["excluded"]
    assert rows["test_run_cases"]["enforced"] == "foreign key" and rows["session_commands"]["enforced"] == "none"
    assert {r["status"] for r in rows.values()} == {"gaps", "ok"}


def test_cli_links_json_matches_and_writes_nothing(links_db: str) -> None:
    def snapshot() -> dict[str, str]:
        """A content hash per public table (statistics counters lag; contents do not)."""
        with psycopg.connect(links_db) as conn:
            names = [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")]
            return {t: conn.execute(f"select md5(coalesce(string_agg(x::text, '|' order by x::text), '')) "
                                    f"from public.{t} x").fetchone()[0] for t in names}

    before = snapshot()
    out = run_amem("coverage", "links", "--json", env=base_env(AMEM_DB_URL=links_db))
    assert out.returncode == 0, out.stderr
    rows = {r["key"]: r for r in json.loads(out.stdout)}
    assert {k: as_tuple(r) for k, r in rows.items()} == EXPECTED
    assert snapshot() == before
    text = run_amem("coverage", "links", env=base_env(AMEM_DB_URL=links_db))
    assert "parent_worker_sessions" in text.stdout and "unresolvable=1" in text.stdout


def test_counts_refuse_a_busy_connection_and_report_missing_tables(links_db: str) -> None:
    with psycopg.connect(links_db) as conn:
        conn.execute("select 1")
        with pytest.raises(ValueError, match="idle connection"):
            CL.counts(conn)
        conn.rollback()
        conn.execute("alter table test_timing_cases rename to test_timing_cases_hidden")
        conn.commit()
        try:
            rows = {r["key"]: r for r in CL.counts(conn)}
            assert rows["test_run_cases"]["status"] == "missing_table"
            assert rows["test_run_cases"]["missing"] == ["test_timing_cases"]
            assert as_tuple(rows["session_commands"]) == EXPECTED["session_commands"]
        finally:
            conn.execute("alter table test_timing_cases_hidden rename to test_timing_cases")
            conn.commit()


def test_counts_are_read_only(links_db: str) -> None:
    """The read-only transaction is real: a write through it fails."""
    with psycopg.connect(links_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with conn.transaction(force_rollback=True):
                conn.execute("set transaction read only")
                conn.execute("insert into sessions (session_id, kind, project_dir) values ('ro', 'chat', 'x')")


def test_the_doc_lists_every_relationship() -> None:
    doc = (REPO / "docs" / "data-use" / "relationships.md").read_text()
    for r in CL.RELATIONSHIPS:
        assert f"`{r.key}`" in doc, r.key
