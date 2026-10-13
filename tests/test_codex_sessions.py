"""Codex sessions: ingest (kinds, skills, repo, fork history), the digest, AGENTS.md sources,
and the team-repo flag. All rollouts are synthetic (tests/codex_support.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_memory import codex_sessions, deviations, rule_catalog, sessions_ingest
from codex_support import (
    Rollout,
    codex_home,
    make_codex_kinds,
    subagent_source,
    tid,
    write_git_config,
)
from conftest import base_env, run_amem
from test_deviations_support import NOW, Home, make_all_kinds, setup_home  # noqa: F401


def _rows(home: Home, tools: tuple[str, ...] = ("codex",)) -> dict[str, sessions_ingest.SessionRow]:
    rows, _skipped = sessions_ingest.collect(home.projects, days=7, now=NOW, tools=tools)
    return {r.session_id: r for r in rows}


# --------------------------------------------------------------- ingest


def test_codex_home_follows_the_claude_home(setup_home: Home) -> None:  # noqa: F811
    assert sessions_ingest.codex_home() == codex_home(setup_home)


def test_each_codex_kind_is_classified(setup_home: Home) -> None:  # noqa: F811
    ids = make_codex_kinds(setup_home)
    rows = _rows(setup_home)
    assert {kind: rows[sid].kind for kind, sid in ids.items()} == {k: k for k in ids}
    assert {r.tool for r in rows.values()} == {"codex"}
    chat = rows[ids["chat"]]
    assert chat.project_dir == "sessions/2026/10/14"
    assert chat.meta["file"] == f"rollout-2026-10-14T09-00-00-{tid(1)}.jsonl"
    assert (chat.meta["originator"], chat.meta["source"], chat.meta["thread_source"]) == (
        "Codex Desktop", "vscode", "user")
    assert chat.repo == "demo-repo" and chat.cwd == str(setup_home.repo)


def test_codex_and_claude_sessions_ingest_together(setup_home: Home) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    make_codex_kinds(setup_home)
    rows = _rows(setup_home, tools=sessions_ingest.TOOLS)
    assert sorted(r.tool for r in rows.values()).count("codex") == 5
    assert sorted(r.tool for r in rows.values()).count("claude") == 6
    assert _rows(setup_home, tools=("claude",)).keys().isdisjoint({f"codex:{tid(1)}"})


def test_codex_skills_are_skill_blocks_and_skill_file_reads(setup_home: Home) -> None:  # noqa: F811
    r = Rollout(tid(7), str(setup_home.repo)).injected().skill("spark").user("Brainstorm with me.", 1)
    r.command("sed -n 1,80p x", reads=("/u/.codex/skills/.system/skill-creator/SKILL.md",), minute=2)
    r.command("cat skills/x/SKILL.md", reads=("/u/agent-config/skills/not-used/SKILL.md",), minute=3)
    r.busy().write(setup_home)
    row = _rows(setup_home)[f"codex:{tid(7)}"]
    assert row.skills == ["spark", "skill-creator"]  # a repo's skill file is not a skill use
    assert row.meta["prompts"] == 1 and row.meta["tool_calls"] == 5


def test_a_forked_subagent_ignores_the_copied_parent_history(setup_home: Home) -> None:  # noqa: F811
    r = Rollout(tid(8), str(setup_home.repo), source=subagent_source(tid(2), "/root/scout"),
                thread_source="subagent", agent_path="/root/scout", fork_start=5)
    r.add("session_meta", {"id": tid(2), "cwd": str(setup_home.repo)})  # the parent's meta, copied
    r.user("PARENT PROMPT ONE", 0).command("PARENT COMMAND")            # ordinals 2..4: below start
    r.reasoning()                                                       # ordinal 5: own, never digested
    r.agent_message("/root", "/root/scout", "Scout the parser.", 1)
    r.command("ls", minute=2).write(setup_home)
    row = _rows(setup_home)[f"codex:{tid(8)}"]
    assert (row.kind, row.meta["parent_session"], row.meta["prompts"], row.meta["tool_calls"]) == (
        "worker", f"codex:{tid(2)}", 1, 1)  # the brief is its one prompt; the parent's are not
    digest = deviations.build_digest(sessions_ingest.load_events(row.path))
    assert "PARENT" not in digest
    assert digest.splitlines()[0] == "[turn 1] MESSAGE from /root: Scout the parser."


def test_codex_worktree_repo_is_the_main_checkout(setup_home: Home, tmp_path: Path) -> None:  # noqa: F811
    wt = tmp_path / "codex-wt" / "single-camera-navigation"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {setup_home.repo}/.git/worktrees/single-camera-navigation\n")
    Rollout(tid(9), str(wt)).user("Fix it.").busy().write(setup_home)
    assert _rows(setup_home)[f"codex:{tid(9)}"].repo == "demo-repo"


def test_codex_ingest_is_idempotent_and_stores_the_tool(setup_home: Home, pg_url: str) -> None:  # noqa: F811
    import psycopg

    make_all_kinds(setup_home)
    make_codex_kinds(setup_home)
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
    first = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=pg_url)
    assert (first.new, first.updated, first.tools) == (11, 0, {"claude": 6, "codex": 5})
    assert first.line().endswith("tools(claude=6 codex=5) team=0")
    second = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=pg_url)
    assert (second.new, second.updated) == (0, 0)
    with psycopg.connect(pg_url) as conn:
        tools = dict(conn.execute("select tool, count(*) from sessions group by tool").fetchall())
        rls = conn.execute("select relrowsecurity from pg_class where relname = 'sessions'").fetchone()
        bad = conn.execute("select 1 from pg_constraint where conname = 'sessions_tool_check'").fetchone()
    assert tools == {"claude": 6, "codex": 5} and rls == (True,) and bad == (1,)


def test_cli_ingest_codex_only(setup_home: Home, pg_url: str) -> None:  # noqa: F811
    import psycopg

    make_codex_kinds(setup_home)
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
    env = base_env(AMEM_DB_URL=pg_url, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config))
    res = run_amem("sessions", "ingest", "--tool", "codex", "--codex-home", str(codex_home(setup_home)),
                   "--projects-dir", str(setup_home.root / "nowhere"), env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("sessions: 5 files, 5 new, 0 updated, 0 skipped (chat=1 parent=1 worker=1 ")
    assert "tools(claude=0 codex=5)" in res.stdout


# --------------------------------------------------------------- digest


def _chat_digest(home: Home) -> str:
    r = Rollout(tid(11), str(home.repo)).injected().skill("handoff")
    r.user("Please tidy the parser module.", 1).reasoning(1).assistant("Looking at it now.", 2)
    r.command("pytest -q", exit_code=1, stderr="1 failed: test_parser", minute=3)
    r.command("rm -rf build", status="declined", exit_code=1, minute=3)
    r.command("export API_KEY=sk-ant-abcdefghijklmnop0123", minute=4)
    r.file_change("/repo/src/parser.py", minute=5).mcp("docs", "search", error="timeout", minute=5)
    r.spawn("worker_api", 6).agent_message("/root/worker_api", "/root", "Status: DONE", 7)
    r.aborted(8).user("Now ship it.", 9)
    return deviations.build_digest(sessions_ingest.load_events(r.write(home)))


def test_codex_digest_lines(setup_home: Home) -> None:  # noqa: F811
    lines = _chat_digest(setup_home).splitlines()
    assert lines == [
        "SKILL handoff invoked",
        "[turn 1] USER: Please tidy the parser module.",
        "ASSISTANT: Looking at it now.",
        "TOOL exec_command: pytest -q",
        "ERROR exec_command: exit 1: 1 failed: test_parser",
        "TOOL exec_command: rm -rf build",
        "DENIED exec_command (declined)",
        "TOOL exec_command: export API_KEY=[redacted]",
        "TOOL apply_patch: /repo/src/parser.py",
        "TOOL mcp:docs/search: q",
        "ERROR mcp:docs/search: timeout",
        "TOOL spawn_agent: worker_api",
        "NOTICE agent-message from /root/worker_api: Status: DONE",
        "INTERRUPTED by the user",
        "[turn 2] USER: Now ship it.",
    ]


def test_codex_digest_leaves_out_injected_text(setup_home: Home) -> None:  # noqa: F811
    digest = _chat_digest(setup_home)
    for hidden in ("NOT FOR THE DIGEST", "sk-ant-", "environment_context", "INSTRUCTIONS", "OUTPUT"):
        assert hidden not in digest


def test_codex_digest_is_capped(setup_home: Home) -> None:  # noqa: F811
    r = Rollout(tid(12), str(setup_home.repo)).user("Start.")
    for i in range(400):
        r.assistant(f"step {i} " + "y" * 500, minute=1)
    digest = deviations.build_digest(sessions_ingest.load_events(r.write(setup_home)), limit=20_000)
    assert len(digest) <= 20_000 and "chars cut from the middle" in digest


def test_older_rollouts_without_items_show_function_calls() -> None:
    events = [{"type": "session_meta", "payload": {"id": tid(1), "cwd": "/w"}},
              {"type": "response_item", "payload": {"type": "function_call", "name": "shell",
                                                    "arguments": '{"command": ["bash", "-lc", "make test"]}'}},
              {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
                                                    "input": "*** Begin Patch"}}]
    assert deviations.codex_digest_lines(events) == ["TOOL shell: make test", "TOOL apply_patch: *** Begin Patch"]
    assert codex_sessions.scan_facts(events).tool_calls == 2


# --------------------------------------------------------------- AGENTS.md sources


def _agents_md(home: Home) -> None:
    cx = codex_home(home)
    cx.mkdir(parents=True, exist_ok=True)
    (cx / "AGENTS.md").write_text("# Codex global\n## Answers\nKeep them short.\n## Safety\nNo secrets.\n")
    (home.repo / "AGENTS.md").write_text("# Demo agents\n## Tests\nRun pytest first.\n")
    (home.claude / "skills" / "handoff").mkdir(parents=True, exist_ok=True)
    (home.claude / "skills" / "handoff" / "SKILL.md").write_text("# Handoff\nWrite the block.\n")


def test_codex_instruction_sources(setup_home: Home) -> None:  # noqa: F811
    _agents_md(setup_home)
    ids = make_codex_kinds(setup_home)
    rows = _rows(setup_home)
    assert rows[ids["chat"]].instructions == ["agents-md:global", "skill:handoff", "agents-md:demo-repo"]
    assert rows[ids["worker"]].instructions == ["agents-md:global", "agents-md:demo-repo"]
    # Codex reads no CLAUDE.md: the repo's CLAUDE.md files are not in force for it
    assert not any(n.startswith("claude-md:") for r in rows.values() for n in r.instructions)


def test_override_wins_for_the_global_agents_md(setup_home: Home) -> None:  # noqa: F811
    _agents_md(setup_home)
    (codex_home(setup_home) / "AGENTS.override.md").write_text("# Override\nOnly this.\n")
    row = sessions_ingest.SessionRow("codex:x", "chat", None, "p", None, None, 0, [], [], {}, tool="codex")
    (src,) = deviations.instruction_sources(row)
    assert (src.name, src.paths) == ("agents-md:global", [codex_home(setup_home) / "AGENTS.override.md"])


def test_claude_sessions_get_the_repo_agents_md(setup_home: Home) -> None:  # noqa: F811
    _agents_md(setup_home)
    ids = make_all_kinds(setup_home)
    rows = _rows(setup_home, tools=("claude",))
    assert rows[ids["chat"]].instructions == ["claude-md:global", "skill:systematic-debugging", "skill:handoff",
                                              "claude-md:demo-repo", "agents-md:demo-repo"]


def test_rule_catalog_reads_agents_md_like_claude_md(setup_home: Home) -> None:  # noqa: F811
    _agents_md(setup_home)
    src = deviations.InstructionSource("agents-md:global", [codex_home(setup_home) / "AGENTS.md"])
    assert deviations.source_catalog(src) == [rule_catalog.Rule("answers", "Answers"),
                                              rule_catalog.Rule("safety", "Safety")]
    res = run_amem("deviations", "rules", str(setup_home.repo / "AGENTS.md"), env=base_env())
    assert res.returncode == 0 and "1. tests — Tests" in res.stdout


# --------------------------------------------------------------- team repos


@pytest.mark.parametrize(("url", "owner"), [
    ("git@github-stupidhuman:stupidhumanAI/app.git", "stupidhumanAI"),
    ("https://github.com/vilmerfrost/agent-memory.git", "vilmerfrost"),
    ("ssh://git@github.com:22/acme/repo", "acme"),
    ("https://github.com/acme/repo/", "acme"),
    ("/Users/x/repos/local.git", None),
    ("file:///Users/x/repo", None),
    ("", None),
])
def test_remote_owner(url: str, owner: str | None) -> None:
    assert sessions_ingest.remote_owner(url) == owner


def test_team_from_git_remote_owner(setup_home: Home) -> None:  # noqa: F811
    write_git_config(setup_home.repo, "git@github-team:acme/demo-repo.git")
    info = sessions_ingest.team_info(str(setup_home.repo / "src"), "demo-repo")  # a subfolder finds the repo
    assert info == {"team": True, "remote_owner": "acme", "team_reason": "remote-owner"}
    write_git_config(setup_home.repo, "https://github.com/VilmerFrost/demo-repo")
    assert sessions_ingest.team_info(str(setup_home.repo), "demo-repo") == {"team": False,
                                                                            "remote_owner": "VilmerFrost"}
    write_git_config(setup_home.repo, None)  # unknown: no flag, so a stored one is kept
    assert sessions_ingest.team_info(str(setup_home.repo), "demo-repo") == {}


def test_team_flag_survives_a_removed_worktree(setup_home: Home, pg_url: str, tmp_path: Path) -> None:  # noqa: F811
    import psycopg

    write_git_config(setup_home.repo, "git@github-team:acme/demo-repo.git")
    wt = tmp_path / "wt" / "demo-repo" / "w1"
    wt.mkdir(parents=True)
    (wt / ".git").write_text(f"gitdir: {setup_home.repo}/.git/worktrees/w1\n")
    Rollout(tid(40), str(wt)).user("Work in the worktree.").busy().write(setup_home)
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
    sessions_ingest.ingest(None, days=7, now=NOW, db_url=pg_url, tools=("codex",))
    (wt / ".git").unlink()  # the worktree is cleaned up
    row = _rows(setup_home)[f"codex:{tid(40)}"]
    assert "team" not in row.meta
    sessions_ingest.ingest(None, days=7, now=NOW, db_url=pg_url, tools=("codex",))
    with psycopg.connect(pg_url) as conn:
        (meta,) = conn.execute("select meta from sessions where session_id = %s", (f"codex:{tid(40)}",)).fetchone()
    assert (meta["team"], meta["remote_owner"]) == (True, "acme")


def test_team_spreads_to_sessions_of_the_same_repo(setup_home: Home, tmp_path: Path) -> None:  # noqa: F811
    Rollout(tid(41), str(setup_home.repo), repo_url="git@github-team:acme/demo-repo.git") \
        .user("Main checkout work.").busy().write(setup_home)
    Rollout(tid(42), str(tmp_path / "gone" / "demo-repo")).user("Worktree long gone.").busy().write(setup_home)
    Rollout(tid(43), str(tmp_path / "gone" / "other-repo")).user("Another repo.").busy().write(setup_home)
    rows = _rows(setup_home)
    assert (rows[f"codex:{tid(42)}"].team, rows[f"codex:{tid(42)}"].meta["team_reason"]) == (True, "same-repo")
    assert not rows[f"codex:{tid(43)}"].team


def test_team_from_profile_mode(setup_home: Home) -> None:  # noqa: F811
    prof = setup_home.claude / "parent" / "demo-repo"
    prof.mkdir(parents=True)
    (prof / "profile.md").write_text("# demo-repo\n\nowner: vilmerfrost\nmode: team\n")
    assert sessions_ingest.team_info(str(setup_home.repo), "demo-repo") == {"team": True, "team_reason": "profile"}
    assert sessions_ingest.profile_mode("../../etc") is None


def test_personal_owners_env(setup_home: Home, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    write_git_config(setup_home.repo, "git@github.com:acme/demo-repo.git")
    monkeypatch.setenv(sessions_ingest.PERSONAL_OWNERS_ENV, "vilmerfrost, acme")
    assert sessions_ingest.team_info(str(setup_home.repo), "demo-repo")["team"] is False


def test_codex_rows_use_the_recorded_remote(setup_home: Home) -> None:  # noqa: F811
    Rollout(tid(21), str(setup_home.repo), repo_url="git@github-stupidhuman:stupidhumanAI/demo-repo.git") \
        .user("Fix the build.").busy().write(setup_home)
    Rollout(tid(22), str(setup_home.repo), repo_url="https://github.com/vilmerfrost/demo-repo") \
        .user("Fix the build.").busy().write(setup_home)
    rows = _rows(setup_home)
    team, mine = rows[f"codex:{tid(21)}"], rows[f"codex:{tid(22)}"]
    assert (team.team, team.meta["remote_owner"], team.meta["team_reason"]) == (True, "stupidhumanAI",
                                                                               "remote-owner")
    assert (mine.team, mine.meta["remote_owner"]) == (False, "vilmerfrost")


def test_claude_rows_get_the_team_flag(setup_home: Home) -> None:  # noqa: F811
    write_git_config(setup_home.repo, "git@github-team:acme/demo-repo.git")
    ids = make_all_kinds(setup_home)
    rows = _rows(setup_home, tools=("claude",))
    assert rows[ids["chat"]].team and rows[ids["chat"]].meta["team_reason"] == "remote-owner"


def test_repo_scoped_sources() -> None:
    assert deviations.repo_scoped("claude-md:demo") and deviations.repo_scoped("agents-md:demo")
    assert not any(deviations.repo_scoped(s) for s in ("claude-md:global", "agents-md:global", "skill:x",
                                                       "parent-md", "agent:worker", "routine:x"))
