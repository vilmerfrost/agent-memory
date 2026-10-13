"""Tokens per session (docs/sessions-live.md): Claude usage dedupe by message id, the
incremental scanner, Codex token_count, the subagent rollup, the cloud digest tokens and the
token columns on ingest. Synthetic transcripts only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import amem_digest, session_tokens, sessions_ingest
from codex_support import Rollout, subagent_source, tid
from test_deviations_support import (  # noqa: F401
    NOW,
    Home,
    assistant,
    busy,
    chat_events,
    result,
    setup_home,
    text,
    tool,
    user,
    write_transcript,
)

CWD = "/work/demo-repo"


def usage(i: int, o: int, cr: int = 0, cc: int = 0, **extra: Any) -> dict[str, Any]:
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cc, "service_tier": "standard", **extra}


def reply(mid: str | None, u: dict[str, Any], *blocks: dict[str, Any], minute: int = 1,
          model: str = "claude-test-1", cwd: str = CWD) -> dict[str, Any]:
    """One transcript line of an API response: `blocks` plus the response's usage."""
    e = assistant(*(blocks or (text("ok"),)), minute=minute, cwd=cwd)
    e["message"].update(model=model, usage=u)
    if mid is not None:
        e["message"]["id"] = mid
    return e


def session_events(cwd: str = CWD) -> list[dict[str, Any]]:
    """Two API responses written as several lines each (one per content block), a streamed
    response whose first line carries a partial usage, a line without an id, a synthetic
    message and a second model."""
    return [
        user("Please fix the parser.", 0, cwd=cwd),
        reply("msg_1", usage(3, 10, 100, 50), text("Looking."), minute=1, cwd=cwd),
        reply("msg_1", usage(3, 10, 100, 50), tool("Bash", "b1", command="pytest"), minute=1, cwd=cwd),
        reply("msg_1", usage(3, 10, 100, 50), tool("Read", "r1", file_path="a.py"), minute=1, cwd=cwd),
        result("b1", "ok", 2, cwd=cwd),
        result("r1", "ok", 2, cwd=cwd),
        reply("msg_2", usage(2, 7, 200, 0), text("partial"), minute=3, cwd=cwd),       # streamed: partial
        reply("msg_2", usage(2, 400, 200, 0), tool("Edit", "e1", file_path="a.py"), minute=3, cwd=cwd),
        result("e1", "ok", 4, cwd=cwd),
        user("Thanks, now the docs.", 5, cwd=cwd),
        reply(None, usage(1, 5, 0, 0), minute=6, cwd=cwd),                              # no id: counted once
        reply("msg_s", usage(0, 0, 0, 0), minute=6, model="<synthetic>", cwd=cwd),      # no API call
        reply("msg_3", usage(4, 20, 300, 10), minute=7, model="claude-other-2", cwd=cwd),
        user("<command-name>/compact</command-name>", 8, cwd=cwd),
    ]


EXPECTED = session_tokens.Tokens(input=3 + 2 + 1 + 4, output=10 + 400 + 5 + 20, cache_read=100 + 200 + 300,
                                 cache_creation=50 + 10)


def test_usage_is_deduped_by_message_id_and_the_latest_line_wins() -> None:
    s = session_tokens.claude_summary(session_events())
    assert s.tokens == EXPECTED
    assert s.models == {
        "claude-test-1": {"input": 6, "output": 415, "cache_read": 300, "cache_creation": 50, "calls": 3},
        "claude-other-2": {"input": 4, "output": 20, "cache_read": 300, "cache_creation": 10, "calls": 1},
    }
    assert s.calls == 4 and s.turns == 2  # typed prompts only (no command echo, no tool result)
    assert s.first_at is not None and s.last_at is not None and s.last_at > s.first_at


def test_incremental_scan_equals_a_full_read(tmp_path: Path) -> None:
    events = session_events()
    path = tmp_path / "s.jsonl"
    lines = [json.dumps(e) for e in events]
    cut = 7  # between msg_2's partial line and its final line
    path.write_text("\n".join(lines[:cut]) + "\n" + lines[cut][:20], encoding="utf-8")  # + a half-written line
    sc = session_tokens.ClaudeScanner()
    first = sc.scan_file(path)
    assert sc.offset == first == len(("\n".join(lines[:cut]) + "\n").encode())  # the half line is not read
    state = json.loads(json.dumps(sc.state()))  # through JSON, like the state file
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sc2 = session_tokens.ClaudeScanner(state)
    second = sc2.scan_file(path)
    assert first + second == path.stat().st_size
    assert sc2.summary.tokens == EXPECTED and sc2.summary.turns == 2
    assert sc2.summary.models == session_tokens.scan_claude_file(path).models
    assert sc2.scan_file(path) == 0  # nothing new
    # A shorter file (replaced): read again from the start.
    path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    sc2.scan_file(path)
    assert sc2.summary.tokens == session_tokens.Tokens(3, 10, 100, 50)


