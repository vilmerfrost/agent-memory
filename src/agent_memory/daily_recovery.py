"""Durable, bounded recovery of an auth-blocked daily scan and its missed night."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path

INTERVAL = timedelta(minutes=15)
MAX_ATTEMPTS = 40


@contextmanager
def job_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def path_for(state: Path) -> Path:
    return state.with_name(state.stem + "-auth-recovery.json")


def read(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("invalid auth recovery state")
    return data


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as fh:
            os.chmod(tmp, 0o600)
            json.dump(data, fh, sort_keys=True)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def record(result, *, state: Path, now: datetime, push: bool, notify=None) -> None:
    from agent_memory import daily, rsi_approval
    path = path_for(state)
    data = read(path)
    night = rsi_approval.night_key(now)
    if result.auth_failed:
        if data.get("night") != night:
            # The prerequisite gate cannot accept this night after local noon.
            deadline = datetime.fromisoformat(night).replace(tzinfo=now.tzinfo) + timedelta(days=1, hours=12)
            data = {"night": night, "started_at": now.isoformat(), "deadline": deadline.isoformat(),
                    "attempts": 0, "daily_done": False, "status": "pending"}
        data["daily_done"] = False
        data["status"] = "pending"
        if push and not data.get("alerted"):
            delivery = daily.send("Claude automation needs authentication",
                                  "Daily scan paused. Renew the dedicated Claude automation token. "
                                  "Unfinished work is saved; recovery checks every 15 minutes until noon.", notify)
            data["alert_delivery"] = delivery  # Mac delivery is best effort.
            data["alerted"] = True
        write(path, data)
    elif data.get("status") == "pending" and result.verdict == "PASS":
        data["daily_done"] = True
        write(path, data)


def recover(*, state: Path, routines_log: Path, now: datetime | None = None,
            config_path: Path | None = None, push: bool = True, notify=None) -> str:
    from agent_memory import config, daily, rsi_schedule
    now = now or datetime.now().astimezone()
    path = path_for(state)
    with job_lock(path.with_suffix(".lock")) as acquired:
        if not acquired:
            return "busy"
        data = read(path)
        if data.get("status") != "pending":
            return "idle"
        if now >= datetime.fromisoformat(data["deadline"]) or data["attempts"] >= MAX_ATTEMPTS:
            data["status"] = "expired"
            write(path, data)
            if push:
                daily.send("Claude recovery window ended",
                           "The missed night remains incomplete. Pending sessions are preserved for the next daily scan.", notify)
            return "expired"
        last = data.get("last_attempt")
        if last and now - datetime.fromisoformat(last) < INTERVAL:
            return "waiting"
        # A normal daily run owns this marker. Never run over it or consume an attempt.
        if daily.RL.running(routines_log, daily.NAME, now):
            return "busy"
        data["attempts"] += 1
        data["last_attempt"] = now.isoformat()
        write(path, data)
        if not data.get("daily_done"):
            result = daily.run_daily(state=state, routines_log=routines_log, now=now, push=push, notify=notify)
            if result.verdict != "PASS":
                return "daily_failed"
            data = read(path)
            data["daily_done"] = True
            write(path, data)
        # Before 01:30, leave the regular night job to start it at its normal time.
        if (now.hour, now.minute) < (1, 30):
            return "waiting_for_night"
        if not data.get("night_done"):
            job = rsi_schedule.run_night_job(
                config_path=config_path or config.REPO_ROOT / "config/rsi-night.subscription.json",
                night=data["night"], routines_log=routines_log, now=now)
            # Overlap, missing prerequisites and credentials remain pending. A real
            # completed night (including partial results) must never be rerun here.
            if job.fields.get("status") not in ("ok", "partial", "budget_exhausted", "aborted") and not (
                job.status == "SKIP" and any("already done:" in w for w in job.warnings)
            ):
                return "night_pending"
            data["night_done"] = True
            write(path, data)
        if (now.hour, now.minute) >= (7, 30):
            brief = rsi_schedule.run_brief_job(night=data["night"], now=now, routines_log=routines_log,
                                             out_dir=routines_log.parent / "reports", state=state, push=False)
            if brief.status != "PASS":
                return "brief_pending"
        data["status"] = "complete"
        data["finished_at"] = now.isoformat()
        write(path, data)
        if push:
            daily.send("Claude automation recovered", "The daily scan completed and the missed night was processed. Check the night report for results.", notify)
        return "complete"
