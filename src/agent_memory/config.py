"""Environment loading and settings.

Env files are optional. Load order: `.env.local`, then `.env`, from the repo root.
Values already in the process environment always win (override=False), and
`.env.local` wins over `.env` because it loads first.
Set `AMEM_NO_DOTENV=1` to skip env files entirely (tests do this).

`AMEM_REQUIRE_REMOTE=1` makes every write command refuse a local or empty
`AMEM_DB_URL` instead of silently falling back to the local default DSN.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

DEFAULT_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
JEV_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "jev"

_loaded = False


def load_env() -> None:
    """Load `.env.local` then `.env` once, if present. Never required."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if os.environ.get("AMEM_NO_DOTENV") == "1":
        return
    for name in (".env.local", ".env"):
        path = REPO_ROOT / name
        if path.is_file():
            load_dotenv(path, override=False)


def db_url() -> str:
    load_env()
    return os.environ.get("AMEM_DB_URL") or DEFAULT_DB_URL


def require_remote() -> bool:
    load_env()
    return os.environ.get("AMEM_REQUIRE_REMOTE", "").strip() == "1"


_LOCAL_NAMES = {"localhost", "::1", "0.0.0.0", "::"}


def _is_local_host(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if not host or host.startswith("/") or host.startswith("@"):
        return True  # empty = libpq default (unix socket); "/..." = socket dir
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    return host.startswith("127.")


def db_hosts(url: str) -> list[str]:
    """Every host libpq could connect to for this DSN (host and hostaddr lists).

    Raises ValueError for a DSN that cannot be parsed. The message never holds the DSN.
    """
    try:
        params = conninfo_to_dict(url)
    except Exception:
        raise ValueError("AMEM_DB_URL is not a valid connection string") from None
    hosts: list[str] = []
    for key in ("host", "hostaddr"):
        value = params.get(key)
        if value:
            hosts.extend(str(value).split(","))
    if not params.get("host") and not params.get("hostaddr"):
        hosts.extend(os.environ.get("PGHOST", "").split(","))
    return hosts or [""]


def remote_write_error() -> str | None:
    """Why a write must be refused under AMEM_REQUIRE_REMOTE=1, or None if it may go ahead.

    Messages name the host at most. Never the DSN or the password.
    """
    if not require_remote():
        return None
    raw = os.environ.get("AMEM_DB_URL", "").strip()
    prefix = "amem: AMEM_REQUIRE_REMOTE=1"
    if not raw:
        return (f"{prefix} but AMEM_DB_URL is empty or unset; refusing to write to the local "
                "default database. Set AMEM_DB_URL to the remote database.")
    try:
        hosts = db_hosts(raw)
    except ValueError as exc:
        return f"{prefix} but {exc}; refusing to write."
    local = [h.strip() or "(default socket)" for h in hosts if _is_local_host(h)]
    if local:
        return (f"{prefix} but the database host is local ({', '.join(local)}); refusing to write. "
                "Set AMEM_DB_URL to the remote database.")
    return None


def typesafe_api_key() -> str:
    load_env()
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def jev_live_flag() -> bool:
    load_env()
    return os.environ.get("AMEM_JEV_LIVE", "").strip() == "1"


def jev_fixtures_dir() -> Path:
    load_env()
    override = os.environ.get("AMEM_JEV_FIXTURES_DIR")
    return Path(override) if override else JEV_FIXTURES_DIR
