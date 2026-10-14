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
SUMMARY = "The session fixed the flaky parser test in parser.py and committed the fix. The work finished."


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
    ("Summary: The session did one thing.  Then another thing. Then a third.",
     "The session did one thing. Then another thing. Then a third."),
    ("The session did one thing. Then another thing. Then a third. And a fourth.", None),   # 4 sentences
    ("This session fixed the parser in parser.py.", "The session fixed the parser in parser.py."),
    # Not about the session: the agent's own reply, advice, first person. Never stored.
    ("A handoff from 19 hours ago exists. Treat its claims as unverified.", None),
    ("I fixed the parser and committed it.", None), ("In this session the parser was fixed.", None),
    ("", None), ("   ", None), ("ok", None), ("I can't help with that request, sorry.", None),
    ('{"summary": "json is junk here"}', None), ("```\nfenced text here\n```", None),
])
def test_clean(reply: str, want: str | None) -> None:
    assert session_summary.clean(reply) == want


def test_clean_redacts_and_rejects_overlong() -> None:
    got = session_summary.clean("The session set api_key=abcdefghijklmnop0123 in the config.")
    assert got is not None and "abcdefghijklmnop0123" not in got
    assert session_summary.clean("The session set a flag in the config " + "word " * 300 + "today.") is None


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
    assert " summary=off" in off.line() and "summaries=" not in off.line()
    monkeypatch.setenv(session_summary.LIVE_ENV, "1")
    # a failure; junk twice (the reply and its one retry); then a good one
    fake = FakeModel(distill.WriterError("timed out after 60s"), "ok", "ok")
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
                        r"skipped-empty \d+, failed 0 \(up-to-date \d+, not-finished \d+, left-for-next-run 0, commits-updated \d+, "
                        r"bad-summaries \d+\)\n" + COMMITS_LINE + r"\n", res.stdout), res.stdout
    gated = run_amem("sessions", "summarize", "--days", "7", env=env)
    assert gated.returncode == 2 and "AMEM_SUMMARY_LIVE" in gated.stderr
    for flags in (["--redo-bad", "--dry-run"], ["--commits-only", "--dry-run"]):
        res = run_amem("sessions", "summarize", "--days", "3650", *flags, env=env)
        assert res.returncode == 0, res.stderr
        assert re.search(COMMITS_LINE, res.stdout) and ("summaries:" in res.stdout) == ("--redo-bad" in flags)


COMMITS_LINE = (r"commits: committed sessions with a non-empty commits list: before \d+/\d+, (?:after|would be) "
                r"\d+/\d+; top-level before \d+/\d+, (?:after|would be) \d+/\d+ \(rows updated \d+, from git log \d+\)")


# --------------------------------------------------------------------------- third-person summaries


ADVICE = ("A handoff from 19 hours ago exists. Treat its claims as unverified and check the branch before "
          "you continue.")


def _advice_session(home: Home) -> str:
    cwd = str(home.repo)
    events = (chat_events(cwd) + commit(cwd, "c1", "[main 1a2b3c4] Fix the parser", minute=7)
              + [assistant(text(ADVICE), minute=9, cwd=cwd)])
    write_transcript(home.project("-work-demo-repo") / "advice-1.jsonl", events)
    return "advice-1"


