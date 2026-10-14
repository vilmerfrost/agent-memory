"""Commits in `outcome_evidence` and `sessions.summary` (docs/sessions-live.md, "Commits" and
"Summary"). Synthetic transcripts only; the model call is always replaced (no LLM in tests)."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import distill, session_commits, session_live, session_summary, sessions_ingest
from codex_support import Rollout, tid
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    Home,
    assistant,
    chat_events,
    result,
    setup_home,
    text,
    tool,
    user,
    write_transcript,
)

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "supabase" / "migrations" / "20261019030000_session_summary.sql"
SUMMARY = "Fixed the flaky parser test in parser.py and committed the fix. The work finished."


def commit(cwd: str, tid_: str, output: str, *, command: str = 'git commit -m "x"', minute: int = 5,
           **extra: Any) -> list[dict[str, Any]]:
    use = assistant(tool("Bash", tid_, command=command), minute=minute, cwd=cwd)
    use.update(extra)
    res = result(tid_, output, minute, cwd=cwd, **extra)
    return [use, res]


# --------------------------------------------------------------------------- commit parsing


def test_commit_lines_branch_root_commit_detached_and_repo() -> None:
    cwd = "/Users/x/worktrees/agent-memory/w1"
    events = [
        *commit(cwd, "c1", "[worktree-s-summary 1a2b3c4] Add the summary module\n 2 files changed"),
        *commit(cwd, "c2", "[main (root-commit) 0f0f0f0] Initial commit", command="git -C /src/other-repo commit -m i"),
        *commit(cwd, "c3", "[detached HEAD 9e9e9e9e] Fix a typo", command="cd /work/third && git add -A && git commit -m t"),
        *commit(cwd, "c4", "[feature/x 7777777] Relative path", command="git -C ../sibling commit -am r"),
        *commit(cwd, "c5", "[main 5555555] Via a variable",
                command="W=~/worktrees/agent-config/f1; cp a b && cd $W && git commit -m v"),
        *commit(cwd, "c6", "[main 6666666] Last cd wins", command="cd /work/one && cd /work/two && git commit -m l"),
    ]
    got = session_commits.claude_commits(events, cwd="/fallback")
    assert got == [
        {"sha": "1a2b3c4", "message": "Add the summary module", "repo": "agent-memory", "branch": "worktree-s-summary"},
        {"sha": "0f0f0f0", "message": "Initial commit", "repo": "other-repo", "branch": "main"},
        {"sha": "9e9e9e9e", "message": "Fix a typo", "repo": "third", "branch": "detached HEAD"},
        {"sha": "7777777", "message": "Relative path", "repo": "agent-memory", "branch": "feature/x"},
        {"sha": "5555555", "message": "Via a variable", "repo": "agent-config", "branch": "main"},
        {"sha": "6666666", "message": "Last cd wins", "repo": "two", "branch": "main"},
    ]


def test_commits_dedupe_redact_cap_and_ignore_other_commands() -> None:
    cwd = "/work/demo-repo"
    secret = "token=sk-ant-abcdefghijklmnop0123456789"
    events = [
        *commit(cwd, "c1", f"[main abc1234] Rotate key {secret}"),
        *commit(cwd, "c2", "[main abc1234] Rotate key again"),                    # same sha: first wins
        *commit(cwd, "c3", "[main def5678] Not a commit call", command="git log --oneline"),
        *commit(cwd, "c4", "[main " + "1" * 7 + "] " + "long " * 100),
    ]
    got = session_commits.claude_commits(events)
    assert [c["sha"] for c in got] == ["abc1234", "1111111"]
    assert "sk-ant" not in json.dumps(got) and "[redacted]" in got[0]["message"]
    assert len(got[1]["message"]) <= session_commits.MESSAGE_MAX
    many = [e for i in range(60) for e in commit(cwd, f"m{i}", f"[main {i:07x}a] c{i}")]
    capped = session_commits.claude_commits(many)
    assert len(capped) == session_commits.MAX_COMMITS and capped[-1]["message"] == "c59"


def test_quiet_commits_and_quoted_patterns() -> None:
    cwd = "/work/demo-repo"
    events = [
        # quiet + log after: the first log line is the new commit (branch from the decoration)
        *commit(cwd, "q1", "abc1234 (HEAD -> worktree-a, origin/x) Add the lock\n0000000 Older commit\n",
                command='git add -A && git commit -qm "Add the lock" && git log --oneline --decorate -2'),
        # quiet, &&-only chain, exit 0: made, sha unknown
        *commit(cwd, "q2", "", command='cd /work/other && git add . && git commit -q -m "Fix the docs\n\nbody"'),
        *commit(cwd, "q3", "", command="git commit -q -m \"$(cat <<'EOF'\nHeredoc subject\n\nCo-Authored-By: x\nEOF\n)\""),
        # quiet but `;` after: exit 0 says nothing about the commit
        *commit(cwd, "q4", "", command='git commit -q -m "Maybe"; echo done'),
        # failed or denied: nothing
        *[e for e in commit(cwd, "q5", "", command='git commit -q -m "Failed"') if e["type"] == "assistant"],
        result("q5", "nothing to commit", 5, error=True, cwd=cwd),
        # a grep for the words is not a commit, even with a bracket line in its output
        *commit(cwd, "g1", "[main 1234567] looks like one", command='grep -rn "x\\|git commit" src'),
    ]
    got = session_commits.claude_commits(events)
    assert got == [
        {"sha": "abc1234", "message": "Add the lock", "repo": "demo-repo", "branch": "worktree-a"},
        {"sha": None, "message": "Fix the docs", "repo": "other", "branch": None},
        {"sha": None, "message": "Heredoc subject", "repo": "demo-repo", "branch": None},
    ]
    bg = [assistant(tool("Bash", "b1", command='git commit -q -m "bg"', run_in_background=True), minute=5, cwd=cwd),
          result("b1", "Command running in background with ID: x", 5, cwd=cwd)]
    assert session_commits.claude_commits(bg) == []


def test_commits_of_a_resumed_session_count_own_lines_only() -> None:
    cwd = "/work/demo-repo"
    events = [*commit(cwd, "c1", "[main aaaaaaa] Earlier session", sessionId="old"),
              *commit(cwd, "c2", "[main bbbbbbb] This session", sessionId="new")]
    assert [c["sha"] for c in session_commits.claude_commits(events, own_session="new")] == ["bbbbbbb"]


def test_codex_commits_from_command_items() -> None:
    r = Rollout(tid(9), "/work/demo-repo").user("Commit it.", 1)
    r.item({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", "git commit -m 'Ship it'"],
            "cwd": "file:///Users/x/worktrees/simplitics/w2", "status": "completed", "exit_code": 0,
            "aggregated_output": "[main c0ffee1] Ship it\n 1 file changed\n"}, 2)
    r.command("git status", minute=3)
    assert session_commits.codex_commits(r.events) == [
        {"sha": "c0ffee1", "message": "Ship it", "repo": "simplitics", "branch": "main"}]


# --------------------------------------------------------------------------- summary rules


def _rec(**over: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {"session_id": "s1", "tool": "claude", "kind": "chat", "repo": "demo-repo",
                           "project_dir": "-work-demo-repo", "meta": {"cwd": "/work/demo-repo"}, "title": "t",
                           "outcome": "committed", "outcome_evidence": {"commit": 1, "end": "reply"},
                           "last_activity_at": NOW - timedelta(hours=3), "ended_at": None,
                           "summary": None, "summary_updated_at": None}
    rec.update(over)
    return rec


def test_skip_reasons() -> None:
    assert session_summary.skip_reason(_rec(), NOW) is None
    assert session_summary.skip_reason(_rec(outcome="active"), NOW) == "active"
    assert session_summary.skip_reason(_rec(outcome=None), NOW) == "active"
    recent = NOW - timedelta(minutes=5)
    assert session_summary.skip_reason(_rec(last_activity_at=recent), NOW) == "active"     # not settled
    assert session_summary.skip_reason(_rec(last_activity_at=recent, meta={"ended": True}), NOW) is None
    sub = {"parent_session": "p1"}
    assert session_summary.skip_reason(_rec(meta=sub), NOW) == "subagent"
    with_commit = {"commit": 1, "commits": [{"sha": "1a2b3c4", "message": "m", "repo": "r", "branch": "b"}]}
    assert session_summary.skip_reason(_rec(meta=sub, outcome_evidence=with_commit), NOW) is None
    done = NOW - timedelta(hours=1)
    assert session_summary.skip_reason(_rec(summary="s", summary_updated_at=done), NOW) == "current"
    stale = NOW - timedelta(hours=5)
    assert session_summary.skip_reason(_rec(summary="s", summary_updated_at=stale), NOW) is None


@pytest.mark.parametrize("reply,want", [
    (SUMMARY, SUMMARY),
    ("Summary: Did one thing.  Then another thing. Then a third. And a fourth.",
     "Did one thing. Then another thing. Then a third."),
    ("", None), ("   ", None), ("ok", None), ("I can't help with that request, sorry.", None),
    ('{"summary": "json is junk here"}', None), ("```\nfenced text here\n```", None),
])
def test_clean(reply: str, want: str | None) -> None:
    assert session_summary.clean(reply) == want


def test_clean_caps_and_redacts() -> None:
    got = session_summary.clean("Set api_key=abcdefghijklmnop0123 in the config. " + "word " * 300)
    assert got is not None and len(got) <= session_summary.SUMMARY_MAX and "abcdefghijklmnop0123" not in got


def test_prompt_holds_facts_and_a_bounded_redacted_digest() -> None:
    cwd = "/work/demo-repo"
    events = [user("Please fix the parser. password=hunter2hunter2", 0, cwd=cwd),
              *[assistant(text("step " + "y" * 300), minute=1, cwd=cwd) for _ in range(200)],
              assistant(text("All done, committed."), minute=9, cwd=cwd)]
    rec = _rec(outcome_evidence={"commit": 1, "end": "reply",
                                 "commits": [{"sha": "1a2b3c4", "message": "Fix parser", "repo": "demo-repo",
                                              "branch": "main"}]})
    p = session_summary.prompt(rec, events)
    assert "Outcome: committed" in p and "1a2b3c4 (demo-repo, main): Fix parser" in p
    assert "Please fix the parser" in p and "All done, committed." in p and "hunter2hunter2" not in p
    assert len(p) < session_summary.HEAD_CHARS + session_summary.TAIL_CHARS + 3000


def test_migration_adds_only_the_two_columns() -> None:
    sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8"))
    statements = [" ".join(s.split()).lower() for s in sql.split(";") if s.strip()]
    assert statements == ["alter table public.sessions add column if not exists summary text, "
                          "add column if not exists summary_updated_at timestamptz"]


# --------------------------------------------------------------------------- with a database


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
        conn.execute("delete from cloud_digests")
    return pg_url


def _row(url: str, sid: str) -> dict[str, Any]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        got = conn.execute("select * from sessions where session_id = %s", (sid,)).fetchone()
    assert got is not None
    return got


class FakeModel:
    def __init__(self, *replies: Any, default: Any = SUMMARY) -> None:
        self.replies, self.default, self.prompts = list(replies), default, []

    def __call__(self, text_: str, *, timeout: float = 60.0) -> str:
        self.prompts.append(text_)
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _sessions(home: Home) -> dict[str, Path]:
    """A committed chat, a subagent without commits, a subagent with commits, a plain chat."""
    cwd = str(home.repo)
    proj = home.project("-work-demo-repo")
    out = {"committed": write_transcript(proj / "committed-1.jsonl", chat_events(cwd)
                                         + commit(cwd, "c1", "[main 1a2b3c4] Fix the parser", minute=7)),
           "plain": write_transcript(proj / "plain-1.jsonl", chat_events(cwd))}
    sub = proj / "committed-1" / "subagents"
    out["sub-quiet"] = write_transcript(sub / "agent-q1.jsonl", chat_events(cwd))
    out["sub-commit"] = write_transcript(sub / "agent-w1.jsonl", chat_events(cwd)
                                         + commit(cwd, "c9", "[worktree-w1 9a9a9a9] Add the endpoint", minute=8))
    for name in ("agent-q1", "agent-w1"):
        (sub / f"{name}.meta.json").write_text(json.dumps({"agentType": "worker"}))
    return out


def _ingest(home: Home, db: str, **kw: Any) -> sessions_ingest.IngestReport:
    return sessions_ingest.ingest(home.projects, days=7, now=NOW, db_url=db, tools=("claude",), **kw)


def test_ingest_adds_commits_and_keeps_the_other_evidence_keys(setup_home: Home, db: str) -> None:  # noqa: F811
    _sessions(setup_home)
    _ingest(setup_home, db)
    ev = _row(db, "committed-1")["outcome_evidence"]
    assert ev["commits"] == [{"sha": "1a2b3c4", "message": "Fix the parser", "repo": "demo-repo", "branch": "main"}]
    assert ev["commit"] == 1 and "end" in ev
    plain = _row(db, "plain-1")["outcome_evidence"]
    assert "commits" not in plain and "end" in plain
    assert _row(db, "committed-1")["outcome"] == "committed"
    assert _row(db, "committed-1/agent-w1")["outcome_evidence"]["commits"][0]["branch"] == "worktree-w1"


def test_hook_ping_keeps_stored_commits(setup_home: Home, db: str) -> None:  # noqa: F811
    sid = "5e55a0b1-0000-4000-8000-0000commits"
    cwd = str(setup_home.repo)
    write_transcript(setup_home.project("-work-demo-repo") / f"{sid}.jsonl",
                     chat_events(cwd) + commit(cwd, "c1", "[main 1a2b3c4] Fix the parser", minute=7), pad=False)
    _ingest(setup_home, db)
    assert _row(db, sid)["outcome_evidence"]["commits"][0]["sha"] == "1a2b3c4"
    body = {"session_id": sid, "hook_event_name": "Stop", "cwd": cwd,
            "transcript_path": str(setup_home.projects / "-work-demo-repo" / f"{sid}.jsonl")}
    assert session_live.ping(json.dumps(body), now=NOW + timedelta(minutes=5), db_url=db) == "written"
    ev = _row(db, sid)["outcome_evidence"]
    assert ev["commits"][0]["sha"] == "1a2b3c4" and ev["commit"] == 1 and "end" in ev
    # A session without commits: a ping adds no `commits` key.
    other = "5e55a0b1-0000-4000-8000-00000nocommit"
    write_transcript(setup_home.project("-work-demo-repo") / f"{other}.jsonl", chat_events(cwd), pad=False)
    body.update(session_id=other, transcript_path=str(setup_home.projects / "-work-demo-repo" / f"{other}.jsonl"))
    session_live.ping(json.dumps(body), now=NOW, db_url=db)
    assert "commits" not in _row(db, other)["outcome_evidence"]


def test_summarize_rules_and_writes(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    paths = _sessions(setup_home)
    _ingest(setup_home, db)
    paths["plain"].unlink()                                   # no transcript any more
    fake = FakeModel()
    monkeypatch.setattr(session_summary, "call_model", fake)
    got = session_summary.summarize(days=7, now=NOW, db_url=db)
    assert (got.done, got.failed, got.skipped) == (2, 0, {"no_transcript": 1, "subagent": 1})
    assert _row(db, "committed-1")["summary"] == SUMMARY
    assert _row(db, "committed-1")["summary_updated_at"] == NOW
    assert _row(db, "committed-1/agent-w1")["summary"] == SUMMARY
    assert _row(db, "committed-1/agent-q1")["summary"] is None
    assert all("SECRET_TOKEN" not in p and "do-not-read-me" not in p for p in fake.prompts)
    # Up to date: a second run calls nothing.
    again = session_summary.summarize(days=7, now=NOW, db_url=db)
    assert again.done == 0 and again.skipped.get("current") == 2 and len(fake.prompts) == 2
    # Newer activity than the summary: summarized again.
    with psycopg.connect(db) as conn:
        conn.execute("update sessions set last_activity_at = %s where session_id = 'committed-1'",
                     (NOW + timedelta(minutes=1),))
    assert session_summary.summarize(days=7, now=NOW + timedelta(hours=1), db_url=db).done == 1


def test_active_and_cloud_sessions_get_no_summary(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _sessions(setup_home)
    _ingest(setup_home, db)
    with psycopg.connect(db) as conn:
        conn.execute("update sessions set outcome = 'active' where session_id = 'committed-1'")
        conn.execute("insert into sessions (session_id, kind, project_dir, started_at, meta, outcome, "
                     "outcome_evidence, last_activity_at) values ('cloud:abc', 'routine', 'cloud', %s, "
                     "'{\"cloud\": true}', 'no-change', '{\"end\": \"reply\"}', %s)", (NOW, NOW - timedelta(hours=3)))
    monkeypatch.setattr(session_summary, "call_model", FakeModel())
    got = session_summary.summarize(days=7, now=NOW, db_url=db)
    assert got.skipped["active"] == 1 and got.skipped["no_transcript"] == 1
    assert _row(db, "committed-1")["summary"] is None and _row(db, "cloud:abc")["summary"] is None


def test_time_budget_stops_new_calls(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _sessions(setup_home)
    _ingest(setup_home, db)
    clock = [0.0]

    def slow(text_: str, *, timeout: float = 60.0) -> str:
        clock[0] += 8.0
        return SUMMARY

    monkeypatch.setattr(session_summary, "call_model", slow)
    got = session_summary.summarize(days=7, now=NOW, db_url=db, budget=15.0, clock=lambda: clock[0])
    assert got.done == 1 and got.left >= 1
    # The next run picks up the rest.
    clock[0] = 0.0
    assert session_summary.summarize(days=7, now=NOW, db_url=db, budget=100.0, clock=lambda: clock[0]).done == 2


def test_ingest_runs_the_summary_step_and_never_fails_on_it(setup_home: Home, db: str,  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    _sessions(setup_home)
    off = _ingest(setup_home, db)                              # live flag off: no step
    assert off.summaries == 0 and _row(db, "committed-1")["summary"] is None
    monkeypatch.setenv(session_summary.LIVE_ENV, "1")
    fake = FakeModel(distill.WriterError("timed out after 60s"), "ok")   # a failure, junk, then a good one
    monkeypatch.setattr(session_summary, "call_model", fake)
    monkeypatch.setenv(session_summary.CONCURRENCY_ENV, "1")
    got = _ingest(setup_home, db)
    assert (got.summaries, got.summary_failed) == (1, 2) and "summary_failed=2" in got.line()
    assert _row(db, "committed-1")["summary"] is None and _row(db, "plain-1")["summary"] == SUMMARY
    # The whole step blowing up still leaves the ingest green.
    monkeypatch.setattr(session_summary, "summarize", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    broken = _ingest(setup_home, db)
    assert broken.summary_error == "RuntimeError" and broken.files > 0


def test_backfill_refreshes_commits_without_touching_other_keys(setup_home: Home, db: str,  # noqa: F811
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    _sessions(setup_home)
    _ingest(setup_home, db)
    with psycopg.connect(db) as conn:   # a row stored before commits existed
        conn.execute("update sessions set outcome_evidence = outcome_evidence - 'commits' "
                     "where session_id = 'committed-1'")
    before = _row(db, "committed-1")["outcome_evidence"]
    dry = session_summary.summarize(days=7, now=NOW, db_url=db, dry_run=True, refresh_commits=True)
    assert dry.commits_updated == 1 and dry.done == 3 and "commits" not in _row(db, "committed-1")["outcome_evidence"]
    monkeypatch.setattr(session_summary, "call_model", FakeModel())
    got = session_summary.summarize(days=7, now=NOW, db_url=db, refresh_commits=True)
    after = _row(db, "committed-1")["outcome_evidence"]
    assert got.commits_updated == 1 and after["commits"][0]["sha"] == "1a2b3c4"
    assert {k: v for k, v in after.items() if k != "commits"} == before


def test_summary_columns_exist(db: str) -> None:
    with psycopg.connect(db) as conn:
        cols = dict(conn.execute("select column_name, data_type from information_schema.columns "
                                 "where table_name = 'sessions' and column_name like 'summary%%'").fetchall())
    assert cols == {"summary": "text", "summary_updated_at": "timestamp with time zone"}


def test_cli_summarize_dry_run_and_live_gate(setup_home: Home, db: str) -> None:  # noqa: F811
    _sessions(setup_home)
    _ingest(setup_home, db)
    env = base_env(AMEM_DB_URL=db, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_PROJECTS_DIR=str(setup_home.projects), AMEM_SUMMARY_CLAUDE="/nonexistent/claude")
    res = run_amem("sessions", "summarize", "--days", "3650", "--dry-run", env=env)
    assert res.returncode == 0, res.stderr
    assert re.fullmatch(r"summaries: would summarize \d+, skipped-no-transcript \d+, skipped-subagent \d+, "
                        r"failed 0 \(up-to-date \d+, not-finished \d+, left-for-next-run 0, commits-updated \d+\)\n",
                        res.stdout), res.stdout
    gated = run_amem("sessions", "summarize", "--days", "7", env=env)
    assert gated.returncode == 2 and "AMEM_SUMMARY_LIVE" in gated.stderr
