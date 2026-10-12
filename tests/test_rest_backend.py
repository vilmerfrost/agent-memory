"""REST (PostgREST) backend against a local HTTP stub. No network, no hosted Supabase.

The stub is a tiny in-memory `runs` table that speaks the PostgREST subset the
backend uses and records every request (method, path, query, headers, body).
"""

from __future__ import annotations

import contextlib
import json
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_memory import backend_rest, cli, config, routines_log
from conftest import base_env, run_amem

FIXTURE = Path(__file__).parent / "fixtures" / "routines.sample.log"
KEY = "sb_secret_dummy-never-print-0123456789"
INGEST_PREFER = "resolution=ignore-duplicates,return=minimal"


class Stub:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.fail_with: int | None = None
        self.redirect: tuple[int, str, set[str]] | None = None  # (code, target base URL, methods)
        self.lock = threading.Lock()

    def handle(self, method: str, raw_path: str, headers: dict[str, str], body: Any) -> tuple[int, Any]:
        parts = urlsplit(raw_path)
        query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        with self.lock:
            self.requests.append(
                {"method": method, "path": parts.path, "query": query, "headers": headers, "body": body}
            )
            if self.redirect and method in self.redirect[2]:
                return self.redirect[0], None
            if self.fail_with:
                return self.fail_with, {"code": "XX000", "message": "stub failure"}
            if parts.path != "/rest/v1/runs":
                return 404, {"message": "no such table"}
            return getattr(self, f"_{method.lower()}")(query, headers, body)

    def _get(self, q: dict[str, str], _h: dict[str, str], _b: Any) -> tuple[int, Any]:
        rows = self.rows
        if "source_key" in q:
            assert q["source_key"].startswith("in.(") and q["source_key"].endswith(")")
            keys = set(q["source_key"][4:-1].split(","))
            rows = [r for r in rows if r.get("source_key") in keys]
        if "routine" in q:
            rows = [r for r in rows if r["routine"] == q["routine"].removeprefix("eq.")]
        if q.get("order", "").startswith("started_at.desc"):
            rows = sorted(rows, key=lambda r: r["started_at"] or "", reverse=True)
        if "limit" in q:
            rows = rows[: int(q["limit"])]
        cols = q.get("select", "*")
        if cols != "*":
            rows = [{c: r.get(c) for c in cols.split(",")} for r in rows]
        return 200, rows

    def _post(self, q: dict[str, str], h: dict[str, str], body: Any) -> tuple[int, Any]:
        assert isinstance(body, list)
        assert len({tuple(sorted(r)) for r in body}) == 1, "bulk rows must share keys"
        inserted = []
        for r in body:
            if q.get("on_conflict") == "source_key" and any(
                x.get("source_key") == r["source_key"] for x in self.rows
            ):
                assert "resolution=ignore-duplicates" in h.get("prefer", "")
                continue
            row = {"id": str(uuid.uuid4()), "created_at": "2026-10-12T00:00:00+00:00", "source_key": None, **r}
            self.rows.append(row)
            inserted.append(row)
        if "return=representation" in h.get("prefer", ""):
            cols = q.get("select", "*").split(",")
            return 201, [{c: r[c] for c in cols} for r in inserted]
        return 201, None

    def _patch(self, q: dict[str, str], _h: dict[str, str], body: Any) -> tuple[int, Any]:
        key = q["source_key"].removeprefix("eq.")
        for r in self.rows:
            if r.get("source_key") == key:
                r.update(body)
        return 204, None


@contextlib.contextmanager
def _serve_stub() -> Iterator[tuple[Stub, str]]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = state.handle(self.command, self.path, headers, body)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            if state.redirect and 300 <= status < 400:
                self.send_header("Location", state.redirect[1] + self.path)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = _serve

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def stub() -> Iterator[tuple[Stub, str]]:
    with _serve_stub() as served:
        yield served