def test_advice_like_last_message_gets_a_third_person_summary(setup_home: Home, db: str,  # noqa: F811
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _advice_session(setup_home)
    _ingest(setup_home, db)
    # The model echoes the agent's last reply first: rejected, one retry, the retry is stored.
    fake = FakeModel(ADVICE)
    monkeypatch.setattr(session_summary, "call_model", fake)
    got = session_summary.summarize(days=7, now=NOW, db_url=db)
    stored = _row(db, sid)["summary"]
    assert (got.done, got.failed) == (1, 0) and stored == SUMMARY and stored.startswith("The session")
    first, retry = fake.prompts
    record = first.index("<session_record>\n=== Facts")
    assert record < first.index(ADVICE) < first.rindex("</session_record>")
    assert 'must begin with "The session"' in first and retry == first + session_summary.RETRY_NOTE


def test_non_compliant_reply_is_never_stored(setup_home: Home, db: str,  # noqa: F811
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _advice_session(setup_home)
    _ingest(setup_home, db)
    fake = FakeModel(default=ADVICE)   # the reply and its retry both continue the transcript
    monkeypatch.setattr(session_summary, "call_model", fake)
    got = session_summary.summarize(days=7, now=NOW, db_url=db)
    assert (got.done, got.failed, len(fake.prompts)) == (0, 1, 2)
    assert _row(db, sid)["summary"] is None and "does not start with 'The session'" in got.errors[0]


REFUSAL = "The session record was not included in this message, so I cannot describe what the session did."


@pytest.mark.parametrize("reply,ok", [
    (REFUSAL, False),                                                        # the 2026-10-14 15:00 row
    ("The session could not be described because no transcript was provided.", False),
    ("The session cannot be summarized from the data given.", False),
    ("The session did something, but I can't tell what from this prompt.", False),
    ("The session is unclear; I only see the facts block.", False),
    ("The session could not access the RemoteTrigger tool, so it audited the routines from the repos.", True),
    ("The session fixed the I/O retry in io.py and found no transcript files to migrate; it finished.", True),
])
def test_refusals_are_rejected_even_with_the_prefix(reply: str, ok: bool) -> None:
    assert session_summary.compliant(reply) is ok
    assert (session_summary.clean(reply) is not None) is ok


GOOD_STORED = [
    "The session ran the handoff command in the handoff-chat repo and produced a chat handoff with no recorded task. "
    "No files were changed and no commits were made.",
    "The session ran a scheduled weekly audit of skill usage across the parent workflow and wrote a report to "
    "~/.claude/parent/reports/skill-evals-2026-10-14.md.",
    "The session could not access the RemoteTrigger tool, so it listed the routines from the repo files instead. "
    "No files were changed.",
]


@pytest.mark.parametrize("text", GOOD_STORED)
def test_validator_accepts_real_summaries(text: str) -> None:
    assert session_summary.summary_problem(text) is None and session_summary.valid_summary(text)
    assert session_summary.clean(text) == text


@pytest.mark.parametrize("text,why", [
    ("The session record is missing from this input, so no summary was written.", "says the input is missing"),
    ("The Codex chat session in the spyre-live-codex repo recorded no code changes. The digest shows only "
     "session-start context.", "does not start with 'The session'"),
    ("The session in the spyre-live-codex repo recorded no code changes. The digest shows only session-start "
     "context.", "talks about the prompt"),
    ("The session transcript was empty, so nothing can be said about it.", "says the input is missing"),
    ("The session details were not included in the provided facts.", "says the input is missing"),
    ("The session is described here, but no summary was produced.", "says no summary"),
    ("The session could not be summarized.", "says it cannot describe"),
    ("The session fixed a bug, as you asked.", "addresses the reader"),
    ("The session fixed my parser in parser.py.", "first person"),
    ("The session ended before this message could be read.", "talks about the prompt"),
    ("The session record shows a fix to parser.py.", "talks about the prompt"),
    ("## Summary\nThe session fixed parser.py.", "does not start with 'The session'"),
    ("The session fixed parser.py:\n- added a test\n- committed it.", "markdown, not plain prose"),
    ("The session fixed parser.py.\n```\ncode\n```", "markdown, not plain prose"),
    ("The session fixed one thing. It fixed two. It fixed three. It fixed four.", "more than 3 sentences"),
    ("The session fixed the parser and", "not a finished sentence"),
    ("", "empty"), ("   ", "empty"), (None, "empty"),
    ("The session did.", "too short"),
])
def test_validator_rejects(text: Any, why: str) -> None:
    assert session_summary.summary_problem(text) == why
    assert session_summary.clean(text) is None


def test_redo_bad_selects_rows_failing_the_validator() -> None:
    old = NOW - timedelta(hours=1)
    for bad in ("The session record is missing from this input, so no summary was written.",
                "The session did a. It did b. It did c. It did d."):
        assert session_summary.skip_reason(_rec(summary=bad, summary_updated_at=old), NOW, redo_bad=True) is None
        assert session_summary.skip_reason(_rec(summary=bad, summary_updated_at=old), NOW) == "current"
    assert session_summary.skip_reason(_rec(summary=GOOD_STORED[0], summary_updated_at=old), NOW,
                                       redo_bad=True) == "current"


def test_refusal_reply_is_retried_and_never_stored(setup_home: Home, db: str,  # noqa: F811
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _advice_session(setup_home)
    _ingest(setup_home, db)
    fake = FakeModel(default=REFUSAL)
    monkeypatch.setattr(session_summary, "call_model", fake)
    got = session_summary.summarize(days=7, now=NOW, db_url=db)
    assert (got.done, got.failed, len(fake.prompts)) == (0, 1, 2) and _row(db, sid)["summary"] is None
    assert "previous reply" not in fake.prompts[1] and "<session_record> tags" in fake.prompts[1]
    monkeypatch.setattr(session_summary, "call_model", FakeModel(REFUSAL))   # refusal, then a good retry
    assert session_summary.summarize(days=7, now=NOW, db_url=db).done == 1 and _row(db, sid)["summary"] == SUMMARY


def test_empty_digest_is_skipped_without_a_model_call(setup_home: Home, db: str,  # noqa: F811
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = str(setup_home.repo)
    write_transcript(setup_home.project("-work-demo-repo") / "thin-1.jsonl",
                     [user("hi", 0, cwd=cwd), *commit(cwd, "c1", "[main 1a2b3c4] x", minute=2)])
    _ingest(setup_home, db)
    assert _row(db, "thin-1")["outcome"] not in (None, "active")
    fake = FakeModel()
    monkeypatch.setattr(session_summary, "call_model", fake)
    got = session_summary.summarize(days=7, now=NOW, db_url=db)
    assert got.skipped.get("empty") == 1 and got.done == 0 and fake.prompts == []
    assert "skipped-empty 1" in got.line() and _row(db, "thin-1")["summary"] is None
    assert not session_summary.substantial("") and not session_summary.substantial("[turn 1] USER: " + "x" * 400)
    assert session_summary.substantial("[turn 1] USER: fix it\nTOOL Bash: pytest\nASSISTANT: " + "y" * 200)


def test_redo_bad_also_redoes_a_stored_refusal(setup_home: Home, db: str,  # noqa: F811
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    _sessions(setup_home)
    _ingest(setup_home, db)
    monkeypatch.setattr(session_summary, "call_model", FakeModel())
    session_summary.summarize(days=7, now=NOW, db_url=db)
    with psycopg.connect(db) as conn:
        conn.execute("update sessions set summary = %s where session_id = 'committed-1/agent-w1'", (REFUSAL,))
    dry = session_summary.summarize(days=7, now=NOW, db_url=db, dry_run=True, redo_bad=True)
    assert (dry.bad, dry.done) == (1, 1)
    assert session_summary.summarize(days=7, now=NOW, db_url=db, redo_bad=True).done == 1
    assert _row(db, "committed-1/agent-w1")["summary"] == SUMMARY


def test_commits_only_drops_a_stored_shell_syntax_repo(setup_home: Home, db: str) -> None:  # noqa: F811
    _sessions(setup_home)
    _ingest(setup_home, db)
    bad = {"sha": None, "message": "base", "repo": "$(mktemp", "branch": None}
    good = {"sha": "1a2b3c4", "message": "Fix the parser", "repo": "demo-repo", "branch": "main"}
    with psycopg.connect(db) as conn:   # stored by the older parser; the transcript now gives nothing
        conn.execute("update sessions set outcome_evidence = outcome_evidence || %s::jsonb "
                     "where session_id = 'committed-1/agent-q1'", (json.dumps({"commit": 1, "commits": [bad]}),))
        conn.execute("update sessions set outcome_evidence = outcome_evidence || %s::jsonb "
                     "where session_id = 'plain-1'", (json.dumps({"commits": [bad, good]}),))
    got = session_summary.summarize(days=7, now=NOW, db_url=db, commits_only=True)
    assert got.commits_updated == 2
    assert _row(db, "committed-1/agent-q1")["outcome_evidence"]["commits"] == []
    assert _row(db, "plain-1")["outcome_evidence"]["commits"] == [good]
    assert session_summary.bad_commit(bad) and not session_summary.bad_commit(good)


def test_redo_bad_summarizes_the_bad_rows_again(setup_home: Home, db: str,  # noqa: F811
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    _sessions(setup_home)
    _ingest(setup_home, db)
    monkeypatch.setattr(session_summary, "call_model", FakeModel())
    assert session_summary.summarize(days=7, now=NOW, db_url=db).done == 3
    with psycopg.connect(db) as conn:   # one row holds an old summary: the agent's own reply
        conn.execute("update sessions set summary = %s where session_id = 'committed-1'", (ADVICE,))
    plain = session_summary.summarize(days=7, now=NOW, db_url=db, dry_run=True)
    assert plain.done == 0 and plain.bad == 1                       # without the flag: up to date
    dry = session_summary.summarize(days=7, now=NOW, db_url=db, dry_run=True, redo_bad=True)
    assert dry.done == 1 and dry.bad == 1 and _row(db, "committed-1")["summary"] == ADVICE
    got = session_summary.summarize(days=7, now=NOW, db_url=db, redo_bad=True)
    assert got.done == 1 and _row(db, "committed-1")["summary"] == SUMMARY
    assert session_summary.summarize(days=7, now=NOW, db_url=db, dry_run=True, redo_bad=True).bad == 0


def test_ingest_line_shows_summaries_zero_when_live_and_nothing_is_eligible(setup_home: Home, db: str,  # noqa: F811
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(session_summary.LIVE_ENV, "1")
    monkeypatch.setattr(session_summary, "call_model", FakeModel())
    got = _ingest(setup_home, db)            # no transcripts at all
    assert got.summary_live and " summaries=0" in got.line() and "summary=off" not in got.line()
    monkeypatch.setenv(session_summary.LIVE_ENV, "0")
    assert " summary=off" in _ingest(setup_home, db).line()


# --------------------------------------------------------------------------- commit backfill


def test_commits_only_backfill_counts_before_and_after(setup_home: Home, db: str) -> None:  # noqa: F811
    _sessions(setup_home)
    _ingest(setup_home, db)
    with psycopg.connect(db) as conn:   # rows stored by the old parser: no commits list
        conn.execute("update sessions set outcome_evidence = outcome_evidence - 'commits'")
    dry = session_summary.summarize(days=7, now=NOW, db_url=db, dry_run=True, commits_only=True)
    assert (dry.committed, dry.with_commits_before, dry.with_commits_after) == ([2, 1], [0, 0], [2, 1])
    assert dry.done == 0 and "commits" not in _row(db, "committed-1")["outcome_evidence"]
    assert "before 0/2, would be 2/2; top-level before 0/1, would be 1/1" in dry.commits_line(dry_run=True)
    got = session_summary.summarize(days=7, now=NOW, db_url=db, commits_only=True)
    assert got.commits_updated == 2 and _row(db, "committed-1")["outcome_evidence"]["commits"][0]["sha"] == "1a2b3c4"
    assert session_summary.summarize(days=7, now=NOW, db_url=db, dry_run=True,
                                     commits_only=True).with_commits_before == [2, 1]


def test_backfill_finds_a_transcript_that_moved_project_folder(setup_home: Home, db: str) -> None:  # noqa: F811
    _sessions(setup_home)
    _ingest(setup_home, db)
    with psycopg.connect(db) as conn:   # the row names the folder the session started in
        conn.execute("update sessions set project_dir = '-work-elsewhere', "
                     "outcome_evidence = outcome_evidence - 'commits' where session_id = 'committed-1'")
    rec = {**_row(db, "committed-1")}
    assert session_summary.transcript_of(rec) == setup_home.projects / "-work-demo-repo" / "committed-1.jsonl"
    got = session_summary.summarize(days=7, now=NOW, db_url=db, commits_only=True)
    assert got.commits_updated == 1 and _row(db, "committed-1")["outcome_evidence"]["commits"][0]["sha"] == "1a2b3c4"
