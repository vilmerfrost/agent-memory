"""Run pytest with a live compact summary and a complete, worktree-local log."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Iterator, TextIO

import pytest


class Progress:
    def __init__(self, console: TextIO, interval: float):
        self.console = console
        self.interval = interval
        self.started = time.monotonic()
        self.total: int | None = None
        self.outcomes: dict[str, str] = {}
        self.completed: set[str] = set()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)

    def line(self, message: str) -> None:
        with self.lock:
            print(message, file=self.console, flush=True)

    def summary(self) -> None:
        with self.lock:
            elapsed = time.monotonic() - self.started
            if self.total is None:
                self.line(f"[{elapsed:6.1f}s] Starting workers / collecting tests...")
                return
            done = len(self.completed)
            counts = Counter(self.outcomes.get(n, "unknown") for n in self.completed)
            percent = 100 * done // self.total if self.total else 0
            detail = " ".join(f"{name}={counts[name]}" for name in
                              ("passed", "failed", "errors", "skipped", "xfailed", "xpassed", "unknown")
                              if counts[name] or name in ("passed", "failed", "skipped"))
            self.line(f"[{elapsed:6.1f}s] {percent:3d}% | {done}/{self.total} complete | {detail}")

    def _heartbeat(self) -> None:
        while not self.stop.wait(self.interval):
            self.summary()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        # The xdist controller collects no items itself; its workers report below.
        if not session.config.getoption("numprocesses", default=0):
            with self.lock:
                self.total = len(session.items)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: object, ids: list[str]) -> None:
        with self.lock:
            self.total = len(ids)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        with self.lock:
            if report.failed:
                self.outcomes[report.nodeid] = "failed" if report.when == "call" else "errors"
                self.line(f"FAIL [{report.when}] {report.nodeid} (traceback in full log)")
            elif report.skipped:
                self.outcomes[report.nodeid] = "xfailed" if hasattr(report, "wasxfail") else "skipped"
            elif report.when == "call":
                self.outcomes[report.nodeid] = "xpassed" if hasattr(report, "wasxfail") else "passed"
            # A completed call can still fail during teardown. Count only after cleanup.
            if report.when == "teardown":
                self.completed.add(report.nodeid)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.line(f"COLLECTION ERROR: {report.nodeid} (details in full log)")

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: object, error: object | None) -> None:
        if error is not None:
            self.line("WORKER ERROR: test process exited unexpectedly (details in full log)")


@contextmanager
def log_output(log: TextIO) -> Iterator[None]:
    """Capture Python and raw/subprocess output; pytest's normal capture still works."""
    saved = [os.dup(1), os.dup(2)]
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        with redirect_stdout(log), redirect_stderr(log):
            yield
    finally:
        log.flush()
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4, help="test processes (0 for serial debugging)")
    parser.add_argument("--interval", type=float, default=5, help="seconds between progress lines")
    parser.add_argument("--log-dir", type=Path, help="default: test-logs in this worktree's Git metadata")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="optional pytest arguments after --")
    args = parser.parse_args(argv)
    if args.workers < 0 or not 0 < args.interval < float("inf"):
        parser.error("workers must be non-negative and interval must be positive and finite")
    extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    log_dir = args.log_dir
    if log_dir is None:
        git_dir = subprocess.check_output(["git", "rev-parse", "--absolute-git-dir"], text=True).strip()
        log_dir = Path(git_dir) / "test-logs"
    log_dir = log_dir.resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    log_path = log_dir / f"pytest-{stamp}-{os.getpid()}.log"
    pytest_args = ["-v", "--color=no", "-n", str(args.workers), "--dist", "loadfile",
                   "--max-worker-restart=0", *extra]

    # A separate descriptor keeps heartbeat lines out of tests' capsys/capfd capture.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1) as console, \
            log_path.open("x", buffering=1) as log:
        link = log_dir / f".latest-{os.getpid()}"
        link.symlink_to(log_path.name)
        link.replace(log_dir / "latest.log")
        progress = Progress(console, args.interval)
        progress.line(f"Starting pytest with {args.workers} test processes ({'serial' if not args.workers else 'grouped by file'}).")
        progress.line(f"Full log: {log_path}")
        progress.line(f"Follow live: tail -f '{log_path}'")
        progress.thread.start()
        try:
            with log_output(log):
                code = int(pytest.main(pytest_args, plugins=[progress]))
        finally:
            progress.stop.set()
            progress.thread.join()
        progress.summary()
        result = "FAIL" if code else ("PASS" if progress.completed else "NO TESTS RUN")
        progress.line(f"{result} (pytest exit {code}). Full log: {log_path}")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
