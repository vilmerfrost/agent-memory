"""Environment loading and settings.

Env files are optional. Load order: `.env.local`, then `.env`, from the repo root.
Values already in the process environment always win (override=False), and
`.env.local` wins over `.env` because it loads first.
Set `AMEM_NO_DOTENV=1` to skip env files entirely (tests do this).

`AMEM_REQUIRE_REMOTE=1` makes every write command refuse a local or empty
`AMEM_DB_URL` instead of silently falling back to the local default DSN.
On the REST backend (`AMEM_BACKEND=rest`, see docs/cloud.md) it checks
`AMEM_SUPABASE_URL` the same way.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

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


class BackendError(RuntimeError):
    """A run-store failure that is not a psycopg error (bad AMEM_BACKEND, REST errors)."""


BACKENDS = ("postgres", "rest")


def supabase_url() -> str:
    """Base URL of the Supabase project for the REST backend (no trailing slash)."""
    load_env()
    return os.environ.get("AMEM_SUPABASE_URL", "").strip().rstrip("/")


def supabase_key() -> str:
    """Optional `apikey` header value. Never print or log it."""
    load_env()
    return os.environ.get("AMEM_SUPABASE_KEY", "").strip()


def backend() -> str:
    """`postgres` or `rest`.

    AMEM_BACKEND wins when set. Default: `rest` when AMEM_SUPABASE_URL is set and
    AMEM_DB_URL is not (cloud sessions reach the internet only over HTTPS), else `postgres`.
    Raises BackendError for any other AMEM_BACKEND value.
    """
    load_env()
    explicit = os.environ.get("AMEM_BACKEND", "").strip().lower()
    if explicit:
        if explicit not in BACKENDS:
            raise BackendError(f"AMEM_BACKEND must be one of {', '.join(BACKENDS)}, got {explicit!r}")
        return explicit
    if supabase_url() and not os.environ.get("AMEM_DB_URL", "").strip():
        return "rest"
    return "postgres"


def rest_host(url: str) -> str:
    """Host part of an http(s) URL only, or `none` / `invalid`."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return "invalid"
    if parts.scheme not in ("http", "https"):
        return "invalid"
    return host or "none"


_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _ip_is_local(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Loopback or unspecified, including IPv4 hidden inside IPv6 (mapped or compatible)."""
    if addr.is_loopback or addr.is_unspecified:
        return True
    if isinstance(addr, ipaddress.IPv6Address):
        inner = addr.ipv4_mapped
        if inner is None and int(addr) >> 32 == 0:
            inner = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)  # ::a.b.c.d (IPv4-compatible)
        return inner is not None and _ip_is_local(inner)
    return addr in ipaddress.IPv4Network("0.0.0.0/8")  # "this host" (0, 0.x.y.z)


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An IP address in any spelling the resolver accepts, or None for a name.

    `ipaddress` parses the canonical forms (incl. long-form and mapped IPv6);
    `socket.inet_aton` adds the legacy IPv4 forms libc/getaddrinfo also accept:
    `0`, `127.1`, decimal `2130706433`, hex `0x7f000001`, octal `0177.0.0.1`.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if not host or any(c.isspace() for c in host):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _is_local_host(host: str) -> bool:
    """True for every spelling of this machine: socket paths, localhost names, loopback IPs."""
    host = host.strip()
    if not host or host.startswith("/") or host.startswith("@"):
        return True  # empty = libpq default (unix socket); "/..." = socket dir
    host = host.strip("[]").split("%", 1)[0].lower()  # IPv6 brackets, zone id (%lo0, %25lo0)
    host = host.rstrip(".")  # FQDN root dot: "localhost." == "localhost"
    if not host:
        return True
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        return True
    addr = _parse_ip(host)
    return addr is not None and _ip_is_local(addr)


def supabase_url_error(url: str | None = None) -> str | None:
    """Why AMEM_SUPABASE_URL cannot be used for a request, or None if it can.

    It must be an absolute http(s) URL with a host. Plain `http://` is allowed
    only to a local host (the test stub); any remote host needs `https://`, so an
    `apikey` (set here or added by a network proxy) never travels unencrypted.
    The message names the host at most, never the URL's path, query or the key.
    """
    url = supabase_url() if url is None else url
    if not url:
        return "AMEM_SUPABASE_URL is empty or unset"
    host = rest_host(url)
    if host == "invalid":
        return "AMEM_SUPABASE_URL is not an http(s) URL (expected https://<project-ref>.supabase.co)"
    if host == "none":
        return "AMEM_SUPABASE_URL has no host"
    if urlsplit(url).scheme == "http" and not _is_local_host(host):
        return f"AMEM_SUPABASE_URL uses http:// for remote host {host}; use https://"
    return None


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


def remote_write_error(for_backend: str | None = None) -> str | None:
    """Why a write must be refused under AMEM_REQUIRE_REMOTE=1, or None if it may go ahead.

    `for_backend` forces the check for one backend (`db migrate` is always Postgres);
    None checks the active backend. Messages name the host at most. Never the DSN,
    the password or the API key.
    """
    if not require_remote():
        return None
    prefix = "amem: AMEM_REQUIRE_REMOTE=1"
    try:
        active = for_backend or backend()
    except BackendError as exc:
        return f"{prefix} but {exc}; refusing to write."
    if active == "rest":
        return _rest_write_error(prefix)
    raw = os.environ.get("AMEM_DB_URL", "").strip()
    if not raw:
        return (f"{prefix} but AMEM_DB_URL is empty or unset; refusing to write to the local "
                "default database. Set AMEM_DB_URL to the remote database "
                "(or AMEM_SUPABASE_URL for the REST backend).")
    try:
        hosts = db_hosts(raw)
    except ValueError as exc:
        return f"{prefix} but {exc}; refusing to write."
    local = [h.strip() or "(default socket)" for h in hosts if _is_local_host(h)]
    if local:
        return (f"{prefix} but the database host is local ({', '.join(local)}); refusing to write. "
                "Set AMEM_DB_URL to the remote database.")
    return None


def _rest_write_error(prefix: str) -> str | None:
    url = supabase_url()
    if not url:
        return (f"{prefix} but AMEM_SUPABASE_URL is empty or unset (backend=rest); refusing to write. "
                "Set AMEM_SUPABASE_URL to the Supabase project URL.")
    host = rest_host(url)
    if host == "invalid":
        return f"{prefix} but AMEM_SUPABASE_URL is not an http(s) URL (backend=rest); refusing to write."
    if host == "none" or _is_local_host(host):
        return (f"{prefix} but the REST host is local ({host}) (backend=rest); refusing to write. "
                "Set AMEM_SUPABASE_URL to the remote Supabase project URL.")
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
