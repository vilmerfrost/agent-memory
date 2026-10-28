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
import http.client as http_client
import math
import socket
import threading
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
    def __init__(self, status: int | None, message: str, *, retryable: bool | None = None):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.retryable = status in {500, 502, 503, 504} if retryable is None else retryable


@dataclass
class Response:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _read_attempt(req, *, context, timeout: float, deadline: float | None) -> Response:
    """Abort active sockets at the total deadline, including streaming bodies.

    A urllib socket timeout alone measures inactivity. A peer can otherwise
    keep read() alive indefinitely by dripping bytes. DNS resolution remains an
    OS resolver operation; after it returns an expired connection cannot send.
    """
    if deadline is None:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context) as response:
                return Response(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, exc.read() or b"", dict(exc.headers or {}))
    sockets = []
    lock = threading.Lock()
    expired = threading.Event()

    def remaining():
        left = deadline-time.monotonic()
        if expired.is_set() or left <= 0:
            raise TimeoutError("total HTTP read deadline exhausted")
        return left

    def remember(sock):
        with lock:
            sockets.append(sock)
        if expired.is_set():
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            raise TimeoutError("total HTTP read deadline exhausted")

    def connect_socket(address, connection_timeout=None, source_address=None):
        # Register before connect/tunnel/TLS, so an active connection cannot
        # escape the watchdog during proxy setup or multiple address attempts.
        error = None
        addresses = socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM)
        for family, kind, proto, _, target in addresses:
            remaining()
            sock = socket.socket(family, kind, proto)
            try:
                remember(sock)
                sock.settimeout(min(timeout, remaining()))
                if source_address:
                    sock.bind(source_address)
                sock.connect(target)
                remaining()
                return sock
            except OSError as exc:
                sock.close()
                error = exc
        raise error or OSError("no connection addresses")

    class Connection(http_client.HTTPConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = connect_socket
        def connect(self):
            self.timeout = min(timeout, remaining())
            super().connect()
            self.sock.settimeout(min(timeout, remaining()))

    class SecureConnection(http_client.HTTPSConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = connect_socket
        def connect(self):
            self.timeout = min(timeout, remaining())
            http_client.HTTPConnection.connect(self)
            self.sock.settimeout(min(timeout, remaining()))
            self.sock = self._context.wrap_socket(self.sock,
                server_hostname=self._tunnel_host or self.host, do_handshake_on_connect=False)
            remember(self.sock)
            self.sock.settimeout(min(timeout, remaining()))
            self.sock.do_handshake()
            remaining()

    class PlainHandler(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(Connection, request)

    class SecureHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(SecureConnection, request, context=self._context)

    def abort():
        expired.set()
        with lock:
            active = list(sockets)
        for sock in active:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass

    timer = threading.Timer(remaining(), abort)
    timer.daemon = True
    timer.start()
    try:
        opener = urllib.request.build_opener(PlainHandler(), SecureHandler(context=context))
        try:
            response = opener.open(req, timeout=min(timeout, remaining()))
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            remaining()  # a truncated response after shutdown cannot become valid evidence
            return Response(response.status, body, dict(response.headers))
    finally:
        timer.cancel()
        with lock:
            active = list(sockets)
        for sock in active:
            sock.close()


def request(method: str, url: str, *, headers: Mapping[str, str] | None = None, body: Any = None,
            timeout: float = 10.0, retries: int = 2, backoff: float = 0.5,
            sleep: Callable[[float], None] = time.sleep, ok: Callable[[int], bool] = lambda s: 200 <= s < 300,
            final: Callable[[Response], bool] | None = None,
            total_timeout: float | None = None) -> Response:
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
    if total_timeout is not None and (not math.isfinite(total_timeout) or total_timeout <= 0):
        raise HttpError(None, "total HTTP read deadline exhausted")
    deadline = time.monotonic()+total_timeout if total_timeout is not None else None
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            ctx = tls_context() if url.startswith("https://") else None
            resp = _read_attempt(req, context=ctx, timeout=timeout, deadline=deadline)
        except http_client.HTTPException as exc:
            if deadline is None:
                raise  # preserve the non-budgeted retry contract, especially billed POST reads
            raise HttpError(None, f"{type(exc).__name__}: bounded response interrupted", retryable=True) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            if attempt >= retries or deadline is not None:
                raise HttpError(None, f"{type(e).__name__}: {getattr(e, 'reason', e)!s}"[:300],
                                retryable=not isinstance(getattr(e, "reason", e), ssl.SSLError)) from None
            attempt += 1
            sleep(backoff * 2 ** (attempt - 1))
            continue
        if ok(resp.status):
            return resp
        if deadline is None and resp.status in RETRY_STATUSES and attempt < retries and not (final is not None and final(resp)):
            wait = retry_after(resp.headers)
            if wait is not None and wait > MAX_RETRY_AFTER_S:
                raise HttpError(resp.status, f"Retry-After {wait:.0f}s is over {MAX_RETRY_AFTER_S:.0f}s")
            attempt += 1
            sleep(max(backoff * 2 ** (attempt - 1), wait or 0.0))
            continue
        raise HttpError(resp.status, resp.text()[:300],
                        retryable=False if retry_after(resp.headers) is not None else None)
