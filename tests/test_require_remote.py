"""AMEM_REQUIRE_REMOTE=1: writes refuse a local or empty AMEM_DB_URL. No network needed."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from agent_memory import cli, config, store
from conftest import base_env, run_amem

PW = "secretpw-never-print"
FIXTURE = Path(__file__).parent / "fixtures" / "routines.sample.log"

LOCAL_URLS = [
    f"postgresql://postgres:{PW}@127.0.0.1:54322/postgres",
    f"postgresql://postgres:{PW}@localhost:5432/postgres",
    f"postgresql://postgres:{PW}@[::1]:5432/postgres",
    f"postgresql://postgres:{PW}@127.0.0.2/postgres",
    f"postgresql://postgres:{PW}@app.localhost/postgres",
    "postgresql:///postgres",  # no host: libpq default unix socket
    "postgresql://postgres@%2Ftmp/postgres",  # explicit socket dir
    f"host=localhost password={PW} dbname=postgres",
    f"postgresql://postgres:{PW}@db.example.com/postgres?hostaddr=127.0.0.1",  # hostaddr wins
    f"postgresql://postgres:{PW}@db.example.com,localhost/postgres",  # any local host in the list
]
REMOTE_URLS = [
    f"postgresql://postgres:{PW}@db.example.com:5432/postgres",
    f"postgresql://postgres.abc:{PW}@aws-0-eu-north-1.pooler.example.com:6543/postgres",
    f"postgresql://postgres:{PW}@203.0.113.5/postgres",
    f"host=db.example.com password={PW} dbname=postgres",
]


def _on(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")
    if url is None:
        monkeypatch.delenv("AMEM_DB_URL", raising=False)
    else:
        monkeypatch.setenv("AMEM_DB_URL", url)


def test_off_by_default_allows_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_DB_URL", LOCAL_URLS[0])
    assert config.require_remote() is False
    assert config.remote_write_error() is None
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "0")
    assert config.remote_write_error() is None


@pytest.mark.parametrize("url", [None, "", "   "])
def test_empty_url_is_refused(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    _on(monkeypatch, url)
    error = config.remote_write_error()
    assert error is not None
    assert "AMEM_DB_URL is empty or unset" in error


@pytest.mark.parametrize("url", LOCAL_URLS)
def test_local_hosts_are_refused(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _on(monkeypatch, url)
    error = config.remote_write_error()
    assert error is not None
    assert "database host is local" in error
    assert PW not in error
    assert url not in error


@pytest.mark.parametrize("url", REMOTE_URLS)
def test_remote_hosts_are_allowed(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _on(monkeypatch, url)
    assert config.remote_write_error() is None


# Every spelling of "this machine" a resolver accepts. As URL/DSN host parts:
# IPv6 in brackets. Each is refused on both backends.
LOOPBACK_SPELLINGS = [
    "127.0.0.1", "127.0.0.2", "127.255.255.254",
    "localhost", "LOCALHOST", "localhost.", "app.localhost", "app.localhost.", "localhost.localdomain",
    "[::1]", "[0:0:0:0:0:0:0:1]", "[0000:0000:0000:0000:0000:0000:0000:0001]",
    "[::ffff:127.0.0.1]", "[::ffff:7f00:1]", "[::127.0.0.1]",  # IPv4-mapped / -compatible
    "[::1%25lo0]",  # zone id
    "[::]", "[0:0:0:0:0:0:0:0]", "0.0.0.0", "0", "0.1.2.3",  # unspecified / "this host"
    "2130706433", "0x7f000001", "0x7F.0.0.1", "0177.0.0.1", "127.1", "127.0.1",  # inet_aton forms
    "127.0.0.1.",
]
# Real remote hosts stay allowed, including names that merely contain "localhost"/"127".
REMOTE_SPELLINGS = [
    "db.example.com", "aws-0-eu-north-1.pooler.example.com", "project.supabase.co", "203.0.113.5",
    "[2001:db8::1]", "[::ffff:203.0.113.5]", "localhost.example.com", "mylocalhost", "127.example.com",
    "0x7f.example.com", "db.example.com.",
]


@pytest.mark.parametrize("host", LOOPBACK_SPELLINGS)
def test_loopback_spellings_refused_on_postgres(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    _on(monkeypatch, f"postgresql://postgres:{PW}@{host}:5432/postgres")
    error = config.remote_write_error()
    assert error is not None and "database host is local" in error, host
    assert PW not in error


@pytest.mark.parametrize("host", LOOPBACK_SPELLINGS)
def test_loopback_spellings_refused_on_rest(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")
    monkeypatch.setenv("AMEM_SUPABASE_URL", f"https://{host}")
    assert config.backend() == "rest"
    error = config.remote_write_error()
    assert error is not None and "REST host is local" in error, host


@pytest.mark.parametrize("host", REMOTE_SPELLINGS)
def test_remote_spellings_allowed_on_both_backends(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    _on(monkeypatch, f"postgresql://postgres:{PW}@{host}:5432/postgres")
    assert config.remote_write_error() is None, host
    monkeypatch.delenv("AMEM_DB_URL")
    monkeypatch.setenv("AMEM_SUPABASE_URL", f"https://{host}")
    assert config.backend() == "rest"
    assert config.remote_write_error() is None, host


def test_loopback_spelling_blocks_cli_write() -> None:
    # End to end through the CLI: refused with exit 3 before any connection.
    for url in ("postgresql://postgres:pw@[::ffff:127.0.0.1]:5432/postgres",
                "postgresql://postgres:pw@2130706433:5432/postgres"):
        result = run_amem("log-run", "--routine", "x", "--status", "pass",
                          env=base_env(AMEM_REQUIRE_REMOTE="1", AMEM_DB_URL=url))
        assert result.returncode == cli.REMOTE_REQUIRED_EXIT, result.stderr
    result = run_amem("log-run", "--routine", "x", "--status", "pass",
                      env=base_env(AMEM_REQUIRE_REMOTE="1", AMEM_SUPABASE_URL="https://0x7f000001"))
    assert result.returncode == cli.REMOTE_REQUIRED_EXIT, result.stderr


def test_pghost_counts_when_dsn_has_no_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "postgresql:///postgres")
    monkeypatch.setenv("PGHOST", "db.example.com")
    assert config.remote_write_error() is None
    monkeypatch.setenv("PGHOST", "localhost")
    assert config.remote_write_error() is not None


def test_invalid_dsn_is_refused_without_echoing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, f"not a dsn {PW}")
    error = config.remote_write_error()
    assert error is not None
    assert "not a valid connection string" in error
    assert PW not in error


@pytest.mark.parametrize(
    "args",
    [
        ("log-run", "--routine", "x", "--status", "pass"),
        ("ingest", "routines-log", str(FIXTURE)),
        ("db", "migrate"),
    ],
)
@pytest.mark.parametrize("url", [LOCAL_URLS[0], None])
def test_cli_writes_blocked(args: tuple[str, ...], url: str | None) -> None:
    extra = {"AMEM_REQUIRE_REMOTE": "1"}
    if url is not None:
        extra["AMEM_DB_URL"] = url
    result = run_amem(*args, env=base_env(**extra))
    assert result.returncode == cli.REMOTE_REQUIRED_EXIT, result.stderr
    assert "AMEM_REQUIRE_REMOTE=1" in result.stderr
    assert "refusing to write" in result.stderr
    assert result.stdout == ""
    assert PW not in result.stdout + result.stderr


def test_cli_reads_not_blocked() -> None:
    # doctor and runs are reads: the guard does not apply (runs just fails to connect).
    env = base_env(AMEM_REQUIRE_REMOTE="1", AMEM_DB_URL="postgresql://postgres:pw@127.0.0.1:1/postgres")
    assert run_amem("doctor", env=env).returncode == 0
    result = run_amem("runs", "--routine", "x", env=env)
    assert result.returncode == 1
    assert "refusing to write" not in result.stderr


def test_cli_writes_allowed_with_remote_host(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # In-process with the store stubbed: proves the guard lets a non-local host through
    # without opening any connection.
    _on(monkeypatch, REMOTE_URLS[0])
    run_id = uuid.uuid4()
    calls: list[str] = []
    monkeypatch.setattr(store, "insert_run", lambda *a, **k: calls.append("insert") or run_id)
    monkeypatch.setattr(store, "ingest_runs", lambda rows, **k: calls.append("ingest") or (len(list(rows)), 0))
    monkeypatch.setattr(store, "migrate", lambda **k: calls.append("migrate") or [])

    assert cli.main(["log-run", "--routine", "x", "--status", "pass"]) == 0
    assert cli.main(["ingest", "routines-log", str(FIXTURE)]) == 0
    assert cli.main(["db", "migrate"]) == 0
    assert calls == ["insert", "ingest", "migrate"]
    out = capsys.readouterr()
    assert str(run_id) in out.out
    assert "refusing" not in out.err
