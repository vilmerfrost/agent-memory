"""Cloud chats (docs/cloud.md, "Cloud chats"): `amem_digest.py --hook` against a local fake
PostgREST, the cloud_digests chat/routine triggers, `sessions ingest` refreshes, the
amem_assets table + `amem cloud publish-script`, and deploy/cloud/setup.sh (v2) plus the hook.sh
it writes, under a temp AMEM_SETUP_ROOT and a temp HOME.
Synthetic transcripts only; no request ever leaves 127.0.0.1."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import amem_digest, config, deviations, sessions_ingest
from conftest import base_env, run_amem
from test_deviations_support import NOW, assistant, result, text, tool, user, write_transcript
from test_session_tokens import reply, usage

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "amem_digest.py"
SETUP = ROOT / "deploy" / "cloud" / "setup.sh"
SID = "c4a7c0de-1111-4222-8333-444455556666"
FAKE_KEY = "sk-ant-" + "K" * 30
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
               "NO_PROXY", "no_proxy")


# --------------------------------------------------------------------------- fake servers


class FakeServer:
    """A local HTTP server that records every request and answers with `status` / `body`, or with
    the `routes` entry (path prefix -> (status, body, delay)) the request path starts with."""

    def __init__(self, status: int = 201, body: bytes = b"", delay: float = 0.0,
                 routes: dict[str, tuple[int, bytes, float]] | None = None) -> None:
        self.status, self.body, self.delay = status, body, delay
        self.routes = routes or {}
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": data})
                status, body, delay = next((r for p, r in outer.routes.items() if self.path.startswith(p)),
                                           (outer.status, outer.body, outer.delay))
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                if status in (301, 302):
                    self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:  # the client gave up (timeout tests)
                    pass

            do_GET = do_POST = do_CONNECT = _handle  # noqa: N815

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[FakeServer]:
    s = FakeServer()
    yield s
    s.close()


def dead_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    env = base_env(HOME=str(home), **extra)
    for name in PROXY_NAMES:
        if name not in extra:
            env.pop(name, None)
    env.setdefault("NO_PROXY", "127.0.0.1,localhost")
    return env


# --------------------------------------------------------------------------- a cloud chat


@pytest.fixture
def vm(tmp_path: Path) -> dict[str, Path]:
    """A fake cloud VM: HOME, a repo with CLAUDE.md / AGENTS.md, and the session transcript."""
    home = tmp_path / "root"
    repo = home / "agent-memory"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "CLAUDE.md").write_text(f"# Repo\n## Tests\nRun pytest. token: {FAKE_KEY}\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("# Agents\nBe brief.\n", encoding="utf-8")
    t = home / ".claude" / "projects" / "-root-agent-memory" / f"{SID}.jsonl"
    write_transcript(t, chat_events(str(repo)), pad=False)
    return {"home": home, "repo": repo, "transcript": t}


def chat_events(cwd: str, extra_output: int = 0) -> list[dict[str, Any]]:
    events = [
        {"type": "custom-title", "customTitle": "That's My Handoff", "sessionId": SID},
        user(f"Fix the parser. My key is {FAKE_KEY}", 0, cwd=cwd, entrypoint="remote"),
        reply("m1", usage(10, 100 + extra_output, 1000, 50), text("On it."), tool("Bash", "b1", command="pytest -q"),
              minute=1, cwd=cwd),
        result("b1", "ok", 2, cwd=cwd),
        reply("m2", usage(5, 20, 500, 0), text("Done."), minute=3, cwd=cwd),
    ]
    for e in events:
        e["sessionId"] = SID
    return events


def hook_payload(vm: dict[str, Path], event: str = "Stop", **extra: Any) -> str:
    return json.dumps({"session_id": SID, "transcript_path": str(vm["transcript"]), "cwd": str(vm["repo"]),
                       "hook_event_name": event, "permission_mode": "default", **extra})


def run_hook(vm: dict[str, Path], url: str | None, event: str = "Stop", *, raw: str | None = None,
             **env: str) -> subprocess.CompletedProcess[str]:
    extra = dict(env)
    if url is not None:
        extra["AMEM_SUPABASE_URL"] = url
    return subprocess.run([sys.executable, "-I", str(TOOL), "--hook"], input=raw or hook_payload(vm, event),
                          env=clean_env(vm["home"], **extra), capture_output=True, text=True, timeout=30)


def log_lines(vm: dict[str, Path]) -> list[str]:
    path = vm["home"] / ".claude" / "amem" / "hook.log"
    return path.read_text().splitlines() if path.exists() else []


def assert_silent(res: subprocess.CompletedProcess[str]) -> None:
    assert res.returncode == 0 and res.stdout == "" and res.stderr == "", (res.stdout, res.stderr)


# --------------------------------------------------------------------------- hook mode


def test_hook_posts_a_chat_row(vm: dict[str, Path], server: FakeServer) -> None:
    res = run_hook(vm, server.url)
    assert_silent(res)
    (req,) = server.requests
    assert req["method"] == "POST" and req["path"] == "/rest/v1/cloud_digests?on_conflict=source_key"
    assert req["headers"]["prefer"] == "resolution=merge-duplicates,return=minimal"
    assert req["headers"]["content-type"] == "application/json" and "apikey" not in req["headers"]
    row = json.loads(req["body"])
    assert row["kind"] == "chat" and "routine" not in row and row["ingested_at"] is None
    assert row["source_key"] == f"cloud:{SID}" and row["session_id"] == SID
    meta = row["meta"]
    assert meta["hook"] == "Stop" and meta["script_version"] == amem_digest.SCRIPT_VERSION
    assert meta["tokens"]["input"] == 15 and meta["tokens"]["output"] == 120 and meta["title"] == "That's My Handoff"
    assert meta["repo"] == "agent-memory" and meta["profile"]["model"] == "claude-test-1"
    assert [i["name"] for i in row["instructions"]] == ["claude-md", "agents-md"]
    assert FAKE_KEY not in req["body"].decode() and "[redacted]" in row["instructions"][0]["text"]
    assert row["digest_sha"] == hashlib.sha256(row["digest"].encode()).hexdigest()
    (line,) = log_lines(vm)
    assert " Stop c4a7c0de posted 201 " in line and server.url not in line


def test_debounce_and_session_end(vm: dict[str, Path], server: FakeServer) -> None:
    assert_silent(run_hook(vm, server.url))
    assert_silent(run_hook(vm, server.url))           # < 60 s after the last POST: skipped
    assert len(server.requests) == 1 and "debounced" in log_lines(vm)[-1]
    assert_silent(run_hook(vm, server.url, "SessionEnd"))  # never debounced
    assert len(server.requests) == 2 and json.loads(server.requests[1]["body"])["meta"]["hook"] == "SessionEnd"
    # An old successful POST does not block the next Stop.
    state = vm["home"] / ".claude" / "amem" / "hook-state.json"
    state.write_text(json.dumps({SID: time.time() - 61}))
    assert_silent(run_hook(vm, server.url))
    assert len(server.requests) == 3


def test_failed_post_is_not_debounced(vm: dict[str, Path]) -> None:
    bad = FakeServer(status=500)
    try:
        assert_silent(run_hook(vm, bad.url))
        assert_silent(run_hook(vm, bad.url))
        assert len(bad.requests) == 2 and "failed 500" in log_lines(vm)[-1]
    finally:
        bad.close()


@pytest.mark.parametrize("case", ["no-url", "remote-http", "down", "bad-payload", "other-event", "subagent",
                                  "no-transcript", "redirect"])
def test_hook_never_fails_and_never_prints(vm: dict[str, Path], server: FakeServer, case: str) -> None:
    url: str | None = server.url
    raw = None
    if case == "no-url":
        url = None
    elif case == "remote-http":
        url = "http://example.invalid"
    elif case == "down":
        url = dead_url()
    elif case == "bad-payload":
        raw = "not json {"
    elif case == "other-event":
        raw = hook_payload(vm, "PreToolUse")
    elif case == "subagent":
        raw = hook_payload(vm, agent_id="a1")
    elif case == "no-transcript":
        raw = json.dumps({"session_id": SID, "transcript_path": str(vm["home"] / "nope.jsonl"),
                          "hook_event_name": "Stop"})
    elif case == "redirect":
        server.status = 302
    res = run_hook(vm, url, raw=raw)
    assert_silent(res)
    if case != "redirect":
        assert server.requests == []
    assert log_lines(vm), "every run logs one line"
    if case == "redirect":
        assert "failed 302" in log_lines(vm)[-1]


def test_hook_respects_the_proxy_env(vm: dict[str, Path]) -> None:
    proxy = FakeServer(status=502)
    try:
        res = run_hook(vm, "https://supabase.invalid", HTTPS_PROXY=proxy.url, NO_PROXY="")
        assert_silent(res)
        (req,) = proxy.requests
        assert req["method"] == "CONNECT" and req["path"] == "supabase.invalid:443"
        assert "failed 000" in log_lines(vm)[-1]
    finally:
        proxy.close()


def test_post_row_times_out() -> None:
    slow = FakeServer(delay=2.0)
    try:
        started = time.monotonic()
        assert amem_digest.post_row(slow.url + "/x", {"a": 1}, timeout=0.3) == 0
        assert time.monotonic() - started < 1.5
    finally:
        slow.close()
    assert amem_digest.HOOK_TIMEOUT_SECONDS <= 8 and amem_digest.HTTP_TIMEOUT_SECONDS < amem_digest.HOOK_TIMEOUT_SECONDS


def test_routine_mode_still_requires_a_routine(tmp_path: Path) -> None:
    res = subprocess.run([sys.executable, "-I", str(TOOL)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 2 and "--routine" in res.stderr


def test_package_and_tool_copies_are_identical() -> None:
    assert TOOL.read_bytes() == (ROOT / "src" / "agent_memory" / "amem_digest.py").read_bytes()


# --------------------------------------------------------------------------- database


@pytest.fixture
def db(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    def clean() -> None:
        with psycopg.connect(pg_url) as conn:
            conn.execute("delete from deviations")
            conn.execute("delete from sessions")
            conn.execute("delete from cloud_digests")
            conn.execute("delete from amem_assets")
    clean()
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    yield pg_url
    clean()


def _vals(row: dict[str, Any]) -> tuple[list[str], list[Any]]:
    return list(row), [Jsonb(v) if isinstance(v, (dict, list)) else v for v in row.values()]


def post_merge(url: str, row: dict[str, Any]) -> None:
    """What PostgREST does for `Prefer: resolution=merge-duplicates` + on_conflict=source_key."""
    cols, vals = _vals(row)
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "source_key")
    with psycopg.connect(url) as conn:
        conn.execute(f"insert into cloud_digests ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) "
                     f"on conflict (source_key) do update set {sets}", vals)


def post_ignore(url: str, row: dict[str, Any]) -> int:
    """What PostgREST does for `Prefer: resolution=ignore-duplicates` (the routine's last step)."""
    cols, vals = _vals(row)
    with psycopg.connect(url) as conn:
        return conn.execute(f"insert into cloud_digests ({', '.join(cols)}) "
                            f"values ({', '.join(['%s'] * len(cols))}) on conflict do nothing", vals).rowcount


def hook_row(vm: dict[str, Path], **meta: Any) -> dict[str, Any]:
    row = amem_digest.build_row(vm["transcript"], routine=None,
                                instructions=amem_digest.hook_instructions(str(vm["repo"])))
    row.update(kind="chat", ingested_at=None)
    row["meta"].update(hook="Stop", **meta)
    return row


def routine_row(vm: dict[str, Path]) -> dict[str, Any]:
    prompt = vm["repo"] / "prompt.md"
    prompt.write_text("Run the nightly trials.\n")
    return amem_digest.build_row(vm["transcript"], routine="Nightly Trials", instructions=[("prompt", prompt)])


def _q(url: str, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


def test_hook_after_routine_keeps_the_routine(db: str, vm: dict[str, Path]) -> None:
    assert post_ignore(db, routine_row(vm)) == 1
    later = hook_row(vm)
    later["digest"], later["ended_at"] = "newer digest", "2026-10-14T10:00:00+00:00"
    post_merge(db, later)
    (r,) = _q(db, "select kind, routine, instructions, digest, ingested_at from cloud_digests")
    assert r[:2] == ("routine", "nightly-trials") and [i["name"] for i in r[2]] == ["prompt"]
    assert r[3] == "newer digest" and r[4] is None


@pytest.mark.parametrize("routine_post", [post_ignore, post_merge])
def test_routine_after_hook_claims_the_chat_row(db: str, vm: dict[str, Path], routine_post: Any) -> None:
    chat = hook_row(vm)
    chat["digest"], chat["ended_at"] = "hook digest, newer", "2026-10-14T11:00:00+00:00"
    post_merge(db, chat)
    with psycopg.connect(db) as conn:
        conn.execute("update cloud_digests set ingested_at = now()")
    routine_post(db, routine_row(vm))
    (r,) = _q(db, "select kind, routine, instructions, digest, ended_at is not null, ingested_at from cloud_digests")
    assert r[:2] == ("routine", "nightly-trials") and [i["name"] for i in r[2]] == ["prompt"]
    assert r[3] == "hook digest, newer" and r[5] is None  # the newer digest wins; re-ingested


def test_chat_kind_constraint(db: str, vm: dict[str, Path]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        post_merge(db, {**hook_row(vm), "routine": "x"})
    with pytest.raises(psycopg.errors.CheckViolation):
        post_merge(db, {**hook_row(vm), "kind": "other"})


def test_ingest_refreshes_an_updated_chat_row(db: str, vm: dict[str, Path], tmp_path: Path) -> None:
    empty = tmp_path / "no-projects"
    post_merge(db, hook_row(vm))
    first = sessions_ingest.ingest(empty, days=7, now=NOW, db_url=db, tools=("claude",))
    assert (first.new, first.cloud) == (1, 1) and first.kinds == {"chat": 1}
    (s,) = _q(db, "select kind, tool, repo, project_dir, tokens_output, title, category, project, meta, instructions "
                  "from sessions")
    assert s[:8] == ("chat", "claude", "agent-memory", "cloud", 120, "That's My Handoff", "chat", "agent-memory")
    assert s[8]["cloud"] is True and "routine" not in s[8]
    assert s[9] == ["cloud-file:claude-md", "cloud-file:agents-md"]
    with psycopg.connect(db) as conn:
        conn.execute("update sessions set scanned_at = now()")
    # The chat went on: the hook POSTs again (merge, ingested_at null) with more tokens.
    write_transcript(vm["transcript"], chat_events(str(vm["repo"]), extra_output=1000), pad=False)
    post_merge(db, hook_row(vm))
    again = sessions_ingest.ingest(empty, days=7, now=NOW, db_url=db, tools=("claude",))
    assert (again.new, again.updated, again.cloud) == (0, 1, 1)
    assert _q(db, "select count(*), max(tokens_output), bool_and(scanned_at is not null) from sessions") == [
        (1, 1120, True)]
    assert _q(db, "select ingested_at is not null from cloud_digests") == [(True,)]


def test_ingest_never_turns_a_routine_into_a_chat(db: str, vm: dict[str, Path], tmp_path: Path) -> None:
    empty = tmp_path / "no-projects"
    post_ignore(db, routine_row(vm))
    sessions_ingest.ingest(empty, days=7, now=NOW, db_url=db, tools=("claude",))
    # Even a chat row that bypassed the trigger keeps the stored routine session a routine.
    with psycopg.connect(db) as conn:
        conn.execute("alter table cloud_digests disable trigger cloud_digests_keep_routine")
        try:
            conn.execute("update cloud_digests set kind = 'chat', routine = null, ingested_at = null")
        finally:
            conn.execute("alter table cloud_digests enable trigger cloud_digests_keep_routine")
    sessions_ingest.ingest(empty, days=7, now=NOW, db_url=db, tools=("claude",))
    (s,) = _q(db, "select kind, meta ->> 'routine', instructions, category, project from sessions")
    assert s == ("routine", "nightly-trials", ["routine:nightly-trials"], "routine", "routine:nightly-trials")


def test_scan_reads_a_cloud_chat_like_a_chat(db: str, vm: dict[str, Path], tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_CLAUDE_HOME", str(tmp_path / "mac-claude"))
    events = chat_events(str(vm["repo"]))
    events += [e for i in range(3) for e in (assistant(tool("Read", f"r{i}", file_path="x"), minute=4 + i),
                                              result(f"r{i}", "ok", 4 + i))]
    write_transcript(vm["transcript"], events)  # padded: big enough to be scanned
    post_merge(db, hook_row(vm))
    sessions_ingest.ingest(tmp_path / "none", days=7, now=NOW, db_url=db, tools=("claude",))
    vm["transcript"].unlink()
    (o,) = deviations.scan(dry_run=True, now=NOW).outcomes
    assert o.kind == "chat" and o.status == "dry-run"
    assert o.instructions == ["cloud-file:claude-md", "cloud-file:agents-md"]
    assert 'title="That\'s My Handoff"' in o.line()


# --------------------------------------------------------------------------- assets + publish-script


def test_amem_assets_rls_and_revokes(db: str) -> None:
    assert _q(db, "select relrowsecurity from pg_class where relname = 'amem_assets'") == [(True,)]
    migration = (config.MIGRATIONS_DIR / "20261018020000_amem_assets.sql").read_text()
    with psycopg.connect(db) as conn:
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"create role {role}")
            conn.execute(f"grant select on public.amem_assets to {role}")
        conn.execute(migration)
        granted = conn.execute("select count(*) from information_schema.role_table_grants "
                               "where table_name = 'amem_assets' and grantee in ('anon', 'authenticated')").fetchone()
        assert granted == (0,)
        conn.rollback()


def test_publish_script(db: str, tmp_path: Path) -> None:
    env = base_env(AMEM_DB_URL=db)
    first = run_amem("cloud", "publish-script", env=env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip().endswith("(new)") and f"version {amem_digest.SCRIPT_VERSION} " in first.stdout
    second = run_amem("cloud", "publish-script", env=env)
    assert second.stdout.strip().endswith("(unchanged)")
    body = TOOL.read_text()
    assert _q(db, "select body, sha256, version from amem_assets where name = 'amem_digest.py'") == [
        (body, hashlib.sha256(body.encode()).hexdigest(), amem_digest.SCRIPT_VERSION)]
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n")
    bad = run_amem("cloud", "publish-script", "--file", str(broken), env=env)
    assert bad.returncode == 1 and _q(db, "select count(*) from amem_assets") == [(1,)]
    rest = run_amem("cloud", "publish-script", env=base_env(AMEM_BACKEND="rest", AMEM_SUPABASE_URL="http://127.0.0.1:9"))
    assert rest.returncode == 1 and "Postgres backend" in rest.stderr
    remote = run_amem("cloud", "publish-script", env=base_env(AMEM_DB_URL=db, AMEM_REQUIRE_REMOTE="1"))
    assert remote.returncode == 3


# --------------------------------------------------------------------------- setup.sh

ASSET_PATH = "/rest/v1/amem_assets?name=eq.amem_digest.py&select=body"
POST_PATH = "/rest/v1/cloud_digests?on_conflict=source_key"
ON_LINE = "amem: cloud chat hook on (3/3 settings files; amem_digest.py is fetched at hook time)\n"
HOOK_TOOLS = ("mktemp", "cat", "sed", "head", "find", "date", "rm", "touch", "chmod", "mv")


def layout(root: Path, home: Path) -> dict[str, Path]:
    etc, opt = root / "etc" / "claude-code", root / "opt" / "amem"
    return {"hook": opt / "hook.sh", "script": opt / "amem_digest.py", "marker": opt / "fetch-failed",
            "managed": etc / "managed-settings.json", "dropin": etc / "managed-settings.d" / "50-amem.json",
            "user": home / ".claude" / "settings.json", "setup_log": root / "var" / "log" / "amem-setup.log",
            "hook_log": root / "tmp" / "amem-hook.log"}


def bin_dir(where: Path, tools: tuple[str, ...], python: bool = True) -> Path:
    """A PATH dir holding only `tools` (+ python3 = this interpreter, Python 3.10+)."""
    where.mkdir(parents=True, exist_ok=True)
    for name in tools:
        found = shutil.which(name)
        assert found, name
        (where / name).symlink_to(found)
    if python:
        (where / "python3").symlink_to(os.path.realpath(sys.executable))
    return where


@pytest.fixture
def py_path(tmp_path: Path) -> str:
    return f"{bin_dir(tmp_path / 'pybin', ())}{os.pathsep}{os.environ.get('PATH', '')}"


def run_setup(root: Path, home: Path, url: str | None = None, **env: str) -> subprocess.CompletedProcess[str]:
    extra = {"AMEM_SETUP_ROOT": str(root), **env}
    if url is not None:
        extra["AMEM_SUPABASE_URL"] = url
    return subprocess.run(["/bin/bash", str(SETUP)], env=clean_env(home, **extra), capture_output=True, text=True,
                          timeout=60)


def setup_vm(vm: dict[str, Path], tmp_path: Path) -> dict[str, Path]:
    p = layout(tmp_path / "sysroot", vm["home"])
    res = run_setup(tmp_path / "sysroot", vm["home"])
    assert res.returncode == 0 and res.stdout == ON_LINE and res.stderr == "", res
    return p


def run_installed_hook(p: dict[str, Path], vm: dict[str, Path], url: str | None, event: str = "Stop",
                       path: str | None = None) -> subprocess.CompletedProcess[str]:
    extra = {"PATH": path} if path else {}
    if url is not None:
        extra["AMEM_SUPABASE_URL"] = url
    res = subprocess.run([str(p["hook"])], input=hook_payload(vm, event), env=clean_env(vm["home"], **extra),
                         capture_output=True, text=True, timeout=30)
    assert_silent(res)
    return res


def digest_server(asset: tuple[int, bytes, float] | None = None, body: str | None = None) -> FakeServer:
    if asset is None:
        asset = (200, json.dumps([{"body": body if body is not None else TOOL.read_text()}]).encode(), 0.0)
    return FakeServer(routes={"/rest/v1/amem_assets": asset, "/rest/v1/cloud_digests": (201, b"", 0.0)})


def calls(srv: FakeServer) -> list[tuple[str, str]]:
    return [(r["method"], r["path"]) for r in srv.requests]


def hook_log(p: dict[str, Path]) -> str:
    return p["hook_log"].read_text() if p["hook_log"].exists() else ""


def age(path: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def test_setup_writes_the_hook_and_registers_it(vm: dict[str, Path], tmp_path: Path) -> None:
    root, home = tmp_path / "sysroot", vm["home"]
    p = layout(root, home)
    mine = {"type": "command", "command": "echo mine", "timeout": 5}
    v1 = {"type": "command", "command": 'python3 "/root/.claude/amem/amem_digest.py" --hook', "timeout": 10}
    p["managed"].parent.mkdir(parents=True)
    p["managed"].write_text(json.dumps({"env": {"X": "1"}, "hooks": {"Stop": [{"hooks": [mine]}]}}))
    p["user"].write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [v1]}, {"hooks": [mine]}],
                                                                "SessionEnd": [{"hooks": [mine, v1]}]}}))
    srv = FakeServer(status=200)
    try:
        res = run_setup(root, home, srv.url)
        assert res.returncode == 0 and res.stdout == ON_LINE and res.stderr == "", res
        assert srv.requests == []  # v2 never touches the network at setup time
    finally:
        srv.close()
    hook = p["hook"]
    assert hook.stat().st_mode & 0o777 == 0o755
    assert hook.read_text().startswith("#!/bin/bash\n") and f"AMEM_DIR={root}/opt/amem\n" in hook.read_text()
    entry = {"type": "command", "command": str(hook), "timeout": 10}
    managed = json.loads(p["managed"].read_text())
    assert managed["env"] == {"X": "1"} and p["managed"].stat().st_mode & 0o777 == 0o644
    assert managed["hooks"] == {"Stop": [{"hooks": [mine]}, {"hooks": [entry]}], "SessionEnd": [{"hooks": [entry]}]}
    assert json.loads(p["dropin"].read_text()) == {"hooks": {"Stop": [{"hooks": [entry]}],
                                                             "SessionEnd": [{"hooks": [entry]}]}}
    user = json.loads(p["user"].read_text())  # the v1 entries are gone, mine stays
    assert user["model"] == "opus"
    assert user["hooks"] == {"Stop": [{"hooks": [mine]}, {"hooks": [entry]}],
                             "SessionEnd": [{"hooks": [mine]}, {"hooks": [entry]}]}
    log = p["setup_log"].read_text()
    assert "setup v2 start (AMEM_SUPABASE_URL set)" in log and "hooks merged" in log and ON_LINE.strip() in log
    assert "127.0.0.1" not in log
    files = [hook, p["managed"], p["dropin"], p["user"]]
    before = [f.read_bytes() for f in files]
    again = run_setup(root, home)
    assert again.returncode == 0 and again.stdout == ON_LINE
    assert [f.read_bytes() for f in files] == before  # idempotent
    assert "AMEM_SUPABASE_URL not set" in p["setup_log"].read_text() and "unchanged" in p["setup_log"].read_text()


