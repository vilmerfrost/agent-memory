"""`amem daily` pieces that need no database: push, message, state planning, log
lines, and the launchd plist. No network: ntfy and osascript are stand-ins."""

from __future__ import annotations

import plistlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_memory import cli, daily, routines_log
from test_daily_support import LOG_LINE_RE, DailyIO, Push, daily_io  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
PLIST = REPO / "deploy" / "launchd" / "com.vilmerfrost.amem-daily.plist"
TS = datetime(2026, 10, 13, 9, 20, 4, tzinfo=timezone(timedelta(hours=2)))


@pytest.fixture(autouse=True)
def _io(daily_io: DailyIO) -> DailyIO:  # noqa: F811
    return daily_io


# ---------------------------------------------------------------- send


def test_send_ntfy_200_is_phone(daily_io: DailyIO) -> None:  # noqa: F811
    push = daily_io.use_push(Push(status=200))
    assert daily.send("T", "B", {"NTFY_TOPIC": "topic-x"}) == "phone"
    assert push.posts == [{"url": "https://ntfy.sh/topic-x", "body": "B",
                           "headers": {"Title": "T", "Priority": "default", "Tags": "label"}}]
    assert push.macs == []


@pytest.mark.parametrize("status", [500, 403, None])
def test_send_non_200_or_error_falls_back_to_mac(daily_io: DailyIO, status: int | None) -> None:  # noqa: F811
    push = daily_io.use_push(Push(status=status))
    assert daily.send("T", "B", {"NTFY_TOPIC": "topic-x", "NTFY_SERVER": "https://n.example/"}) == "mac"
    assert [p["url"] for p in push.posts] == ["https://n.example/topic-x"]
    assert push.macs == [("T", "B")]


def test_send_without_topic_uses_mac_only(daily_io: DailyIO) -> None:  # noqa: F811
    push = daily_io.use_push(Push())
    assert daily.send("T", "B", {}) == "mac"
    assert push.posts == [] and push.macs == [("T", "B")]


def test_notify_env_reads_only_topic_and_server(daily_io: DailyIO) -> None:  # noqa: F811
    daily_io.notify_env.parent.mkdir(parents=True)
    daily_io.notify_env.write_text('# comment\nNTFY_TOPIC="abc"\nOTHER_SECRET=zzz\nNTFY_SERVER=https://s.example\n')
    assert daily.read_notify_env(daily_io.notify_env) == {"NTFY_TOPIC": "abc", "NTFY_SERVER": "https://s.example"}
    push = daily_io.use_push(Push(status=200))
    assert daily.send("T", "B") == "phone"  # notify=None reads daily.NOTIFY_ENV
    assert push.posts[0]["url"] == "https://s.example/abc"
    assert daily.read_notify_env(daily_io.tmp / "missing.env") == {}


# ---------------------------------------------------------------- message + plan


def test_message_names_counts_and_routines_only() -> None:
    rows = [
        {"run_id": "11111111-1111-1111-1111-111111111111", "routine": "config-check",
         "summary": "SECRET-SUMMARY", "failures": ["SECRET-FAILURE"], "answers": {"verdict": "needs_action"}},
        {"run_id": "22222222-2222-2222-2222-222222222222", "routine": "config-check",
         "summary": "x", "failures": []},
        {"run_id": "33333333-3333-3333-3333-333333333333", "routine": "routine-audit",
         "summary": "y", "failures": []},
    ]
    title, body = daily.message(rows, 5)
    assert title == "amem: 3 new runs to label"
    assert body.startswith("3 new escalations (5 open): config-check 2, routine-audit 1.")
    for secret in ("SECRET", "1111", "2222", "3333", "needs_action"):
        assert secret not in title and secret not in body


def test_plan_push_new_ids_and_pruning() -> None:
    rows = [{"run_id": "a"}, {"run_id": "b"}, {"run_id": "c"}]
    new, kept = daily.plan_push(rows, {"a", "gone"})
    assert [r["run_id"] for r in new] == ["b", "c"]
    assert kept == {"a"}  # "gone" is no longer open: pruned
    assert daily.plan_push([], {"a"}) == ([], set())


# ---------------------------------------------------------------- log lines


def test_log_lines_house_format_and_parse_back() -> None:
    result = daily.DailyResult(scored=4, escalations=2, new=1, push="phone")
    result.fail("score t-other: 1 Jev error; first: boom\nsecond line")
    lines = daily.log_lines(TS.strftime(daily.TS_FORMAT), result, 7)
    assert lines == [
        "2026-10-13T09:20:04+0200 amem-daily FAIL scored=4 escalations=2 new=1 push=phone 7s",
        "2026-10-13T09:20:04+0200 amem-daily-fail score t-other: 1 Jev error; first: boom second line",
    ]
    assert re.fullmatch(LOG_LINE_RE, lines[0])
    parsed = routines_log.parse_lines(lines)
    assert parsed.unparsed == []
    (run,) = parsed.runs
    assert (run.routine, run.status, run.started_at) == ("amem-daily", "fail", TS)
    assert run.meta["scored"] == 4 and run.meta["escalations"] == 2 and run.meta["new"] == 1
    assert run.meta["push"] == "phone" and run.meta["duration_s"] == 7
    assert run.meta["failures"] == ["score t-other: 1 Jev error; first: boom second line"]


def test_cli_registers_daily() -> None:
    args = cli.build_parser().parse_args(["daily", "--no-push", "--routines-log", "/tmp/x.log", "--state", "/tmp/s.json"])
    assert args.no_push is True and args.routines_log == Path("/tmp/x.log") and args.state == Path("/tmp/s.json")
    defaults = cli.build_parser().parse_args(["daily"])
    assert defaults.no_push is False and defaults.routines_log is None and defaults.state is None


def test_paths_env_overrides(daily_io: DailyIO, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    assert daily.routines_log_path() == daily_io.routines_log
    assert daily.state_path() == daily_io.state
    assert daily.routines_log_path(Path("/tmp/a.log")) == Path("/tmp/a.log")
    monkeypatch.delenv("AMEM_ROUTINES_LOG")
    monkeypatch.delenv("AMEM_DAILY_STATE")
    assert daily.routines_log_path() == Path.home() / ".claude" / "parent" / "routines.log"
    assert daily.state_path() == Path.home() / ".claude" / "parent" / "amem-daily-state.json"


# ---------------------------------------------------------------- launchd plist


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil not installed")
def test_plist_lints() -> None:
    result = subprocess.run(["plutil", "-lint", str(PLIST)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_plist_content() -> None:
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "com.vilmerfrost.amem-daily"
    assert data["ProgramArguments"] == [
        "/opt/homebrew/bin/uv", "run", "--directory", "/Users/vilmerfrost/Projects/agent-memory",
        "--quiet", "amem", "daily",
    ]
    env = data["EnvironmentVariables"]
    assert env["AMEM_REQUIRE_REMOTE"] == "1" and env["AMEM_JEV_LIVE"] == "1"
    assert "/Users/vilmerfrost/.local/bin" in env["PATH"].split(":")  # gh lives there; launchd's PATH lacks it
    assert data["StartCalendarInterval"] == {"Hour": 9, "Minute": 20}
    assert data["StandardOutPath"] == data["StandardErrorPath"] == "/Users/vilmerfrost/.claude/parent/reports/launchd.log"
    assert data["RunAtLoad"] is False
