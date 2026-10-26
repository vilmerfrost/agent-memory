"""Mission 004B: the investigator's six questions (questions.py) and the Supabase question queries
(sb_queries.py), on disposable Postgres as amem_app. ClickHouse, Langfuse and past.dev are stand-ins that
answer with the real named-query specs, so the plans, statuses and re-reads are what is under test; each
adapter's own queries are tested in its own file. This module uses a private database because
promotion_history counts every production session in its time window."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import cli
from agent_memory.integrations import clickhouse as CH, evidence as E, langfuse as LF, pastdev as PD, questions as Q
from synth_support import fresh_db

APP_PW = "questions-pw-0123456789"
RULE = "claude-md:global:q-quote-globs"
EXP = "exp-20261016-q1"
TRACE = "ab" * 16
MAIL = "q-owner@example.com"
NOW = datetime.now(UTC).replace(microsecond=0)
PIVOT = NOW - timedelta(days=5)


class Ids:
    def __init__(self) -> None:
        self.sessions: list[str] = []
        self.deviations: list[str] = []
        self.p1 = str(uuid.uuid4())             # promoted
        self.p2 = str(uuid.uuid4())             # rejected
        self.c1 = str(uuid.uuid4())
        self.edit = str(uuid.uuid4())
        self.e_fail = str(uuid.uuid4())
        self.e_pass = str(uuid.uuid4())
        self.d_apply = str(uuid.uuid4())
        self.d_reject = str(uuid.uuid4())
        self.art = ""


def _seed(conn: psycopg.Connection, ids: Ids) -> None:
    conn.execute("set session_replication_role = replica")     # superuser only: skips the append-only triggers

    def session(sid: str, created: datetime, env: str = "prod") -> str:
        conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env, created_at) "
                     "values (%s, 'chat', '/q-test', 1, %s, %s)", (sid, env, created))
        ids.sessions.append(sid)
        return sid

    def deviation(sid: str, at: datetime, rule_id: str = "q-quote-globs") -> None:
        did = str(uuid.uuid4())
        conn.execute("insert into deviations (id, source_key, session_id, instruction_source, instruction_ref, what, why, "
                     "how, followed_grade, result_grade, rule_id, created_at) values "
                     "(%s, %s, %s, 'claude-md:global', 'ref', 'w', 'y', 'h', 0.3, 0.5, %s, %s)",
                     (did, f"q-{did}", sid, rule_id, at))
        ids.deviations.append(did)

    b1, b2 = session("q-b1", PIVOT - timedelta(days=8)), session("q-b2", PIVOT - timedelta(days=6))
    session("q-b3", PIVOT - timedelta(days=3))
    a1 = session("q-a1", PIVOT + timedelta(days=1))
    session("q-a2", PIVOT + timedelta(days=2))
    session("q-a3", PIVOT + timedelta(days=3))
    sx = session("q-sx", PIVOT + timedelta(days=1), env="sandbox")
    for sid, at in ((b1, PIVOT - timedelta(days=8)), (b1, PIVOT - timedelta(days=7)), (b2, PIVOT - timedelta(days=6)),
                    (a1, PIVOT + timedelta(days=1)), (sx, PIVOT + timedelta(days=1))):
        deviation(sid, at)
    deviation(b2, PIVOT - timedelta(days=6), "q-rare-rule")
    manifest = json.dumps({"format": "rsi-artifact/1", "q": uuid.uuid4().hex}, separators=(",", ":"))
    ids.art = hashlib.sha256(manifest.encode()).hexdigest()
    conn.execute("insert into rsi_artifacts (hash, manifest_canonical, manifest, files, baseline_id) values (%s, %s, %s, '{}', 'bl-q')",
                 (ids.art, manifest, Jsonb(json.loads(manifest))))
    conn.execute("insert into rsi_experiments (id, kind, status, verdict, created_at, finished_at) "
                 "values (%s, 'edit', 'evaluated', 'worse', %s, %s)", (EXP, PIVOT - timedelta(days=2), PIVOT - timedelta(days=1)))
    conn.execute("insert into rsi_candidates (id, dedupe_key, source, kind, rule_key, status, decided_reason) "
                 "values (%s, %s, 'deviations', 'edit_instruction', %s, 'rejected', %s)",
                 (ids.c1, f"q-{ids.c1}", RULE, f"tried before, ask {MAIL}"))
    for pid, status, decided, promoted, title in (
            (ids.p1, "promoted", PIVOT - timedelta(hours=1), PIVOT, f"Quote zsh globs ({MAIL})"),
            (ids.p2, "rejected", NOW - timedelta(days=1), None, "Quote zsh globs, take 2")):
        conn.execute("insert into rsi_proposals (id, artifact_hash, experiment_id, candidate_id, title, targets, status, "
                     "confidence, created_at, decided_at, promoted_at) values (%s, %s, %s, %s, %s, '{~/x.md}', %s, 0.7, %s, %s, %s)",
                     (pid, ids.art, EXP, ids.c1, title, status, PIVOT - timedelta(days=1), decided, promoted))
    for did, pid, action, reason, at in ((ids.d_apply, ids.p1, "apply", None, PIVOT - timedelta(hours=1)),
                                         (ids.d_reject, ids.p2, "reject", f"worse on sandbox-replay; ask {MAIL}",
                                          NOW - timedelta(days=1))):
        conn.execute("insert into rsi_decisions (id, proposal_id, action, artifact_hash, source, verified, reason, "
                     "decided_at, expires_at) values (%s, %s, %s, %s, 'cli', 'cli-tty', %s, %s, %s)",
                     (did, pid, action, ids.art, reason, at, at + timedelta(days=1)))
    for eid, model, verdict in ((ids.e_fail, "claude-haiku-5-5", "fail"), (ids.e_pass, "claude-sonnet-5-5", "pass")):
        conn.execute("insert into rsi_eval_results (id, artifact_hash, experiment_id, baseline_id, suite, suite_version, "
                     "model, effort, verdict, record, record_sha) values (%s, %s, %s, 'bl-q', 'sandbox-replay', '1', %s, "
                     "'low', %s, %s, %s)", (eid, ids.art, EXP, model, verdict, Jsonb({"format": "rsi-eval/1"}),
                                            hashlib.sha256(eid.encode()).hexdigest()))
    conn.execute("insert into edits (id, status, instruction_source, rule_id, rule_title) "
                 "values (%s, 'reverted', 'claude-md:global', 'q-quote-globs', 'Quote globs')", (ids.edit,))


def _clean(conn: psycopg.Connection, ids: Ids) -> None:
    conn.execute("set session_replication_role = replica")
    conn.execute("delete from rsi_evidence where external_ref like any(array['lf:trace/abab%%', 'ch:q.%%q-quote%%', "
                 "'lf:dataset_run/exp-20261016-q1%%', 'past:amem:proposal/%%', 'answer:%%'])")
    conn.execute("delete from rsi_decisions where id = any(%s::uuid[])", ([ids.d_apply, ids.d_reject],))
    conn.execute("delete from rsi_eval_results where id = any(%s::uuid[])", ([ids.e_fail, ids.e_pass],))
    conn.execute("delete from rsi_proposals where id = any(%s::uuid[])", ([ids.p1, ids.p2],))
    conn.execute("delete from rsi_candidates where id = %s", (ids.c1,))
    conn.execute("delete from rsi_experiments where id = %s", (EXP,))
    if ids.art:
        conn.execute("delete from rsi_artifacts where hash = %s", (ids.art,))
    conn.execute("delete from edits where id = %s", (ids.edit,))
    conn.execute("delete from deviations where id = any(%s::uuid[])", (ids.deviations,))
    conn.execute("delete from sessions where session_id = any(%s)", (ids.sessions,))


@pytest.fixture(scope="module")
def questions_db(pg_url: str) -> Iterator[str]:
    with fresh_db(pg_url) as url:
        yield url


@pytest.fixture
def world(questions_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Ids]]:
    ids = Ids()
    with psycopg.connect(questions_db, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        _seed(conn, ids)
    app = questions_db.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)
    monkeypatch.setenv("AMEM_DB_URL", app)
    with psycopg.connect(app) as conn:                       # the trace link the sandbox sink would have written
        E.record(conn, E.EvidenceRef(f"lf:trace/{TRACE}", "lf", "trace", [f"amem:experiment/{EXP}"],
                                     {"check_passed": False, "tool_errors": 2, "arm": "B"}))
        conn.commit()
    yield app, ids
    with psycopg.connect(questions_db, autocommit=True) as conn:
        _clean(conn, ids)


# ---------------------------------------------------------------- stand-ins with the real specs


class _Stub:
    def __init__(self, source: str, specs: Mapping[str, E.NamedQuery], answers: Mapping[str, list[E.EvidenceRef]],
                 healthy: bool = True):
        self.source, self._specs, self._answers, self._healthy = source, specs, answers, healthy
        self.calls: list[tuple[str, dict[str, str]]] = []

    def health(self) -> E.Health:
        return E.Health(self._healthy, "" if self._healthy else "not configured")

    def named_queries(self) -> Mapping[str, E.NamedQuery]:
        return self._specs

    def query(self, name: str, params: Mapping[str, str]) -> list[E.EvidenceRef]:
        self.calls.append((name, dict(params)))
        return list(self._answers.get(name, []))


def _stubs(ids: Ids) -> dict[str, Any]:
    dev = f"amem:deviation/{ids.deviations[0]}"
    ch = _Stub("ch", {n: q.spec for n, q in CH.QUERIES.items()}, {
        "rule_gap": [E.EvidenceRef("ch:q.rule_gap/claude-md:global:q-quote-globs", "ch", "metric", [dev],
                                   {"rule_key": RULE, "n_deviations": 4, "gap": 0.6})],
        "routine_streaks": [E.EvidenceRef("ch:q.routine_streaks/q-gone", "ch", "metric",
                                          [f"amem:run/{uuid.uuid4()}"], {"routine": "q-gone", "fail_streak": 3})],
        "tokens_by_outcome": [E.EvidenceRef("ch:q.tokens_by_outcome/shipped/chat/m/high", "ch", "metric", [],
                                            {"n_sessions": 9})],
        "skill_outcomes": [E.EvidenceRef("ch:q.skill_outcomes/dispatch/m", "ch", "metric", ["amem:session/q-a1"],
                                         {"skill": "dispatch", "n_sessions": 3, "done_rate": 0.67})],
        "rule_weekly_rate": [E.EvidenceRef("ch:q.rule_weekly_rate/claude-md:global:q-quote-globs@2026-10-12", "ch",
                                           "metric", [dev], {"rate": 0.2})]})
    lf = _Stub("lf", LF.LangfuseRetriever._QUERIES, {
        "experiment_scores": [E.EvidenceRef(f"lf:dataset_run/{EXP}-B", "lf", "score", [f"amem:experiment/{EXP}"],
                                            {"traces": 2, "check_passed_rate": 0.0})],
        "trace": [E.EvidenceRef(f"lf:trace/{TRACE}", "lf", "trace", [f"amem:experiment/{EXP}"],
                                {"tool_errors": 2, "level": "ERROR"})]})
    pd = _Stub("pd", PD.PastDevRetriever._QUERIES, {}, healthy=False)
    return {"sb": E.SupabaseRetriever(lambda: psycopg.connect(_app_url)), "ch": ch, "lf": lf, "pd": pd}


_app_url = ""


def _ask(app: str, ids: Ids, name: str, params: Mapping[str, str]) -> tuple[Q.Answer, dict[str, Any]]:
    global _app_url
    _app_url = app
    rs = _stubs(ids)
    return Q.ask(name, params, rs, lambda: psycopg.connect(app)), rs


def _status(ans: Q.Answer) -> dict[str, str]:
    return {f"{st.source}.{st.query}": res.status for st, res in ans.steps}


# ---------------------------------------------------------------- the six questions


def test_recurring_failures(world: tuple[str, Ids]) -> None:
    app, ids = world
    ans, _ = _ask(app, ids, "recurring_failures", {"days": "30", "min_n": "3"})
    assert _status(ans) == {"sb.recurring_deviations": "ok", "ch.rule_gap": "ok", "ch.routine_streaks": "ok"}
    sb = [f for f in ans.findings if f["source"] == "sb"]
    (pat,) = [f for f in sb if f["metrics"]["rule_key"] == RULE]
    assert pat["status"] == "authoritative" and pat["type"] == "pattern"
    assert (pat["metrics"]["n"], pat["metrics"]["sessions"]) == (4, 3)            # the sandbox deviation is left out
    assert pat["measure"] == {"basis": "aggregate", "n": 4} and pat["supabase_ref"].startswith("amem:deviation/")
    assert not any(f["metrics"].get("rule_key") == "claude-md:global:q-rare-rule" for f in sb)   # below min_n
    by_ref = {f["ref"]: f for f in ans.findings}
    assert by_ref["ch:q.rule_gap/claude-md:global:q-quote-globs"]["status"] == "linked"
    assert by_ref["ch:q.routine_streaks/q-gone"]["status"] == "stale"            # its run is not in Supabase
    for f in ans.findings:
        assert set(f) >= {"source", "service", "ref", "supabase_ref", "timestamp", "type", "measure", "status"}


def test_best_combinations(world: tuple[str, Ids]) -> None:
    app, ids = world
    ans, _ = _ask(app, ids, "best_combinations", {"experiment": EXP})
    assert _status(ans) == {"sb.eval_by_model": "ok", "ch.skill_outcomes": "ok", "ch.tokens_by_outcome": "ok",
                            "lf.experiment_scores": "ok"}
    evals = {f["metrics"]["model"]: f for f in ans.findings if f["step"] == "sb.eval_by_model"
             and f["metrics"].get("suite") == "sandbox-replay" and f["metrics"].get("effort") == "low"}
    assert evals["claude-sonnet-5-5"]["metrics"]["good_rate"] == 1.0 and evals["claude-haiku-5-5"]["metrics"]["bad"] == 1
    by_ref = {f["ref"]: f for f in ans.findings}
    assert by_ref["ch:q.skill_outcomes/dispatch/m"]["status"] == "linked"
    assert by_ref["ch:q.tokens_by_outcome/shipped/chat/m/high"]["status"] == "unlinked"
    assert by_ref[f"lf:dataset_run/{EXP}-B"]["measure"] == {"basis": "score", "n": 2, "check_passed_rate": 0.0}


def test_attempted_before(world: tuple[str, Ids]) -> None:
    app, ids = world
    ans, rs = _ask(app, ids, "attempted_before", {"rule": RULE})
    assert _status(ans) == {"sb.attempts_for": "ok", "pd.recall": "unavailable"}
    assert rs["pd"].calls == []                                                   # down: never queried
    (pd_step,) = [st for st, _ in ans.steps if st.source == "pd"]
    assert pd_step.params == {"query": "q quote globs", "limit": "10"}           # the rule id's words
    refs_ = {f["ref"]: f for f in ans.findings}
    assert set(refs_) == {f"amem:proposal/{ids.p1}", f"amem:proposal/{ids.p2}", f"amem:edit/{ids.edit}"}
    p1 = refs_[f"amem:proposal/{ids.p1}"]
    assert p1["metrics"]["status"] == "promoted" and p1["measure"]["confidence"] == 0.7
    assert MAIL not in json.dumps(ans.to_json())                                 # titles are scrubbed
    assert Q.ask("attempted_before", {"artifact": ids.art}, _stubs(ids), lambda: psycopg.connect(app)).findings


def test_why_rejected(world: tuple[str, Ids]) -> None:
    app, ids = world
    ans, _ = _ask(app, ids, "why_rejected", {"experiment": EXP})
    assert _status(ans) == {"sb.decisions_for": "ok", "lf.experiment_scores": "ok", "pd.recall": "unavailable"}
    by_ref = {f["ref"]: f for f in ans.findings}
    assert by_ref[f"amem:experiment/{EXP}"]["metrics"]["verdict"] == "worse"
    rej = by_ref[f"amem:decision/{ids.d_reject}"]
    assert rej["metrics"]["action"] == "reject" and rej["metrics"]["has_reason"] is True
    assert rej["summary"].startswith("worse on sandbox-replay") and MAIL not in rej["summary"]
    assert f"amem:proposal/{ids.p2}" in rej["supabase_refs"]
    one = Q.ask("why_rejected", {"proposal": ids.p2}, _stubs(ids), lambda: psycopg.connect(app))
    assert [f["ref"] for f in one.findings] == [f"amem:decision/{ids.d_reject}"]


def test_failed_eval_traces_follow_up_live(world: tuple[str, Ids]) -> None:
    app, ids = world
    ans, rs = _ask(app, ids, "failed_eval_traces", {"experiment": EXP})
    assert _status(ans) == {"sb.evals_for": "ok", "sb.evidence_for": "ok", "lf.experiment_scores": "ok", "lf.trace": "ok"}
    assert ("trace", {"trace_id": TRACE}) in rs["lf"].calls                     # phase 2: the failing run's trace
    evals = {f["metrics"]["model"]: f["metrics"]["failed"] for f in ans.findings if f["step"] == "sb.evals_for"}
    assert evals == {"claude-haiku-5-5": True, "claude-sonnet-5-5": False}
    live = [f for f in ans.findings if f["step"] == "lf.trace"]
    assert live and live[0]["status"] == "linked" and live[0]["supabase_ref"] == f"amem:experiment/{EXP}"


def test_after_approval_counts_before_and_after(world: tuple[str, Ids]) -> None:
    app, ids = world
    ans, rs = _ask(app, ids, "after_approval", {"proposal": ids.p1, "window_days": "14"})
    assert _status(ans) == {"sb.promotion_history": "ok", "sb.evidence_for": "ok", "ch.rule_weekly_rate": "ok"}
    assert ("rule_weekly_rate", {"rule": RULE, "weeks": "8"}) in rs["ch"].calls
    (h,) = [f for f in ans.findings if f["step"] == "sb.promotion_history"]
    m = h["metrics"]
    assert (m["pivot"], m["deviations_before"], m["deviations_after"]) == ("promoted_at", 3, 1)
    assert (m["sessions_before"], m["sessions_after"]) == (3, 3)                  # the sandbox session is left out
    assert (m["rate_before"], m["rate_after"], m["after_days_observed"]) == (1.0, 0.3333, 5)
    assert h["type"] == "change" and h["status"] == "authoritative" and MAIL not in (h["summary"] or "")


def test_promotion_counts_ignore_sessions_from_other_test_modules(
    world: tuple[str, Ids], pg_url: str,
) -> None:
    """A different file can leave sessions in the shared DB before this module runs."""
    app, ids = world
    foreign = f"other-module-{uuid.uuid4().hex}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env, created_at) "
                     "values (%s, 'chat', '/other-module', 1, 'prod', %s)",
                     (foreign, PIVOT + timedelta(days=1)))
        try:
            ans, _ = _ask(app, ids, "after_approval", {"proposal": ids.p1, "window_days": "14"})
            (history,) = [f for f in ans.findings if f["step"] == "sb.promotion_history"]
            metrics = history["metrics"]
            assert (metrics["sessions_before"], metrics["sessions_after"]) == (3, 3)
            assert (metrics["rate_before"], metrics["rate_after"]) == (1.0, 0.3333)
        finally:
            conn.execute("delete from sessions where session_id = %s", (foreign,))


# ---------------------------------------------------------------- recording, params, the CLI


def test_record_stores_only_linked_external_findings(world: tuple[str, Ids]) -> None:
    app, ids = world
    ans, _ = _ask(app, ids, "recurring_failures", {})
    with psycopg.connect(app) as conn:
        first = Q.record_findings(conn, ans)
        again = Q.record_findings(conn, ans)
        assert len(first) == 2 and first == again                                  # idempotent
        rows = conn.execute("select id::text, system, kind, external_ref, metrics, valid_until is not null "
                            "from rsi_evidence where id = any(%s::uuid[])", (first,)).fetchall()
    by_id = {r[0]: r[1:] for r in rows}
    system, _, ext, metrics, expires = by_id[first[0]]                             # the one linked external finding
    assert (system, ext) == ("clickhouse", "ch:q.rule_gap/claude-md:global:q-quote-globs")
    assert metrics["question"] == "recurring_failures" and expires
    system, kind, ext, metrics, expires = by_id[first[1]]                         # Mission 004C: plus the answer row
    assert (system, kind) == ("supabase", "answer") and ext.startswith("answer:recurring_failures/") and expires
    assert metrics["providers"] == {"supabase": "ok", "clickhouse": "ok"} and metrics["findings"] == len(ans.findings)


@pytest.mark.parametrize("name, params, err", [
    ("after_approval", {}, "missing"), ("attempted_before", {}, "give one of"),
    ("why_rejected", {"experiment": "x y"}, "bad value"), ("recurring_failures", {"sql": "1"}, "unknown param"),
    ("attempted_before", {"query": "a:b/c"}, "bad value"), ("failed_eval_traces", {"experiment": "EXP"}, "bad value"),
])
def test_question_params_are_checked(name: str, params: dict[str, str], err: str) -> None:
    with pytest.raises(ValueError, match=err):
        Q.QUESTIONS[name].check(params)


def test_cli_ask_with_unconfigured_adapters(world: tuple[str, Ids], capsys: pytest.CaptureFixture[str],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AMEM_CLICKHOUSE_URL", "AMEM_LANGFUSE_HOST", "AMEM_PASTDEV_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(CH.amem_config, "load_env", lambda: None)
    assert cli.main(["evidence", "questions"]) == 0
    assert len(capsys.readouterr().out.strip().splitlines()) == 7              # Mission 004C: + repeat_failures
    assert cli.main(["evidence", "ask", "why_rejected", "--param", f"experiment={EXP}", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {s["source"]: s["status"] for s in out["steps"]} == {"sb": "ok", "lf": "unavailable", "pd": "unavailable"}
    assert out["findings"] and out["recorded"] == 0
    assert cli.main(["evidence", "ask", "after_approval"]) == 2