@pytest.mark.parametrize("case", ["managed-broken", "all-broken", "opt-is-a-file", "log-unwritable", "no-python3"])
def test_setup_always_exits_0(tmp_path: Path, case: str) -> None:
    root, home = tmp_path / "sysroot", tmp_path / "home"
    p = layout(root, home)
    env: dict[str, str] = {}
    broken = {"managed": "{not json", "dropin": "[]", "user": '{"hooks": []}'}
    if case in ("managed-broken", "all-broken"):
        for key, text in broken.items():
            if key == "managed" or case == "all-broken":
                p[key].parent.mkdir(parents=True, exist_ok=True)
                p[key].write_text(text)
    elif case == "opt-is-a-file":
        root.mkdir()
        (root / "opt").write_text("x")
    elif case == "log-unwritable":
        root.mkdir()
        (root / "var").write_text("x")
    else:
        env["PATH"] = str(bin_dir(tmp_path / "bin", ("date", "mkdir", "dirname"), python=False))
    res = run_setup(root, home, **env)
    assert res.returncode == 0 and res.stdout.count("\n") == 1 and res.stderr == "", res
    expected = {"managed-broken": "amem: cloud chat hook on (2/3 settings files;",
                "all-broken": "amem: no settings file changed, cloud chat hook off",
                "opt-is-a-file": f"amem: cannot write {p['hook']}, cloud chat hook off",
                "log-unwritable": ON_LINE.strip(),
                "no-python3": "amem: no python3, cloud chat hook off"}[case]
    assert res.stdout.startswith(expected)
    if case in ("managed-broken", "all-broken"):
        assert p["managed"].read_text() == "{not json"
    if case == "all-broken":
        assert p["dropin"].read_text() == "[]" and p["user"].read_text() == '{"hooks": []}'
    if case in ("opt-is-a-file", "no-python3"):
        assert not p["managed"].exists() and not p["user"].exists()


