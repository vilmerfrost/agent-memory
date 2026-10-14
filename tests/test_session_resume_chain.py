"""Resume chains (session_resume, docs/sessions-live.md "Resumed sessions"): Claude Desktop
restarts a chat's CLI with `--resume`, and Claude Code writes a new transcript that starts with a
copy of the earlier transcript's lines. The copy keeps each line's `uuid` and `timestamp` but
writes the NEW `sessionId`. Each line counts once, for the earliest transcript of the chain, in
any ingest order, in `sessions ingest` and in the live Stop ping. Synthetic transcripts only."""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_memory import session_live, session_resume, session_tokens, sessions_ingest
from test_deviations_support import NOW, T0, Home, setup_home, tool, user, write_transcript  # noqa: F401
from test_session_live import _row, db, payload  # noqa: F401
from test_session_tokens import reply, usage

A = "aaaaaaaa-0000-4000-8000-00000000000a"
B = "bbbbbbbb-0000-4000-8000-00000000000b"
C = "cccccccc-0000-4000-8000-00000000000c"
TA = session_tokens.Tokens(input=1, output=10, cache_read=100, cache_creation=1000)
TB = session_tokens.Tokens(input=2, output=20, cache_read=200, cache_creation=2000)
TC = session_tokens.Tokens(input=3, output=30, cache_read=300, cache_creation=3000)


def segment(tag: str, sid: str, minute: int, t: session_tokens.Tokens, skill: str, cwd: str) -> list[dict[str, Any]]:
    """One transcript's own lines: a typed prompt, a Skill call and an API response (two lines)."""
    u = usage(t.input, t.output, t.cache_read, t.cache_creation)
    lines = [user(f"Step {tag}.", minute, cwd=cwd),
             reply(f"msg_{tag}", u, tool("Skill", f"sk_{tag}", skill=skill), minute=minute + 1, cwd=cwd),
             reply(f"msg_{tag}", u, minute=minute + 1, cwd=cwd)]
    return [{**e, "uuid": f"{tag}-{i}", "sessionId": sid} for i, e in enumerate(lines)]


def copy(events: list[dict[str, Any]], sid: str) -> list[dict[str, Any]]:
    """The copied prefix of a resumed transcript: same uuids and times, the new sessionId."""
    return [{**e, "sessionId": sid} for e in events]


def chain(cwd: str) -> dict[str, list[dict[str, Any]]]:
    """A -> B -> C: B copies A, C copies B (and so A)."""
    a = segment("a", A, 0, TA, "spark", cwd)
    b = copy(a, B) + segment("b", B, 30, TB, "dispatch", cwd)
    c = copy(b, C) + segment("c", C, 60, TC, "handoff", cwd)
    return {A: a, B: b, C: c}


def write(proj: Path, sid: str, events: list[dict[str, Any]]) -> Path:
    path = write_transcript(proj / f"{sid}.jsonl", events, pad=False)
    time.sleep(0.01)   # the chain order is the file birth order
    return path


def write_chain(home: Home, ids: tuple[str, ...] = (A, B, C)) -> Path:
    proj = home.project("-work-demo-repo")
    events = chain(str(home.repo))
    for sid in ids:
        write(proj, sid, events[sid])
    return proj


def tokens_of(r: sessions_ingest.SessionRow) -> session_tokens.Tokens:
    assert r.tokens is not None
    return r.tokens.tokens


# ------------------------------------------------------------------ the rule


def test_three_resumes_count_each_line_once(setup_home: Home) -> None:  # noqa: F811
    write_chain(setup_home)
    rows = {r.session_id: r for r in sessions_ingest.collect(setup_home.projects, days=7, now=NOW)[0]}
    a, b, c = rows[A], rows[B], rows[C]
    assert (tokens_of(a), tokens_of(b), tokens_of(c)) == (TA, TB, TC)
    assert [r.tokens.turns for r in (a, b, c)] == [1, 1, 1]  # type: ignore[union-attr]
    assert [r.started_at for r in (a, b, c)] == [T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=60)]
    assert [r.ended_at for r in (a, b, c)] == [T0 + timedelta(minutes=m) for m in (1, 31, 61)]
    assert [r.skills for r in (a, b, c)] == [["spark"], ["dispatch"], ["handoff"]]
    assert [r.meta.get("prompts") for r in (a, b, c)] == [1, 1, 1]
    assert [r.meta.get("tool_calls") for r in (a, b, c)] == [1, 1, 1]
    assert [r.profile["duration_seconds"] for r in (a, b, c)] == [60.0, 60.0, 60.0]  # type: ignore[index]
    assert "resumed_from" not in a.meta and b.meta["resumed_from"] == A and c.meta["resumed_from"] == B
    assert (b.meta["copied_lines"], c.meta["copied_lines"]) == (3, 6)
    # Kind reads the whole chain: the copied `dispatch` call still makes C a parent.
    assert (a.kind, b.kind, c.kind) == ("chat", "parent", "parent")


