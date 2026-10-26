"""Loopback-only OTLP/JSON receiver. Raw telemetry is filtered before any disk write."""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from agent_memory.execution_timing import Span, otlp_spans

MAX_BODY = 4_000_000


def append_spans(path: Path, spans: list[Span]) -> None:
    if not spans:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError("timing spool cannot be a symlink")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for span in spans:
            data = (json.dumps(span.record(), separators=(",", ":")) + "\n").encode()
            with os.fdopen(os.dup(fd), "ab", closefd=True) as out:
                out.write(data)
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class CollectorState:
    spool: Path
    received: int = 0
    accepted: int = 0
    flushes: int = 0
    database_error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {"app": "agent-memory-timing", "status": "ok", "received_batches": self.received, "accepted_api_spans": self.accepted,
                    "database_flushes": self.flushes, "database_error": self.database_error,
                    "raw_payloads_stored": False}


def server(state: CollectorState, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass  # no request bodies, URL query strings, auth fields or tool output in logs

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def respond(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self.respond(200 if self.path == "/health" else 404, state.health() if self.path == "/health" else {})

        def do_POST(self) -> None:
            if self.path != "/v1/logs" or self.headers.get("Origin"):
                self.respond(403, {})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY or "application/json" not in self.headers.get("Content-Type", ""):
                    self.respond(413 if size > MAX_BODY else 415, {})
                    return
                raw = self.rfile.read(size)
                encoding = self.headers.get("Content-Encoding", "identity")
                if encoding == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                        raw = compressed.read(MAX_BODY + 1)
                elif encoding != "identity":
                    self.respond(415, {})
                    return
                if len(raw) > MAX_BODY:
                    self.respond(413, {})
                    return
                spans = otlp_spans(json.loads(raw))
                append_spans(state.spool, spans)
                with state.lock:
                    state.received += 1
                    state.accepted += len(spans)
                self.respond(200, {})  # OTLP ExportLogsServiceResponse
            except (ValueError, TypeError, AttributeError, OSError, OverflowError):
                self.respond(400, {})

    result = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    result.daemon_threads = True
    return result


def serve(port: int, *, flush_seconds: float = 15, db_url: str | None = None) -> None:
    from agent_memory import sessions_ingest, timing_store

    parent = sessions_ingest.claude_home() / "parent"
    state = CollectorState(parent / "timing-events.jsonl")
    httpd = server(state, port)
    stop = threading.Event()

    def flush() -> None:
        signatures: dict[Path, tuple[int, int]] = {}
        last_pruned = 0.0
        while not stop.wait(flush_seconds):
            try:
                from agent_memory.timing_sync import flush_queue
                from agent_memory import test_timings
                flush_queue(parent/'timing-sync-queue',db_url=db_url)
                paths = timing_store.spool_paths(parent)
                changed = []
                latest = {}
                for path in paths:
                    stat = path.stat()
                    latest[path] = (stat.st_mtime_ns, stat.st_size)
                    if latest[path] != signatures.get(path):
                        changed.append(path)
                if changed:
                    timing_store.ingest_spools(changed, db_url=db_url, cursor_path=parent/'timing-cursors.json')
                signatures = latest
                # A test-batch failure must not prevent existing native span
                # ingestion. Retry test batches after the original work commits.
                for queue in parent.glob('*/test-timing-queue'):
                    test_timings.flush_queue(queue, db_url=db_url)
                if time.monotonic()-last_pruned >= 3600:
                    test_timings.prune(db_url=db_url)
                    last_pruned = time.monotonic()
                with state.lock:
                    state.flushes += 1
                    state.database_error = None
            except Exception as exc:
                # Spool is retained; native clients still receive an acknowledgement.
                with state.lock:
                    state.database_error = type(exc).__name__

    thread = threading.Thread(target=flush, daemon=True)
    thread.start()
    print(f"Timing collector: http://127.0.0.1:{httpd.server_port}/v1/logs (raw payloads discarded)", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        stop.set()
        thread.join(timeout=5)
        httpd.server_close()
