"""`amem admin split-credentials | restore-credentials | credentials-status` (docs/rsi/least-privilege.md, A2).

split-credentials, run once by Vilmer in his own terminal:
  1. reads the current AMEM_DB_URL (the postgres connection string) from the environment / .env.local;
  2. sets a fresh random password on the amem_app role (migration 20261023010000) and checks it logs in;
  3. stores the postgres connection string in a Keychain item that shows a dialog on every read;
  4. rewrites the AMEM_DB_URL line of .env.local to the amem_app connection string (one line, atomic, 0600).
No value is ever printed. restore-credentials reverses step 4 and deletes the Keychain item.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from agent_memory import config
from agent_memory import credentials as C
from agent_memory import rsi_approval as AP

ENV_FILE_ENV = "AMEM_ENV_FILE"     # override for tests; default <repo>/.env.local


def env_file() -> Path:
    return Path(os.environ.get(ENV_FILE_ENV) or config.REPO_ROOT / ".env.local")


def _human_only(what: str, *, stdin_isatty: bool | None = None, environ: dict[str, str] | None = None) -> str | None:
    reason = AP.agent_session_reason(environ)
    if reason:
        return f"amem: admin {what}: refused inside an agent session ({reason}); run it in your own terminal"
    if not (sys.stdin.isatty() if stdin_isatty is None else stdin_isatty):
        return f"amem: admin {what}: refused without a terminal; run it in your own terminal"
    return None


def split(admin_dsn: str, path: Path, *, say=print) -> str:
    """Steps 2-4. Returns the supabase-key kind. Raises on any failure before .env.local changes."""
    if C.dsn_user(admin_dsn) not in ("postgres",) and not (C.dsn_user(admin_dsn) or "").startswith("postgres."):
        raise ValueError("AMEM_DB_URL does not use the postgres user (already split?)")
    password = secrets.token_urlsafe(32)
    app = C.app_dsn(admin_dsn, password)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        if conn.execute("select 1 from pg_roles where rolname = %s", (C.APP_ROLE,)).fetchone() is None:
            raise ValueError("role amem_app does not exist: apply migration 20261023010000_amem_app_role.sql first")
        conn.execute(sql.SQL("alter role {} with login password {}").format(sql.Identifier(C.APP_ROLE), sql.Literal(password)))
    with psycopg.connect(app) as conn:
        who = conn.execute("select current_user").fetchone()[0]
        forge = conn.execute("select has_table_privilege(current_user, 'public.rsi_decisions', 'insert')").fetchone()[0]
    if who != C.APP_ROLE or forge:
        raise ValueError("the amem_app login did not behave as expected; nothing else changed")
    say("A Keychain dialog appears next. Click Deny: that proves the stored item needs you to read it.")
    C.store_admin(admin_dsn)
    try:
        C.replace_env_value(path, "AMEM_DB_URL", app)
    except BaseException:
        C.delete_admin()
        raise
    return C.supabase_key_kind(os.environ.get("AMEM_SUPABASE_KEY"))


def restore(path: Path) -> None:
    admin = C.admin_db_url()                       # dialog: click Allow (once)
    C.replace_env_value(path, "AMEM_DB_URL", admin)
    C.delete_admin()


def cmd_split(args: argparse.Namespace) -> int:
    if (msg := _human_only("split-credentials")) is not None:
        print(msg, file=sys.stderr)
        return 2
    config.load_env()
    admin = os.environ.get("AMEM_DB_URL", "").strip()
    path = env_file()
    if not admin or not path.is_file():
        print("amem: admin split-credentials: no AMEM_DB_URL in .env.local", file=sys.stderr)
        return 1
    try:
        kind = split(admin, path)
    except (ValueError, psycopg.Error, C.AdminUnavailable) as exc:
        print(f"amem: admin split-credentials: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("split-credentials: done. AMEM_DB_URL now connects as amem_app; the postgres connection string is in "
          f"the Keychain item {C.ADMIN_SERVICE!r} (a dialog on every read: Allow, never Always Allow).")
    if kind in ("secret", "service_role"):
        print(f"WARNING: AMEM_SUPABASE_KEY is a {kind} key. Remove it from .env.local: it bypasses every grant.")
    else:
        print(f"AMEM_SUPABASE_KEY: {kind}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    if (msg := _human_only("restore-credentials")) is not None:
        print(msg, file=sys.stderr)
        return 2
    try:
        restore(env_file())
    except (ValueError, OSError, C.AdminUnavailable) as exc:
        print(f"amem: admin restore-credentials: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("restore-credentials: done. AMEM_DB_URL is the postgres connection string again; Keychain item deleted.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config.load_env()
    user = C.dsn_user(os.environ.get("AMEM_DB_URL", "")) or "?"
    role = "amem_app" if user.split(".")[0] == C.APP_ROLE else "postgres" if user.split(".")[0] == "postgres" else user
    print(f"credentials: AMEM_DB_URL connects as {role}; admin Keychain item: "
          f"{'present' if C.admin_present() else 'absent'}; AMEM_SUPABASE_KEY: "
          f"{C.supabase_key_kind(os.environ.get('AMEM_SUPABASE_KEY'))}")
    return 0 if role == "amem_app" and C.admin_present() else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("admin", help="least-privilege credentials (docs/rsi/least-privilege.md)")
    s = p.add_subparsers(dest="admin_cmd", required=True)
    q = s.add_parser("split-credentials", help="routines -> amem_app; postgres DSN -> Keychain (your terminal only)")
    q.set_defaults(func=cmd_split)
    q = s.add_parser("restore-credentials", help="undo split-credentials (your terminal only)")
    q.set_defaults(func=cmd_restore)
    q = s.add_parser("credentials-status", help="which role AMEM_DB_URL uses; Keychain item present (no values)")
    q.set_defaults(func=cmd_status)
