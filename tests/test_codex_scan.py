"""`amem deviations scan|report` on Codex sessions against the throwaway Postgres: dry run,
the writer prompt, AGENTS.md sources and the team flag in the candidates JSON. Synthetic
rollouts only (tests/codex_support.py)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import psycopg
import pytest

from agent_memory import deviations, sessions_ingest
from codex_support import Rollout, codex_home, make_codex_kinds, tid, write_git_config
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    FakeJev,
    FakeWriter,
    Home,
    envelope,
    fake_jev,
    finding,
    setup_home,
    writer,
)

Writer = Callable[..., FakeWriter]


@pytest.fixture
def db(setup_home: Home, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    cx = codex_home(setup_home)
    cx.mkdir(parents=True, exist_ok=True)
    (cx / "AGENTS.md").write_text("# Codex global\n## Answers\nKeep them short.\n")
    (setup_home.repo / "AGENTS.md").write_text("# Demo agents\n## Tests\nRun pytest first.\n")
    yield pg_url


def _ingest(home: Home, url: str) -> dict[str, str]:
    ids = make_codex_kinds(home)
    sessions_ingest.ingest(home.projects, days=7, now=NOW, db_url=url, tools=("codex",))
    return ids


def test_dry_run_on_codex_sessions(setup_home: Home, db: str) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    report = deviations.scan(dry_run=True, now=NOW)
    lines = {o.session_id: o.line() for o in report.outcomes}
    assert set(lines) == {ids["chat"], ids["parent"], ids["worker"], ids["headless"]}  # eval is never picked
    chat = lines[ids["chat"]]
    assert chat.startswith(f"dry-run {ids['chat']} kind=chat tool=codex digest_chars=")
    assert chat.endswith("instructions=agents-md:global,agents-md:demo-repo")  # handoff has no file here
    assert all(o.digest_chars > 0 for o in report.outcomes)


def test_scan_codex_session_end_to_end_with_team_repo(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                      fake_jev: FakeJev) -> None:  # noqa: F811
    write_git_config(setup_home.repo, "git@github-stupidhuman:stupidhumanAI/demo-repo.git")
    ids = _ingest(setup_home, db)
    fake = writer(default=envelope({"deviations": [
        finding("agents-md:demo-repo", "tests", 1, 0.2, 0.5),
        finding("agents-md:global", "answers", 1, 0.4, 0.9)]}))
    report = deviations.scan(session_id=ids["chat"], now=NOW)
    (o,) = report.outcomes
    assert o.status == "ok" and len(o.judged) == 2, o.reason
    assert o.line().startswith(f"ok {ids['chat']} kind=chat tool=codex team=1 found=2")
    (prompt,) = fake.prompts
    assert "You audit one Codex agent session" in prompt and "=== Session (Codex, kind chat" in prompt
    assert "--- agents-md:demo-repo ---\n1. tests — Tests" in prompt
    assert "[turn 1] USER: Please tidy the parser module." in prompt
    for hidden in ("NOT FOR THE DIGEST", "BASE INSTRUCTIONS"):
        assert hidden not in prompt

    cands = {c["instruction_source"]: c for c in deviations.candidates(now=NOW)}
    repo, glob = cands["agents-md:demo-repo"], cands["agents-md:global"]
    assert (repo["team"], repo["team_sessions"], repo["tools"]) == (True, 1, {"codex": 1})
    assert repo["target_paths"] == [str(setup_home.repo / "AGENTS.md")]
    assert repo["examples"][0]["team"] is True and repo["rule_id"] == "tests"
    assert (glob["team"], glob["team_sessions"]) == (False, 1)  # a personal file: still editable
    assert glob["target_paths"] == [str(codex_home(setup_home) / "AGENTS.md")]
    lines = deviations.report_lines(list(cands.values()), 7)
    assert any(ln.startswith("agents-md:demo-repo / Tests [tests]") and ln.endswith(" | team repo")
               for ln in lines)

    env = base_env(AMEM_DB_URL=db, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config))
    res = run_amem("deviations", "report", "--json", "--days", "3650", env=env)
    assert res.returncode == 0, res.stderr
    by_source = {c["instruction_source"]: c for c in json.loads(res.stdout)["candidates"]}
    assert by_source["agents-md:demo-repo"]["team"] is True
    assert by_source["agents-md:global"]["team"] is False


def test_codex_transcript_path_never_leaves_the_codex_home(setup_home: Home, db: str) -> None:  # noqa: F811
    row = sessions_ingest.SessionRow("codex:x", "chat", None, "sessions/2026/10/14", None, None, 0, [], [],
                                     {"file": "../../../.claude/CLAUDE.md"}, tool="codex")
    assert deviations.transcript_path(row) == codex_home(setup_home) / "missing-transcript"
    Rollout(tid(30), str(setup_home.repo)).user("Hi.").busy().write(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("codex",))
    (stored,) = deviations.pick_sessions(since=NOW.replace(year=2025), limit=5, session_id=f"codex:{tid(30)}")
    assert stored.tool == "codex" and deviations.transcript_path(stored).is_file()