def test_hook_fetches_compiles_and_posts(vm: dict[str, Path], tmp_path: Path, py_path: str) -> None:
    p = setup_vm(vm, tmp_path)
    assert "AMEM_SUPABASE_URL not set" in p["setup_log"].read_text()  # installed anyway; checked at hook time
    command = json.loads(p["managed"].read_text())["hooks"]["SessionEnd"][0]["hooks"][0]["command"]
    srv = digest_server()
    try:
        # The registered command, as Claude Code runs it.
        res = subprocess.run(["/bin/bash", "-c", command], input=hook_payload(vm, "SessionEnd"),
                             env=clean_env(vm["home"], AMEM_SUPABASE_URL=srv.url + "/", PATH=py_path),
                             capture_output=True, text=True, timeout=30)
        assert_silent(res)
        assert calls(srv) == [("GET", ASSET_PATH), ("POST", POST_PATH)]
        assert p["script"].read_text() == TOOL.read_text() and p["script"].stat().st_mode & 0o777 == 0o644
        row = json.loads(srv.requests[1]["body"])
        assert row["kind"] == "chat" and row["meta"]["hook"] == "SessionEnd"
        run_installed_hook(p, vm, srv.url, "SessionEnd", py_path)  # fresh copy: no second GET
        assert calls(srv) == [("GET", ASSET_PATH), ("POST", POST_PATH), ("POST", POST_PATH)]
    finally:
        srv.close()
    log = hook_log(p).splitlines()
    assert len(log) == 3 and log[0].endswith(" SessionEnd fetch ok") and " SessionEnd digest rc=0 " in log[1]
    assert "127.0.0.1" not in hook_log(p) and "http://" not in hook_log(p)
    assert sorted(f.name for f in p["script"].parent.iterdir()) == ["amem_digest.py", "hook.sh"]
    assert [f.name for f in p["hook_log"].parent.iterdir()] == ["amem-hook.log"]  # temp input removed


