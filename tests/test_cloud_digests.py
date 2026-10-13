"""Cloud routine sessions (docs/cloud.md, "Cloud digests"): tools/amem_digest.py (and its
package copy `amem digest-self`), the cloud_digests table, `sessions ingest` of its rows and
`deviations scan` on them. Synthetic cloud-shaped transcripts only."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import amem_digest, deviations, sessions_ingest, tasks_ingest
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    PAD,
    FakeJev,
    FakeWriter,
    Home,
    assistant,
    busy,
    chat_events,
    envelope,
    fake_jev,
    finding,
    parent_events,
    result,
    routine_events,
    setup_home,
    text,
    tool,
    user,
    worker_events,
    write_transcript,
    writer,
)

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "amem_digest.py"
PACKAGE = ROOT / "src" / "agent_memory" / "amem_digest.py"
CLOUD_CWD = "/home/user/nightly-trials"
SID = "0b1c2d3e-4f50-4617-8899-aabbccddeeff"
FAKE_KEY = "sk-ant-" + "Q" * 30          # credential-shaped, made up
FAKE_TOKEN = "ghp_" + "Z" * 30
NIGHTLY_MD = "# Nightly\n## Report\nPost the report as the last step.\n## Budget\nStay under one hour.\n"
PROMPT = "Run the nightly trials. Follow NIGHTLY.md.\n"

Writer = Callable[..., FakeWriter]


def cloud_events(sid: str = SID, cwd: str = CLOUD_CWD) -> list[dict[str, Any]]:
    events = [
        user(f"Run the nightly trials. The key is {FAKE_KEY}", 0, cwd=cwd, entrypoint="remote"),
        assistant(text("Starting."), tool("Skill", "s1", skill="systematic-debugging"), minute=1, cwd=cwd),
        result("s1", "loaded", 1, cwd=cwd),
        assistant(tool("Bash", "b1", command=f"git push https://x:{FAKE_TOKEN}@github.com/a/b"), minute=2, cwd=cwd),
        result("b1", "rejected", 3, error=True, cwd=cwd),
        user("<command-name>/handoff</command-name>", 4, cwd=cwd),
        user("<command-name>/compact</command-name>", 5, cwd=cwd),
        assistant(text("All trials done."), minute=6, cwd=cwd),
    ] + busy(cwd)
    for e in events:
        e["sessionId"] = sid
    return events


def write_cloud(path: Path, sid: str = SID) -> Path:
    return write_transcript(path, cloud_events(sid))


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    d = tmp_path / "cloud-vm"
    d.mkdir()
    (d / "NIGHTLY.md").write_text(NIGHTLY_MD, encoding="utf-8")
    (d / "prompt.md").write_text(PROMPT, encoding="utf-8")
    t = write_cloud(d / "projects" / "-home-user-nightly-trials" / f"{SID}.jsonl")
    return {"dir": d, "nightly": d / "NIGHTLY.md", "prompt": d / "prompt.md", "transcript": t,
            "projects": d / "projects"}


def run_tool(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    # -I: isolated mode, so the script cannot import agent_memory from the environment.
    return subprocess.run([sys.executable, "-I", str(TOOL), *args], env=env or base_env(),
                          capture_output=True, text=True, timeout=60, cwd=cwd)


# --------------------------------------------------------------------------- one implementation


def test_tool_is_a_byte_identical_copy_of_the_package_module() -> None:
    assert TOOL.read_bytes() == PACKAGE.read_bytes(), "run: cp src/agent_memory/amem_digest.py tools/"


def test_tool_imports_only_the_stdlib() -> None:
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names - {"__future__"} <= set(sys.stdlib_module_names), names


def test_script_and_digest_self_print_the_same_row(files: dict[str, Path], tmp_path: Path) -> None:
    args = ["--transcript", str(files["transcript"]), "--routine", "Nightly Trials",
            "--prompt", str(files["prompt"]), "--instruction", f"nightly-md={files['nightly']}"]
    script = run_tool(*args, cwd=tmp_path)
    package = run_amem("digest-self", *args, env=base_env())
    assert script.returncode == 0 and package.returncode == 0, (script.stderr, package.stderr)
    assert script.stdout == package.stdout and script.stdout.count("\n") == 1
    row = json.loads(script.stdout)
    assert row == amem_digest.build_row(files["transcript"], routine="Nightly Trials", repo=None, instructions=[
        ("prompt", files["prompt"]), ("nightly-md", files["nightly"])])


def _transcripts() -> list[list[dict[str, Any]]]:
    cwd = "/work/demo-repo"
    big = [user("first prompt " + PAD, 0, cwd=cwd)]
    for i in range(400):
        big.append(assistant(text(f"step {i} " + PAD), tool("Bash", f"b{i}", command=f"echo {i}"), minute=1, cwd=cwd))
        big.append(result(f"b{i}", "boom " * 80, 1, error=True, cwd=cwd))
    big.append(user("last prompt", 2, cwd=cwd))
    denied = [user("go", 0), assistant(tool("Write", "w1", file_path="a.txt"), minute=1),
              result("w1", "no", 1, permissionDecision={"decision": "reject", "source": "hook"}),
              {"type": "attachment", "attachment": {"type": "hook_blocking_error", "hookEvent": "PreToolUse",
                                                    "blockingError": "blocked"}},
              user("[Request interrupted by user]", 2), user("<task-notification>done</task-notification>", 3),
              user("meta", 4, isMeta=True)]
    return [cloud_events(), chat_events(cwd), parent_events(cwd), worker_events(cwd), routine_events(cwd), big,
            denied]


def test_digest_equals_deviations_build_digest() -> None:
    for events in _transcripts():
        assert amem_digest.build_digest(events) == deviations.build_digest(events)
        assert amem_digest.build_digest(events, 3_000) == deviations.build_digest(events, 3_000)
        f = sessions_ingest.scan_facts(events)
        mine = amem_digest.facts(events)
        assert (mine["skills"], mine["tool_calls"], mine["prompts"]) == (f.skills, f.tool_calls, f.prompts)


def test_copied_rules_match_the_package() -> None:
    theirs = [*tasks_ingest._REDACT_RES, *deviations._EXTRA_REDACT_RES]
    assert [(p.pattern, p.flags) for p in amem_digest.REDACT_RES] == [(p.pattern, p.flags) for p in theirs]
    assert amem_digest.BUILTIN_COMMANDS == sessions_ingest.BUILTIN_COMMANDS
    assert amem_digest.SHORT_ARG_KEYS == deviations._SHORT_ARG_KEYS
    for name in ("DIGEST_MAX", "HEAD_SHARE", "FIRST_USER_MAX", "USER_MAX", "ASSISTANT_MAX", "ARGS_MAX",
                 "ERROR_MAX", "NOTICE_MAX"):
        assert getattr(amem_digest, name) == getattr(deviations, name), name
    assert amem_digest.SOURCE_PREFIX == sessions_ingest.CLOUD_PREFIX
    assert amem_digest.PROMPT_NAME == sessions_ingest.CLOUD_PROMPT


# --------------------------------------------------------------------------- the script


def test_row_shape_redaction_and_no_env(files: dict[str, Path], tmp_path: Path) -> None:
    secret_env = "env-value-" + "V" * 20
    env = base_env(SOME_SECRET=secret_env, AMEM_SUPABASE_URL="https://example.invalid")
    (files["dir"] / ".env").write_text(f"X={secret_env}\n")
    nightly = files["nightly"]
    nightly.write_text(NIGHTLY_MD + f"password: {'p' * 16}\n", encoding="utf-8")
    res = run_tool("--transcript", str(files["transcript"]), "--routine", "Nightly Trials",
                   "--instruction", f"nightly-md={nightly}", "--instruction", f"envfile={files['dir'] / '.env'}",
                   env=env, cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    assert "env files are never read" in res.stderr
    row = json.loads(res.stdout)
    assert set(row) == {"source_key", "routine", "session_id", "started_at", "ended_at", "digest", "digest_sha",
                        "instructions", "skills", "meta"}
    assert row["source_key"] == f"cloud:{SID}" and row["session_id"] == SID and row["routine"] == "nightly-trials"
    assert row["started_at"] == "2026-10-14T09:00:00+00:00" and row["ended_at"] == "2026-10-14T09:12:00+00:00"
    assert row["digest_sha"] == hashlib.sha256(row["digest"].encode()).hexdigest()
    assert row["skills"] == ["systematic-debugging", "handoff"]
    assert row["meta"] == {"cloud": True, "repo": "nightly-trials", "script_version": amem_digest.SCRIPT_VERSION,
                           "transcript_bytes": files["transcript"].stat().st_size, "tool_calls": 5, "prompts": 1}
    (instr,) = row["instructions"]
    assert instr["name"] == "nightly-md" and "[redacted]" in instr["text"]
    assert instr["sha256"] == hashlib.sha256(nightly.read_bytes()).hexdigest()
    for leak in (FAKE_KEY, FAKE_TOKEN, "p" * 16, secret_env, "example.invalid"):
        assert leak not in res.stdout and leak not in res.stderr, leak
    assert "[turn 1] USER: Run the nightly trials. The key is [redacted]" in row["digest"]
    assert "TOOL Bash: git push https://[redacted]@github.com/a/b" in row["digest"]


def test_picks_the_newest_session_transcript(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    old = write_cloud(projects / "-a" / "old-session.jsonl", "old-session")
    new = write_cloud(projects / "-b" / "new-session.jsonl", "new-session")
    sub = write_cloud(projects / "-b" / "new-session" / "subagents" / "agent-x.jsonl", "sub-session")
    now = time.time()
    os.utime(old, (now - 600, now - 600))
    os.utime(new, (now - 60, now - 60))
    os.utime(sub, (now, now))  # newer, but a subagent transcript is never picked
    assert amem_digest.find_transcript(projects, now=now) == new
    res = run_tool("--routine", "fresh-install", "--projects-dir", str(projects))
    assert res.returncode == 0 and json.loads(res.stdout)["session_id"] == "new-session"
    # The newest file is too old to be this session: nothing sent, exit 0.
    with pytest.raises(amem_digest.DigestError, match="minutes old"):
        amem_digest.find_transcript(projects, max_age_minutes=5, now=now + 3600)
    for f in (old, new):
        os.utime(f, (now - 3 * 3600, now - 3 * 3600))
    res = run_tool("--routine", "fresh-install", "--projects-dir", str(projects))  # default limit: 120 minutes
    assert res.returncode == 0 and res.stdout == "" and "minutes old" in res.stderr
    res = run_tool("--routine", "fresh-install", "--projects-dir", str(projects), "--max-age-minutes", "0")
    assert json.loads(res.stdout)["session_id"] == "new-session"


@pytest.mark.parametrize("args", [
    ("--projects-dir", "{tmp}/missing"),
    ("--projects-dir", "{tmp}/empty"),
    ("--transcript", "{tmp}/nope.jsonl"),
])
def test_no_transcript_exits_0_with_one_stderr_line(tmp_path: Path, args: tuple[str, str]) -> None:
    (tmp_path / "empty" / "-proj").mkdir(parents=True)
    res = run_tool("--routine", "nightly-trials", args[0], args[1].format(tmp=tmp_path))
    assert res.returncode == 0 and res.stdout == ""
    assert res.stderr.startswith("amem_digest: ") and res.stderr.rstrip().endswith("Nothing to send.")
    assert res.stderr.count("\n") == 1


def test_digest_is_capped_head_and_tail(tmp_path: Path) -> None:
    events = _transcripts()[5]
    for e in events:
        e["sessionId"] = "big-one"
    t = write_transcript(tmp_path / "big.jsonl", events, pad=False)
    row = amem_digest.build_row(t, routine="nightly-trials", instructions=[])
    digest = row["digest"]
    assert len(digest) <= amem_digest.DIGEST_MAX and "chars cut from the middle" in digest
    assert digest.startswith("[turn 1] USER: first prompt") and digest.endswith("[turn 2] USER: last prompt")


def test_prompt_from_transcript(files: dict[str, Path], tmp_path: Path) -> None:
    res = run_tool("--transcript", str(files["transcript"]), "--routine", "nightly-trials",
                   "--prompt-from-transcript", "--instruction", f"nightly-md={files['nightly']}", cwd=tmp_path)
    row = json.loads(res.stdout)
    assert [i["name"] for i in row["instructions"]] == ["prompt", "nightly-md"]
    first = f"Run the nightly trials. The key is {FAKE_KEY}"
    assert row["instructions"][0] == {"name": "prompt", "sha256": hashlib.sha256(first.encode()).hexdigest(),
                                      "text": "Run the nightly trials. The key is [redacted]"}
    # A prompt file wins over the transcript.
    res = run_tool("--transcript", str(files["transcript"]), "--routine", "nightly-trials",
                   "--prompt-from-transcript", "--prompt", str(files["prompt"]), cwd=tmp_path)
    assert json.loads(res.stdout)["instructions"][0]["text"] == PROMPT


def test_routine_slug() -> None:
    assert amem_digest.slug("Nightly Trials") == "nightly-trials"
    assert amem_digest.slug("  Fresh Install!! ") == "fresh-install"
    assert amem_digest.slug("***") == "cloud"


# --------------------------------------------------------------------------- database


@pytest.fixture
def db(setup_home: Home, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    def clean() -> None:
        with psycopg.connect(pg_url) as conn:
            conn.execute("delete from deviations")
            conn.execute("delete from sessions")
            conn.execute("delete from cloud_digests")
    clean()
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    yield pg_url
    clean()  # never leave a pending cloud row for another test's ingest


def _q(url: str, sql: str, *args: object) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


def post(url: str, row: dict[str, Any]) -> int:
    """What PostgREST does for `Prefer: resolution=ignore-duplicates`: insert, on conflict on the
    primary key do nothing. Returns the rows inserted."""
    cols = list(row)
    vals = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in row.values()]
    with psycopg.connect(url) as conn:
        cur = conn.execute(f"insert into cloud_digests ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
                           "on conflict do nothing", vals)
        return cur.rowcount


def cloud_row(files: dict[str, Path]) -> dict[str, Any]:
    return amem_digest.build_row(files["transcript"], routine="Nightly Trials", instructions=[
        ("prompt", files["prompt"]), ("nightly-md", files["nightly"])])


def test_rls_on_cloud_digests_and_api_roles_revoked(db: str) -> None:
    assert _q(db, "select relrowsecurity from pg_class where relname = 'cloud_digests'") == [(True,)]
    migration = (deviations.config.MIGRATIONS_DIR / "20261017040000_cloud_digests.sql").read_text()
    with psycopg.connect(db) as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"create role {role}")
            conn.execute(f"grant select, insert on public.cloud_digests to {role}")
        conn.execute(migration)  # re-runnable, and revokes the grants
        granted = conn.execute("select count(*) from information_schema.role_table_grants "
                               "where table_name = 'cloud_digests' and grantee in ('anon', 'authenticated')").fetchone()
        assert granted == (0,)
        conn.rollback()


def test_repeated_post_is_ignored(db: str, files: dict[str, Path]) -> None:
    row = cloud_row(files)
    assert post(db, row) == 1
    assert post(db, {**row, "digest": "changed"}) == 0
    assert _q(db, "select count(*), min(digest) = %s from cloud_digests", row["digest"]) == [(1, True)]
    with pytest.raises(psycopg.errors.CheckViolation):
        post(db, {**row, "source_key": "cloud:../x"})


def test_ingest_turns_cloud_rows_into_sessions(setup_home: Home, db: str,  # noqa: F811
                                               files: dict[str, Path]) -> None:
    post(db, cloud_row(files))
    first = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert first.line() == ("sessions: 1 files, 1 new, 0 updated, 0 skipped (chat=0 parent=0 worker=0 headless=0 "
                            "eval=0 routine=1) tools(claude=1 codex=0) team=0 cloud=1")
    (s,) = _q(db, "select session_id, tool, kind, repo, project_dir, started_at is not null, transcript_bytes, "
                  "skills, instructions, meta from sessions")
    assert s[:7] == (f"cloud:{SID}", "claude", "routine", "nightly-trials", "cloud", True,
                     files["transcript"].stat().st_size)
    assert s[7] == ["systematic-debugging", "handoff"]
    assert s[8] == ["routine:nightly-trials", "cloud-file:nightly-md"]
    assert s[9]["cloud"] is True and s[9]["routine"] == "nightly-trials" and s[9]["tool_calls"] == 5
    assert _q(db, "select ingested_at is not null from cloud_digests") == [(True,)]

    again = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert (again.new, again.updated, again.cloud) == (0, 0, 0) and "cloud=" not in again.line()
    # A row ingested again (ingested_at reset) changes nothing and keeps scanned_at.
    with psycopg.connect(db) as conn:
        conn.execute("update sessions set scanned_at = now()")
        conn.execute("update cloud_digests set ingested_at = null")
    third = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert (third.new, third.updated, third.cloud) == (0, 0, 1)
    assert _q(db, "select count(*), bool_and(scanned_at is not null) from sessions") == [(1, True)]

    # --tool codex leaves cloud rows alone (they are Claude Code sessions).
    with psycopg.connect(db) as conn:
        conn.execute("update cloud_digests set ingested_at = null")
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("codex",))
    assert _q(db, "select ingested_at is null from cloud_digests") == [(True,)]


def test_cli_sessions_ingest_reads_cloud_rows(setup_home: Home, db: str,  # noqa: F811
                                              files: dict[str, Path]) -> None:
    post(db, cloud_row(files))
    env = base_env(AMEM_DB_URL=db, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_PROJECTS_DIR=str(setup_home.projects))
    res = run_amem("sessions", "ingest", env=env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.rstrip().endswith("cloud=1")


def _cloud_reply(prompt: str) -> str:
    return envelope({"deviations": [
        finding("cloud-file:nightly-md", "report", 1, 0.2, 0.5),
        finding("routine:nightly-trials", "Run the nightly trials", None, 0.4, 0.6),
    ]})


def test_scan_uses_the_stored_digest_and_instructions(setup_home: Home, db: str, files: dict[str, Path],  # noqa: F811
                                                       writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    row = cloud_row(files)
    post(db, row)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    files["transcript"].unlink()  # the scan never needs the transcript

    dry = deviations.scan(dry_run=True, now=NOW)
    (o,) = dry.outcomes
    assert o.line() == (f"dry-run cloud:{SID} kind=routine digest_chars={len(row['digest'])} "
                        "instructions=routine:nightly-trials,cloud-file:nightly-md")

    w = writer(default=_cloud_reply)
    report = deviations.scan(now=NOW)
    assert report.summary() == "deviations: sessions=1 found=2 edit=2 enforce=0 leave=0 errors=0"
    (prompt,) = w.prompts
    sources = next(ln for ln in prompt.splitlines() if ln.startswith("- instruction_source: exactly one of: "))
    assert sources.endswith("routine:nightly-trials, cloud-file:nightly-md")  # no local CLAUDE.md
    assert "Keep answers short." not in prompt  # the Mac's global CLAUDE.md was not in force
    assert "1. report — Report" in prompt and "2. budget — Budget" in prompt and PROMPT.strip() in prompt
    assert row["digest"] in prompt and FAKE_KEY not in prompt

    got = _q(db, "select instruction_source, rule_id, rule_title, instruction_sha from deviations "
                 "order by instruction_source")
    nightly_sha = hashlib.sha256(files["nightly"].read_bytes()).hexdigest()
    prompt_sha = hashlib.sha256(files["prompt"].read_bytes()).hexdigest()
    assert got == [("cloud-file:nightly-md", "report", "Report", nightly_sha),
                   ("routine:nightly-trials", "whole", "(whole file)", prompt_sha)]
    assert _q(db, "select scanned_at is not null from sessions") == [(True,)]
    states = "\n".join(fake_jev.states)
    assert "instruction_source: cloud-file:nightly-md" in states and "instruction_rule: report" in states

    # reref snaps against the stored texts too (no local file exists for these sources).
    rr = deviations.reref()
    assert rr.how["no-source"] == 0 and rr.rows == 2 and rr.changed == 0

    cands = deviations.candidates(now=NOW)
    assert {c["instruction_source"]: c["target_paths"] for c in cands} == {
        "cloud-file:nightly-md": [], "routine:nightly-trials": []}


def test_scan_errors_when_the_cloud_row_is_gone(setup_home: Home, db: str, files: dict[str, Path],  # noqa: F811
                                                writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    post(db, cloud_row(files))
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    with psycopg.connect(db) as conn:
        conn.execute("delete from cloud_digests")
    writer(default=_cloud_reply)
    report = deviations.scan(now=NOW)
    (o,) = report.outcomes
    assert o.status == "error" and o.reason == "cloud digest not found"
