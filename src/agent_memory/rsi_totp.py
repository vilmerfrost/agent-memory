"""TOTP for phone Apply / Undo (RFC 6238: HMAC-SHA1, 30-second steps, 6 digits). docs/rsi/least-privilege.md, A3.

The edge function amem-decide verifies codes (supabase/functions/amem-decide/totp.ts, the same algorithm and
test vector). This module creates the secret at enrollment and backs the tests; the secret lives in the
phone's authenticator and in the function secret RSI_TOTP_SECRET, never in a file or in the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import subprocess
import tempfile
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
# RFC 6238 appendix B: ASCII secret "12345678901234567890", T = 59 s -> 94287082 (8 digits) -> 287082 (6 digits).
RFC_SECRET_B32 = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTOR = (59, "287082")


def new_secret() -> str:
    """160 random bits, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    s = secret_b32.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret_b32: str, step: int) -> str:
    mac = hmac.new(_key(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return f"{n:0{DIGITS}d}"


def step_of(t: float | None = None) -> int:
    return int((time.time() if t is None else t) // STEP)


def verify(secret_b32: str, code: str, t: float | None = None, window: int = 1) -> int | None:
    """The matched step (now ± window), or None."""
    if not (isinstance(code, str) and len(code) == DIGITS and code.isdigit()):
        return None
    now = step_of(t)
    for s in range(now - window, now + window + 1):
        if hmac.compare_digest(code_at(secret_b32, s), code):
            return s
    return None


def otpauth_uri(secret_b32: str, account: str = "rsi-approvals", issuer: str = "agent-memory") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret_b32}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def set_function_secrets(values: dict[str, str], *, project_ref: str | None = None,
                         supabase: str = "supabase") -> int:
    """`supabase secrets set` from a 0600 temp file (values never in argv), then delete the file.
    Returns the CLI exit code. Needs a logged-in Supabase CLI (the A4 deploy window)."""
    fd, tmp = tempfile.mkstemp(prefix="amem-fn-secrets-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("".join(f"{k}={v}\n" for k, v in values.items()))
        cmd = [supabase, "secrets", "set", "--env-file", tmp]
        if project_ref:
            cmd += ["--project-ref", project_ref]
        return subprocess.run(cmd, capture_output=True, text=True, check=False).returncode
    finally:
        os.unlink(tmp)
