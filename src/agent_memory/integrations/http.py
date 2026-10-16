"""A small JSON-over-HTTP client for the adapters (stdlib only: no SDK enters the dependency tree).

Retries 429, 5xx and connection errors with exponential backoff. Never logs or raises with request
headers (they carry keys) and cuts response bodies to 300 characters in errors.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


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
            sleep: Callable[[float], None] = time.sleep, ok: Callable[[int], bool] = lambda s: 200 <= s < 300) -> Response:
    """One request. `body`: bytes, str, or anything json.dumps takes (sent as application/json)."""
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost", "http://[::1]")):
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
            with urllib.request.urlopen(req, timeout=timeout) as r:
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
        if resp.status in RETRY_STATUSES and attempt < retries:
            attempt += 1
            sleep(backoff * 2 ** (attempt - 1))
            continue
        raise HttpError(resp.status, resp.text()[:300])