def test_codex_token_count_takes_the_cumulative_totals_per_model() -> None:
    r = Rollout(tid(1), CWD).injected()

    def count(i: int, cached: int, o: int, write: int = 0) -> None:
        r.add("event_msg", {"type": "token_count", "rate_limits": {},
                            "info": {"total_token_usage": {"input_tokens": i, "cached_input_tokens": cached,
                                                           "output_tokens": o, "reasoning_output_tokens": 1,
                                                           "cache_write_input_tokens": write,
                                                           "total_tokens": i + o},
                                     "last_token_usage": {"input_tokens": 1}}})

    r.add("event_msg", {"type": "token_count", "info": None})  # no info yet: ignored
    count(100, 40, 10)
    count(250, 140, 30, 5)
    r.add("turn_context", {"cwd": CWD, "model": "gpt-two"})
    count(400, 200, 50, 5)
    s = session_tokens.codex_summary(r.events)
    assert s.tokens == session_tokens.Tokens(input=200, output=50, cache_read=200, cache_creation=5)
    assert s.models == {"m": {"input": 110, "output": 30, "cache_read": 140, "cache_creation": 5, "calls": 2},
                        "gpt-two": {"input": 90, "output": 20, "cache_read": 60, "cache_creation": 0, "calls": 1}}


def test_codex_forked_history_is_not_counted() -> None:
    r = Rollout(tid(3), CWD, source=subagent_source(tid(2), "/root/w"), thread_source="subagent",
                agent_path="/root/w", fork_start=2)
    r.add("event_msg", {"type": "token_count",  # ordinal 1: the parent's copied history
                        "info": {"total_token_usage": {"input_tokens": 9999, "output_tokens": 999}}})
    r.add("event_msg", {"type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 50, "cached_input_tokens": 20,
                                                       "output_tokens": 5}}})
    s = session_tokens.codex_summary(r.events)
    assert s.tokens == session_tokens.Tokens(input=30, output=5, cache_read=20, cache_creation=0)


def test_subagent_rows_and_the_parent_rollup(setup_home: Home) -> None:  # noqa: F811
    proj = setup_home.project("-work-demo-repo")
    cwd = str(setup_home.repo)
    write_transcript(proj / "parent-9.jsonl", session_events(cwd) + busy(cwd))
    sub = proj / "parent-9" / "subagents"
    write_transcript(sub / "agent-a1.jsonl", [user("## Task\nDo it.", 0, cwd=cwd),
                                              reply("w1", usage(1, 2, 3, 4), minute=1, cwd=cwd)])
    write_transcript(sub / "agent-a2.jsonl", [user("## Task\nAnd this.", 0, cwd=cwd),
                                              reply("w2", usage(10, 20, 30, 40), minute=1, cwd=cwd),
                                              reply("w2", usage(10, 20, 30, 40), minute=1, cwd=cwd)])
    rows = {r.session_id: r for r in sessions_ingest.collect(setup_home.projects, days=7, now=NOW)[0]}
    parent, w1 = rows["parent-9"], rows["parent-9/agent-a1"]
    assert parent.tokens is not None and parent.tokens.tokens == EXPECTED  # its own lines only
    assert w1.kind == "worker" and w1.tokens is not None and w1.tokens.tokens.as_list() == [1, 2, 3, 4]
    assert parent.meta["subagent_tokens"] == {"input": 11, "output": 22, "cache_read": 33, "cache_creation": 44,
                                              "sessions": 2}


def test_codex_subagent_rollup(setup_home: Home) -> None:  # noqa: F811
    from codex_support import codex_home

    cwd = str(setup_home.repo)

    def counted(r: Rollout, n: int) -> Rollout:
        return r.add("event_msg", {"type": "token_count",
                                   "info": {"total_token_usage": {"input_tokens": n, "output_tokens": n}}}, 2)

    counted(Rollout(tid(2), cwd).injected().user("Split it.", 1), 100).busy().write(setup_home)
    w = Rollout(tid(3), cwd, source=subagent_source(tid(2), "/root/w"), thread_source="subagent", agent_path="/root/w")
    counted(w.agent_message("/root", "/root/w", "## Task\nGo.", 1), 7).busy().write(setup_home)
    rows = {r.session_id: r for r in sessions_ingest.collect(None, days=7, now=NOW, codex=codex_home(setup_home),
                                                             tools=("codex",))[0]}
    assert rows[f"codex:{tid(3)}"].tokens.tokens.as_list() == [7, 7, 0, 0]  # type: ignore[union-attr]
    assert rows[f"codex:{tid(2)}"].meta["subagent_tokens"] == {"input": 7, "output": 7, "cache_read": 0,
                                                              "cache_creation": 0, "sessions": 1}
    assert rows[f"codex:{tid(2)}"].tokens.turns == 1  # type: ignore[union-attr]