@pytest.mark.parametrize("hours,fetched", [(23, False), (25, True)])
def test_hook_refetches_a_stale_script(vm: dict[str, Path], tmp_path: Path, py_path: str, hours: float,
                                       fetched: bool) -> None:
    p = setup_vm(vm, tmp_path)
    old = TOOL.read_text() + "\n# old copy\n"
    p["script"].write_text(old)
    age(p["script"], hours)
    srv = digest_server()
    try:
        run_installed_hook(p, vm, srv.url, "SessionEnd", py_path)
        assert (("GET", ASSET_PATH) in calls(srv)) is fetched and ("POST", POST_PATH) in calls(srv)
    finally:
        srv.close()
    assert p["script"].read_text() == (TOOL.read_text() if fetched else old)


@pytest.mark.parametrize("case", ["500", "not-json", "no-rows", "invalid-script", "redirect"])
def test_broken_fetch_keeps_the_old_script(vm: dict[str, Path], tmp_path: Path, py_path: str, case: str) -> None:
    p = setup_vm(vm, tmp_path)
    old = TOOL.read_text() + "\n# old copy\n"
    p["script"].write_text(old)
    age(p["script"], 25)
    asset = {"500": (500, b'{"message": "no"}', 0.0), "not-json": (200, b"<html>", 0.0), "no-rows": (200, b"[]", 0.0),
             "invalid-script": (200, json.dumps([{"body": "def (:\n"}]).encode(), 0.0),
             "redirect": (302, b"", 0.0)}[case]
    srv = digest_server(asset)
    try:
        run_installed_hook(p, vm, srv.url, "SessionEnd", py_path)
        assert calls(srv) == [("GET", ASSET_PATH), ("POST", POST_PATH)]  # the old copy still posts
        assert p["script"].read_text() == old and p["marker"].exists()
        reason = {"500": "http 500", "redirect": "http 302"}.get(case, "no script or it does not compile")
        assert f" SessionEnd fetch failed: {reason}" in hook_log(p) or f" SessionEnd fetch failed {reason}" in hook_log(p)
        run_installed_hook(p, vm, srv.url, "SessionEnd", py_path)  # < 5 min after a failure: no retry
        assert [m for m, _ in calls(srv)] == ["GET", "POST", "POST"]
        age(p["marker"], 0.1)  # 6 min later it tries again
        run_installed_hook(p, vm, srv.url, "SessionEnd", py_path)
        assert [m for m, _ in calls(srv)] == ["GET", "POST", "POST", "GET", "POST"]
    finally:
        srv.close()
    assert p["script"].read_text() == old and "127.0.0.1" not in hook_log(p)


