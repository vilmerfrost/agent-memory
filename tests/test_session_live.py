"""Live session rows from Claude Code hooks (docs/sessions-live.md): `amem sessions ping`, its
state file, debounce, spool, the hook script, and `amem sessions tokens`. Synthetic payloads
and transcripts only; the Claude home is a tmp dir (setup_home)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import session_live, sessions_ingest, tokens_report
from conftest import base_env, run_amem
from test_deviations_support import NOW, Home, setup_home, write_transcript  # noqa: F401
from test_session_tokens import EXPECTED, reply, session_events, usage

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "deploy" / "hooks" / "amem-session-hook.sh"
SID = "5e55a0b1-0000-4000-8000-00000000live"


def payload(event: str, home: Home, sid: str = SID, **extra: Any) -> str:
    proj = home.projects / "-work-demo-repo"
    body = {"session_id": sid, "transcript_path": str(proj / f"{sid}.jsonl"), "cwd": str(home.repo),
            "permission_mode": "default", "hook_event_name": event, **extra}
    if event == "SessionStart":
        body.setdefault("source", "startup")
        body.setdefault("model", "claude-test-1")
    if event == "Stop":
        body.setdefault("stop_hook_active", False)
        body.setdefault("last_assistant_message", "SECRET-REPLY-TEXT should never be stored")
    return json.dumps(body)


def transcript(home: Home, events: list[dict[str, Any]], sid: str = SID) -> Path:
    return write_transcript(home.project("-work-demo-repo") / f"{sid}.jsonl", events, pad=False)


def parent(home: Home) -> Path:
    return home.claude / "parent"


def dead_url() -> str:
    """A local port with nothing listening: connecting fails at once."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
        conn.execute("delete from cloud_digests")
    return pg_url


def _row(url: str, sid: str = SID) -> dict[str, Any] | None:
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute("select * from sessions where session_id = %s", (sid,)).fetchone()


# --------------------------------------------------------------------------- payloads


def test_payload_parsing() -> None:
    p = session_live.parse_payload(json.dumps({"session_id": "abc", "hook_event_name": "SessionStart",
                                               "cwd": "/w/x", "transcript_path": "/p/-w-x/abc.jsonl",
                                               "source": "resume", "agent_type": "reviewer"}))
    assert p is not None and (p.event, p.session_id, p.source, p.agent_type) == ("SessionStart", "abc", "resume",
                                                                               "reviewer")
    for bad in ("", "nope", "[]", json.dumps({"session_id": "abc", "hook_event_name": "PreToolUse"}),
                json.dumps({"session_id": "../x", "hook_event_name": "Stop"}),
                json.dumps({"session_id": "abc", "hook_event_name": "Stop", "agent_id": "a1"})):
        assert session_live.parse_payload(bad) is None


# --------------------------------------------------------------------------- ping


def test_session_start_writes_a_minimal_row(setup_home: Home, db: str) -> None:  # noqa: F811
    assert session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db) == "written"
    row = _row(db)
    assert row is not None
    assert (row["tool"], row["kind"], row["repo"], row["project_dir"]) == ("claude", "chat", "demo-repo",
                                                                           "-work-demo-repo")
    assert row["started_at"] == NOW and row["last_activity_at"] == NOW
    assert row["tokens_input"] is None and row["turns"] is None and row["transcript_bytes"] == 0
    assert row["meta"]["live"] is True and row["meta"]["start_source"] == "startup"
    assert row["meta"]["model"] == "claude-test-1" and row["meta"]["cwd"] == str(setup_home.repo)