# --------------------------------------------------------------------------- cloud digests


def test_digest_tokens_equal_the_package_parser() -> None:
    for events in (session_events(), chat_events(CWD), []):
        assert amem_digest.token_totals(events) == session_tokens.claude_summary(events).totals_dict()


def test_digest_row_carries_tokens_and_ingest_reads_them(tmp_path: Path) -> None:
    events = session_events()
    for e in events:
        e["sessionId"] = "cloud-sess-1"
    t = write_transcript(tmp_path / "cloud-sess-1.jsonl", events)
    row = amem_digest.build_row(t, routine="nightly-trials", instructions=[])
    assert row["meta"]["script_version"] == amem_digest.SCRIPT_VERSION and int(amem_digest.SCRIPT_VERSION) >= 3  # tokens since v3
    assert row["meta"]["tokens"] == {**EXPECTED.as_dict(), "models": session_tokens.claude_summary(events).models}
    sr = sessions_ingest.cloud_row({**row, "started_at": None, "ended_at": NOW})
    assert sr is not None and sr.tokens is not None
    assert sr.tokens.tokens == EXPECTED and sr.tokens.turns == row["meta"]["prompts"] and sr.tokens.last_at == NOW
    # An older digest (script_version 1) has no tokens: the columns stay empty.
    old = {**row, "meta": {k: v for k, v in row["meta"].items() if k != "tokens"}}
    assert sessions_ingest.cloud_row(old).tokens is None  # type: ignore[union-attr]


# --------------------------------------------------------------------------- database


@pytest.fixture
def db(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
        conn.execute("delete from cloud_digests")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    return pg_url


def _q(url: str, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


def test_ingest_writes_token_columns(setup_home: Home, db: str) -> None:  # noqa: F811
    proj = setup_home.project("-work-demo-repo")
    cwd = str(setup_home.repo)
    path = write_transcript(proj / "tok-1.jsonl", session_events(cwd) + busy(cwd))
    write_transcript(proj / "tok-1" / "subagents" / "agent-a1.jsonl",
                     [user("## Task", 0, cwd=cwd), reply("w1", usage(1, 2, 3, 4), minute=1, cwd=cwd)])
    first = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("claude",))
    assert (first.new, first.updated) == (2, 0)
    got = _q(db, "select tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation, turns, models, "
                 "last_activity_at, tokens_updated_at, meta -> 'subagent_tokens' from sessions "
                 "where session_id = 'tok-1'")[0]
    assert list(got[:5]) == [*EXPECTED.as_list(), 2]
    assert got[5]["claude-other-2"]["output"] == 20 and got[6].isoformat().startswith("2026-10-14T09:1")
    assert got[8] == {"input": 1, "output": 2, "cache_read": 3, "cache_creation": 4, "sessions": 1}
    stamp = got[7]
    # Re-ingest: nothing changed, nothing touched (tokens_updated_at stays).
    again = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("claude",))
    assert (again.new, again.updated) == (0, 0)
    assert _q(db, "select tokens_updated_at from sessions where session_id = 'tok-1'") == [(stamp,)]
    # The transcript grows: the totals and the stamp move.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(reply("msg_9", usage(1, 1, 1, 1), minute=30, cwd=cwd)) + "\n")
    third = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("claude",))
    assert third.updated == 1
    (row,) = _q(db, "select tokens_output, tokens_updated_at > %s from sessions where session_id = 'tok-1'", stamp)
    assert row == (EXPECTED.output + 1, True)


def test_rls_stays_on_for_sessions(db: str) -> None:
    assert _q(db, "select relrowsecurity from pg_class where relname = 'sessions'") == [(True,)]
    migration = (sessions_ingest.config.MIGRATIONS_DIR / "20261018010000_session_tokens.sql").read_text()
    with psycopg.connect(db) as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"create role {role}")
            conn.execute(f"grant select on public.sessions to {role}")
        conn.execute(migration)  # re-runnable, and revokes the grants
        granted = conn.execute("select count(*) from information_schema.role_table_grants "
                               "where table_name = 'sessions' and grantee in ('anon', 'authenticated')").fetchone()
        assert granted == (0,)
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into sessions (session_id, kind, project_dir, tokens_input) values ('x', 'chat', 'p', -1)")
        conn.rollback()
