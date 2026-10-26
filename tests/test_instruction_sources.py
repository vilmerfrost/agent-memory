"""Instruction sources with authority and version (docs/data-use/instruction-sources.md).

Fixture homes only (test_deviations_support.setup_home points the Claude home, the projects folder and
agent-config at tmp dirs). Git repos are made in tmp dirs with fixed commit dates. The deviation writer
and Jev are stand-ins. The throwaway Postgres is the conftest one (and a database of our own for RLS).
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import data_families, deviations, sessions_ingest
from agent_memory import instruction_sources as IS
from agent_memory.sessions_ingest import SessionRow
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    T0,
    NOW,
    FakeJev,
    FakeWriter,
    Home,
    assistant,
    busy,
    envelope,
    fake_jev,
    finding,
    result,
    setup_home,
    text,
    tool,
    user,
    write_transcript,
    writer,
)

Writer = Callable[..., FakeWriter]
OLD = (T0 - timedelta(days=1)).timestamp()      # before the fixture sessions start
MIGRATION = Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "20261026200000_instruction_sources.sql"
APP_PW = "app-test-password-0123456789"
OWNER = "00000000-0000-4000-8000-0000000000aa"
STRANGER = "00000000-0000-4000-8000-0000000000bb"
DB = "amem_instruction_sources_test"


def old(*paths: Path) -> None:
    """Make files look unchanged since before the session started."""
    for p in paths:
        os.utime(p, (OLD, OLD))


def git(repo: Path, *args: str, when: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else None
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                          check=True, capture_output=True, text=True, env=env).stdout.strip()


def row(home: Home, kind: str = "chat", skills: list[str] | None = None, *, start: Any = T0, **meta: Any) -> SessionRow:
    return SessionRow("s1", kind, "demo-repo", "-work-demo-repo", start, None, 0, list(skills or []), [],
                      {"cwd": str(home.repo), **meta})


def by_name(entries: list[IS.Entry]) -> dict[str, list[IS.Entry]]:
    out: dict[str, list[IS.Entry]] = {}
    for e in entries:
        out.setdefault(e.name, []).append(e)
    return out


def one(entries: list[IS.Entry], name: str) -> IS.Entry:
    (e,) = by_name(entries)[name]
    return e


# --------------------------------------------------------------------------- plugin and built-in skills


def _plugin(home: Home, *, last_updated: str = "2026-10-07T16:52:15.979Z") -> Path:
    root = home.claude / "plugins" / "cache" / "magicpath" / "magicpath" / "0.9.0"
    skill = root / "skills" / "magicpath" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# MagicPath\n## Canvas\nAsk before rearranging the canvas.\n")
    (home.claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
        "magicpath@magicpath": [{"scope": "user", "installPath": str(root), "version": "0.9.0",
                                 "installedAt": "2026-10-07T16:52:15.979Z", "lastUpdated": last_updated,
                                 "gitCommitSha": "3afaaa53607788185648432969e4731c4eed880c"}]}}))
    return skill


def test_plugin_skill_resolves_to_its_cache_path_with_version(setup_home: Home) -> None:  # noqa: F811
    skill = _plugin(setup_home)
    old(skill)
    r = row(setup_home, skills=["magicpath:magicpath"])
    e = one(IS.capture(r), "skill:magicpath:magicpath")
    assert (e.kind, e.path, e.authority) == ("plugin-skill", "~/.claude/plugins/cache/magicpath/magicpath/0.9.0/"
                                                              "skills/magicpath/SKILL.md", "reconstructed")
    assert e.plugin == {"id": "magicpath@magicpath", "version": "0.9.0", "scope": "user",
                        "git_commit_sha": "3afaaa53607788185648432969e4731c4eed880c",
                        "installed_at": "2026-10-07T16:52:15.979Z", "last_updated": "2026-10-07T16:52:15.979Z"}
    assert e.version == {"kind": "content", "id": IS.sha256(skill.read_text()), "basis": "mtime"}
    assert e.stored    # under the Claude home: its text is kept
    src = {s.name: s.paths for s in deviations.instruction_sources(r)}
    assert src["skill:magicpath:magicpath"] == [skill]


def test_builtin_and_missing_skills_are_unresolved_with_a_reason(setup_home: Home) -> None:  # noqa: F811
    _plugin(setup_home)
    entries = IS.capture(row(setup_home, skills=["anthropic-skills:docs", "superpowers:test-driven-development",
                                                 "magicpath:no-such-skill", "loop"]))
    docs = one(entries, "skill:anthropic-skills:docs")
    assert (docs.authority, docs.path, docs.version, docs.sha256) == ("unresolved", "anthropic-skills:docs", None, None)
    assert "built into the Claude desktop app" in (docs.reason or "")
    assert "not installed" in (one(entries, "skill:superpowers:test-driven-development").reason or "")
    assert "not in plugin magicpath@magicpath 0.9.0" in (one(entries, "skill:magicpath:no-such-skill").reason or "")
    assert "no SKILL.md" in (one(entries, "skill:loop").reason or "")
    # Unresolved names have no file: they are not offered to the writer.
    assert not any(s.name.startswith("skill:") for s in deviations.instruction_sources(
        row(setup_home, skills=["anthropic-skills:docs", "superpowers:test-driven-development"])))


def test_a_builtin_skill_the_transcript_shows_is_observed(setup_home: Home) -> None:  # noqa: F811
    base = "/Users/x/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/o/s/skills/docs"
    body = "# Docs\n## Skeleton first\nCreate the doc before anything else.\n"
    events = [user(f"Base directory for this skill: {base}\n\n{body}", 1, isMeta=True)]
    e = one(IS.capture(row(setup_home, skills=["anthropic-skills:docs"]), events), "skill:anthropic-skills:docs")
    assert (e.authority, e.sha256, e.path) == ("observed", IS.sha256(body), base + "/SKILL.md")
    assert not e.stored  # outside the allowed roots: recoverable from the transcript only


def test_a_plugin_updated_after_the_session_started_is_unresolved(setup_home: Home) -> None:  # noqa: F811
    _plugin(setup_home, last_updated="2026-10-20T10:00:00Z")   # the cache file is new too (mtime now)
    e = one(IS.capture(row(setup_home, skills=["magicpath:magicpath"])), "skill:magicpath:magicpath")
    assert e.authority == "unresolved" and e.version is None and e.sha256 is None
    assert "plugin magicpath@magicpath was updated after the session started" in (e.reason or "")


# --------------------------------------------------------------------------- ancestors and imports


def test_imports_of_skips_code_and_emails() -> None:
    text = ("See @AGENTS.md and @~/agent-config/parent/PARENT.md.\nMail me@example.com\n"
            "`@not-this.md`\n```\n@nor-this.md\n```\n@./docs/a.md, @types")
    assert IS.imports_of(text) == ["AGENTS.md", "~/agent-config/parent/PARENT.md", "./docs/a.md"]


def test_ancestor_memory_and_import_chains_resolve(setup_home: Home, tmp_path: Path) -> None:  # noqa: F811
    repo = setup_home.repo
    (setup_home.root / "work" / "CLAUDE.md").write_text("# Work\n## Ancestor\nAncestor rule.\n")
    (repo / "CLAUDE.md").write_text("# Demo repo\n@docs/a.md\n@chain/c1.md\n@.env\n@" +
                                    str(tmp_path / "outside" / "x.md") + "\n")
    (repo / "CLAUDE.local.md").write_text("# Local\n@~/agent-config/parent/PARENT.md\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("# A\n@b.md\n")
    (repo / "docs" / "b.md").write_text("# B\n@a.md\n")          # a cycle back to a.md
    (repo / "chain").mkdir()
    for i in range(1, 8):                                         # c1 -> c2 -> ... -> c7
        (repo / "chain" / f"c{i}.md").write_text(f"# C{i}\n@c{i + 1}.md\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "x.md").write_text("# Outside\n")
    cands = IS.candidates(row(setup_home))
    paths = {IS.label(c.path) if c.path else c.label: c for c in cands}
    assert paths["~/work/CLAUDE.md"].name == "claude-md:~/work" and paths["~/work/CLAUDE.md"].kind == "ancestor"
    assert paths["~/agent-config/parent/PARENT.md"].name == "parent-md"          # imported by CLAUDE.local.md
    assert paths["~/work/demo-repo/docs/b.md"].imported_by == repo / "docs" / "a.md"
    assert sum(1 for c in cands if c.path and c.path.name == "a.md") == 1        # the cycle stops
    chain = sorted(c.path.name for c in cands if c.path and c.path.parent.name == "chain")
    assert chain == ["c1.md", "c2.md", "c3.md", "c4.md", "c5.md"]               # depth cap 5
    env = next(c for c in cands if (c.label or "").endswith("/.env"))
    assert env.path is None and env.reason == "env file: never read"
    out = next(c for c in cands if (c.label or "").endswith("outside/x.md"))
    assert out.reason == "import outside the allowed roots: not read"
    entries = IS.capture(row(setup_home))
    assert all("do-not-read-me" not in (e.text or "") for e in entries)      # the .env text is never read
    names = [s.name for s in deviations.instruction_sources(row(setup_home))]
    assert names[:2] == ["claude-md:global", "claude-md:demo-repo"] and "claude-md:~/work" in names
    assert "import:~/work/demo-repo/docs/a.md" in names and "parent-md" in names


# --------------------------------------------------------------------------- authority labels


def test_authority_labels(setup_home: Home) -> None:  # noqa: F811
    old(setup_home.claude / "CLAUDE.md")
    # reconstructed: unchanged since before the start; unresolved: a non-git file changed after the start
    entries = IS.capture(row(setup_home, skills=["dispatch", "missing-skill"]))
    g = one(entries, "claude-md:global")
    assert g.authority == "reconstructed" and g.version == {
        "kind": "content", "id": IS.sha256((setup_home.claude / "CLAUDE.md").read_text()), "basis": "mtime"}
    d = one(entries, "skill:dispatch")                          # written now: after the session started
    assert (d.authority, d.version, d.sha256) == ("unresolved", None, None)
    assert d.reason == "changed after the session started and not in git: the version in force is unknown"
    assert one(entries, "skill:missing-skill").authority == "unresolved"
    # declared: the session start is unknown, so no version can be verified
    g2 = one(IS.capture(row(setup_home, start=None)), "claude-md:global")
    assert (g2.authority, g2.version) == ("declared", None) and "session start unknown" in (g2.reason or "")


def test_observed_from_the_instructions_attachment(setup_home: Home) -> None:  # noqa: F811
    loaded = "# Global (as loaded)\n## Explaining\nKeep answers short and plain.\n"
    (setup_home.repo / "AGENTS.md").write_text("# Demo agents\nNot imported.\n")
    ancestor = setup_home.root / "work" / "CLAUDE.md"
    events = [{"type": "attachment", "timestamp": "2026-10-14T09:00:00Z", "attachment": {
        "type": "instructions", "files": [
            {"path": str(setup_home.claude / "CLAUDE.md"), "type": "User", "content": loaded},
            {"path": str(setup_home.repo / "CLAUDE.md"), "type": "Project", "content": "# Demo repo\n"},
            {"path": str(ancestor), "type": "Project", "content": "# Work (gone now)\n"}]}},
        {"type": "attachment", "attachment": {"type": "instructions", "files": [
            {"path": str(setup_home.claude / "CLAUDE.md"), "type": "User", "content": "# edited mid-session\n"}]}}]
    entries = IS.capture(row(setup_home), events)
    g = one(entries, "claude-md:global")
    assert (g.authority, g.sha256, g.text) == ("observed", IS.sha256(loaded), loaded)
    assert g.version == {"kind": "content", "id": IS.sha256(loaded), "basis": "transcript"}
    assert g.changed_during_session
    names = by_name(entries)
    # The attachment lists what was loaded: CLAUDE.local.md and the un-imported AGENTS.md were not.
    assert [e.path for e in names["claude-md:demo-repo"]] == ["~/work/demo-repo/CLAUDE.md"]
    assert "agents-md:demo-repo" not in names
    w = one(entries, "claude-md:~/work")                     # loaded, though no file is there now
    assert (w.authority, w.kind) == ("observed", "project")


def test_codex_observed_from_the_injected_agents_block(setup_home: Home) -> None:  # noqa: F811
    cx = sessions_ingest.codex_home()
    cx.mkdir(parents=True, exist_ok=True)
    (cx / "AGENTS.md").write_text("# Codex global\n## Answers\nKeep them short.\n")
    (setup_home.repo / "AGENTS.md").write_text("# Demo agents\n## Tests\nRun pytest first.\n")
    block = ("# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>\n# Codex global\n## Answers\nKeep them short.\n"
             "</INSTRUCTIONS>")
    events = [{"type": "response_item", "payload": {"type": "message", "role": "user",
                                                    "content": [{"type": "input_text", "text": block}]}}]
    r = SessionRow("codex:x", "chat", "demo-repo", "p", T0, None, 0, [], [], {"cwd": str(setup_home.repo)},
                   tool="codex")
    entries = IS.capture(r, events)
    assert one(entries, "agents-md:global").authority == "observed"
    assert one(entries, "agents-md:demo-repo").authority == "unresolved"   # not in the block, written now


# --------------------------------------------------------------------------- git versions


def _git_repo(home: Home) -> tuple[str, str]:
    """The fixture repo under git: CLAUDE.md v1 committed before the session, v2 committed after it."""
    repo = home.repo
    (repo / "CLAUDE.local.md").unlink()
    git(repo, "init", "-q", "-b", "work")
    (repo / "CLAUDE.md").write_text("# Demo repo\n## Old rule\nRun tests with pytest.\n")
    git(repo, "add", "CLAUDE.md")
    git(repo, "commit", "-q", "-m", "v1", when="2026-10-12T09:00:00+00:00")
    v1 = git(repo, "rev-parse", "HEAD")
    (repo / "CLAUDE.md").write_text("# Demo repo\n## New rule\nRun tests with uv.\n")
    git(repo, "commit", "-q", "-am", "v2", when="2026-10-20T09:00:00+00:00")
    return v1, "# Demo repo\n## Old rule\nRun tests with pytest.\n"


def test_a_git_file_changed_after_the_start_reconstructs_the_old_text(setup_home: Home) -> None:  # noqa: F811
    v1, old_text = _git_repo(setup_home)
    e = one(IS.capture(row(setup_home)), "claude-md:demo-repo")
    assert (e.authority, e.text, e.sha256) == ("reconstructed", old_text, IS.sha256(old_text))
    assert e.version is not None and (e.version["kind"], e.version["id"], e.version["basis"]) == (
        "git", v1, "git-history")
    assert e.version["repo"] == "~/work/demo-repo" and len(e.version["blob"]) == 40


def test_a_skill_symlinked_into_a_git_repo_gets_that_repos_history(setup_home: Home) -> None:  # noqa: F811
    """~/.claude/skills/<x> is a symlink into agent-config (a git repo), like the real setup."""
    cfg = setup_home.agent_config
    git(cfg, "init", "-q", "-b", "work")
    target = cfg / "skills" / "recon" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Recon\n## Read only\nNever write.\n")
    git(cfg, "add", "skills/recon/SKILL.md")
    git(cfg, "commit", "-q", "-m", "v1", when="2026-10-13T09:00:00+00:00")
    v1 = git(cfg, "rev-parse", "HEAD")
    target.write_text("# Recon\n## Read only\nNever write, never fix.\n")
    git(cfg, "commit", "-q", "-am", "v2", when="2026-10-21T09:00:00+00:00")
    (setup_home.claude / "skills" / "recon").symlink_to(target.parent)
    e = one(IS.capture(row(setup_home, skills=["recon"])), "skill:recon")
    assert (e.authority, e.text, e.path) == ("reconstructed", "# Recon\n## Read only\nNever write.\n",
                                             "~/.claude/skills/recon/SKILL.md")
    assert e.version is not None and (e.version["id"], e.version["repo"], e.version["basis"]) == (
        v1, "~/agent-config", "git-history")


def test_a_non_git_file_changed_after_the_start_is_unresolved_not_todays_hash(setup_home: Home) -> None:  # noqa: F811
    e = one(IS.capture(row(setup_home)), "claude-md:global")
    today = IS.sha256((setup_home.claude / "CLAUDE.md").read_text())
    assert (e.authority, e.version, e.sha256) == ("unresolved", None, None)
    assert today not in json.dumps(e.to_json())


# --------------------------------------------------------------------------- database: ingest, scan, reconcile


@pytest.fixture
def db(setup_home: Home, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
        conn.execute("delete from instruction_texts")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    yield pg_url


def _q(url: str, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


def _chat(home: Home, name: str = "chat-1") -> str:
    cwd = str(home.repo)
    write_transcript(home.project() / f"{name}.jsonl", [
        user("Please fix the flaky test in the parser.", 0, cwd=cwd),
        assistant(text("Looking at it."), tool("Bash", "b1", command="pytest -q"), minute=1, cwd=cwd),
        result("b1", "ok", 2, cwd=cwd), *busy(cwd)])
    return name


def test_ingest_captures_versions_and_replay_changes_nothing(setup_home: Home, db: str) -> None:  # noqa: F811
    v1, old_text = _git_repo(setup_home)
    old(setup_home.claude / "CLAUDE.md")
    sid = _chat(setup_home)
    assert sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db).new == 1
    (stored,) = _q(db, "select instruction_sources from sessions where session_id = %s", sid)[0]
    got = {e["name"]: e for e in stored}
    assert got["claude-md:global"]["version"]["kind"] == "content" and got["claude-md:global"]["stored"]
    assert got["claude-md:demo-repo"]["version"]["id"] == v1
    texts = dict(_q(db, "select sha256, text from instruction_texts"))
    assert texts[IS.sha256(old_text)] == old_text
    assert IS.sha256((setup_home.claude / "CLAUDE.md").read_text()) in texts
    report = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert (report.new, report.updated) == (0, 0)
    assert _q(db, "select instruction_sources from sessions where session_id = %s", sid)[0][0] == stored
    assert _q(db, "select count(*) from instruction_texts")[0][0] == len(texts)


def test_scan_grades_against_the_reconstructed_text(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                    fake_jev: FakeJev) -> None:  # noqa: F811
    _v1, old_text = _git_repo(setup_home)
    sid = _chat(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    w = writer(default=envelope({"deviations": [finding("claude-md:demo-repo", "Old rule", 1, 0.2, 0.6),
                                                finding("claude-md:global", "Explaining", 1)]}))
    report = deviations.scan(now=NOW, session_id=sid)
    assert report.errors == 0
    (prompt,) = w.prompts
    assert "## Old rule" in prompt and "New rule" not in prompt and "old-rule — Old rule" in prompt
    rows = dict((r[0], r[1:]) for r in _q(db, "select instruction_source, instruction_authority, instruction_sha, "
                                              "rule_id from deviations where session_id = %s", sid))
    assert rows["claude-md:demo-repo"] == ("reconstructed", IS.sha256(old_text), "old-rule")
    assert rows["claude-md:global"][0] == "unverified"     # changed after the start, not in git: today's file


def test_legacy_rows_are_graded_against_todays_files_as_unverified(setup_home: Home, db: str,  # noqa: F811
                                                                   writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    sid = _chat(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    _q_exec(db, "update sessions set instruction_sources = null")
    writer(default=envelope({"deviations": [finding()]}))
    deviations.scan(now=NOW, session_id=sid)
    assert _q(db, "select instruction_authority from deviations") == [("unverified",)]


def _q_exec(url: str, sql: str, *args: object) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(sql, args)


def test_reconcile_dry_run_writes_nothing_and_apply_twice_changes_nothing(setup_home: Home, db: str) -> None:  # noqa: F811
    _git_repo(setup_home)
    sids = [_chat(setup_home, "chat-1"), _chat(setup_home, "chat-2")]
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    _q_exec(db, "update sessions set instruction_sources = null")
    _q_exec(db, "delete from instruction_texts")
    dry = IS.reconcile(limit=1, db_url=db)
    assert (dry.considered, dry.written, dry.remaining) == (1, 0, 2)
    assert dry.authorities["reconstructed"] >= 1 and dry.authorities["unresolved"] >= 1
    assert _q(db, "select count(*) from sessions where instruction_sources is not null") == [(0,)]
    assert _q(db, "select count(*) from instruction_texts") == [(0,)]
    first = IS.reconcile(apply=True, limit=1, db_url=db)       # bounded: one row, the other remains
    assert (first.written, first.remaining) == (1, 1) and first.texts >= 1
    second = IS.reconcile(apply=True, db_url=db)                # resumes with the rest
    assert (second.considered, second.written, second.remaining) == (1, 1, 0)
    snapshot = _q(db, "select session_id, instruction_sources from sessions order by 1")
    texts = _q(db, "select count(*) from instruction_texts")
    again = IS.reconcile(apply=True, db_url=db)
    assert (again.considered, again.written) == (0, 0)
    assert _q(db, "select session_id, instruction_sources from sessions order by 1") == snapshot
    assert _q(db, "select count(*) from instruction_texts") == texts
    assert {r[0] for r in snapshot} == set(sids)
    env = base_env(AMEM_DB_URL=db, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config), AMEM_PROJECTS_DIR=str(setup_home.projects))
    res = run_amem("sessions", "instruction-sources", "--json", env=env)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["considered"] == 0


def test_reconcile_counts_unresolved_reasons(setup_home: Home, db: str) -> None:  # noqa: F811
    sid = _chat(setup_home)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    _q_exec(db, "update sessions set instruction_sources = null, skills = %s where session_id = %s",
            json.dumps(["anthropic-skills:docs"]), sid)
    report = IS.reconcile(db_url=db)
    assert report.reasons[IS.BUILTIN_NAMESPACES["anthropic-skills"]] == 1
    assert any(line.startswith("  unresolved: 1 x built into the Claude desktop app") for line in report.lines())


# --------------------------------------------------------------------------- access and registry


@pytest.fixture(scope="module")
def own_db(pg_url: str) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"drop database if exists {DB} with (force)")
        conn.execute(f"create database {DB}")
    u = pg_url.rsplit("/", 1)[0] + f"/{DB}"
    try:
        assert run_amem("db", "migrate", env=base_env(AMEM_DB_URL=u)).returncode == 0
        yield u
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f"drop database if exists {DB} with (force)")


def _identity(c: psycopg.Connection, role: str, uid: str | None = None) -> None:
    c.execute("reset role")
    c.execute("select set_config('request.jwt.claim.sub', %s, true)", (uid or "",))
    c.execute(f"set local role {role}")


def test_instruction_texts_rls_is_on_and_owner_only(own_db: str) -> None:
    with psycopg.connect(own_db) as c:
        c.execute("insert into instruction_texts (sha256, text, bytes, path) values (%s, 'x', 1, '~/x.md')",
                  ("a" * 64,))
        assert c.execute("select relrowsecurity from pg_class where relname = 'instruction_texts'").fetchone() == (True,)
        for role in ("anon", "authenticated", "service_role", "stranger"):
            c.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{role}') then "
                      f"create role {role}; end if; end $$")
        c.execute("create schema if not exists auth")
        c.execute("create or replace function auth.uid() returns uuid language sql stable as "
                  "$$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$")
        c.execute("grant usage on schema auth, public to anon, authenticated, service_role, stranger, rsi_reader")
        c.execute("grant execute on function public.amem_owner_uid() to authenticated")
        c.execute("insert into amem_private.dashboard_owner values (true, %s) "
                  "on conflict (singleton) do update set uid = excluded.uid", (OWNER,))
        c.execute(MIGRATION.read_text())                         # re-run: now the auth branch applies
        for role in ("anon", "service_role", "stranger", "rsi_reader", "rsi_writer"):
            _identity(c, role)
            c.execute("savepoint s")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("select * from instruction_texts")
            c.execute("rollback to savepoint s")
        _identity(c, "authenticated", STRANGER)
        assert c.execute("select count(*) from instruction_texts").fetchone() == (0,)
        _identity(c, "authenticated", OWNER)
        assert c.execute("select count(*) from instruction_texts").fetchone() == (1,)
        c.execute("savepoint s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("delete from instruction_texts")
        c.execute("rollback to savepoint s")
        _identity(c, "amem_app")
        assert c.execute("select count(*) from instruction_texts").fetchone() == (1,)
        c.execute("insert into instruction_texts (sha256, text, bytes, path) values (%s, 'y', 1, '~/y.md') "
                  "on conflict (sha256) do nothing", ("b" * 64,))
        for sql in ("update instruction_texts set text = 'z'", "delete from instruction_texts"):
            c.execute("savepoint s")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)
            c.execute("rollback to savepoint s")
        c.execute("reset role")
        c.rollback()


def test_the_registry_and_docs_name_the_new_table_and_columns() -> None:
    assert data_families.assignments()["instruction_texts"] == ["deviations"] and data_families.check() == []
    fam = data_families.family("deviations")
    assert {"sessions.instruction_sources", "deviations.instruction_authority"} <= set(fam.columns)
    doc = (Path(__file__).resolve().parents[1] / "docs" / "data-use" / "instruction-sources.md").read_text()
    for word in ("observed", "reconstructed", "declared", "unresolved", "instruction_texts", "instruction-sources",
                 "installed_plugins.json", "@import", "unverified"):
        assert word in doc, word
