"""T3 workers (agent_memory/t3_workers.py): a child T3 started and the root registered is saved as a worker with a
parent link, from either provider. It is a session of its own, so its lines keep their own id."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import deviations, session_summary, session_tokens, sessions_ingest, t3_workers
from codex_support import tid
from guarded_support import rollout_events, rollout_name, write_jsonl
from test_deviations_support import NOW, Home, chat_events, setup_home, write_transcript  # noqa: F401
from test_session_live import db  # noqa: F401

PARENT = "aaaaaaaa-1111-4222-8333-444455556666"
WORKER = "bbbbbbbb-1111-4222-8333-444455556666"
THREAD = "thread:delegated-task:command%3Amcp%3Aone"
AGENT = "t3-8ce8ee73"
CWD = f"/Users/v/worktrees/demo-repo/{AGENT}"


def record(home: Home, agent: str = AGENT, **extra: Any) -> Path:
    data = {"agent_id": agent, "repo": "demo-repo", "name": "t3-fix", "provider": "claudeAgent", "model": "claude-opus-5-5",
            "effort": "high", "thread": THREAD, "why": "default", "branch": "worktree-t3-fix", "parent_session": PARENT, **extra}
    path = t3_workers.record_path(home.claude / "parent", "demo-repo", agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def claude_worker(home: Home, sid: str = WORKER, cwd: str = CWD) -> Path:
    events = [dict(e, sessionId=sid) for e in chat_events(cwd=cwd)]
    return write_transcript(home.project("-Users-v-worktrees-demo-repo-t3-8ce8ee73") / f"{sid}.jsonl", events, pad=False)


def codex_worker(home: Home, thread: str = tid(7), cwd: str = CWD) -> Path:
    text = json.dumps(rollout_events(thread)).replace("/work/demo-repo", cwd)
    return write_jsonl(home.claude.parent / ".codex/sessions/2026/10/14" / rollout_name(thread), json.loads(text))


def test_a_registered_claude_child_is_a_worker_with_a_parent(setup_home: Home) -> None:  # noqa: F811
    record(setup_home)
    row = sessions_ingest.session_row(claude_worker(setup_home), "p")
    assert row.kind == "worker" and row.session_id == WORKER
    assert row.meta["parent_session"] == PARENT and row.meta["agent_type"] == t3_workers.AGENT_TYPE
    assert row.meta["t3_worker"] == {"name": "t3-fix", "provider": "claudeAgent", "model": "claude-opus-5-5", "effort": "high",
                                     "thread": THREAD, "why": "default", "branch": "worktree-t3-fix"}


def test_a_registered_codex_child_is_a_worker_with_a_parent(setup_home: Home) -> None:  # noqa: F811
    record(setup_home, provider="codex", parent_session="codex:" + tid(1))
    row = sessions_ingest.codex_row(codex_worker(setup_home), "sessions/2026/10/14")
    assert row.kind == "worker" and row.meta["parent_session"] == "codex:" + tid(1)
    assert row.meta["t3_worker"]["provider"] == "codex"


def test_no_record_changes_nothing(setup_home: Home) -> None:  # noqa: F811
    assert sessions_ingest.session_row(claude_worker(setup_home), "p").kind == "chat"
    row = sessions_ingest.codex_row(codex_worker(setup_home), "sessions/2026/10/14")
    assert row.kind != "worker" and "t3_worker" not in row.meta


def test_a_broken_record_is_no_record(setup_home: Home) -> None:  # noqa: F811
    record(setup_home).write_text("{not json", encoding="utf-8")
    assert sessions_ingest.session_row(claude_worker(setup_home), "p").kind == "chat"


def test_a_worker_with_no_known_parent_has_no_parent_key(setup_home: Home) -> None:  # noqa: F811
    record(setup_home, parent_session=None)
    row = sessions_ingest.session_row(claude_worker(setup_home), "p")
    assert row.kind == "worker" and "parent_session" not in row.meta


def test_an_eval_stays_an_eval(setup_home: Home) -> None:  # noqa: F811
    record(setup_home)
    assert sessions_ingest.session_row(claude_worker(setup_home), "-Users-v--cache-agent-evals-run").kind == "eval"


def test_tokens_count_in_the_parent_total_from_both_providers(setup_home: Home) -> None:  # noqa: F811
    record(setup_home)
    record(setup_home, "t3-11112222", provider="codex")
    parent = write_transcript(setup_home.project("-work-demo-repo") / f"{PARENT}.jsonl",
                              [dict(e, sessionId=PARENT) for e in chat_events("/work/demo-repo")], pad=False)
    rows = [sessions_ingest.session_row(parent, "-work-demo-repo"), sessions_ingest.session_row(claude_worker(setup_home), "p"),
            sessions_ingest.codex_row(codex_worker(setup_home, cwd="/Users/v/worktrees/demo-repo/t3-11112222"), "s")]
    for r in rows[1:]:
        r.tokens = session_tokens.Summary(tokens=session_tokens.Tokens(input=10, output=5))
    sessions_ingest.rollup_subagents(rows)
    total = rows[0].meta["subagent_tokens"]
    assert (total["sessions"], total["input"], total["output"]) == (2, 20, 10)


def test_own_lines_stay_with_the_worker(setup_home: Home) -> None:  # noqa: F811
    meta = {"agent_type": t3_workers.AGENT_TYPE, "parent_session": PARENT}
    assert t3_workers.own_session(meta, WORKER) == WORKER
    assert t3_workers.own_session({"parent_session": PARENT, "agent_type": "worker"}, "x") == PARENT
    assert t3_workers.own_session({}, "x") == "x"
    assert not t3_workers.in_parent_folder(meta) and t3_workers.in_parent_folder({"parent_session": PARENT})
    events = [{"sessionId": WORKER, "n": 1}, {"sessionId": "other", "n": 2}]
    assert [e["n"] for e in session_summary.own_events(events, {"tool": "claude", "session_id": WORKER, "meta": meta})] == [1]


def test_the_digest_keeps_the_worker_file_and_lines(setup_home: Home) -> None:  # noqa: F811
    record(setup_home)
    path = claude_worker(setup_home)
    row = sessions_ingest.session_row(path, path.parent.name)
    assert deviations.transcript_path(row, path.parent.parent) == path
    events = sessions_ingest.load_events(path)
    assert sessions_ingest.own_events(path, events, t3_workers.own_session(row.meta, row.session_id))


def test_the_row_stores_the_link_and_a_repeat_keeps_it(setup_home: Home, db: str) -> None:  # noqa: F811
    """The stage pass: read back from the database, as `amem` and the estimates read it."""
    record(setup_home)
    parent = write_transcript(setup_home.project("-work-demo-repo") / f"{PARENT}.jsonl",
                              [dict(e, sessionId=PARENT) for e in chat_events("/work/demo-repo")], pad=False)
    rows = [sessions_ingest.session_row(parent, "-work-demo-repo"), sessions_ingest.session_row(claude_worker(setup_home), "p")]
    sessions_ingest.upsert(rows, db_url=db)
    sessions_ingest.upsert(rows, db_url=db)
    with psycopg.connect(db, row_factory=dict_row) as conn:
        got = conn.execute("select kind, parent_session_id, meta from sessions where session_id = %s", (WORKER,)).fetchone()
        joined = conn.execute("select count(*) as n from sessions s join sessions p on p.session_id = s.parent_session_id "
                              "where s.session_id = %s", (WORKER,)).fetchone()
    assert got is not None and (got["kind"], got["parent_session_id"]) == ("worker", PARENT)
    assert got["meta"]["t3_worker"]["model"] == "claude-opus-5-5" and joined == {"n": 1}


# --------------------------------------------------------------------------- older children


def t3_db(path: Path, rows: list[tuple[str, str | None]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table orchestration_v2_projection_threads (thread_id text, default_provider text, title text, "
                "created_at text, payload_json text)")
    for tid_, worktree in rows:
        payload = {"worktreePath": worktree, "lineage": {"parentThreadId": "mcp:launch:p"},
                   "modelSelection": {"model": "gpt-6.1-sol", "options": [{"id": "reasoningEffort", "value": "medium"}]}}
        con.execute("insert into orchestration_v2_projection_threads values (?, 'codex', 'probe', '2026-11-01T18:00:00Z', ?)",
                    (tid_, json.dumps(payload)))
    con.commit()
    con.close()
    return path


def test_older_children_get_records_and_a_repeat_writes_none(setup_home: Home, tmp_path: Path) -> None:  # noqa: F811
    db_path = t3_db(tmp_path / "state.sqlite", [(THREAD, CWD), (t3_workers.CHILD_PREFIX + "no-worktree", None),
                                                ("mcp:launch:own", "/Users/v/worktrees/demo-repo/t3-99998888"),
                                                (t3_workers.CHILD_PREFIX + "lost", "/Users/v/worktrees/demo-repo/t3-77776666")])
    parent = write_jsonl(setup_home.project("-work-demo-repo") / f"{PARENT}.jsonl",
                         [{"type": "user", "text": json.dumps({"childThreadId": THREAD})}])
    bystander = write_jsonl(setup_home.project("-work-other") / f"{WORKER}.jsonl", [{"type": "user", "text": THREAD}])
    parents = setup_home.claude / "parent"
    plan = t3_workers.plan(db_path, parents, [bystander, parent])
    assert {c.parent_session for c in plan.children} == {PARENT, None} and len(plan.children) == 2
    assert not t3_workers.record_path(parents, "demo-repo", AGENT).exists()     # a plan writes nothing
    assert t3_workers.apply_plan(plan, parents, NOW) == 2
    data = json.loads(t3_workers.record_path(parents, "demo-repo", AGENT).read_text())
    assert (data["parent_session"], data["effort"], data["provider"]) == (PARENT, "medium", "codex")
    again = t3_workers.plan(db_path, parents, [parent])
    assert again.skipped == 2 and t3_workers.apply_plan(again, parents, NOW) == 0
    assert sessions_ingest.session_row(claude_worker(setup_home), "p").kind == "worker"


def test_a_registered_record_is_never_overwritten(setup_home: Home, tmp_path: Path) -> None:  # noqa: F811
    path = record(setup_home)
    before = path.read_text()
    plan = t3_workers.plan(t3_db(tmp_path / "state.sqlite", [(THREAD, CWD)]), setup_home.claude / "parent", [])
    assert plan.skipped == 1 and t3_workers.apply_plan(plan, setup_home.claude / "parent", NOW) == 0
    assert path.read_text() == before


def test_a_codex_parent_is_found_by_its_rollout_name() -> None:
    assert t3_workers.session_of(Path(rollout_name(tid(3)))) == "codex:" + tid(3)
    assert t3_workers.session_of(Path(f"{PARENT}.jsonl")) == PARENT