def test_the_order_is_the_file_birth_not_the_name(setup_home: Home) -> None:  # noqa: F811
    proj = setup_home.project("-work-demo-repo")
    events = chain(str(setup_home.repo))
    # C sorts first by name; it is still the latest transcript of the chain.
    names = {A: "zz000000-0000-4000-8000-000000000001", B: "mm000000-0000-4000-8000-000000000002",
             C: "aa000000-0000-4000-8000-000000000003"}
    for sid in (A, B, C):
        write(proj, names[sid], events[sid])
    resumes = {sid: session_resume.resume_of(proj / f"{names[sid]}.jsonl") for sid in (A, B, C)}
    assert resumes[A] == session_resume.NONE
    assert resumes[B].resumed_from == names[A] and resumes[B].copied == {"a-0", "a-1", "a-2"}
    assert resumes[C].resumed_from == names[B] and len(resumes[C].copied) == 6


def test_a_pure_copy_gets_no_row(setup_home: Home) -> None:  # noqa: F811
    proj = write_chain(setup_home, (A,))
    write(proj, B, copy(chain(str(setup_home.repo))[A], B))   # resumed, then closed before a message
    rows, skipped = sessions_ingest.collect(setup_home.projects, days=7, now=NOW)
    assert [r.session_id for r in rows] == [A] and skipped == 1


def test_copied_subagent_file_adds_nothing(setup_home: Home) -> None:  # noqa: F811
    proj = write_chain(setup_home, (A, B))
    sub = segment("w", A, 2, TC, "x", str(setup_home.repo))
    write_transcript(proj / A / "subagents" / "agent-w1.jsonl", sub, pad=False)
    time.sleep(0.01)
    write_transcript(proj / B / "subagents" / "agent-w1.jsonl", copy(sub, B), pad=False)
    rows = {r.session_id: r for r in sessions_ingest.collect(setup_home.projects, days=7, now=NOW)[0]}
    assert f"{A}/agent-w1" in rows and f"{B}/agent-w1" not in rows
    assert rows[A].meta["subagent_tokens"]["sessions"] == 1 and "subagent_tokens" not in rows[B].meta


def test_own_events_drop_the_copy(setup_home: Home) -> None:  # noqa: F811
    proj = write_chain(setup_home, (A, B))
    events = sessions_ingest.load_events(proj / f"{B}.jsonl")
    own = sessions_ingest.own_events(proj / f"{B}.jsonl", events, B)
    assert [e["uuid"] for e in own] == ["b-0", "b-1", "b-2"]


# ------------------------------------------------------------------ the database


def _snapshot(url: str) -> dict[str, tuple[Any, ...]]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        recs = conn.execute("select * from sessions order by session_id").fetchall()
    return {r["session_id"]: (r["tokens_input"], r["tokens_output"], r["tokens_cache_read"],
                              r["tokens_cache_creation"], r["turns"], r["started_at"], r["ended_at"],
                              r["duration_seconds"], r["active_seconds"], r["kind"], list(r["skills"]),
                              r["meta"].get("resumed_from")) for r in recs}


