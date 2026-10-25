"""Night Desk passkeys (Face ID) for web Apply / Undo (docs/rsi/approval.md "Web decisions").

`amem rsi passkey-enroll`  Vilmer's terminal only: a one-use enrollment code (10 minutes; only its sha256 is
                           stored) and the Night Desk enroll URL. Night Desk registers the passkey with it
                           through amem-decide /web/enroll/*, which also needs the owner's login.
`amem rsi passkey-list`    the enrolled passkeys (no keys printed).
`amem rsi passkey-revoke`  Vilmer's terminal only: revoke one passkey (kept for the audit trail).

The tables are owner-only (no grant to amem_app), so these commands use the admin connection string, like
`amem rsi decide`: after the credential split that shows the Keychain dialog.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import sys
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import psycopg

from agent_memory import rsi_approval as AP, store

ENROLL_TTL = timedelta(minutes=10)
CODE_BYTES = 15                       # 120 bits -> 24 base32 characters
# Night Desk routes by hash: the page reads the code from the fragment, then clears it from the address bar.
ENROLL_URL = "https://night-desk-vilmer-frosts-projects.vercel.app/#approval-face-id/"


def normalize_code(code: str) -> str:
    """Same as web.ts normalizeEnrollCode: base32 letters only, upper case (dashes and spaces dropped)."""
    return re.sub(r"[^A-Z2-7]", "", code.upper())


def code_hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def new_code() -> str:
    raw = base64.b32encode(secrets.token_bytes(CODE_BYTES)).decode().rstrip("=")
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def enroll_link(base: str, code: str) -> str:
    """The Night Desk link for an enroll code: the code goes URL-encoded into the fragment, never a query."""
    return f"{base}{quote(code, safe='')}"


def create_enroll_code(conn: psycopg.Connection, ttl: timedelta = ENROLL_TTL) -> str:
    """Store a fresh code's hash (earlier unused codes end now) and return the code. Caller commits."""
    code = new_code()
    conn.execute("update rsi_passkey_enroll_codes set expires_at = least(expires_at, now()) where used_at is null")
    conn.execute("insert into rsi_passkey_enroll_codes (code_hash, expires_at) values (%s, now() + %s)",
                 (code_hash(code), ttl))
    return code


def list_passkeys(conn: psycopg.Connection) -> list[dict[str, Any]]:
    cur = conn.execute("select id, left(credential_id, 12) as credential, sign_count, transports, device_type, backed_up, "
                       "created_at, last_used_at, revoked_at from rsi_passkeys order by created_at")
    return [dict(zip([c.name for c in cur.description], r)) for r in cur.fetchall()]


def revoke_passkey(conn: psycopg.Connection, ident: str) -> int:
    """Revoke by passkey id or credential-id prefix (at least 8 characters). Returns rows revoked."""
    if len(ident) < 8:
        return 0
    rows = conn.execute("update rsi_passkeys set revoked_at = now() where revoked_at is null and "
                        "(id::text = %s or credential_id like %s) returning id",
                        (ident, ident.replace("%", "").replace("_", r"\_") + "%")).fetchall()
    return len(rows)


def _human(what: str) -> int | None:
    if AP.agent_session_reason() or not sys.stdin.isatty() or not sys.stdout.isatty():
        print(f"amem: rsi {what}: refused outside Vilmer's own terminal (agent session or no TTY)", file=sys.stderr)
        return 2
    return None


def _admin_url() -> str | None:
    from agent_memory import credentials
    return credentials.admin_db_url() if credentials.admin_present() else None   # dialog once split


def cmd_enroll(args: argparse.Namespace) -> int:
    if (code := _human("passkey-enroll")) is not None:
        return code
    try:
        with store.connect(_admin_url()) as conn:
            enroll = create_enroll_code(conn)
    except Exception as exc:   # noqa: BLE001 - AdminUnavailable, psycopg.Error: say which, never the DSN
        print(f"amem: rsi passkey-enroll failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"Enroll code (one use, {int(ENROLL_TTL.total_seconds() // 60)} minutes): {enroll}\n"
          f"Open on your iPhone, signed in to Night Desk:\n  {enroll_link(args.url, enroll)}\n"
          "A new code ends any earlier unused code.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        with store.connect(_admin_url()) as conn:
            rows = list_passkeys(conn)
    except Exception as exc:   # noqa: BLE001
        print(f"amem: rsi passkey-list failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, default=str, sort_keys=True))
        return 0
    if not rows:
        print("rsi passkeys: none enrolled (web Apply / Undo are refused: not-enrolled)")
    for r in rows:
        state = f"revoked {r['revoked_at']:%Y-%m-%d %H:%M}" if r["revoked_at"] else "active"
        used = f"{r['last_used_at']:%Y-%m-%d %H:%M}" if r["last_used_at"] else "never"
        print(f"{r['id']}  {r['credential']}…  {state}  enrolled {r['created_at']:%Y-%m-%d %H:%M}  last used {used}  "
              f"count {r['sign_count']}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    if (code := _human("passkey-revoke")) is not None:
        return code
    try:
        with store.connect(_admin_url()) as conn:
            n = revoke_passkey(conn, args.passkey)
    except Exception as exc:   # noqa: BLE001
        print(f"amem: rsi passkey-revoke failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if n != 1:
        print(f"amem: rsi passkey-revoke: {n} passkeys matched; give the full id from `amem rsi passkey-list`",
              file=sys.stderr)
        return 1
    print("rsi passkey-revoke: revoked. Web Apply / Undo with that passkey now return not-enrolled.")
    return 0


def register(s: argparse._SubParsersAction) -> None:
    p = s.add_parser("passkey-enroll", help="Vilmer's terminal only: a one-use code to enroll a Night Desk passkey")
    p.add_argument("--url", default=ENROLL_URL, help="the Night Desk enroll link base; the code is appended to its #fragment")
    p.set_defaults(func=cmd_enroll)
    p = s.add_parser("passkey-list", help="the enrolled Night Desk passkeys")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)
    p = s.add_parser("passkey-revoke", help="Vilmer's terminal only: revoke one Night Desk passkey")
    p.add_argument("passkey", help="passkey id (or a credential-id prefix of 8+ characters)")
    p.set_defaults(func=cmd_revoke)