def test_stop_writes_tokens_turns_and_activity(setup_home: Home, db: str) -> None:  # noqa: F811
    cwd = str(setup_home.repo)
    path = transcript(setup_home, session_events(cwd))
    sub = path.parent / SID / "subagents"
    write_transcript(sub / "agent-a1.jsonl", [reply("w1", usage(1, 2, 3, 4), cwd=cwd)], pad=False)
    session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db)
    assert session_live.ping(payload("Stop", setup_home), now=NOW + timedelta(minutes=5), db_url=db) == "written"
    row = _row(db)
    assert row is not None
    assert [row["tokens_input"], row["tokens_output"], row["tokens_cache_read"],
            row["tokens_cache_creation"]] == EXPECTED.as_list()
    assert row["turns"] == 2 and row["models"]["claude-other-2"]["calls"] == 1
    assert row["last_activity_at"] == NOW + timedelta(minutes=5) and row["tokens_updated_at"] is not None
    assert row["started_at"].isoformat().startswith("2026-10-14T09:00")  # the transcript's first event
    assert row["meta"]["subagent_tokens"] == {"input": 1, "output": 2, "cache_read": 3, "cache_creation": 4,
                                              "sessions": 1}
    # No transcript text anywhere: not in the row, not in the state file.
    dump = json.dumps(row, default=str) + (parent(setup_home) / session_live.STATE_NAME).read_text()
    assert "SECRET-REPLY-TEXT" not in dump and "Please fix the parser" not in dump
    # The state holds the byte offset: the whole file was read.
    state = json.loads((parent(setup_home) / session_live.STATE_NAME).read_text())
    assert state[SID]["scan"]["offset"] == path.stat().st_size


def test_incremental_offset_and_debounce(setup_home: Home, db: str) -> None:  # noqa: F811
    cwd = str(setup_home.repo)
    events = session_events(cwd)
    path = transcript(setup_home, events[:5])
    t0 = NOW
    assert session_live.ping(payload("Stop", setup_home), now=t0, db_url=db) == "written"  # no row yet: inserted
    first = _row(db)
    assert first is not None and first["tokens_output"] == 10 and first["kind"] == "chat"
    offset = json.loads((parent(setup_home) / session_live.STATE_NAME).read_text())[SID]["scan"]["offset"]
    assert offset == path.stat().st_size
    transcript(setup_home, events)  # the session goes on
    assert session_live.ping(payload("Stop", setup_home), now=t0 + timedelta(seconds=5), db_url=db) == "debounced"
    assert _row(db)["tokens_output"] == 10  # type: ignore[index]
    assert session_live.ping(payload("Stop", setup_home), now=t0 + timedelta(seconds=21), db_url=db) == "written"
    assert _row(db)["tokens_output"] == EXPECTED.output  # type: ignore[index]
    # SessionEnd is never debounced.
    assert session_live.ping(payload("SessionEnd", setup_home, reason="other"),
                             now=t0 + timedelta(seconds=22), db_url=db) == "written"
    assert _row(db)["meta"]["ended"] is True  # type: ignore[index]


def test_spool_when_the_db_is_down_then_flush(setup_home: Home, db: str) -> None:  # noqa: F811
    transcript(setup_home, session_events(str(setup_home.repo)))
    status = session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=dead_url())
    assert status.startswith("spooled (db error")
    status = session_live.ping(payload("Stop", setup_home), now=NOW + timedelta(minutes=1), db_url=dead_url())
    assert status.startswith("spooled (db error")
    spool = parent(setup_home) / session_live.SPOOL_NAME
    assert len(spool.read_text().splitlines()) == 2 and _row(db) is None
    assert "SECRET-REPLY-TEXT" not in spool.read_text()
    # The next ping reaches the database: the spool goes first, then this ping.
    other = "6e55a0b1-0000-4000-8000-0000000other"
    assert session_live.ping(payload("SessionStart", setup_home, sid=other), now=NOW, db_url=db) \
        == "written (+2 from spool)"
    assert not spool.exists()
    row = _row(db)
    assert row is not None and row["tokens_output"] == EXPECTED.output and _row(db, other) is not None


def test_sessions_ingest_flushes_the_spool(setup_home: Home, db: str) -> None:  # noqa: F811
    session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=dead_url())
    report = sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("claude",))
    assert report.spool == 1 and report.line().endswith("spool=1")
    assert _row(db) is not None and not (parent(setup_home) / session_live.SPOOL_NAME).exists()


