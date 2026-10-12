"""Shared helpers for tests/test_daily_*.py. Holds no tests.

`daily_io` (autouse in each daily test file) makes `amem daily` offline and local:
- `gh` is never run: `cli_cloud.run_command` answers 404 unless a test sets a fake.
- ntfy and osascript never run: `daily.http_post` / `daily.mac_notify` record any
  unexpected call, and the test fails at teardown if one happened.
- notify.env, the routines log and the state file all point into tmp_path.
"""

from __future__ import annotations

import base64
import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agent_memory import cli_cloud, daily

CLOUD_LOG = (
    "2026-10-12T05:00:03+0000 morning-briefing PASS sections=3 42s report=/root/reports/briefing.md\n"
    "2026-10-12T05:10:00+0000 cloud-audit FAIL pass=9 fail=1 3s\n"
    "2026-10-12T05:10:00+0000 cloud-audit-fail token scope too wide\n"
)

# routines.log house format, as written by agent-config lib/routine.py and amem daily.
LOG_LINE_RE = (
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} amem-daily (PASS|FAIL) "
    r"scored=\d+ escalations=\d+ new=\d+ push=(none|phone|mac) \d+s"
)


def _done(argv: list[str], code: int, out: str, err: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, out, err)


def gh_contents(text: str) -> str:
    encoded = base64.encodebytes(text.encode()).decode()
    return json.dumps({"type": "file", "encoding": "base64", "size": len(text), "content": encoded})


class FakeGh:
    """Replays one `gh api` answer for every call (GET only); records argv."""

    def __init__(self, code: int, out: str, err: str = "") -> None:
        self.answer = (code, out, err)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["gh", "api"], argv
        assert "-X" not in argv and "--method" not in argv
        self.calls.append(argv)
        return _done(argv, *self.answer)


@dataclass
class Push:
    """Programmable ntfy + osascript stand-ins."""

    status: int | None = 200  # None: http_post raises
    posts: list[dict[str, Any]] = field(default_factory=list)
    macs: list[tuple[str, str]] = field(default_factory=list)

    def http_post(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.posts.append({"url": url, "body": body.decode(), "headers": dict(headers)})
        if self.status is None:
            raise OSError("network down")
        return self.status

    def mac_notify(self, title: str, body: str) -> None:
        self.macs.append((title, body))


@dataclass
class DailyIO:
    tmp: Path
    routines_log: Path
    state: Path
    notify_env: Path
    unexpected: list[str]
    monkeypatch: pytest.MonkeyPatch

    def use_push(self, push: Push) -> Push:
        self.monkeypatch.setattr(daily, "http_post", push.http_post)
        self.monkeypatch.setattr(daily, "mac_notify", push.mac_notify)
        return push

    def use_gh(self, fake: FakeGh) -> FakeGh:
        self.monkeypatch.setattr(cli_cloud, "run_command", fake)
        return fake

    def log_lines(self) -> list[str]:
        return self.routines_log.read_text().splitlines() if self.routines_log.exists() else []

    def pushed_ids(self) -> list[str]:
        return json.loads(self.state.read_text())["pushed_run_ids"]


@pytest.fixture
def daily_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DailyIO]:
    unexpected: list[str] = []
    routines_log = tmp_path / "parent" / "routines.log"
    state = tmp_path / "parent" / "amem-daily-state.json"
    notify_env = tmp_path / "parent" / "notify.env"  # absent unless a test writes it
    monkeypatch.setenv("AMEM_ROUTINES_LOG", str(routines_log))
    monkeypatch.setenv("AMEM_DAILY_STATE", str(state))
    for name in ("AMEM_SUPABASE_URL", "AMEM_SUPABASE_KEY", "AMEM_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(daily, "NOTIFY_ENV", notify_env)
    monkeypatch.setattr(daily, "http_post", lambda *a, **k: unexpected.append("http_post") or 599)
    monkeypatch.setattr(daily, "mac_notify", lambda *a, **k: unexpected.append("mac_notify"))
    monkeypatch.setattr(cli_cloud, "run_command", FakeGh(1, '{"message":"Not Found"}', "gh: Not Found (HTTP 404)"))
    yield DailyIO(tmp_path, routines_log, state, notify_env, unexpected, monkeypatch)
    assert unexpected == [], f"unexpected push calls: {unexpected}"
