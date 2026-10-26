"""The 2026-10-26 schedule: amem-daily at 00:40 (no edits step, no push), the 01:30 night's gate on that same
night's daily line (it waits while amem-daily still runs), and the 09:20 outcome check plist.

No model, no hosted database, nothing installed. The running marker uses this test process's own pid (alive) or a
pid that cannot exist (dead)."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_memory import alerts, config, daily, routines_log as RL, rsi_schedule as S

CEST = timezone(timedelta(hours=2))
NIGHT = "2026-10-18"
NOW = datetime(2026, 10, 19, 1, 30, 5, tzinfo=CEST)
LAUNCHD = config.REPO_ROOT / "deploy" / "launchd"
BASE = """\
2026-10-18T08:45:09+0200 amem-archive PASS heartbeat 5s
2026-10-19T00:09:12+0200 amem-ingest PASS heartbeat 4s
"""
PREV_DAILY = "2026-10-18T00:40:03+0200 amem-daily PASS scored=2 escalations=0 new=0 push=none tasks=0 300s\n"
TONIGHT = "2026-10-19T00:40:04+0200 amem-daily PASS scored=16 escalations=14 new=0 push=none tasks=10 1900s\n"
DEAD_PID = 2_000_000_000


def load_plist(name: str) -> dict[str, Any]:
    return plistlib.loads((LAUNCHD / name).read_bytes())


def lint(name: str) -> None:
    if shutil.which("plutil"):
        r = subprocess.run(["plutil", "-lint", str(LAUNCHD / name)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.strip().endswith(": OK"), r.stdout + r.stderr


# --------------------------------------------------------------------------- plists


def test_daily_runs_at_0040_before_the_night_without_the_edits_step() -> None:
    lint("com.vilmerfrost.amem-daily.plist")
    d, night = load_plist("com.vilmerfrost.amem-daily.plist"), load_plist("com.vilmerfrost.amem-rsi-night.plist")
    assert d["StartCalendarInterval"] == {"Hour": 0, "Minute": 40} and d["RunAtLoad"] is False
    env = d["EnvironmentVariables"]
    for k in ("AMEM_EDITS_LIVE", "AMEM_EDITS_MODE", daily.DAILY_EDITS_ENV):    # no morning edits step
        assert k not in env, k
    assert env["AMEM_DEVIATIONS_LIVE"] == "1" and env["AMEM_REQUIRE_REMOTE"] == "1"
    start = d["StartCalendarInterval"]["Hour"] * 60 + d["StartCalendarInterval"]["Minute"]
    n = night["StartCalendarInterval"]["Hour"] * 60 + night["StartCalendarInterval"]["Minute"]
    assert start < n and n - start + S.DAILY_WAIT_S / 60 >= 60   # 50 min ahead + 30 min wait covers a long scan
    assert S.run_night_key(datetime(2026, 10, 19, 0, 40, tzinfo=CEST)) == NIGHT   # its line carries the night's key


def test_outcome_plist_runs_the_check_at_0920_read_only_toward_rules() -> None:
    name = "com.vilmerfrost.amem-rsi-outcome.plist"
    lint(name)
    p = load_plist(name)
    assert p["Label"] == name.removesuffix(".plist") and p["RunAtLoad"] is False and "KeepAlive" not in p
    assert p["StartCalendarInterval"] == {"Hour": 9, "Minute": 20} and p["StandardInPath"] == "/dev/null"
    args = p["ProgramArguments"]
    assert args[:8] == ["/opt/homebrew/bin/uv", "run", "--directory", "/Users/vilmerfrost/Projects/agent-memory",
                        "--quiet", "amem", "rsi", "outcome"]
    assert not {"apply", "promote", "decide", "undo", "--push"} & set(args)
    env = p["EnvironmentVariables"]
    assert env["AMEM_HOOK_OFF"] == "1" and env["AMEM_REQUIRE_REMOTE"] == "1"
    for k in env:
        assert not any(s in k for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "DB_URL", "EDITS")), k


# --------------------------------------------------------------------------- the gate waits for a running daily


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def log_with(tmp: Path, text: str) -> Path:
    p = tmp / "routines.log"
    p.write_text(text)
    return p


def test_gate_passes_at_once_when_tonights_daily_is_done(tmp_path: Path) -> None:
    log = log_with(tmp_path, BASE + PREV_DAILY + TONIGHT)
    RL.mark_running(log, "amem-daily", NOW - timedelta(hours=1))     # a live marker does not matter once it passed
    clock = Clock()
    g = S.wait_for_prerequisites(log, NIGHT, NOW, S.DEFAULT_PREREQS, sleep=clock.sleep, clock=clock)
    assert g.reasons == [] and clock.slept == [] and g.waited_s == 0


def test_gate_waits_while_daily_runs_then_uses_its_new_line(tmp_path: Path) -> None:
    log = log_with(tmp_path, BASE + PREV_DAILY)
    RL.mark_running(log, "amem-daily", datetime(2026, 10, 19, 0, 40, 4, tzinfo=CEST))
    clock = Clock()

    def sleep(s: float) -> None:
        clock.sleep(s)
        if len(clock.slept) == 3:                       # daily finishes after 3 minutes of waiting
            with log.open("a") as fh:
                fh.write(TONIGHT)
            RL.clear_running(log, "amem-daily")
    g = S.wait_for_prerequisites(log, NIGHT, NOW, S.DEFAULT_PREREQS, poll_s=60, sleep=sleep, clock=clock)
    assert g.reasons == [] and clock.slept == [60, 60, 60] and g.waited_s == 180


def test_gate_skips_when_daily_still_runs_after_30_minutes(tmp_path: Path) -> None:
    log = log_with(tmp_path, BASE + PREV_DAILY)
    RL.mark_running(log, "amem-daily", datetime(2026, 10, 19, 0, 40, 4, tzinfo=CEST))
    clock = Clock()
    g = S.wait_for_prerequisites(log, NIGHT, NOW, S.DEFAULT_PREREQS, poll_s=60, sleep=clock.sleep, clock=clock)
    assert sum(clock.slept) == S.DAILY_WAIT_S == 1800 and len(clock.slept) == 30
    assert g.reasons == [f"prerequisite amem-daily: still running after 30 min (started 2026-10-19T00:40:04+0200, "
                         f"pid {os.getpid()}); the night waited and did not run"]
    assert g.causes == ["prerequisite amem-daily was still running after the night waited 30 min"]
    assert g.routines == ["amem-daily"]


def test_gate_skips_at_once_when_daily_failed_or_is_not_running(tmp_path: Path) -> None:
    fail = ("2026-10-19T00:40:04+0200 amem-daily FAIL scored=1 600s\n"
            "2026-10-19T00:40:04+0200 amem-daily-fail deviations: 9 errors in 12 sessions; first: session x: boom\n")
    clock = Clock()
    g = S.wait_for_prerequisites(log_with(tmp_path, BASE + PREV_DAILY + fail), NIGHT, NOW, S.DEFAULT_PREREQS,
                                 sleep=clock.sleep, clock=clock)
    assert g.reasons == ["prerequisite amem-daily: last run FAIL at 2026-10-19T00:40:04+0200 "
                         "(deviations: 9 errors in 12 sessions; first: session x: boom)"] and clock.slept == []
    # it fails while the night waits: the night stops waiting and skips on that FAIL
    log = log_with(tmp_path, BASE + PREV_DAILY)
    RL.mark_running(log, "amem-daily", datetime(2026, 10, 19, 0, 40, 4, tzinfo=CEST))

    def sleep(s: float) -> None:
        clock.sleep(s)
        with log.open("a") as fh:
            fh.write(fail)
        RL.clear_running(log, "amem-daily")
    g = S.wait_for_prerequisites(log, NIGHT, NOW, S.DEFAULT_PREREQS, sleep=sleep, clock=clock)
    assert g.reasons[0].startswith("prerequisite amem-daily: last run FAIL at 2026-10-19T00:40:04+0200")
    assert len(clock.slept) == 1
    # a marker left by a crashed run (its pid is gone) is not "running": no wait, the stale line decides
    log = log_with(tmp_path, BASE + PREV_DAILY)
    RL.mark_running(log, "amem-daily", datetime(2026, 10, 19, 0, 40, 4, tzinfo=CEST), pid=DEAD_PID)
    clock = Clock()
    g = S.wait_for_prerequisites(log, NIGHT, NOW, S.DEFAULT_PREREQS, sleep=clock.sleep, clock=clock)
    assert g.reasons == ["prerequisite amem-daily: no PASS for night 2026-10-18 (last PASS 2026-10-18T00:40:03+0200)"]
    assert clock.slept == []
    # so is a marker older than RL.RUNNING_MAX_AGE_S, and an unreadable one
    RL.mark_running(log, "amem-daily", NOW - timedelta(hours=7))
    assert RL.running(log, "amem-daily", NOW) is None
    RL.running_marker(log, "amem-daily").write_text("{not json")
    assert RL.running(log, "amem-daily", NOW) is None
    # a prerequisite without wait_s never waits, even with a live marker
    RL.mark_running(log, "amem-archive", NOW - timedelta(minutes=5))
    arch_missing = log_with(tmp_path, "2026-10-19T00:09:12+0200 amem-ingest PASS heartbeat 4s\n" + TONIGHT)
    g = S.wait_for_prerequisites(arch_missing, NIGHT, NOW, S.DEFAULT_PREREQS, sleep=clock.sleep, clock=clock)
    assert g.reasons == ["prerequisite amem-archive: no run in routines.log"] and clock.slept == []


def test_night_job_logs_the_still_running_skip_and_the_brief_names_it(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import rsi_night as N
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path / "sandbox"))
    log = log_with(tmp_path, BASE + PREV_DAILY)
    RL.mark_running(log, "amem-daily", datetime(2026, 10, 19, 0, 40, 4, tzinfo=CEST))
    clock = Clock()
    res = S.run_night_job(night=NIGHT, now=NOW, routines_log=log, watchdog=False, sleep=clock.sleep, clock=clock)
    assert res.status == "SKIP" and res.fields["skipped"] == "prerequisites" and res.fields["waited_s"] == 1800
    assert res.items[0].startswith("prerequisite amem-daily: still running after 30 min")
    (run,) = [r for r in RL.parse_file(log).runs if r.routine == "amem-rsi-night"]
    assert run.status == "skip" and run.meta["waited_s"] == 1800
    rep = json.loads(N.report_path(NIGHT).read_text())
    assert rep["skipped"]["summary"] == "prerequisite amem-daily was still running after the night waited 30 min"
    msg = alerts.night_message({"night": NIGHT, "status": rep["status"], "notes": rep["notes"],
                                "skipped": rep["skipped"]})
    assert msg is not None and "Skipped: the deviation scan (amem-daily) was still running." in msg[1]


# --------------------------------------------------------------------------- amem-daily keeps the marker


def test_daily_writes_its_running_marker_and_removes_it_after_the_line(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "parent" / "routines.log"
    seen: list[Any] = []
    monkeypatch.setattr(daily, "wait_for_db", lambda *a, **k: seen.append(RL.running(log, "amem-daily", NOW)) or True)
    for step in ("step_ingest", "step_tasks", "step_score", "step_distill", "step_escalations", "step_deviations",
                 "step_edits", "step_rsi_push", "step_sync"):
        monkeypatch.setattr(daily, step, lambda *a, **k: None)
    r = daily.run_daily(routines_log=log, state=tmp_path / "state.json", now=NOW - timedelta(minutes=50))
    assert seen[0] is not None and seen[0]["pid"] == os.getpid() and seen[0]["started_at"] == "2026-10-19T00:40:05+0200"
    assert not RL.running_marker(log, "amem-daily").exists() and r.lines[0] in log.read_text()

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("step crashed")
    monkeypatch.setattr(daily, "step_sync", boom)
    with pytest.raises(RuntimeError):
        daily.run_daily(routines_log=log, state=tmp_path / "state.json", now=NOW)
    assert not RL.running_marker(log, "amem-daily").exists()           # a crash never leaves the night waiting
