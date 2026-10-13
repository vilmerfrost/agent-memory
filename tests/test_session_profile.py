"""Session profile columns (docs/sessions-live.md): title, project + category, model, effort,
duration, outcome; the session_groups view and `amem sessions tokens --by`. Synthetic lines only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import amem_digest, codex_sessions, deviations, session_live, session_tokens, sessions_ingest
from agent_memory import tokens_report
from codex_support import Rollout, codex_home, tid
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    T0,
    Home,
    assistant,
    result,
    setup_home,
    text,
    tool,
    user,
    write_transcript,
)
from test_session_tokens import reply, usage

CWD = "/work/demo-repo"


def title_line(t: str) -> dict[str, Any]:
    return {"type": "custom-title", "customTitle": t, "sessionId": "s1"}


def at(minute: float) -> str:
    return (T0 + timedelta(minutes=minute)).isoformat()


def r(mid: str, model: str, out: int, minute: float, effort: str | None = None, *blocks: dict[str, Any]) -> dict[str, Any]:
    e = reply(mid, usage(1, out), *(blocks or (text("ok"),)), model=model)
    e["timestamp"] = at(minute)
    if effort:
        e["effort"] = e["perTurnEffort"] = effort
    return e


# --------------------------------------------------------------------------- title


def test_title_last_rename_wins_redacted_and_capped() -> None:
    key = "sk-ant-" + "T" * 30
    events = [title_line("First name"), user("hi", 0), title_line(f"Renamed  {key}\n" + "y" * 400)]
    s = session_tokens.claude_summary(events)
    assert s.title is not None and s.title.startswith("Renamed [redacted] yyy") and len(s.title) == 200
    assert key not in s.title and amem_digest.title_of(events) == s.title
    assert session_tokens.claude_summary([title_line("A"), title_line("B")]).title == "B"
    assert session_tokens.claude_summary([user("no title", 0)]).title is None  # never invented


def test_codex_titles_from_the_session_index(tmp_path: Path) -> None:
    lines = [{"id": tid(1), "thread_name": "Old name", "updated_at": "2026-10-14T09:00:00Z"},
             {"id": tid(1), "thread_name": "New name", "updated_at": "2026-10-14T10:00:00Z"},
             {"id": tid(1), "thread_name": "Stale line", "updated_at": "2026-10-14T08:00:00Z"},
             {"id": tid(2), "thread_name": "", "updated_at": "2026-10-14T08:00:00Z"},
             {"thread_name": "no id"}]
    (tmp_path / "session_index.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    assert sessions_ingest.codex_titles(tmp_path) == {tid(1): "New name"}


# --------------------------------------------------------------------------- model, effort, time


def switch_events() -> list[dict[str, Any]]:
    """Model A writes most output early; the user switches to model B (and effort high -> low)."""
    return [
        user("go", 0),
        r("m1", "claude-a", 500, 1, "high"),
        r("m1", "claude-a", 600, 1.1, "high"),     # the same response again: one reply
        r("m2", "claude-a", 100, 2, "high"),
        r("m2b", "claude-a", 10, 2.5, "high"),
        r("m3", "claude-b", 50, 30, "low"),        # 28 min idle before this one
        r("m4", "claude-b", 50, 31, "low"),
        {"type": "assistant", "timestamp": at(32), "message": {"model": "<synthetic>", "usage": usage(0, 0)}},
        {"type": "cost-state", "timestamp": at(32), "totalAPIDuration": 4500, "totalToolDuration": 1250,
         "totalLinesAdded": 12, "totalLinesRemoved": 3, "totalDuration": 1_920_000},
    ]


def test_model_effort_and_time_with_a_mid_session_switch() -> None:
    p = session_tokens.claude_summary(switch_events()).profile()
    assert (p["model"], p["model_last"]) == ("claude-a", "claude-b")
    assert (p["effort"], p["effort_last"], p["efforts"]) == ("high", "low", {"high": 3, "low": 2})
    assert p["duration_seconds"] == 32 * 60
    assert p["active_seconds"] == pytest.approx(4.5 * 60)  # the 27.5-minute gap is idle
    assert (p["api_seconds"], p["tool_seconds"], p["lines_added"], p["lines_removed"]) == (4.5, 1.25, 12, 3)
    assert amem_digest.profile(switch_events()) == p  # the cloud digest computes the same


def test_ties_go_to_the_most_recent_and_missing_is_null() -> None:
    p = session_tokens.claude_summary([r("a", "claude-a", 10, 1, "high"), r("b", "claude-b", 10, 2, "low")]).profile()
    assert (p["model"], p["effort"]) == ("claude-b", "low")
    bare = session_tokens.claude_summary([user("hi", 0)]).profile()
    assert bare["model"] is None and bare["effort"] is None and bare["efforts"] == {}
    assert bare["api_seconds"] is None and bare["lines_added"] is None and bare["duration_seconds"] == 0
    assert session_tokens.claude_summary([]).profile()["active_seconds"] is None


def test_incremental_scan_keeps_profile_and_outcome(tmp_path: Path) -> None:
    events = switch_events()
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events[:3]))
    sc = session_tokens.ClaudeScanner()
    sc.scan_file(path)
    sc2 = session_tokens.ClaudeScanner(json.loads(json.dumps(sc.state())))
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    sc2.scan_file(path)
    assert sc2.summary.profile() == session_tokens.claude_summary(events).profile()


def test_codex_model_effort_and_time() -> None:
    ro = Rollout(tid(5), CWD).injected()
    ro.add("turn_context", {"cwd": CWD, "model": "gpt-a", "effort": "high"}, minute=1)

    def count(total_out: int, minute: int) -> None:
        ro.add("event_msg", {"type": "token_count",
                             "info": {"total_token_usage": {"input_tokens": 10 * minute, "output_tokens": total_out}}},
               minute)

    count(100, 2)
    count(200, 3)
    ro.add("turn_context", {"cwd": CWD, "model": "gpt-b", "reasoning_effort": "low"}, minute=40)
    count(250, 41)
    p = session_tokens.codex_summary(ro.events).profile()
    assert (p["model"], p["model_last"], p["effort"], p["effort_last"]) == ("gpt-a", "gpt-b", "high", "low")
    assert p["duration_seconds"] == 41 * 60 and p["active_seconds"] == pytest.approx(4 * 60)
    assert p["api_seconds"] is None


# --------------------------------------------------------------------------- outcome


def bash(tid_: str, command: str, minute: int = 1) -> dict[str, Any]:
    return assistant(tool("Bash", tid_, command=command), minute=minute)


def ok(tid_: str, minute: int = 1) -> dict[str, Any]:
    return result(tid_, "ok", minute)


def outcome_of(events: list[dict[str, Any]], now: datetime) -> tuple[str, dict[str, Any]]:
    return session_tokens.claude_summary(events).outcome(now)


LATER = T0 + timedelta(hours=5)       # past "active", not yet "abandoned"
MUCH_LATER = T0 + timedelta(days=3)


@pytest.mark.parametrize(("name", "events", "now"), [
    ("shipped", [bash("1", "git push origin main"), ok("1"), assistant(text("done"), minute=2)], LATER),
    ("shipped", [bash("1", "gh pr merge 12 --merge"), ok("1")], LATER),
    ("shipped", [bash("1", "git checkout main && git merge feature"), ok("1")], LATER),
    ("shipped", [bash("1", "vercel deploy --prod"), ok("1")], LATER),
    ("shipped", [bash("1", "uv run amem db migrate"), ok("1")], LATER),
    ("pushed", [bash("1", "git -C /w push -u origin worktree-x"), ok("1"), bash("2", "gh pr create --fill"), ok("2")],
     LATER),
    ("committed", [assistant(tool("Edit", "e1", file_path="a.py")), ok("e1"), bash("1", "git commit -m x"), ok("1")],
     MUCH_LATER),
    ("partial", [assistant(tool("Write", "e1", file_path="a.py")), ok("e1"), assistant(text("stopped"), minute=2)],
     LATER),
    ("partial", [assistant(tool("SubagentHandback", "h", message="Status: PARTIAL\nOpen: x")), ok("h")], MUCH_LATER),
    ("active", [user("go", 0), assistant(tool("Edit", "e1", file_path="a.py")), ok("e1")], T0 + timedelta(hours=1)),
    ("abandoned", [user("go", 0), assistant(text("hm"), minute=1), user("are you there?", 2)], MUCH_LATER),
    ("abandoned", [user("go", 0), bash("1", "sleep 99")], MUCH_LATER),
    ("abandoned", [assistant(tool("Edit", "e1", file_path="a.py")), ok("e1"), assistant(text("done"), minute=2)],
     MUCH_LATER),
    ("failed", [user("go", 0), bash("1", "pytest"), result("1", "boom", 2, error=True)], LATER),
    ("failed", [user("go", 0), assistant(text("x"), minute=1), user("[Request interrupted by user]", 2)], MUCH_LATER),
    ("no-change", [user("what is X?", 0), assistant(text("X is Y."), minute=1)], MUCH_LATER),
    ("no-change", [bash("1", "git status && git log -3"), ok("1"), assistant(text("clean"), minute=2)], MUCH_LATER),
])
def test_outcome_states(name: str, events: list[dict[str, Any]], now: datetime) -> None:
    got, evidence = outcome_of(events, now)
    assert got == name, evidence


def test_outcome_precedence_and_evidence_has_no_command_text() -> None:
    secret = "sk-ant-" + "E" * 30
    events = [assistant(tool("Edit", "e1", file_path="/secret/path.py")), ok("e1"),
              bash("1", f"git commit -m 'msg {secret}'"), ok("1"),
              bash("2", "git push origin feature-branch"), ok("2"),
              bash("3", "gh pr merge 7"), ok("3"),
              assistant(text("Status: DONE"), minute=3)]
    got, evidence = outcome_of(events, MUCH_LATER)
    assert got == "shipped"  # the most advanced state wins
    assert evidence == {"commit": 1, "edit": 1, "pr_merge": 1, "push": 1, "report_done": 1, "end": "reply"}
    dumped = json.dumps(evidence)
    for leak in (secret, "feature-branch", "/secret", "msg", "gh pr"):
        assert leak not in dumped
    assert set(evidence) - {"end"} <= {"edit", "commit", "push", "push_main", "pr_create", "pr_merge",
                                       "merge_main", "deploy", "report_done", "report_partial", "report_blocked"}


def test_command_signals() -> None:
    cs = amem_digest.command_signals
    assert cs("git push origin HEAD:main") == ["push_main"]
    assert cs("git push -u origin worktree-main-fix") == ["push"]
    assert cs("echo git push is mentioned") == []  # `git` must be the command word
    assert cs("git merge feature") == []           # a merge, but not known to be into main
    assert cs("supabase functions deploy x; railway up") == ["deploy", "deploy"]


def test_codex_outcome_signals() -> None:
    ro = Rollout(tid(6), CWD).injected().user("ship it", 1)
    ro.file_change("a.py", minute=2).command("git commit -am x && git push origin main", minute=3)
    ro.assistant("Status: DONE", minute=4)
    signals, end = codex_sessions.outcome_signals(ro.events)
    assert signals == {"commit": 1, "edit": 1, "push_main": 1, "report_done": 1} and end == "reply"
    assert session_tokens.codex_summary(ro.events).outcome(MUCH_LATER)[0] == "shipped"
    aborted = Rollout(tid(7), CWD).injected().user("go", 1).aborted(2)
    assert codex_sessions.outcome_signals(aborted.events) == ({}, "interrupt")


def test_digest_carries_outcome_signals() -> None:
    events = [bash("1", "git push origin main"), ok("1"), assistant(text("done"), minute=2)]
    assert amem_digest.outcome_signals(events) == {"signals": {"push_main": 1}, "end": "reply"}


# --------------------------------------------------------------------------- project and category


def git_repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def test_project_rules(tmp_path: Path) -> None:
    po = sessions_ingest.project_of
    main = git_repo(tmp_path / "Projects" / "agent-memory")
    wt = tmp_path / "anywhere" / "agent-a05a5bfb2f142d702"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/agent-a05a5bfb2f142d702\n")
    claude_wt = main / ".claude" / "worktrees" / "agent-a1b2"
    claude_wt.mkdir(parents=True)
    spaced = git_repo(tmp_path / "Projects" / "simplitics drift check")
    (spaced / "videos").mkdir()
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    cases = {
        str(main): "agent-memory",
        str(main / "src" / "deep"): "agent-memory",                          # inside a git repo
        str(wt): "agent-memory",                                            # git worktree checkout
        str(claude_wt): "agent-memory",                                     # <repo>/.claude/worktrees/<x>
        "/Users/v/worktrees/agent-memory/agent-a297498c7b2d0145a": "agent-memory",
        str(spaced / "videos"): "simplitics drift check",
        "/Users/v/Library/Application Support/Claude/scratch-workspaces/abc/scratch-2026-10-11": "scratch",
        "/Users/v/Documents/Codex/2026-10-13/use-the-handoff-skill-at-users": "scratch",
        "/private/tmp/claude-502/-Users-v/abc/scratchpad": "scratch",
        "/Users/v/.cache/agent-evals/eval-jo-a": "evals",
        "/tmp/x/eval-jo-f/repo": "evals",
        str(plain): "plain-folder",
        str(Path.home()): "home",
        "": "unknown",
    }
    for cwd, want in cases.items():
        assert po(cwd, kind="chat") == want, cwd
    assert po("/Users/v/Projects/x", kind="routine", routine="evals-parent") == "routine:evals-parent"
    assert po("", kind="routine", routine="Nightly Trials") == "routine:nightly-trials"
    assert po("", kind="routine") == "routine:unknown"
    assert po("/Users/v/Projects/x", kind="eval") == "evals"
    assert po("", kind="chat", scratch_only=True) == "scratch"
    assert po("", kind="chat", project_dir="-Users-v--cache-agent-evals-eval-jo-b") == "evals"
    assert po("", kind="chat", repo="from-digest") == "from-digest"
    for kind in sessions_ingest.KINDS:
        assert sessions_ingest.category_of(kind) == kind


# --------------------------------------------------------------------------- database


@pytest.fixture
def db(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
        conn.execute("delete from cloud_digests")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    return pg_url


def _rows(url: str, sql: str = "select * from sessions order by session_id") -> list[dict[str, Any]]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(sql).fetchall()


def test_ingest_fills_every_profile_column(setup_home: Home, db: str) -> None:  # noqa: F811
    cwd = str(setup_home.repo)
    events = [title_line("Parser fix"), user("fix it", 0, cwd=cwd)] + switch_events()[1:]
    for e in events:
        e["cwd"] = cwd
    write_transcript(setup_home.project("-work-demo-repo") / "s-profile.jsonl", events, pad=False)
    ro = Rollout(tid(9), cwd).injected().user("hello", 1)
    ro.add("turn_context", {"cwd": cwd, "model": "gpt-a", "effort": "medium"}, minute=1)
    ro.add("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 5,
                                                                               "output_tokens": 7}}}, 2)
    ro.assistant("ok", 2).write(setup_home, pad=False)
    (codex_home(setup_home) / "session_index.jsonl").write_text(
        json.dumps({"id": tid(9), "thread_name": "Codex chat", "updated_at": "2026-10-14T09:05:00Z"}) + "\n")
    report = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert report.new == 2
    claude, codex = _rows(db, "select * from sessions order by tool")
    assert (claude["title"], claude["category"], claude["project"]) == ("Parser fix", "chat", "demo-repo")
    assert (claude["model"], claude["model_last"], claude["effort"], claude["effort_last"]) == (
        "claude-a", "claude-b", "high", "low")
    assert claude["efforts"] == {"high": 3, "low": 2} and claude["duration_seconds"] == 32 * 60
    assert (claude["api_seconds"], claude["tool_seconds"], claude["lines_added"]) == (4.5, 1.25, 12)
    assert claude["outcome"] == "no-change" and claude["outcome_evidence"]["end"] == "reply"
    assert claude["title_updated_at"] is not None and claude["outcome_at"] is not None
    assert (codex["title"], codex["model"], codex["effort"], codex["category"]) == ("Codex chat", "gpt-a", "medium",
                                                                                  "chat")
    # Backfill: a row stored before these columns existed gets them on the next ingest.
    with psycopg.connect(db) as conn:
        conn.execute("update sessions set title = null, category = null, project = null, model = null, "
                     "outcome = null, title_updated_at = null")
    again = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert again.updated == 2
    claude = _rows(db, "select * from sessions where tool = 'claude'")[0]
    assert (claude["title"], claude["category"], claude["model"], claude["outcome"]) == (
        "Parser fix", "chat", "claude-a", "no-change")
    assert sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db).updated == 0  # idempotent


def test_ping_fills_title_profile_and_outcome(setup_home: Home, db: str) -> None:  # noqa: F811
    cwd = str(setup_home.repo)
    sid = "5e55a0b1-0000-4000-8000-0000000prof"
    path = write_transcript(setup_home.project("-work-demo-repo") / f"{sid}.jsonl",
                            [title_line("Live name"), user("go", 0, cwd=cwd)] + switch_events()[1:], pad=False)
    payload = json.dumps({"session_id": sid, "transcript_path": str(path), "cwd": cwd, "hook_event_name": "Stop"})
    assert session_live.ping(payload, now=T0 + timedelta(minutes=33), db_url=db) == "written"
    (row,) = _rows(db)
    assert (row["title"], row["category"], row["project"], row["model"], row["model_last"]) == (
        "Live name", "chat", "demo-repo", "claude-a", "claude-b")
    assert row["outcome"] == "active" and row["effort"] == "high" and row["api_seconds"] == 4.5


def seed_groups(url: str) -> None:
    rows = [("g1", "chat", "agent-memory", "claude-a", "high", "shipped", 100, 600.0, 300.0),
            ("g2", "chat", "agent-memory", "claude-b", "high", "no-change", 50, 1200.0, 100.0),
            ("g3", "routine", "routine:nightly-trials", "claude-a", None, "failed", 10, 60.0, 60.0)]
    with psycopg.connect(url) as conn:
        for sid, cat, proj, model, effort, outcome, out, dur, act in rows:
            conn.execute(
                "insert into sessions (session_id, tool, kind, repo, project_dir, started_at, last_activity_at, meta,"
                " tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation, models, category, project,"
                " model, effort, outcome, duration_seconds, active_seconds, title) values (%s, 'claude', %s, 'r', 'p',"
                " %s, %s, %s, 1, %s, 0, 0, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (sid, cat, NOW - timedelta(hours=1), NOW - timedelta(hours=1), Jsonb({}), out,
                 Jsonb({model: {"input": 1, "output": out, "cache_read": 0, "cache_creation": 0, "calls": 1}}),
                 cat, proj, model, effort, outcome, dur, act, f"Title {sid}"))


def test_session_groups_view(db: str) -> None:
    seed_groups(db)
    got = {(g["category"], g["project"]): g for g in _rows(db, "select * from session_groups")}
    chat = got[("chat", "agent-memory")]
    assert chat["sessions"] == 2 and chat["tokens_output"] == 150 and chat["tokens_total"] == 152
    assert chat["duration_seconds"] == 1800 and chat["duration_median_seconds"] == 900
    assert chat["active_seconds"] == 400 and chat["active_median_seconds"] == 200
    assert chat["models"] == {"claude-a": 1, "claude-b": 1} and chat["efforts"] == {"high": 2}
    assert chat["outcomes"] == {"shipped": 1, "no-change": 1}
    assert got[("routine", "routine:nightly-trials")]["efforts"] == {}
    opts = _rows(db, "select reloptions from pg_class where relname = 'session_groups'")[0]["reloptions"]
    assert "security_invoker=true" in opts


def test_tokens_report_groups_by_category_then_project(db: str) -> None:
    seed_groups(db)
    doc = tokens_report.summarize(tokens_report.fetch(days=7, now=NOW, db_url=db), days=7)
    (chat, routine) = doc["by_category"]
    assert chat["key"] == "chat" and [p["key"] for p in chat["projects"]] == ["agent-memory"]
    assert routine["projects"][0]["key"] == "routine:nightly-trials"
    assert {g["key"]: g["sessions"] for g in doc["by_outcome"]} == {"shipped": 1, "no-change": 1, "failed": 1}
    assert {g["key"]: g["sessions"] for g in doc["by_effort"]} == {"high": 2, "(none)": 1}
    assert doc["top"][0]["title"] == "Title g1" and doc["top"][0]["model"] == "claude-a"
    text_ = "\n".join(tokens_report.lines(doc))
    assert "by category / project" in text_ and "    agent-memory" in text_ and '"Title g1"' in text_
    res = run_amem("sessions", "tokens", "--by", "outcome", "--by", "effort", env=base_env(AMEM_DB_URL=db))
    assert res.returncode == 0, res.stderr
    assert "by outcome" in res.stdout and "by effort" in res.stdout and "by category" not in res.stdout


def test_deviation_candidates_carry_session_titles(db: str) -> None:
    seed_groups(db)
    with psycopg.connect(db) as conn:
        conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, what, why,"
                     " how, followed_grade, result_grade) values ('k1', 'g1', 'claude-md:global', 'Explaining',"
                     " 'w', 'y', 'h', 0.5, 0.5)")
    (c,) = deviations.candidates(now=NOW, db_url=db)
    assert c["session_titles"] == {"g1": "Title g1"} and c["examples"][0]["title"] == "Title g1"


def test_view_has_no_api_role_grants(db: str) -> None:
    with psycopg.connect(db) as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"create role {role}")
            conn.execute(f"grant select on public.session_groups to {role}")
        conn.execute(deviations.config.MIGRATIONS_DIR.joinpath("20261018040000_session_profile.sql").read_text())
        granted = conn.execute("select count(*) from information_schema.role_table_grants "
                               "where table_name = 'session_groups' and grantee in ('anon', 'authenticated')").fetchone()
        assert granted == (0,)
        conn.rollback()


def test_outcome_rules_constants() -> None:
    assert amem_digest.OUTCOMES == ("shipped", "pushed", "committed", "partial", "active", "abandoned", "failed",
                                    "no-change")
    assert datetime.now(UTC) > NOW - timedelta(days=3650)
