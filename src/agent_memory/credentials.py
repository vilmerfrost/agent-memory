"""The admin credential behind a human-presence gate (docs/rsi/least-privilege.md, A2).

Routines and the promotion worker connect as `amem_app` (AMEM_DB_URL). The `postgres` connection string
lives in a macOS Keychain item that trusts no application (`-T ""`), so every read shows a macOS dialog.
An unattended process cannot answer it and fails closed. Only `amem db migrate` and `amem rsi decide`
ask for it, and only once the item exists; before the split they keep using AMEM_DB_URL as today.

Values never go to argv, logs or stdout: the item is written with `security -i` (the command comes on
stdin, the value hex-encoded) and read with `find-generic-password -w` (captured, never printed).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, quote

ADMIN_SERVICE = "amem-admin-db"
ADMIN_ACCOUNT = "postgres"
SECURITY_ENV = "AMEM_ADMIN_SECURITY"      # path of the `security` executable (tests use a fake)
ADMIN_ENV = "AMEM_ADMIN_DB_URL"           # explicit override (tests; a one-off admin shell)
DEFAULT_SECURITY = "/usr/bin/security"
NOT_FOUND = 44                            # `security` exit code: errSecItemNotFound
READ_TIMEOUT = 120.0                      # seconds to answer the Keychain dialog
APP_ROLE = "amem_app"


class AdminUnavailable(RuntimeError):
    """The admin credential is needed but cannot be read. The message says why; it never holds a value."""


def _security() -> str:
    return os.environ.get(SECURITY_ENV, "").strip() or DEFAULT_SECURITY


def _run(args: list[str], *, stdin: bytes | None = None, timeout: float = 30.0) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run([_security(), *args], input=stdin, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise AdminUnavailable("the macOS `security` tool was not found") from None
    except subprocess.TimeoutExpired:
        raise AdminUnavailable("the Keychain dialog was not answered (unattended runs cannot read the admin "
                               "credential; run this yourself)") from None


def admin_present() -> bool:
    """True when the Keychain item exists. Reads attributes only (no -w), so no dialog appears."""
    if os.environ.get(ADMIN_ENV, "").strip():
        return True
    try:
        return _run(["find-generic-password", "-s", ADMIN_SERVICE, "-a", ADMIN_ACCOUNT]).returncode == 0
    except AdminUnavailable:
        return False


def admin_db_url() -> str:
    """The admin connection string. Shows the macOS dialog. Raises AdminUnavailable, never falls back."""
    override = os.environ.get(ADMIN_ENV, "").strip()
    if override:
        return override
    res = _run(["find-generic-password", "-s", ADMIN_SERVICE, "-a", ADMIN_ACCOUNT, "-w"], timeout=READ_TIMEOUT)
    if res.returncode == NOT_FOUND:
        raise AdminUnavailable(f"no admin credential in the Keychain (item {ADMIN_SERVICE!r})")
    if res.returncode != 0:
        raise AdminUnavailable(f"the Keychain refused the admin credential (security exit {res.returncode}; "
                               "answer the dialog with Allow, never Always Allow)")
    value = res.stdout.decode("utf-8", "replace").strip()
    if not value:
        raise AdminUnavailable("the admin credential in the Keychain is empty")
    return value


def store_admin(dsn: str) -> None:
    """Create the Keychain item with no trusted application. Refuses to overwrite an existing item."""
    if admin_present():
        raise AdminUnavailable(f"Keychain item {ADMIN_SERVICE!r} already exists; refusing to overwrite it")
    command = (f'add-generic-password -s {ADMIN_SERVICE} -a {ADMIN_ACCOUNT} -l {ADMIN_SERVICE} -T "" '
               f"-X {dsn.encode('utf-8').hex()}\n")
    res = _run(["-i"], stdin=command.encode("ascii"))
    if res.returncode != 0 or not admin_present():
        raise AdminUnavailable(f"storing the admin credential in the Keychain failed (security exit {res.returncode})")
    if not gated():
        delete_admin()
        raise AdminUnavailable("the new Keychain item could be read without a dialog (its access list is open); "
                               "it was deleted again and nothing else changed")


GATE_PROBE = 5.0   # seconds: a gated item shows a dialog and does not answer within this time


def gated() -> bool:
    """True when reading the item needs a human: the read does not return the value within GATE_PROBE
    seconds (a dialog is waiting) or is refused. False when the value comes straight back (open ACL).
    The value is captured and dropped, never printed."""
    try:
        res = subprocess.run([_security(), "find-generic-password", "-s", ADMIN_SERVICE, "-a", ADMIN_ACCOUNT, "-w"],
                             capture_output=True, timeout=GATE_PROBE, check=False)
    except subprocess.TimeoutExpired:
        return True
    except FileNotFoundError:
        return False
    return res.returncode != 0


def delete_admin() -> bool:
    return _run(["delete-generic-password", "-s", ADMIN_SERVICE, "-a", ADMIN_ACCOUNT]).returncode == 0


# -- connection strings ------------------------------------------------------------------------------------
def app_dsn(admin_dsn: str, password: str) -> str:
    """The admin DSN with the user swapped for amem_app. `postgres` -> `amem_app`; the Supabase pooler form
    `postgres.<ref>` -> `amem_app.<ref>`. Anything else is refused (unknown admin user)."""
    parts = urlsplit(admin_dsn)
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname or parts.username is None:
        raise ValueError("the admin connection string is not a postgresql:// URL with a user")
    user = parts.username
    if user == "postgres":
        new_user = APP_ROLE
    elif user.startswith("postgres.") and len(user) > len("postgres."):
        new_user = APP_ROLE + user[len("postgres"):]
    else:
        raise ValueError("the admin connection string does not use the postgres user")
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    netloc = f"{quote(new_user, safe='.')}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def dsn_user(dsn: str) -> str | None:
    try:
        return urlsplit(dsn).username
    except ValueError:
        return None


# -- the env file ------------------------------------------------------------------------------------------
def replace_env_value(path: Path, key: str, value: str) -> None:
    """Rewrite one KEY=value line in place (atomic, mode 0600). Other lines stay byte-identical."""
    raw = path.read_bytes().decode("utf-8")
    lines = raw.splitlines(keepends=True)
    hits = [i for i, ln in enumerate(lines) if ln.lstrip().startswith(f"{key}=") or ln.lstrip().startswith(f"export {key}=")]
    if len(hits) != 1:
        raise ValueError(f"{path.name} must hold exactly one {key}= line (found {len(hits)})")
    i = hits[0]
    old = lines[i]
    end = "\n" if old.endswith("\n") else ""
    prefix = old[: old.index("=") + 1]
    rest = old[len(prefix):].rstrip("\r\n")
    quote_char = rest[0] if rest[:1] in ("'", '"') else ""
    lines[i] = f"{prefix}{quote_char}{value}{quote_char}{end}"
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, "".join(lines).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def supabase_key_kind(value: str | None) -> str:
    """What AMEM_SUPABASE_KEY is, without showing it: none | publishable | secret | anon | service_role | unknown."""
    v = (value or "").strip()
    if not v:
        return "none"
    if v.startswith("sb_secret_"):
        return "secret"
    if v.startswith("sb_publishable_"):
        return "publishable"
    parts = v.split(".")
    if len(parts) == 3:
        try:
            pad = "=" * (-len(parts[1]) % 4)
            role = json.loads(base64.urlsafe_b64decode(parts[1] + pad)).get("role")
            return role if role in ("anon", "service_role") else "unknown"
        except (ValueError, json.JSONDecodeError):
            return "unknown"
    return "unknown"