def test_old_spooled_ping_never_lowers_tokens(setup_home: Home, db: str) -> None:  # noqa: F811
    cwd = str(setup_home.repo)
    events = session_events(cwd)
    transcript(setup_home, events[:5])
    session_live.ping(payload("Stop", setup_home), now=NOW, db_url=dead_url())  # spooled: output 10
    spool = parent(setup_home) / session_live.SPOOL_NAME
    aside = spool.with_name("aside.jsonl")
    spool.rename(aside)
    transcript(setup_home, events)
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db, tools=("claude",))
    assert _row(db)["tokens_output"] == EXPECTED.output  # type: ignore[index]
    aside.rename(spool)  # the old ping arrives late
    assert session_live.flush_spool(db_url=db) == 1
    row = _row(db)
    assert row is not None and row["tokens_output"] == EXPECTED.output and row["meta"]["live"] is True


def test_no_remote_configured_spools_only(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    spool = parent(setup_home) / session_live.SPOOL_NAME
    # No AMEM_DB_URL at all.
    assert session_live.ping(payload("SessionStart", setup_home), now=NOW) == \
        "spooled (no AMEM_DB_URL (no remote configured))"
    # A local database is never written by accident (only the test flag allows it).
    monkeypatch.setenv("AMEM_DB_URL", db)
    assert session_live.ping(payload("SessionStart", setup_home), now=NOW) == \
        "spooled (AMEM_DB_URL is local (no remote configured))"
    # The REST backend has no sessions endpoint: spool too.
    monkeypatch.setenv("AMEM_BACKEND", "rest")
    monkeypatch.setenv("AMEM_SUPABASE_URL", "http://127.0.0.1:9")
    assert session_live.ping(payload("SessionStart", setup_home), now=NOW).startswith("spooled (backend rest")
    assert len(spool.read_text().splitlines()) == 3 and _row(db) is None
    monkeypatch.delenv("AMEM_BACKEND")
    monkeypatch.delenv("AMEM_SUPABASE_URL")
    monkeypatch.setenv(session_live.ALLOW_LOCAL_ENV, "1")
    assert session_live.ping(payload("SessionStart", setup_home), now=NOW) == "written (+3 from spool)"


def test_a_busy_lock_never_blocks_long(setup_home: Home, db: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(session_live, "LOCK_WAIT_SECONDS", 0.2)
    with session_live.locked() as got:  # another ping holds the lock
        assert got
        started = time.monotonic()
        assert session_live.ping(payload("Stop", setup_home), now=NOW, db_url=db) == "skipped (busy)"
        assert session_live.ping(payload("SessionStart", setup_home), now=NOW, db_url=db) == "spooled (busy)"
        assert time.monotonic() - started < 2
    assert len((parent(setup_home) / session_live.SPOOL_NAME).read_text().splitlines()) == 1


# --------------------------------------------------------------------------- the command never fails


def _ping_cli(stdin: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(Path(sys.executable).parent / "amem"), "sessions", "ping", *args], input=stdin,
                          env=base_env(**env), capture_output=True, text=True, timeout=60)


def test_cli_ping_writes_and_always_exits_0(setup_home: Home, db: str, tmp_path: Path) -> None:  # noqa: F811
    transcript(setup_home, session_events(str(setup_home.repo)))
    home = {"AMEM_CLAUDE_HOME": str(setup_home.claude)}
    ok = _ping_cli(payload("Stop", setup_home), AMEM_DB_URL=db, AMEM_PING_ALLOW_LOCAL="1", **home)
    assert ok.returncode == 0 and ok.stdout == "" and " Stop 5e55a0b1 written " in ok.stderr
    assert _row(db)["tokens_output"] == EXPECTED.output  # type: ignore[index]
    for stdin, args, env in [
        ("not json", (), {}),
        (payload("Stop", setup_home), ("--bogus",), {}),
        (payload("SessionStart", setup_home), (), {"AMEM_DB_URL": dead_url(), "AMEM_PING_ALLOW_LOCAL": "1"}),
        (payload("SessionStart", setup_home), (), {"AMEM_BACKEND": "nonsense"}),
        (payload("SessionStart", setup_home), (), {"AMEM_DB_URL": "not a dsn ==="}),
    ]:
        res = _ping_cli(stdin, *args, **home, **env)
        assert res.returncode == 0 and res.stdout == "", (args, env, res.stderr)
        assert res.stderr.startswith("amem-ping") and res.stderr.count("\n") == 1, res.stderr
        assert "postgres:postgres" not in res.stderr
    # --input + --delete-input: the payload file is read and removed.
    f = tmp_path / "payload.json"
    f.write_text(payload("SessionStart", setup_home))
    res = _ping_cli("", "--input", str(f), "--delete-input", **home)
    assert res.returncode == 0 and not f.exists() and "spooled" in res.stderr


def test_hard_timeout(setup_home: Home, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    def slow(_raw: bytes) -> str:
        time.sleep(5)
        return "written"

    monkeypatch.setattr(session_live, "ping", slow)
    f = setup_home.root / "p.json"
    f.write_text(payload("Stop", setup_home))
    started = time.monotonic()
    assert session_live.main(["--input", str(f), "--timeout", "1"]) == 0
    assert time.monotonic() - started < 3
    assert " timeout " in capsys.readouterr().err


# --------------------------------------------------------------------------- the hook script


def test_hook_script_exits_0_fast_and_detaches(tmp_path: Path) -> None:
    hook_dir = tmp_path / "parent"
    marker = tmp_path / "ran.txt"
    fake_uv = tmp_path / "uv"
    # Sleeps first: if the hook waited for it (or kept its pipes), the run below would take 3 s.
    fake_uv.write_text("#!/bin/bash\nsleep 3\n"
                       f"{{ printf '%s\\n' \"$@\"; echo ---; cat \"${{9}}\"; }} > '{marker}'\n")
    fake_uv.chmod(0o755)
    repo = tmp_path / "repo"
    repo.mkdir()
    body = json.dumps({"session_id": "s1", "hook_event_name": "Stop", "cwd": "/w"})
    env = {**base_env(), "AMEM_HOOK_UV": str(fake_uv), "AMEM_HOOK_REPO": str(repo), "AMEM_HOOK_DIR": str(hook_dir)}
    started = time.monotonic()
    res = subprocess.run(["/bin/bash", str(HOOK)], input=body, env=env, capture_output=True, text=True, timeout=30)
    took = time.monotonic() - started
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""
    assert took < 2, took
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    args = marker.read_text().splitlines()
    sep = args.index("---")
    assert args[:sep] == ["run", "--directory", str(repo), "--quiet", "amem", "sessions", "ping", "--input",
                          args[8], "--delete-input", "--setsid"]
    assert args[8].startswith(str(hook_dir / "amem-hook-tmp" / "payload."))
    assert json.loads("\n".join(args[sep + 1:])) == json.loads(body)
    assert oct(os.stat(args[8]).st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("env", [{"AMEM_HOOK_OFF": "1"}, {"AMEM_HOOK_UV": "/nonexistent/uv"}])
def test_hook_script_exits_0_when_off_or_broken(tmp_path: Path, env: dict[str, str]) -> None:
    full = {**base_env(), "AMEM_HOOK_REPO": str(tmp_path / "missing"), "AMEM_HOOK_DIR": str(tmp_path / "p"), **env}
    res = subprocess.run(["/bin/bash", str(HOOK)], input="{}", env=full, capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""


# --------------------------------------------------------------------------- tokens report


def _seed(url: str) -> None:
    rows = [
        ("t-a", "claude", "chat", "agent-memory", [10, 1000, 50000, 2000], {"claude-x": [10, 1000, 50000, 2000]}, 4),
        ("t-a/agent-w1", "claude", "worker", "agent-memory", [5, 500, 9000, 100], {"claude-x": [5, 500, 9000, 100]}, 1),
        ("codex:t-b", "codex", "chat", "simplitics", [300, 40, 700, 0], {"gpt-y": [300, 40, 700, 0]}, 2),
        ("t-old", "claude", "chat", "agent-memory", [1, 1, 1, 1], {"claude-x": [1, 1, 1, 1]}, 1),
        ("t-none", "claude", "chat", "agent-memory", None, None, None),
    ]
    with psycopg.connect(url) as conn:
        for sid, tool, kind, repo, t, models, turns in rows:
            when = NOW - timedelta(days=30 if sid == "t-old" else 1)
            m = {k: dict(zip(("input", "output", "cache_read", "cache_creation"), v, strict=True)) | {"calls": 1}
                 for k, v in (models or {}).items()}
            conn.execute(
                "insert into sessions (session_id, tool, kind, repo, project_dir, started_at, last_activity_at, meta, "
                "tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation, models, turns) "
                "values (%s, %s, %s, %s, 'p', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (sid, tool, kind, repo, when, when,
                 Jsonb({"subagent_tokens": {"input": 5, "output": 500, "cache_read": 9000,
                                            "cache_creation": 100, "sessions": 1}} if sid == "t-a" else {}),
                 *(t or [None] * 4), Jsonb(m) if models else None, turns))


def test_tokens_report(db: str) -> None:
    _seed(db)
    doc = tokens_report.summarize(tokens_report.fetch(days=7, now=NOW, db_url=db), days=7)
    assert (doc["sessions"], doc["measured"]) == (4, 3)  # t-old is outside the window, t-none unmeasured
    assert doc["totals"] == {"input": 315, "output": 1540, "cache_read": 59700, "cache_creation": 2100,
                             "total": 63655}  # the parent's subagent_tokens are not added again
    assert [(g["key"], g["sessions"], g["total"]) for g in doc["by_repo"]] == [
        ("agent-memory", 3, 62615), ("simplitics", 1, 1040)]
    assert [g["key"] for g in doc["by_tool"]] == ["claude", "codex"]
    assert {g["key"]: g["total"] for g in doc["by_kind"]} == {"chat": 54050, "worker": 9605}
    assert {g["key"]: g["total"] for g in doc["by_model"]} == {"claude-x": 62615, "gpt-y": 1040}
    assert [s["session_id"] for s in doc["top"]] == ["t-a", "t-a/agent-w1", "codex:t-b"]
    text = "\n".join(tokens_report.lines(doc))
    assert text.startswith("tokens: last 7 days: 4 sessions (3 measured), total 63.7k")
    assert "by repo" in text and "by model" in text and "(subagents 9.6k)" in text
    only = tokens_report.summarize(tokens_report.fetch(days=7, tool="codex", now=NOW, db_url=db), days=7,
                                   tool="codex")
    assert only["sessions"] == 1 and only["totals"]["total"] == 1040


def test_cli_sessions_tokens(db: str) -> None:
    _seed(db)
    res = run_amem("sessions", "tokens", "--days", "3650", "--json", env=base_env(AMEM_DB_URL=db))
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["sessions"] == 5 and doc["totals"]["total"] == 63655 + 4
    res = run_amem("sessions", "tokens", "--repo", "simplitics", "--days", "3650", env=base_env(AMEM_DB_URL=db))
    assert res.returncode == 0 and res.stdout.startswith("tokens: last 3650 days, repo simplitics: 1 sessions")
    rest = run_amem("sessions", "tokens", env=base_env(AMEM_BACKEND="rest", AMEM_SUPABASE_URL="http://127.0.0.1:9"))
    assert rest.returncode == 1 and "Postgres backend" in rest.stderr
