"""`amem daily` step 3d (2026-10-24): one bounded `sync run` per configured, switched-on sink, at most once per day.

The sinks, the database and `sync.run` are stand-ins: nothing here reaches ClickHouse, Langfuse or past.dev.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_memory import cli_integrations, config, daily, rsi_schedule, store
from agent_memory.integrations import sync

NOW = datetime(2026, 10, 24, 9, 20, 4, tzinfo=timezone(timedelta(hours=2)))
CONFIGURED = {
    "AMEM_CLICKHOUSE_URL": "https://abc.eu-central-1.aws.clickhouse.cloud:8443", "AMEM_CLICKHOUSE_USER": "amem_writer",
    "AMEM_CLICKHOUSE_KEY": "test-only-ch-key", "AMEM_LANGFUSE_HOST": "https://cloud.langfuse.com",
    "AMEM_LANGFUSE_PUBLIC_KEY": "pk-lf-test", "AMEM_LANGFUSE_SECRET_KEY": "sk-lf-test",
    "AMEM_PASTDEV_KEY": "past_sk_test_0123456789"}


class Sinks:
    """sink_for / sync.run stand-ins: records each run; `outcome[name]` is a status or an exception to raise."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, float]] = []
        self.outcome: dict[str, Any] = {}

    def sink_for(self, name: str, *a: Any, **k: Any) -> Any:
        return type("S", (), {"name": name})()

    def run(self, sink: Any, conn: Any, *, budget_s: float = 60.0, **k: Any) -> sync.SyncReport:
        self.runs.append((sink.name, budget_s))
        out = self.outcome.get(sink.name, "ok")
        if isinstance(out, BaseException):
            raise out
        detail = "provider cap: HTTP 402" if out == "capped" else ("health: down" if out == "unavailable" else "")
        return sync.SyncReport(sink.name, status=out, rows=3, detail=detail)


@pytest.fixture
def sinks(monkeypatch: pytest.MonkeyPatch) -> Iterator[Sinks]:
    s = Sinks()
    monkeypatch.setattr(cli_integrations, "sink_for", s.sink_for)
    monkeypatch.setattr(sync, "run", s.run)
    monkeypatch.setattr(store, "connect", lambda *a, **k: contextlib.nullcontext(object()))
    monkeypatch.setattr(config, "backend", lambda: "postgres")
    monkeypatch.setattr(config, "remote_write_error", lambda **k: None)
    for k, v in CONFIGURED.items():
        monkeypatch.setenv(k, v)
    for k in ("AMEM_SYNC_CLICKHOUSE", "AMEM_SYNC_LANGFUSE", "AMEM_SYNC_PASTDEV", "AMEM_PASTDEV"):
        monkeypatch.delenv(k, raising=False)
    yield s


def test_sync_configured_reads_only_the_env() -> None:
    assert all(daily.sync_configured(n, CONFIGURED) for n in sync.SINKS)
    assert not any(daily.sync_configured(n, {}) for n in sync.SINKS)
    assert not daily.sync_configured("clickhouse", {**CONFIGURED, "AMEM_CLICKHOUSE_URL": "http://ch.example.com"})
    assert not daily.sync_configured("pastdev", {"AMEM_PASTDEV_KEY": "past_mk_management0123456789"})
    assert not daily.sync_configured("ncm", CONFIGURED)


