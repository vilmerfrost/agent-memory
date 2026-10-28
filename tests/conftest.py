"""Shared fixtures.

- The test process never inherits a store, a key or a live flag from the calling
  shell. Every `AMEM_*`, `PG*`, `TYPESAFE_*` and `SUPABASE_*` variable is removed
  from `os.environ` when this conftest loads (before collection and before any
  session fixture), again before every test, and from every subprocess env built
  by `base_env`. So `AMEM_SUPABASE_URL` / `AMEM_BACKEND=rest` / `AMEM_DB_URL`
  exported by a cloud session can never route a test to a hosted DB. A test that
  wants the REST backend sets its own stub URL explicitly.
- Every test runs with env files disabled and Jev forced to fixture mode.
- Every test and every subprocess env built by `base_env` reads the admin Keychain item through a tripwire,
  never `/usr/bin/security` (`AMEM_ADMIN_SECURITY`). Once `amem admin split-credentials` has run, the real
  item holds the production connection string; a test must never open it. A test that needs Keychain
  behaviour sets its own fake (test_least_privilege.py, `keychain`).
- The SDK client is replaced by a tripwire, so no test can make a live Jev call.
- `pg_url` starts one throwaway Postgres container per session on an OS-assigned
  port (never a fixed port: other worktrees run in parallel) and removes it after.
  Its data dir is tmpfs (no Docker volume), and each session first removes the
  containers of earlier runs that were killed before their teardown.
- `TEST_POSTGRES_URL` replaces that container with one the caller started, for runs
  that cannot reach Docker (`recon-test run --postgres TEST_POSTGRES_URL` starts it
  outside the Seatbelt sandbox). Only a loopback host and an empty database are
  accepted, so it can never point the suite at a real store. The name has no
  `AMEM_`/`PG` prefix on purpose: those are removed below.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest

PG_IMAGE = "pgvector/pgvector:pg17"
EXTERNAL_PG_ENV = "TEST_POSTGRES_URL"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
AMEM = str(Path(sys.executable).parent / "amem")

# Env names a test must never inherit: they pick or authenticate a store
# (AMEM_DB_URL, AMEM_BACKEND, AMEM_SUPABASE_URL/KEY, PG* for libpq), carry a key
# (TYPESAFE_API_KEY, SUPABASE_*), or change behaviour (AMEM_JEV_LIVE,
# AMEM_REQUIRE_REMOTE, AMEM_QUESTIONS_DIR, ...). Tests set what they need.
ISOLATED_PREFIXES = ("AMEM_", "PG", "TYPESAFE_", "SUPABASE_")


SECURITY_ENV = "AMEM_ADMIN_SECURITY"           # agent_memory.credentials.SECURITY_ENV
_TRIPWIRE_DIR = Path(tempfile.mkdtemp(prefix="amem-test-keychain-"))
KEYCHAIN_TRIPWIRE = str(_TRIPWIRE_DIR / "security")
Path(KEYCHAIN_TRIPWIRE).write_text('#!/bin/sh\necho "tests never read the real Keychain" >&2\nexit 44\n')
os.chmod(KEYCHAIN_TRIPWIRE, 0o755)
atexit.register(shutil.rmtree, _TRIPWIRE_DIR, True)


def is_isolated_name(name: str) -> bool:
    return name.upper().startswith(ISOLATED_PREFIXES)


def _safe_security(value: str | None) -> str:
    """A test's own fake stays; nothing else (unset, or the real tool) ever reaches the real Keychain."""
    v = (value or "").strip()
    return KEYCHAIN_TRIPWIRE if not v or os.path.realpath(v) == "/usr/bin/security" else v


def isolated_environ(source: Mapping[str, str]) -> dict[str, str]:
    """A copy of `source` without any isolated name, with env files disabled and the Keychain tripwire."""
    env = {k: v for k, v in source.items() if not is_isolated_name(k)}
    env["AMEM_NO_DOTENV"] = "1"
    env[SECURITY_ENV] = _safe_security(source.get(SECURITY_ENV) if source is os.environ else None)
    return env


def _scrub_process_env() -> None:
    for name in [k for k in os.environ if is_isolated_name(k)]:
        del os.environ[name]
    os.environ["AMEM_NO_DOTENV"] = "1"
    os.environ[SECURITY_ENV] = KEYCHAIN_TRIPWIRE


# At import: runs before collection, before session fixtures (pg_url) and before
# any agent_memory code reads the environment.
_scrub_process_env()


@pytest.fixture(autouse=True)
def _offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Again per test, in case anything outside monkeypatch wrote one of the names.
    for name in [k for k in os.environ if is_isolated_name(k)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AMEM_NO_DOTENV", "1")
    monkeypatch.setenv(SECURITY_ENV, KEYCHAIN_TRIPWIRE)

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
    env[SECURITY_ENV] = _safe_security(env.get(SECURITY_ENV))
    return env


def run_amem(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([AMEM, *args], env=env, capture_output=True, text=True, timeout=60)


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=300).stdout.strip()


# Every test container carries the pid of the pytest process that started it.
OWNER_LABEL = "agent-memory.test-owner-pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_orphan_containers() -> None:
    """Remove test containers whose pytest process is gone (killed before its teardown ran).

    Containers of live runs in other worktrees keep their owner pid, so they stay.
    """
    rows = _docker("ps", "-a", "--filter", f"label={OWNER_LABEL}",
                   "--format", f'{{{{.ID}}}} {{{{.Label "{OWNER_LABEL}"}}}}').splitlines()
    for row in rows:
        container, _, owner = row.partition(" ")
        if not owner.isdigit() or not _pid_alive(int(owner)):
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, timeout=60)


def _wait_and_migrate(url: str) -> None:
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


def external_pg_url(url: str) -> str:
    """The caller's throwaway Postgres, only if it is on loopback and holds no tables yet."""
    host = urlsplit(url).hostname
    if host not in LOOPBACK_HOSTS:
        raise RuntimeError(f"{EXTERNAL_PG_ENV} must point at a loopback host, not {host!r}")
    with psycopg.connect(url, connect_timeout=5) as conn:
        tables = conn.execute(
            "select count(*) from pg_tables where schemaname not in ('pg_catalog', 'information_schema')"
        ).fetchone()[0]
    if tables:
        raise RuntimeError(f"{EXTERNAL_PG_ENV} must point at an empty database; it has {tables} table(s)")
    return url


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    external = os.environ.get(EXTERNAL_PG_ENV)
    if external:
        url = external_pg_url(external)
        _wait_and_migrate(url)
        yield url
        return
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    _sweep_orphan_containers()
    # The data dir is tmpfs, so Docker creates no volume for it: a killed run cannot leak one.
    container = _docker(
        "run", "-d", "--rm", "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD=postgres",
        "--tmpfs", "/var/lib/postgresql/data", "--label", f"{OWNER_LABEL}={os.getpid()}", PG_IMAGE
    )
    try:
        mapping = _docker("port", container, "5432/tcp").splitlines()[0]  # e.g. 127.0.0.1:55012
        port = mapping.rsplit(":", 1)[1]
        url = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
        _wait_and_migrate(url)
        yield url
    finally:
        # -v: `rm -f` on a running container skips --rm, so the image's anonymous data volume would stay.
        subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True, timeout=60)


@pytest.fixture
def db_env(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    return base_env(AMEM_DB_URL=pg_url)