def test_hook_without_url_or_network(vm: dict[str, Path], tmp_path: Path, py_path: str) -> None:
    p = setup_vm(vm, tmp_path)
    run_installed_hook(p, vm, None, "Stop", py_path)
    assert hook_log(p).rstrip().endswith(" Stop no AMEM_SUPABASE_URL")
    run_installed_hook(p, vm, dead_url(), "SessionEnd", py_path)
    lines = hook_log(p).splitlines()
    assert lines[-2].endswith(" SessionEnd fetch failed http 000") and lines[-1].endswith(" no amem_digest.py")
    assert not p["script"].exists() and p["marker"].exists()


def test_hook_fetch_is_bounded(vm: dict[str, Path], tmp_path: Path, py_path: str) -> None:
    p = setup_vm(vm, tmp_path)
    srv = digest_server((200, b"[]", 6.0))
    try:
        started = time.monotonic()
        run_installed_hook(p, vm, srv.url, "Stop", py_path)
        assert time.monotonic() - started < 5.5  # curl --max-time 3
    finally:
        srv.close()
    assert "Stop fetch failed http 000" in hook_log(p) and not p["script"].exists()


@pytest.mark.parametrize("case", ["ok", "redirect"])
def test_hook_fetches_without_curl(vm: dict[str, Path], tmp_path: Path, case: str) -> None:
    p = setup_vm(vm, tmp_path)
    path = str(bin_dir(tmp_path / "nocurl", HOOK_TOOLS))
    srv = digest_server((302, b"", 0.0) if case == "redirect" else None)
    try:
        run_installed_hook(p, vm, srv.url, "SessionEnd", path)
        assert calls(srv)[0] == ("GET", ASSET_PATH)
    finally:
        srv.close()
    if case == "ok":
        assert p["script"].read_text() == TOOL.read_text() and calls(srv)[1:] == [("POST", POST_PATH)]
    else:
        assert not p["script"].exists() and "fetch failed http 302" in hook_log(p) and len(srv.requests) == 1


def test_setup_and_hook_are_valid_bash(tmp_path: Path) -> None:
    root = tmp_path / "sysroot"
    assert run_setup(root, tmp_path / "home").stdout == ON_LINE
    hook = layout(root, tmp_path)["hook"]
    for script in (SETUP, hook):
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True, timeout=30).returncode == 0
        assert os.access(script, os.X_OK)
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    res = subprocess.run([shellcheck, str(SETUP), str(hook)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout
