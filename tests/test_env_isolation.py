"""The suite never inherits a store from the calling shell (audit B2).

A cloud session exports AMEM_SUPABASE_URL, AMEM_BACKEND=rest and (sometimes)
AMEM_SUPABASE_KEY. Without isolation, any test that drops AMEM_DB_URL would pick
the REST backend and write fixture rows into the hosted DB. conftest removes
every AMEM_* / PG* / TYPESAFE_* / SUPABASE_* name from the test process and from
every subprocess env; these tests prove it, including from a fresh pytest run
whose outer env carries cloud-style variables aimed at a recording server.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_memory import config
from conftest import base_env, is_isolated_name, isolated_environ

TESTS_DIR = Path(__file__).resolve().parent
CLOUD_NAMES = ("AMEM_SUPABASE_URL", "AMEM_BACKEND", "AMEM_SUPABASE_KEY")

INNER_TEST = '''
import os

import conftest
from agent_memory import cli, config, store

CLOUD_NAMES = {names!r}


def test_inner_env_is_local(capsys):
    assert [n for n in CLOUD_NAMES if n in os.environ] == []
    assert config.backend() == "postgres"
    assert store._rest() is False
    env = conftest.base_env()
    assert [n for n in CLOUD_NAMES if n in env] == []
    assert cli.main(["doctor"]) == 0
    assert "backend=postgres" in capsys.readouterr().out.splitlines()
    result = conftest.run_amem("doctor", env=env)
    assert result.returncode == 0, result.stderr
    assert "backend=postgres" in result.stdout.splitlines()
'''


@pytest.fixture
def recorder() -> Iterator[tuple[list[str], str]]:
    """Stands in for the hosted Supabase URL: records any request that reaches it."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _any(self) -> None:
            seen.append(f"{self.command} {self.path}")
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = _any

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield seen, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_isolated_environ_drops_every_store_selector() -> None:
    outer = {
        "AMEM_SUPABASE_URL": "https://audit-recheck.invalid",
        "AMEM_BACKEND": "rest",
        "AMEM_SUPABASE_KEY": "recheck-not-a-key",
        "AMEM_DB_URL": "postgresql://u:pw@db.example.com/postgres",
        "AMEM_JEV_LIVE": "1",
        "AMEM_REQUIRE_REMOTE": "1",
        "AMEM_NO_DOTENV": "0",
        "PGHOST": "db.example.com",
        "PGPASSWORD": "pw",
        "TYPESAFE_API_KEY": "k",
        "SUPABASE_ACCESS_TOKEN": "t",
        "HOME": "/home/x",
        "PATH": "/usr/bin",
    }
    env = isolated_environ(outer)
    assert env == {"HOME": "/home/x", "PATH": "/usr/bin", "AMEM_NO_DOTENV": "1"}


def test_this_process_holds_no_isolated_names() -> None:
    # Meaningful when the suite itself runs under exported cloud variables
    # (the audit recheck): conftest removed them before collection.
    assert [k for k in os.environ if is_isolated_name(k) and k != "AMEM_NO_DOTENV"] == []
    assert os.environ["AMEM_NO_DOTENV"] == "1"
    assert config.backend() == "postgres"
    assert [n for n in CLOUD_NAMES if n in base_env()] == []


def test_base_env_passes_only_explicit_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_SUPABASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AMEM_BACKEND", "rest")
    env = base_env()
    assert [n for n in CLOUD_NAMES if n in env] == []
    assert base_env(AMEM_BACKEND="rest")["AMEM_BACKEND"] == "rest"


def test_outer_cloud_vars_never_reach_tests(tmp_path: Path, recorder: tuple[list[str], str]) -> None:
    """A fresh pytest process, launched the way a cloud session would launch it."""
    seen, url = recorder
    inner = tmp_path / "test_inner_isolation.py"
    inner.write_text(INNER_TEST.format(names=CLOUD_NAMES))
    outer_env = {k: v for k, v in os.environ.items() if not is_isolated_name(k)}
    outer_env.update(
        AMEM_SUPABASE_URL=url,  # the "hosted" store: a recorder that must see nothing
        AMEM_BACKEND="rest",
        AMEM_SUPABASE_KEY="recheck-not-a-key",
        AMEM_NO_DOTENV="1",  # if isolation ever broke, never load a real .env here
        PYTHONPATH=os.pathsep.join(filter(None, [str(TESTS_DIR), os.environ.get("PYTHONPATH")])),
        no_proxy="*",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "conftest",
         "--rootdir", str(tmp_path), str(inner)],
        cwd=tmp_path, env=outer_env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert seen == []  # zero requests reached the configured Supabase URL
