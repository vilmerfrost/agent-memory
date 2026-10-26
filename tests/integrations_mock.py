"""A local HTTP stand-in for an external API (ClickHouse HTTP, Langfuse, past.dev) in tests.

    with MockService() as svc:
        svc.route("POST", "/api/v1/ingest/batch", lambda req: (202, {"status": "queued"}))
        ...  # point the adapter at svc.url
        assert svc.requests[0].json["items"][0]["id"] == "amem:proposal/..."

Every request is recorded (method, path, query, headers, raw body), so a test can prove exactly what would
have left the Mac. Unrouted requests answer 404. `fail_next(n, status)` makes the next n requests fail,
to test retries and backoff. Binds 127.0.0.1 on an OS-assigned port; nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def q(self, name: str) -> str | None:
        v = self.query.get(name)
        return v[0] if v else None


Handler = Callable[[Request], "tuple[int, Any] | tuple[int, Any, dict[str, str]]"]


@dataclass
class MockService:
    routes: list[tuple[str, str, Handler]] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    _fail: list[int] = field(default_factory=list)

    def route(self, method: str, path_prefix: str, handler: Handler) -> None:
        self.routes.insert(0, (method, path_prefix, handler))       # later routes win

    def fail_next(self, n: int, status: int = 503) -> None:
        self._fail.extend([status] * n)

    def requests_to(self, method: str, path_prefix: str) -> list[Request]:
        return [r for r in self.requests if r.method == method and r.path.startswith(path_prefix)]

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> MockService:
        svc = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:   # quiet
                pass

            def _handle(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                u = urlsplit(self.path)
                req = Request(self.command, u.path, parse_qs(u.query), {k: v for k, v in self.headers.items()}, body)
                svc.requests.append(req)
                if svc._fail:
                    self._send(svc._fail.pop(0), {"error": "injected failure"}, {})
                    return
                for method, prefix, h in svc.routes:
                    if method == self.command and u.path.startswith(prefix):
                        out = h(req)
                        status, payload = out[0], out[1]
                        self._send(status, payload, out[2] if len(out) > 2 else {})
                        return
                self._send(404, {"error": "no route"}, {})

            def _send(self, status: int, payload: Any, headers: dict[str, str]) -> None:
                data = payload if isinstance(payload, bytes) else (
                    payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
                self.send_response(status)
                ctype = "application/json" if not isinstance(payload, (bytes, str)) else "text/plain"
                self.send_header("Content-Type", headers.pop("Content-Type", ctype))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        # Poll shutdown promptly without changing request handling or client timeouts.
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
