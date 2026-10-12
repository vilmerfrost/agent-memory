"""Shared fixtures.

- The test process never inherits a store, a key or a live flag from the calling
  shell. Every `AMEM_*`, `PG*`, `TYPESAFE_*` and `SUPABASE_*` variable is removed
  from `os.environ` when this conftest loads (before collection and before any
  session fixture), again before every test, and from every subprocess env built
  by `base_env`. So `AMEM_SUPABASE_URL` / `AMEM_BACKEND=rest` / `AMEM_DB_URL`
  exported by a cloud session can never route a test to a hosted DB. A test that
  wants the REST backend sets its own stub URL explicitly.
- Every test runs with env files disabled and Jev forced to fixture mode.
- The SDK client is replaced by a tripwire, so no test can make a live Jev call.
- `pg_url` starts one throwaway Postgres container per session on an OS-assigned
  port (never a fixed port: other worktrees run in parallel) and removes it after.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import psycopg
import pytest

PG_IMAGE = "pgvector/pgvector:pg17"
AMEM = str(Path(sys.executable).parent / "amem")

# Env names a test must never inherit: they pick or authenticate a store
# (AMEM_DB_URL, AMEM_BACKEND, AMEM_SUPABASE_URL/KEY, PG* for libpq), carry a key
# (TYPESAFE_API_KEY, SUPABASE_*), or change behaviour (AMEM_JEV_LIVE,
# AMEM_REQUIRE_REMOTE, AMEM_QUESTIONS_DIR, ...). Tests set what they need.
ISOLATED_PREFIXES = ("AMEM_", "PG", "TYPESAFE_", "SUPABASE_")


def is_isolated_name(name: str) -> bool:
    return name.upper().startswith(ISOLATED_PREFIXES)


def isolated_environ(source: Mapping[str, str]) -> dict[str, str]:
    """A copy of `source` without any isolated name, with env files disabled."""
    env = {k: v for k, v in source.items() if not is_isolated_name(k)}
    env["AMEM_NO_DOTENV"] = "1"
    return env


def _scrub_process_env() -> None:
    for name in [k for k in os.environ if is_isolated_name(k)]:
        del os.environ[name]
    os.environ["AMEM_NO_DOTENV"] = "1"


# At import: runs before collection, before session fixtures (pg_url) and before
# any agent_memory code reads the environment.
_scrub_process_env()


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Again per test, in case anything outside monkeypatch wrote one of the names.
    for name in [k for k in os.environ if is_isolated_name(k)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AMEM_NO_DOTENV", "1")

    import typesafe_sdk

    class _NoLiveCalls:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("live Jev call attempted in tests")

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _NoLiveCalls)


def base_env(**extra: str) -> dict[str, str]:
    """Env for CLI subprocesses: the current env minus every isolated name, plus `extra`.

    No env files, no Jev key, no live flag, no remote requirement, no store
    selection. Only what the test passes in `extra` reaches the subprocess.
    """
    env = isolated_environ(os.environ)
    env.update(extra)
    return env


def run_amem(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([AMEM, *args], env=env, capture_output=True, text=True, timeout=60)


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=300).stdout.strip()


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    container = _docker(
        "run", "-d", "--rm", "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD=postgres", PG_IMAGE
    )
    try:
        mapping = _docker("port", container, "5432/tcp").splitlines()[0]  # e.g. 127.0.0.1:55012
        port = mapping.rsplit(":", 1)[1]
        url = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(url, connect_timeout=2) as conn:
                    conn.execute("select 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        result = run_amem("db", "migrate", env=base_env(AMEM_DB_URL=url))
        assert result.returncode == 0, result.stderr
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)


@pytest.fixture
def db_env(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    return base_env(AMEM_DB_URL=pg_url)