@pytest.fixture
def other_stub() -> Iterator[tuple[Stub, str]]:
    """A second server on another port: the target of a redirect."""
    with _serve_stub() as served:
        yield served


@pytest.fixture(autouse=True)
def _rest_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest has already removed every AMEM_* the shell exported; each test
    # points AMEM_SUPABASE_URL at its own local stub explicitly.
    monkeypatch.setenv("no_proxy", "*")  # never route the local stub through a proxy


def _use(monkeypatch: pytest.MonkeyPatch, url: str, key: str | None = None) -> None:
    monkeypatch.setenv("AMEM_SUPABASE_URL", url)
    if key is not None:
        monkeypatch.setenv("AMEM_SUPABASE_KEY", key)


def _rest_cli_env(url: str, **extra: str) -> dict[str, str]:
    # base_env carries no AMEM_* from the shell: only the stub URL and `extra`.
    return base_env(AMEM_SUPABASE_URL=url, no_proxy="*", **extra)


# ------------------------------------------------------------ backend selection


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, "postgres"),
        ({"AMEM_SUPABASE_URL": "https://project.example.com"}, "rest"),
        ({"AMEM_SUPABASE_URL": "https://project.example.com", "AMEM_DB_URL": "postgresql://h/db"}, "postgres"),
        ({"AMEM_BACKEND": "rest"}, "rest"),
        ({"AMEM_BACKEND": "postgres", "AMEM_SUPABASE_URL": "https://project.example.com"}, "postgres"),
        ({"AMEM_BACKEND": "REST", "AMEM_DB_URL": "postgresql://h/db"}, "rest"),
    ],
)
def test_backend_selection(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert config.backend() == expected


def test_invalid_backend(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AMEM_BACKEND", "mysql")
    with pytest.raises(config.BackendError, match="AMEM_BACKEND"):
        config.backend()
    assert cli.main(["log-run", "--routine", "x", "--status", "pass"]) == 1
    assert "AMEM_BACKEND" in capsys.readouterr().err


# ------------------------------------------------------------ writes


def test_log_run_posts_one_row_without_key(stub: tuple[Stub, str], monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    state, url = stub
    _use(monkeypatch, url)
    assert cli.main(["log-run", "--routine", "r1", "--status", "fail", "--summary", "s",
                     "--started-at", "2026-10-12T09:00:00+02:00"]) == 0
    (req,) = state.requests
    assert req["method"] == "POST"
    assert req["path"] == "/rest/v1/runs"
    assert req["query"] == {"select": "id"}
    assert req["headers"]["prefer"] == "return=representation"
    assert "apikey" not in req["headers"]
    assert "authorization" not in req["headers"]
    (row,) = req["body"]
    assert row["routine"] == "r1" and row["status"] == "fail" and row["source"] == "manual"
    assert row["started_at"] == "2026-10-12T09:00:00+02:00"
    assert row["meta"] == {}
    assert capsys.readouterr().out.strip() == state.rows[0]["id"]


def test_apikey_sent_only_when_set_and_never_printed(stub: tuple[Stub, str]) -> None:
    state, url = stub
    result = run_amem("log-run", "--routine", "r1", "--status", "pass",
                      env=_rest_cli_env(url, AMEM_SUPABASE_KEY=KEY))
    assert result.returncode == 0, result.stderr
    (req,) = state.requests
    assert req["headers"]["apikey"] == KEY
    assert "authorization" not in req["headers"]
    assert KEY not in result.stdout + result.stderr

    state.fail_with = 500
    result = run_amem("log-run", "--routine", "r1", "--status", "pass",
                      env=_rest_cli_env(url, AMEM_SUPABASE_KEY=KEY))
    assert result.returncode == 1
    assert "HTTP 500" in result.stderr and "stub failure" in result.stderr
    assert KEY not in result.stdout + result.stderr


def test_ingest_batches_counts_and_is_idempotent(stub: tuple[Stub, str], monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    state, url = stub
    _use(monkeypatch, url)
    monkeypatch.setattr(backend_rest, "KEY_BATCH", 3)
    monkeypatch.setattr(backend_rest, "INSERT_BATCH", 2)
    parsed = routines_log.parse_file(FIXTURE)
    total = len(parsed.runs)
    assert total >= 3

    assert cli.main(["ingest", "routines-log", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert f"ingested {total} new, 0 already present, {len(parsed.unparsed)} unparsed" in out

    gets = [r for r in state.requests if r["method"] == "GET"]
    posts = [r for r in state.requests if r["method"] == "POST"]
    assert len(gets) == -(-total // 3)
    for g in gets:
        assert g["query"]["select"] == "source_key,meta"
        assert len(g["query"]["source_key"][4:-1].split(",")) <= 3
    assert len(posts) == -(-total // 2)
    for p in posts:
        assert p["path"] == "/rest/v1/runs"
        assert p["query"] == {"on_conflict": "source_key"}
        assert p["headers"]["prefer"] == INGEST_PREFER
        assert "apikey" not in p["headers"]
        assert 1 <= len(p["body"]) <= 2
    sent = [row for p in posts for row in p["body"]]
    assert [r["source_key"] for r in sent] == [r.source_key for r in parsed.runs]
    assert set(sent[0]) == {"routine", "source", "started_at", "status", "summary",
                            "report_path", "meta", "source_key"}
    assert len(state.rows) == total

    state.requests.clear()
    assert cli.main(["ingest", "routines-log", str(FIXTURE)]) == 0
    assert f"ingested 0 new, {total} already present" in capsys.readouterr().out
    assert {r["method"] for r in state.requests} == {"GET"}  # nothing to post or patch
    assert len(state.rows) == total


def test_ingest_refreshes_only_failures(stub: tuple[Stub, str], monkeypatch: pytest.MonkeyPatch) -> None:
    state, url = stub
    _use(monkeypatch, url)
    run = routines_log.parse_file(FIXTURE).runs[0]
    state.rows.append({"id": "x", "routine": run.routine, "started_at": "", "source_key": run.source_key,
                       "meta": {"failures": ["old"], "score": 7}})
    run.meta["failures"] = ["a", "b"]
    assert backend_rest.ingest_runs([run]) == (0, 1)
    (patch,) = [r for r in state.requests if r["method"] == "PATCH"]
    assert patch["query"] == {"source_key": f"eq.{run.source_key}"}
    assert patch["body"] == {"meta": {"failures": ["a", "b"], "score": 7}}
    assert state.rows[0]["meta"] == {"failures": ["a", "b"], "score": 7}


def test_ingest_counts_new_when_server_ignores_a_raced_duplicate(stub: tuple[Stub, str],
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    # A row inserted by someone else between the pre-check GET and the POST is ignored by
    # the server (ignore-duplicates) and never duplicated.
    state, url = stub
    _use(monkeypatch, url)
    runs = routines_log.parse_file(FIXTURE).runs[:2]
    real = backend_rest.existing_meta

    def racing(keys: list[str]) -> dict[str, Any]:
        found = real(keys)
        state.rows.append({"id": "raced", "routine": runs[0].routine, "started_at": "",
                           "source_key": runs[0].source_key, "meta": {}})
        return found

    monkeypatch.setattr(backend_rest, "existing_meta", racing)
    backend_rest.ingest_runs(runs)
    assert [r["source_key"] for r in state.rows].count(runs[0].source_key) == 1


# ------------------------------------------------------------ reads


def test_runs_reads_via_get(stub: tuple[Stub, str], monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
    state, url = stub
    _use(monkeypatch, url, KEY)
    for ts in ("2026-10-11T00:00:00+00:00", "2026-10-12T00:00:00+00:00"):
        state.rows.append({"id": ts, "routine": "r1", "started_at": ts, "status": "pass"})
    state.rows.append({"id": "other", "routine": "r2", "started_at": "2026-10-13T00:00:00+00:00"})
    assert cli.main(["runs", "--routine", "r1", "--limit", "5"]) == 0
    (req,) = state.requests
    assert req["method"] == "GET"
    assert req["query"] == {"select": "*", "routine": "eq.r1",
                            "order": "started_at.desc.nullslast,created_at.desc", "limit": "5"}
    assert req["headers"]["apikey"] == KEY
    ids = [json.loads(line)["id"] for line in capsys.readouterr().out.splitlines()]
    assert ids == ["2026-10-12T00:00:00+00:00", "2026-10-11T00:00:00+00:00"]


def test_doctor_rest_reachable_and_unreachable(stub: tuple[Stub, str]) -> None:
    state, url = stub
    result = run_amem("doctor", env=_rest_cli_env(url, AMEM_SUPABASE_KEY=KEY))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "jev_mode=fixture", "backend=rest", "db_host=127.0.0.1", "db=reachable", "require_remote=off",
    ]
    (req,) = state.requests
    assert req["method"] == "GET" and req["query"] == {"select": "id", "limit": "1"}
    assert KEY not in result.stdout + result.stderr

    state.fail_with = 401
    result = run_amem("doctor", env=_rest_cli_env(url))
    assert "db=unreachable" in result.stdout.splitlines()


# ------------------------------------------------------------ AMEM_REQUIRE_REMOTE on REST


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:54321", "http://localhost:54321", "http://[::1]:54321",
     "http://api.localhost", "https://127.0.0.9", "not-a-url", ""],
)
def test_require_remote_refuses_local_or_empty_rest_url(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")
    monkeypatch.setenv("AMEM_BACKEND", "rest")
    monkeypatch.setenv("AMEM_SUPABASE_URL", url)
    error = config.remote_write_error()
    assert error is not None and "refusing to write" in error and "backend=rest" in error


def test_require_remote_allows_remote_rest_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")
    monkeypatch.setenv("AMEM_SUPABASE_URL", "https://project.example.com")
    assert config.backend() == "rest"
    assert config.remote_write_error() is None
    # db migrate is always Postgres: it still needs a remote AMEM_DB_URL.
    assert config.remote_write_error("postgres") is not None


@pytest.mark.parametrize(
    "args",
    [("log-run", "--routine", "x", "--status", "pass"), ("ingest", "routines-log", str(FIXTURE)),
     ("ingest", "git-ref"), ("db", "migrate")],
)
def test_cli_refuses_local_stub_and_sends_nothing(stub: tuple[Stub, str], args: tuple[str, ...]) -> None:
    state, url = stub
    result = run_amem(*args, env=_rest_cli_env(url, AMEM_REQUIRE_REMOTE="1", AMEM_SUPABASE_KEY=KEY))
    assert result.returncode == cli.REMOTE_REQUIRED_EXIT, result.stderr
    assert "AMEM_REQUIRE_REMOTE=1" in result.stderr and "refusing to write" in result.stderr
    assert state.requests == []
    assert KEY not in result.stdout + result.stderr


# ------------------------------------------------------------ redirects are never followed


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_is_an_error_and_key_never_reaches_target(
    stub: tuple[Stub, str], other_stub: tuple[Stub, str], code: int
) -> None:
    state, url = stub
    target, target_url = other_stub
    state.redirect = (code, target_url, {"GET", "POST", "PATCH"})
    result = run_amem("log-run", "--routine", "r1", "--status", "pass",
                      env=_rest_cli_env(url, AMEM_SUPABASE_KEY=KEY))
    assert result.returncode == 1
    assert f"HTTP {code} redirect refused" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""  # no run id printed
    assert len(state.requests) == 1
    assert target.requests == []  # nothing (and no apikey) reached the second server
    assert KEY not in result.stdout + result.stderr


@pytest.mark.parametrize("methods", [{"GET"}, {"POST"}, {"GET", "POST", "PATCH"}])
def test_ingest_redirect_reports_no_new_rows(
    stub: tuple[Stub, str], other_stub: tuple[Stub, str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], methods: set[str]
) -> None:
    state, url = stub
    target, target_url = other_stub
    _use(monkeypatch, url, KEY)
    state.redirect = (302, target_url, methods)
    assert cli.main(["ingest", "routines-log", str(FIXTURE)]) == 1
    out = capsys.readouterr()
    assert "ingested" not in out.out
    assert "HTTP 302 redirect refused" in out.err
    assert target.requests == []
    assert state.rows == [] and target.rows == []
    assert KEY not in out.out + out.err


def test_doctor_redirect_is_unreachable(stub: tuple[Stub, str], other_stub: tuple[Stub, str]) -> None:
    state, url = stub
    target, target_url = other_stub
    state.redirect = (302, target_url, {"GET"})
    result = run_amem("doctor", env=_rest_cli_env(url, AMEM_SUPABASE_KEY=KEY))
    assert result.returncode == 0, result.stderr
    assert "db=unreachable" in result.stdout.splitlines()
    assert target.requests == []


# ------------------------------------------------------------ AMEM_SUPABASE_URL validation


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def tripwire(*_a: object, **_k: object) -> None:
        raise AssertionError("REST request attempted for a URL that must be refused")

    monkeypatch.setattr(backend_rest._OPENER, "open", tripwire)


@pytest.mark.parametrize("url", ["project.supabase.co", "project.supabase.co/rest", "ftp://project.example.com",
                                 "https://", "http://[bad"])
@pytest.mark.parametrize(
    "args",
    [("log-run", "--routine", "x", "--status", "pass"), ("runs", "--routine", "x"),
     ("ingest", "routines-log", str(FIXTURE))],
)
def test_unusable_url_is_a_clean_error(url: str, args: tuple[str, ...]) -> None:
    result = run_amem(*args, env=_rest_cli_env(url, AMEM_BACKEND="rest"))
    assert result.returncode == 1, result.stderr
    assert result.stderr.startswith("amem: ")
    assert "AMEM_SUPABASE_URL" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("url", ["project.supabase.co", "https://", "http://[bad"])
def test_doctor_unusable_url_is_unreachable(url: str) -> None:
    # Without AMEM_BACKEND: a set AMEM_SUPABASE_URL alone selects rest.
    result = run_amem("doctor", env=_rest_cli_env(url))
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    lines = result.stdout.splitlines()
    assert "backend=rest" in lines
    assert "db=unreachable" in lines
    assert any(line in ("db_host=invalid", "db_host=none") for line in lines)


@pytest.mark.parametrize("key", [KEY, None])
def test_remote_plain_http_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], key: str | None
) -> None:
    _no_network(monkeypatch)
    _use(monkeypatch, "http://project.example.com", key)
    assert cli.main(["log-run", "--routine", "x", "--status", "pass"]) == 1
    assert cli.main(["runs", "--routine", "x"]) == 1
    assert cli.main(["ingest", "routines-log", str(FIXTURE)]) == 1
    err = capsys.readouterr().err
    assert err.count("uses http:// for remote host project.example.com; use https://") == 3
    assert KEY not in err
    assert backend_rest.reachable() is False


def test_local_plain_http_with_key_is_allowed(stub: tuple[Stub, str], monkeypatch: pytest.MonkeyPatch) -> None:
    # The test stub is http://127.0.0.1:<port>: a local host may use plain http.
    state, url = stub
    _use(monkeypatch, url, KEY)
    assert config.supabase_url_error() is None
    assert backend_rest.reachable() is True
    assert state.requests[0]["headers"]["apikey"] == KEY


def test_https_remote_url_passes_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, "https://project.example.com", KEY)
    assert config.supabase_url_error() is None
