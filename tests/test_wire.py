"""Session facts wired into scoring (e-wire): the worker / chat outcome rules, the stored-row
recompute, the objective result grade on deviations, `amem sessions cost` and the session
outcome as an `ab report` source. Synthetic transcripts and rows only."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import ab, deviations, lessons, questions, sessions_ingest, store, tokens_report
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    T0,
    FakeJev,
    FakeWriter,
    Home,
    assistant,
    envelope,
    fake_jev,
    finding,
    result,
    setup_home,
    text,
    tool,
    user,
    worker_events,
    write_transcript,
    writer,
)
from test_scoring_support import ScoringDb, sdb  # noqa: F401

LATER = T0 + timedelta(days=3)      # well past the 24 h abandoned limit


@pytest.fixture
def db(setup_home: Home, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
        conn.execute("delete from cloud_digests")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    monkeypatch.setenv("AMEM_QUESTIONS_DIR", str(questions.QUESTIONS_DIR))   # deviation.toml
    yield pg_url


def _q(url: str, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


# --------------------------------------------------------------------------- 0. outcome rules


def _subagent(home: Home, events: list[dict[str, Any]], agent: str = "agent-w1",
              agent_type: str = "worker") -> sessions_ingest.SessionRow:
    sub = home.project() / "parent-x" / "subagents"
    path = write_transcript(sub / f"{agent}.jsonl", events, pad=False)
    (sub / f"{agent}.meta.json").write_text(json.dumps({"agentType": agent_type}))
    return sessions_ingest.session_row(path, "-work-demo-repo", parent_session="parent-x")


def _outcome(row: sessions_ingest.SessionRow, now: datetime = LATER) -> tuple[str, dict[str, Any]]:
    p = sessions_ingest._params(row, now)
    return p["outcome"], p["outcome_evidence"].obj


def handback(status: str, minute: int = 2) -> list[dict[str, Any]]:
    return [assistant(tool("SubagentHandback", "h1", message=f"Status: {status}\nEvidence: x"), minute=minute),
            result("h1", "delivered", minute)]


def test_worker_ending_on_its_handback_result_is_not_abandoned(setup_home: Home) -> None:  # noqa: F811
    # The real-data shape: a subagent's last line is the tool result of its hand-back.
    row = _subagent(setup_home, [user("## Task\nLook it up.", 0)] + handback("DONE"))
    assert row.kind == "worker"
    name, evidence = _outcome(row)
    assert (name, evidence["end"], evidence["report_done"]) == ("no-change", "result", 1)


@pytest.mark.parametrize(("want", "tail"), [
    ("partial", handback("PARTIAL")),
    ("partial", handback("BLOCKED")),
    ("committed", [assistant(tool("Edit", "e1", file_path="a.py"), minute=1), result("e1", "ok", 1),
                   assistant(tool("Bash", "b1", command="git commit -m x"), minute=1), result("b1", "ok", 1)]
     + handback("DONE")),
    ("pushed", [assistant(tool("Bash", "b1", command="git push -u origin worktree-x"), minute=1),
                result("b1", "ok", 1)] + handback("DONE")),
    ("partial", [assistant(tool("Edit", "e1", file_path="a.py"), minute=1), result("e1", "ok", 1)]),  # no report
    ("no-change", [assistant(tool("Read", "r1", file_path="a.py"), minute=1)]),     # a call with no result
    ("no-change", [assistant(text("Here is what I found."), minute=1)]),           # an Explore-style answer
    ("failed", [assistant(tool("Bash", "b1", command="pytest"), minute=1), result("b1", "boom", 1, error=True)]),
])
def test_worker_outcome_rules(setup_home: Home, want: str, tail: list[dict[str, Any]]) -> None:  # noqa: F811
    row = _subagent(setup_home, [user("## Task\nDo it.", 0)] + tail)
    assert _outcome(row)[0] == want


def test_worker_rule_comes_from_the_kind(setup_home: Home) -> None:  # noqa: F811
    events = [user("## Task\nDo it.", 0)] + worker_events(str(setup_home.repo))[1:]   # ends on a call, no result
    assert _outcome(_subagent(setup_home, events))[0] == "no-change"
    # The same signals in a main chat session: an unanswered tool call a day later is abandoned.
    chat = sessions_ingest.session_row(write_transcript(setup_home.project() / "c1.jsonl", events, pad=False),
                                       "-work-demo-repo")
    assert chat.kind == "chat" and _outcome(chat)[0] == "abandoned"
    assert _outcome(_subagent(setup_home, events), now=T0 + timedelta(minutes=30))[0] == "active"


@pytest.mark.parametrize(("want", "events"), [
    # Ended on a tool result: it got its answer. Not abandoned for that.
    ("no-change", [user("check it", 0), assistant(tool("Read", "r1", file_path="a.py"), minute=1),
                   result("r1", "ok", 1)]),
    ("abandoned", [user("go", 0), assistant(text("hm"), minute=1), user("still there?", 2)]),
    ("abandoned", [user("go", 0), assistant(tool("Bash", "b1", command="sleep 99"), minute=1)]),
    ("abandoned", [user("go", 0), assistant(tool("Edit", "e1", file_path="a.py"), minute=1), result("e1", "ok", 1),
                   assistant(text("done"), minute=2)]),
    ("no-change", [user("what is X?", 0), assistant(text("X is Y."), minute=1)]),
])
def test_chat_abandoned_rule(setup_home: Home, want: str, events: list[dict[str, Any]]) -> None:  # noqa: F811
    path = write_transcript(setup_home.project() / "c2.jsonl", events, pad=False)
    row = sessions_ingest.session_row(path, "-work-demo-repo")
    assert row.kind == "chat" and _outcome(row)[0] == want


def _stored(url: str, sid: str, kind: str, outcome: str | None, evidence: dict[str, Any] | None,
            last: datetime, *, category: str | None = None, project: str | None = None,
            cwd: str | None = None) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into sessions (session_id, tool, kind, project_dir, started_at, last_activity_at, meta, "
                     "category, project, outcome, outcome_evidence) values (%s, 'claude', %s, '-work-demo-repo', "
                     "%s, %s, %s, %s, %s, %s, %s)",
                     (sid, kind, last - timedelta(minutes=5), last, Jsonb({"cwd": cwd} if cwd else {}),
                      category, project, outcome, Jsonb(evidence) if evidence is not None else None))


def test_ingest_recomputes_stored_outcomes_and_fills_places(setup_home: Home, db: str) -> None:  # noqa: F811
    old = NOW - timedelta(days=20)      # far outside the ingest window: no transcript is read
    _stored(db, "p/agent-1", "worker", "abandoned", {"report_done": 1, "end": "result"}, old,
            category="worker", project="demo-repo")
    _stored(db, "p/agent-2", "worker", "abandoned", {"edit": 2, "end": "tool"}, old,
            category="worker", project="demo-repo")
    _stored(db, "chat-r", "chat", "abandoned", {"end": "result"}, old, category="chat", project="demo-repo")
    _stored(db, "chat-u", "chat", "abandoned", {"end": "user"}, old, category="chat", project="demo-repo")
    _stored(db, "chat-null", "chat", None, None, old, cwd="/work/demo-repo")   # a ping row of an old version
    report = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert (report.outcomes, report.places) == (3, 1)
    assert report.line().endswith(" outcomes=3 places=1")
    got = dict(_q(db, "select session_id, outcome from sessions"))
    assert got == {"p/agent-1": "no-change", "p/agent-2": "partial", "chat-r": "no-change",
                   "chat-u": "abandoned", "chat-null": None}
    assert _q(db, "select category, project from sessions where session_id = 'chat-null'") == [
        ("chat", "demo-repo")]
    again = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert (again.outcomes, again.places) == (0, 0)                   # idempotent
    assert " outcomes=" not in again.line()


# --------------------------------------------------------------------------- 2. objective result


@pytest.mark.parametrize(("outcome", "kind", "agent_type", "want"), [
    ("shipped", "parent", None, 1.0), ("pushed", "worker", "worker", 0.9), ("committed", "chat", None, 0.8),
    ("no-change", "chat", None, 0.7), ("no-change", "headless", None, 0.7), ("no-change", "routine", None, 0.7),
    ("no-change", "worker", "Explore", 0.7), ("no-change", "worker", "worker", None), ("no-change", "parent", None, None),
    ("partial", "worker", "worker", 0.4), ("abandoned", "chat", None, 0.1), ("failed", "chat", None, 0.0),
    ("active", "chat", None, None), (None, "chat", None, None),
])
def test_objective_result(outcome: str | None, kind: str, agent_type: str | None, want: float | None) -> None:
    assert deviations.objective_result(outcome, kind, agent_type) == want


def test_scan_stores_the_objective_result_and_jev_reads_it(setup_home: Home, db: str, writer: Callable[..., FakeWriter],  # noqa: F811
                                                           fake_jev: FakeJev) -> None:  # noqa: F811
    cwd = str(setup_home.repo)
    events = [user("Fix the parser.", 0, cwd=cwd),
              assistant(tool("Edit", "e1", file_path="p.py"), minute=1, cwd=cwd), result("e1", "ok", 1, cwd=cwd),
              assistant(tool("Bash", "b1", command="git commit -am fix"), minute=2, cwd=cwd),
              result("b1", "ok", 2, cwd=cwd), assistant(text("Committed."), minute=3, cwd=cwd)]
    write_transcript(setup_home.project() / "obj-1.jsonl", events)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert _q(db, "select outcome from sessions where session_id = 'obj-1'") == [("committed",)]
    writer(default=envelope({"deviations": [finding()]}))
    deviations.scan(session_id="obj-1", now=NOW)
    (state,) = fake_jev.states
    assert "objective_result: 0.80 (session outcome committed;" in state
    assert "result_grade: 0.80 (the writer's view" in state
    assert _q(db, "select result_outcome, objective_result from deviations") == [("committed", pytest.approx(0.8))]
    # The session outcome moves on (it was pushed later): the report recomputes, reref stores it.
    with psycopg.connect(db) as conn:
        conn.execute("update sessions set outcome = 'shipped' where session_id = 'obj-1'")
    (c,) = deviations.candidates(now=NOW, db_url=db)
    assert c["objective_result_mean"] == 1.0 and c["outcomes"] == {"shipped": 1}
    assert "objective=1.00" in deviations.report_lines([c], 7)[1]
    rr = deviations.reref(db_url=db)
    assert rr.objective == 1 and rr.summary().endswith(" objective=1")
    assert _q(db, "select result_outcome, objective_result from deviations") == [("shipped", pytest.approx(1.0))]
    assert deviations.reref(db_url=db).objective == 0


def test_jev_state_without_an_outcome_says_unknown() -> None:
    f = deviations.parse_findings(json.dumps({"deviations": [finding()]}), {"claude-md:global"})[0]
    state = deviations.jev_state(f, "chat", 0, "active")
    assert "objective_result: unknown (session outcome active)" in state
    assert "objective_result: unknown (session outcome unknown)" in deviations.jev_state(f, "chat", 0)


# --------------------------------------------------------------------------- 3. cost of a rule


def test_candidate_cost_counts_each_session_once(setup_home: Home, db: str) -> None:  # noqa: F811
    with psycopg.connect(db) as conn:
        for sid, tok, act in (("c-1", 2_000_000, 120.0), ("c-2", 1_000_000, 60.0)):
            conn.execute("insert into sessions (session_id, kind, project_dir, started_at, tokens_input, tokens_output,"
                         " tokens_cache_read, tokens_cache_creation, active_seconds) values (%s, 'chat', 'p', %s,"
                         " %s, 0, 0, 0, %s)", (sid, NOW - timedelta(hours=2), tok, act))
        for key, sid, turn in (("k1", "c-1", 1), ("k2", "c-1", 2), ("k3", "c-2", 1)):   # two on c-1
            conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, turn,"
                         " what, why, how, followed_grade, result_grade, rule_id) values (%s, %s, 'claude-md:global',"
                         " 'Explaining', %s, 'w', 'y', 'h', 0.5, 0.5, 'explaining')", (key, sid, turn))
    (c,) = deviations.candidates(now=NOW, db_url=db)
    assert c["cost"] == {"sessions": 2, "tokens": 3_000_000, "active_seconds": 180.0, "score": 6.0}


# --------------------------------------------------------------------------- 4. sessions cost


def _cost_row(url: str, sid: str, started: datetime, category: str, project: str, tokens: tuple[int, ...],
              active: float | None, api: float | None, tool_s: float | None) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, started_at, category, project, tokens_input,"
                     " tokens_output, tokens_cache_read, tokens_cache_creation, active_seconds, api_seconds,"
                     " tool_seconds) values (%s, %s, 'p', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                     (sid, category, started, category, project, *tokens, active, api, tool_s))


def test_sessions_cost_window_filters_and_contract(sdb: ScoringDb) -> None:  # noqa: F811
    lo = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
    hi = lo + timedelta(hours=1)
    _cost_row(sdb.url, "e1", lo, "eval", "evals", (10, 20, 300, 40), 60.0, 30.0, 5.0)
    _cost_row(sdb.url, "e2", lo + timedelta(minutes=59), "eval", "evals", (1, 2, 3, 4), 1.5, None, None)
    _cost_row(sdb.url, "r1", lo + timedelta(minutes=5), "routine", "routine:nightly", (100, 0, 0, 0), 10.0, 1.0, 1.0)
    _cost_row(sdb.url, "early", lo - timedelta(seconds=1), "eval", "evals", (999, 0, 0, 0), 9.0, 9.0, 9.0)
    _cost_row(sdb.url, "late", hi, "eval", "evals", (999, 0, 0, 0), 9.0, 9.0, 9.0)       # until is exclusive
    doc = tokens_report.cost(since=lo, until=hi, category="eval", db_url=sdb.url)
    assert doc == {
        "sessions": 2,
        "tokens": {"input": 11, "output": 22, "cache_read": 303, "cache_creation": 44, "total": 380},
        "active_seconds": 61.5, "api_seconds": 30.0, "tool_seconds": 5.0,
        "by_project": {"evals": {"sessions": 2, "tokens": {"input": 11, "output": 22, "cache_read": 303,
                                                           "cache_creation": 44, "total": 380},
                                 "active_seconds": 61.5, "api_seconds": 30.0, "tool_seconds": 5.0}},
    }
    every = tokens_report.cost(since=lo, until=hi, db_url=sdb.url)
    assert every["sessions"] == 3 and set(every["by_project"]) == {"evals", "routine:nightly"}
    assert tokens_report.cost(since=lo, until=hi, project="routine:nightly", db_url=sdb.url)["sessions"] == 1
    # CLI: --json is one object (agent-config's improve parses it); +00:00 offsets as improve sends them.
    res = run_amem("sessions", "cost", "--since", lo.isoformat(), "--until", hi.isoformat(), "--category", "eval",
                   "--json", env=sdb.env)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == doc
    text_ = run_amem("sessions", "cost", "--since", "2026-10-18T10:00:00", "--until", "2026-10-18T11:00:00",
                     env=sdb.env)
    assert text_.returncode == 0 and text_.stdout.startswith("cost: 2026-10-18T10:00:00+00:00 .. ")
    assert "3 sessions" in text_.stdout


def test_sessions_cost_nothing_matches_is_zeros(sdb: ScoringDb) -> None:  # noqa: F811
    res = run_amem("sessions", "cost", "--since", "2030-01-01T00:00:00Z", "--json", "--category", "routine",
                   env=sdb.env)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout) == {
        "sessions": 0, "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0},
        "active_seconds": 0.0, "api_seconds": 0.0, "tool_seconds": 0.0, "by_project": {}}
    bad = run_amem("sessions", "cost", "--since", "yesterday", env=sdb.env)
    assert bad.returncode == 2 and "not an ISO timestamp" in bad.stderr
    rest = run_amem("sessions", "cost", "--since", "2030-01-01", env=base_env(
        AMEM_BACKEND="rest", AMEM_SUPABASE_URL="https://stub.example.test", AMEM_SUPABASE_KEY="stub"))
    assert rest.returncode == 1 and "Postgres backend" in rest.stderr


# --------------------------------------------------------------------------- 5. ab report


def test_ab_report_uses_the_session_outcome_after_label_and_jev(sdb: ScoringDb) -> None:  # noqa: F811
    lessons.upsert([lessons.Lesson(slug="sess", title="t", rule="r", scope_repo="repo-s", status="trial")],
                   db_url=sdb.url)
    with psycopg.connect(sdb.url) as conn:
        for sid, outcome in (("parent-ok", "shipped"), ("parent-bad", "abandoned"), ("parent-open", "active")):
            conn.execute("insert into sessions (session_id, kind, project_dir, outcome) values (%s, 'parent', 'p', %s)",
                         (sid, outcome))

    def run(status: str, session: str | None, key: str = "session") -> Any:
        meta: dict[str, Any] = {"repo": "repo-s", "failures": []}
        if session:
            meta[key] = session
        return store.insert_run("dispatch", status, meta=meta, db_url=sdb.url)

    by_session = run("fail", "parent-ok")                 # status says fail, the session shipped: pass
    by_session_id = run("pass", "parent-bad", "session_id")  # status says pass, the session was abandoned: fail
    open_session = run("pass", "parent-open")             # active decides nothing: status
    no_session = run("fail", "parent-missing")            # not stored: status
    labeled = run("pass", "parent-bad")
    with psycopg.connect(sdb.url) as conn:
        conn.execute("insert into labels (run_id, verdict) values (%s, 'up')", (labeled,))
        conn.execute("insert into lesson_uses (lesson_id, run_id) select id, %s from lessons where slug = 'sess'",
                     (by_session,))
    (e,) = ab.report(db_url=sdb.url, include_runs=True, n=1)
    runs = {r["run_id"]: r for r in e["runs"]}
    assert (runs[str(by_session)]["outcome"], runs[str(by_session)]["source"]) == ("pass", "session")
    assert runs[str(by_session)]["session_outcome"] == "shipped"
    assert (runs[str(by_session_id)]["outcome"], runs[str(by_session_id)]["source"]) == ("fail", "session")
    assert (runs[str(open_session)]["outcome"], runs[str(open_session)]["source"]) == ("pass", "status")
    assert runs[str(no_session)]["source"] == "status" and runs[str(no_session)]["session_id"] is None
    assert (runs[str(labeled)]["outcome"], runs[str(labeled)]["source"]) == ("pass", "label")
    assert e["with"]["sources"] == {"label": 0, "jev": 0, "session": 1, "status": 0}
    assert e["without"]["sources"] == {"label": 1, "jev": 0, "session": 1, "status": 2}
    out = ab.render_text(e)
    assert f"{'session':>7}" in out.splitlines()[1]
    assert "  with         1     1  1.00" in out
