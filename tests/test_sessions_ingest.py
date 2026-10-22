"""`amem sessions ingest`: kind classification, skills, instruction names, idempotent upsert."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from agent_memory import sessions_ingest
from conftest import base_env, run_amem
from test_deviations_support import NOW, Home, cli_days, make_all_kinds, setup_home  # noqa: F401


def _rows(home: Home) -> dict[str, sessions_ingest.SessionRow]:
    rows, _skipped = sessions_ingest.collect(home.projects, days=7, now=NOW)
    return {r.session_id: r for r in rows}


def test_each_kind_is_classified(setup_home: Home) -> None:  # noqa: F811
    ids = make_all_kinds(setup_home)
    rows = _rows(setup_home)
    assert {kind: rows[sid].kind for kind, sid in ids.items()} == {k: k for k in ids}


def test_skills_are_skill_calls_plus_slash_commands_without_builtins(setup_home: Home) -> None:  # noqa: F811
    ids = make_all_kinds(setup_home)
    rows = _rows(setup_home)
    assert rows[ids["chat"]].skills == ["systematic-debugging", "handoff"]  # /compact is a built-in
    assert rows[ids["parent"]].skills == ["dispatch"]


def test_worker_meta_and_repo(setup_home: Home) -> None:  # noqa: F811
    ids = make_all_kinds(setup_home)
    w = _rows(setup_home)[ids["worker"]]
    assert w.meta["parent_session"] == "parent-1" and w.meta["agent_type"] == "worker"
    assert w.repo == "demo-repo" and w.project_dir == "-work-demo-repo"


def test_routine_name_from_scheduled_task_tag(setup_home: Home) -> None:  # noqa: F811
    ids = make_all_kinds(setup_home)
    r = _rows(setup_home)[ids["routine"]]
    assert r.meta["routine"] == "nightly-check"


def test_scheduled_tag_after_a_reminder_block_is_still_a_routine(setup_home: Home) -> None:  # noqa: F811
    from test_deviations_support import routine_events, write_transcript

    events = routine_events(str(setup_home.repo))
    events[0]["message"]["content"] = "<system-reminder>context</system-reminder>\n" + events[0]["message"]["content"]
    write_transcript(setup_home.project() / "routine-2.jsonl", events)
    row = _rows(setup_home)["routine-2"]
    assert (row.kind, row.meta["routine"]) == ("routine", "nightly-check")


def test_instruction_names_per_kind(setup_home: Home) -> None:  # noqa: F811
    ids = make_all_kinds(setup_home)
    rows = _rows(setup_home)
    assert rows[ids["chat"]].instructions == ["claude-md:global", "skill:systematic-debugging", "claude-md:demo-repo"]
    assert rows[ids["parent"]].instructions == ["claude-md:global", "parent-md", "skill:dispatch",
                                                "claude-md:demo-repo"]
    assert rows[ids["worker"]].instructions == ["claude-md:global", "agent:worker", "claude-md:demo-repo"]
    assert rows[ids["routine"]].instructions == ["claude-md:global", "routine:nightly-check",
                                                 "claude-md:demo-repo"]


@pytest.mark.parametrize(("cwd", "repo"), [
    ("/Users/x/worktrees/agent-memory/agent-af46", "agent-memory"),
    ("/Users/x/Projects/simplitics", "simplitics"),
    ("", None),
])
def test_repo_of(cwd: str, repo: str | None) -> None:
    assert sessions_ingest.repo_of(cwd) == repo


def test_scratch_cwd_is_never_the_work_dir() -> None:
    scratch = "/Users/x/Library/Application Support/Claude/scratch-workspaces/abc/scratch"
    assert sessions_ingest.work_cwd([scratch, "/Users/x/Projects/a"], "") == ("/Users/x/Projects/a", False)
    assert sessions_ingest.work_cwd([scratch], "") == ("", True)


def test_old_transcripts_are_skipped(setup_home: Home) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    rows, skipped = sessions_ingest.collect(setup_home.projects, days=1, now=NOW.replace(month=11))
    assert rows == [] and skipped == 0  # mtime filter: nothing touched in the window


def test_ingest_is_idempotent(setup_home: Home, pg_url: str) -> None:  # noqa: F811
    ids = make_all_kinds(setup_home)
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from sessions")
    first = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=pg_url)
    assert (first.new, first.updated) == (6, 0)
    assert first.kinds == {"chat": 1, "parent": 1, "worker": 1, "headless": 1, "routine": 1, "eval": 1}
    with psycopg.connect(pg_url) as conn:
        conn.execute("update sessions set scanned_at = now(), meta = meta || '{\"scan_found\": 2}' "
                     "where session_id = %s", (ids["chat"],))
    second = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=pg_url)
    assert (second.new, second.updated) == (0, 0)
    with psycopg.connect(pg_url) as conn:
        row = conn.execute("select scanned_at is not null, meta ->> 'scan_found', kind from sessions "
                           "where session_id = %s", (ids["chat"],)).fetchone()
        n = conn.execute("select count(*) from sessions").fetchone()
    assert row == (True, "2", "chat") and n == (6,)


def test_cli_sessions_ingest(setup_home: Home, pg_url: str) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from sessions")
    env = base_env(AMEM_DB_URL=pg_url, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config))
    res = run_amem("sessions", "ingest", "--days", cli_days(), "--projects-dir", str(setup_home.projects), env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("sessions: 6 files, 6 new, 0 updated, 0 skipped (chat=1 parent=1 worker=1 ")
    again = run_amem("sessions", "ingest", "--days", cli_days(), "--projects-dir", str(setup_home.projects), env=env)
    assert "0 new, 0 updated" in again.stdout


def test_cli_sessions_ingest_refuses_rest_backend(setup_home: Home, tmp_path: Path) -> None:  # noqa: F811
    env = base_env(AMEM_BACKEND="rest", AMEM_SUPABASE_URL="http://127.0.0.1:9")
    res = run_amem("sessions", "ingest", "--days", cli_days(), "--projects-dir", str(setup_home.projects), env=env)
    assert res.returncode == 1 and "Postgres backend" in res.stderr