def test_every_configured_sink_syncs_once_a_day(sinks: Sinks, tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    r = daily.DailyResult()
    daily.step_sync(r, state_file=state, now=NOW)
    assert sinks.runs == [(n, daily.SYNC_BUDGET_S) for n in ("clickhouse", "langfuse", "pastdev")]
    assert (r.failures, r.warnings) == ([], []) and [x.sink for x in r.synced] == list(sync.SINKS)
    assert json.loads(state.read_text())[daily.SYNC_KEY] == "2026-10-24"
    r2 = daily.DailyResult()
    daily.step_sync(r2, state_file=state, now=NOW + timedelta(hours=5))        # same day: not due
    assert len(sinks.runs) == 3 and r2.synced is None
    daily.step_sync(daily.DailyResult(), state_file=state, now=NOW + timedelta(days=1))
    assert len(sinks.runs) == 6                                                 # the next day: again
    (line,) = daily.log_lines("ts", r, 1)[:1]
    assert "sync=clickhouse:ok,langfuse:ok,pastdev:ok sync_rows=9 " in line


def test_switched_off_and_unconfigured_sinks_are_skips(sinks: Sinks, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_SYNC_LANGFUSE", "off")
    monkeypatch.delenv("AMEM_PASTDEV_KEY")
    r = daily.DailyResult()
    daily.step_sync(r, state_file=tmp_path / "s.json", now=NOW)
    assert [n for n, _ in sinks.runs] == ["clickhouse"] and (r.failures, r.warnings) == ([], [])
    monkeypatch.setenv("AMEM_PASTDEV_KEY", CONFIGURED["AMEM_PASTDEV_KEY"])
    monkeypatch.setenv("AMEM_PASTDEV", "off")                                   # past.dev's own kill switch
    monkeypatch.delenv("AMEM_CLICKHOUSE_URL")
    r = daily.DailyResult()
    daily.step_sync(r, state_file=tmp_path / "t.json", now=NOW)
    assert [n for n, _ in sinks.runs] == ["clickhouse"] and r.synced is None   # nothing due: no fields, no state
    assert not (tmp_path / "t.json").exists()
    assert daily.log_lines("ts", r, 1)[0] == "ts amem-daily PASS scored=0 escalations=0 new=0 push=none tasks=0 1s"


def test_a_sync_failure_is_a_warning_and_never_blocks_the_night(sinks: Sinks, tmp_path: Path) -> None:
    sinks.outcome = {"clickhouse": RuntimeError("boom"), "langfuse": "unavailable", "pastdev": "capped"}
    r = daily.DailyResult()
    daily.step_sync(r, state_file=tmp_path / "s.json", now=NOW)
    assert r.failures == [] and r.verdict == "PASS" and len(sinks.runs) == 3
    assert r.warnings == ["sync clickhouse: RuntimeError: boom", "sync langfuse: unavailable (health: down)",
                          "sync pastdev: capped (provider cap: HTTP 402)"]
    log = tmp_path / "routines.log"
    log.write_text("".join(f"{x}\n" for x in daily.log_lines(NOW.strftime(daily.TS_FORMAT), r, 7)))
    assert all("amem-daily-fail" not in x for x in log.read_text().splitlines())
    daily_prereq = rsi_schedule.DEFAULT_PREREQS[0]
    assert daily_prereq.routine == "amem-daily" and "sync" not in daily_prereq.nonblocking_steps
    gate = rsi_schedule.evaluate_prerequisites(log, "2026-10-24", NOW + timedelta(hours=1), [daily_prereq])
    assert gate.reasons == []                                                   # the night may run


def test_a_database_that_is_not_postgres_is_a_warning(sinks: Sinks, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "backend", lambda: "sqlite")
    r = daily.DailyResult()
    daily.step_sync(r, state_file=tmp_path / "s.json", now=NOW)
    assert sinks.runs == [] and r.failures == [] and r.warnings and r.warnings[0].startswith("sync: needs the Postgres")


def test_run_daily_runs_sync_last(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    order: list[str] = []
    for name in ("wait_for_db", "step_ingest", "step_tasks", "step_score", "step_distill", "step_escalations",
                 "step_deviations", "step_edits", "step_rsi_push", "step_sync"):
        monkeypatch.setattr(daily, name, lambda *a, _n=name, **k: order.append(_n))
    daily.run_daily(routines_log=tmp_path / "routines.log", state=tmp_path / "s.json", now=NOW, notify={})
    assert order[-1] == "step_sync" and order.index("step_deviations") < order.index("step_sync")