def _clear(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("delete from sessions")


def _ingest(home: Home, url: str) -> sessions_ingest.IngestReport:
    return sessions_ingest.ingest(home.projects, days=7, now=NOW, db_url=url, tools=("claude",))


def test_any_ingest_order_stores_the_same_rows(setup_home: Home, db: str) -> None:  # noqa: F811
    proj = write_chain(setup_home)
    _ingest(setup_home, db)
    once = _snapshot(db)
    assert [list(once[s][:4]) for s in (A, B, C)] == [TA.as_list(), TB.as_list(), TC.as_list()]
    assert _ingest(setup_home, db).updated == 0   # a re-ingest changes nothing

    # Newest first: only C is in the window (A and B were last written 30 days ago), then all.
    _clear(db)
    old = (NOW - timedelta(days=30)).timestamp()
    for sid in (A, B):
        os.utime(proj / f"{sid}.jsonl", (old, old))
    _ingest(setup_home, db)
    assert set(_snapshot(db)) == {C} and _snapshot(db)[C] == once[C]
    for sid in (A, B):
        os.utime(proj / f"{sid}.jsonl", None)
    _ingest(setup_home, db)
    assert _snapshot(db) == once

    # Oldest first: each transcript is ingested as it appears.
    _clear(db)
    for sid in (A, B, C):
        (proj / f"{sid}.jsonl").unlink()
    events = chain(str(setup_home.repo))
    for sid in (A, B, C):
        write(proj, sid, events[sid])
        _ingest(setup_home, db)
    assert _snapshot(db) == once


def test_stop_ping_counts_only_own_lines(setup_home: Home, db: str) -> None:  # noqa: F811
    write_chain(setup_home, (A, B))
    assert session_live.ping(payload("Stop", setup_home, sid=B), now=NOW, db_url=db) == "written"
    row = _row(db, B)
    assert row is not None
    assert [row["tokens_input"], row["tokens_output"], row["tokens_cache_read"],
            row["tokens_cache_creation"]] == TB.as_list()
    assert row["turns"] == 1 and row["started_at"] == T0 + timedelta(minutes=30)
    assert row["meta"]["resumed_from"] == A
    # A later ping reads only the new lines; the ingest then agrees with the ping.
    with open(setup_home.projects / "-work-demo-repo" / f"{B}.jsonl", "a", encoding="utf-8") as fh:
        for e in segment("b2", B, 40, TA, "x", str(setup_home.repo)):
            fh.write(json.dumps(e) + "\n")
    assert session_live.ping(payload("Stop", setup_home, sid=B), now=NOW + timedelta(minutes=1),
                             db_url=db) == "written"
    want = session_tokens.Tokens()
    want.add(TB)
    want.add(TA)
    row = _row(db, B)
    assert row is not None and [row["tokens_input"], row["tokens_output"], row["tokens_cache_read"],
                                row["tokens_cache_creation"]] == want.as_list()
    _ingest(setup_home, db)
    row = _row(db, B)
    assert row is not None and row["tokens_output"] == want.output and row["turns"] == 2


# ------------------------------------------------------------------ the backfill


def test_backfill_fixes_stored_rows_and_reverts(setup_home: Home, db: str, tmp_path: Path,  # noqa: F811
                                                monkeypatch: Any) -> None:
    from agent_memory import session_resume_backfill as backfill

    proj = write_chain(setup_home)
    d = "dddddddd-0000-4000-8000-00000000000d"
    write(proj, d, copy(chain(str(setup_home.repo))[A], d))   # a pure copy of A
    _ingest(setup_home, db)
    fixed = _snapshot(db)
    assert d not in fixed

    # The stored state before the rule: every line counted for every transcript.
    _clear(db)
    with monkeypatch.context() as m:
        m.setattr(session_resume, "resume_of", lambda _path: session_resume.NONE)
        _ingest(setup_home, db)
    buggy = _snapshot(db)
    assert buggy[B][1] == TA.output + TB.output and buggy[B][5] == T0 and d in buggy
    with psycopg.connect(db) as conn:
        conn.execute("insert into skill_calls (ts, skill, invoked_by, agent, source_key, session_id) "
                     "values (%s, 'spark', 'claude', 'claude', 'k1', %s)", (T0, d))

    p = backfill.plan(setup_home.projects, db_url=db)
    assert {c.session_id: c.action for c in p.changes} == {B: "update", C: "update", d: "delete"}
    assert p.tokens(False) - p.tokens(True) == 2 * TA.total + TB.total + TA.total   # B and C's copies, D
    log = tmp_path / "log.json"
    assert backfill.apply(p, log, db_url=db) == {"updated": 2, "deleted": 1, "skill_calls": 1}
    assert _snapshot(db) == fixed
    with psycopg.connect(db) as conn:
        assert conn.execute("select session_id from skill_calls where source_key = 'k1'").fetchone() == (A,)
    assert backfill.plan(setup_home.projects, db_url=db).changes == []   # a second run finds nothing

    assert backfill.revert(log, db_url=db) == {"rows": 3, "skill_calls": 1, "deviations": 0}
    assert _snapshot(db) == buggy
    with psycopg.connect(db) as conn:
        assert conn.execute("select session_id from skill_calls where source_key = 'k1'").fetchone() == (d,)
