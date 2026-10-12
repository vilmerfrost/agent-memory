"""Shared fixtures.

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
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

PG_IMAGE = "pgvector/pgvector:pg17"
AMEM = str(Path(sys.executable).parent / "amem")


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_NO_DOTENV", "1")
    monkeypatch.delenv("AMEM_JEV_LIVE", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    import typesafe_sdk

    class _NoLiveCalls:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("live Jev call attempted in tests")

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", _NoLiveCalls)


def base_env(**extra: str) -> dict[str, str]:
    """Env for CLI subprocesses: no env files, no Jev key, no live flag."""
    env = {k: v for k, v in os.environ.items() if k not in ("TYPESAFE_API_KEY", "AMEM_JEV_LIVE", "AMEM_DB_URL")}
    env["AMEM_NO_DOTENV"] = "1"
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
