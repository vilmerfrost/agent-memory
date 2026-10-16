"""A small JSON-over-HTTP client for the adapters (stdlib only: no SDK enters the dependency tree).

Retries 429, 5xx and connection errors with exponential backoff, or after the server's Retry-After when it
asks for at most MAX_RETRY_AFTER_S (a longer wait raises, so the sync loop backs the table off instead).
Never logs or raises with request headers (they carry keys) and cuts response bodies to 300 characters in errors.

TLS (Mission 004B, hosted endpoints): every https request uses an explicit verifying context (system CA store,
hostname check, TLS 1.2 or newer). `AMEM_TLS_CA_FILE` may add a private CA bundle; nothing can turn
verification off.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
CA_FILE_ENV = "AMEM_TLS_CA_FILE"
MAX_RETRY_AFTER_S = 60.0
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def plain_http_ok(url: str) -> bool:
    """https anywhere; http only to a loopback host (the parsed host, not a prefix: not localhost.evil.test)."""
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in LOOPBACK)


def retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds from a Retry-After header (delta-seconds form only), or None."""
    v = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return max(float(str(v).strip()), 0.0) if v is not None else None
    except ValueError:
        return None


def tls_context(environ: Mapping[str, str] | None = None) -> ssl.SSLContext:
    cafile = ((os.environ if environ is None else environ).get(CA_FILE_ENV) or "").strip() or None
    ctx = ssl.create_default_context(cafile=cafile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:
        raise RuntimeError("TLS verification is off; refusing")
    return ctx


class HttpError(Exception):
    def __init__(self, status: int | None, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


@dataclass
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def request(method: str, url: str, *, headers: Mapping[str, str] | None = None, body: Any = None,
            timeout: float = 10.0, retries: int = 2, backoff: float = 0.5,
            sleep: Callable[[float], None] = time.sleep, ok: Callable[[int], bool] = lambda s: 200 <= s < 300,
            final: Callable[[Response], bool] | None = None) -> Response:
    """One request. `body`: bytes, str, or anything json.dumps takes (sent as application/json).
    `final(response)` true: an answer the caller knows a retry cannot change (past.dev's 429 with a cap-reached
    code), raised at once, even for a status that is otherwise retried."""
    if not plain_http_ok(url):
        raise HttpError(None, "refused: plain http only to localhost")
    hdrs = dict(headers or {})
    data: bytes | None
    if body is None or isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            ctx = tls_context() if url.startswith("https://") else None
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                resp = Response(r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            resp = Response(e.code, e.read() or b"", dict(e.headers or {}))
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt >= retries:
                raise HttpError(None, f"{type(e).__name__}: {getattr(e, 'reason', e)!s}"[:300]) from None
            attempt += 1
            sleep(backoff * 2 ** (attempt - 1))
            continue
        if ok(resp.status):
            return resp
        if resp.status in RETRY_STATUSES and attempt < retries and not (final is not None and final(resp)):
            wait = retry_after(resp.headers)
            if wait is not None and wait > MAX_RETRY_AFTER_S:
                raise HttpError(resp.status, f"Retry-After {wait:.0f}s is over {MAX_RETRY_AFTER_S:.0f}s")
            attempt += 1
            sleep(max(backoff * 2 ** (attempt - 1), wait or 0.0))
            continue
        raise HttpError(resp.status, resp.text()[:300])
