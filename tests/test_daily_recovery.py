from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_memory import daily, daily_recovery as recovery, rsi_schedule

NOW = datetime.fromisoformat("2026-10-29T02:00:00+01:00")


@pytest.fixture
def pending(tmp_path, monkeypatch):
    state, log = tmp_path / "daily.json", tmp_path / "routines.log"
    sends = []
    monkeypatch.setattr(daily, "send", lambda *a: sends.append(a) or "phone")
    recovery.record(daily.DailyResult(auth_failed=True, failures=["deviations: auth"]),
                    state=state, now=NOW, push=True)
    return state, log, sends


def job(status="partial", verdict="FAIL", warnings=None):
    return SimpleNamespace(fields={"status": status}, status=verdict, warnings=warnings or [])


def test_alert_once_and_preserve_attempts(pending):
    state, _, sends = pending
    data = recovery.read(recovery.path_for(state))
    data["attempts"] = 3
    recovery.write(recovery.path_for(state), data)
    recovery.record(daily.DailyResult(auth_failed=True), state=state, now=NOW, push=True)
    assert len(sends) == 1
    assert recovery.read(recovery.path_for(state))["attempts"] == 3


def test_recovery_retries_daily_then_night_once(pending, monkeypatch):
    state, log, sends = pending
    calls = []
    monkeypatch.setattr(daily, "run_daily", lambda **kw: calls.append("daily") or daily.DailyResult())
    monkeypatch.setattr(rsi_schedule, "run_night_job", lambda **kw: calls.append(("night", kw["night"])) or job())
    assert recovery.recover(state=state, routines_log=log, now=NOW) == "complete"
    assert calls == ["daily", ("night", "2026-10-28")]
    assert recovery.recover(state=state, routines_log=log, now=NOW + timedelta(minutes=20)) == "idle"
    assert len(sends) == 2


def test_auth_still_broken_keeps_work_pending_and_throttles(pending, monkeypatch):
    state, log, _ = pending
    calls = []
    monkeypatch.setattr(daily, "run_daily", lambda **kw: calls.append(kw) or daily.DailyResult(failures=["deviations: auth"]))
    monkeypatch.setattr(rsi_schedule, "run_night_job", lambda **kw: pytest.fail("night must not run"))
    assert recovery.recover(state=state, routines_log=log, now=NOW) == "daily_failed"
    assert recovery.recover(state=state, routines_log=log, now=NOW + timedelta(minutes=14)) == "waiting"
    assert len(calls) == 1


@pytest.mark.parametrize("limit", ["deadline", "attempts"])
def test_recovery_is_bounded(pending, monkeypatch, limit):
    state, log, _ = pending
    if limit == "attempts":
        data = recovery.read(recovery.path_for(state))
        data["attempts"] = recovery.MAX_ATTEMPTS
        recovery.write(recovery.path_for(state), data)
    monkeypatch.setattr(daily, "run_daily", lambda **kw: pytest.fail("expired job must not run"))
    now = NOW.replace(hour=12) if limit == "deadline" else NOW
    assert recovery.recover(state=state, routines_log=log, now=now) == "expired"
    assert recovery.recover(state=state, routines_log=log, now=now) == "idle"


def test_daily_marker_and_file_lock_prevent_overlap(pending, monkeypatch):
    state, log, _ = pending
    daily.RL.mark_running(log, daily.NAME, NOW)
    assert recovery.recover(state=state, routines_log=log, now=NOW) == "busy"
    assert recovery.read(recovery.path_for(state))["attempts"] == 0
    daily.RL.clear_running(log, daily.NAME)
    with recovery.job_lock(recovery.path_for(state).with_suffix(".lock")):
        assert recovery.recover(state=state, routines_log=log, now=NOW) == "busy"


def test_other_prerequisite_failure_keeps_night_pending_without_rescanning(pending, monkeypatch):
    state, log, _ = pending
    calls = []
    monkeypatch.setattr(daily, "run_daily", lambda **kw: calls.append("daily") or daily.DailyResult())
    monkeypatch.setattr(rsi_schedule, "run_night_job", lambda **kw: job("preflight_failed", "SKIP"))
    assert recovery.recover(state=state, routines_log=log, now=NOW) == "night_pending"
    assert recovery.recover(state=state, routines_log=log, now=NOW + timedelta(minutes=15)) == "night_pending"
    assert calls == ["daily"]


def test_already_completed_night_is_not_replayed(pending, monkeypatch):
    state, log, _ = pending
    monkeypatch.setattr(daily, "run_daily", lambda **kw: daily.DailyResult())
    monkeypatch.setattr(rsi_schedule, "run_night_job", lambda **kw: job("preflight_failed", "SKIP", ["already done: night finished earlier"]))
    assert recovery.recover(state=state, routines_log=log, now=NOW) == "complete"


def test_late_recovery_refreshes_brief_and_retries_only_brief(pending, monkeypatch):
    state, log, _ = pending
    calls = []
    monkeypatch.setattr(daily, "run_daily", lambda **kw: calls.append("daily") or daily.DailyResult())
    monkeypatch.setattr(rsi_schedule, "run_night_job", lambda **kw: calls.append("night") or job())
    replies = iter(["FAIL", "PASS"])
    def brief(**kw):
        assert kw["push"] is False
        calls.append("brief")
        return SimpleNamespace(status=next(replies))
    monkeypatch.setattr(rsi_schedule, "run_brief_job", brief)
    assert recovery.recover(state=state, routines_log=log, now=NOW.replace(hour=8)) == "brief_pending"
    assert recovery.recover(state=state, routines_log=log, now=NOW.replace(hour=8, minute=15)) == "complete"
    assert calls == ["daily", "night", "brief", "brief"]


def test_idle_cli_uses_requested_paths_and_emits_ingestable_status(tmp_path, monkeypatch, capsys):
    from agent_memory import cli, routines_log
    monkeypatch.setattr(daily, "run_daily", lambda **kw: pytest.fail("idle recovery must not run daily"))
    state, log = tmp_path / "daily.json", tmp_path / "routines.log"
    assert cli.main(["daily-recover", "--no-push", "--state", str(state), "--routines-log", str(log)]) == 0
    text = log.read_text()
    assert text == capsys.readouterr().out
    rows = routines_log.parse_lines(text.splitlines(keepends=True)).runs
    assert len(rows) == 1 and rows[0].routine == "amem-auth-recovery"
    assert rows[0].status == "pass" and rows[0].meta["status"] == "idle"
