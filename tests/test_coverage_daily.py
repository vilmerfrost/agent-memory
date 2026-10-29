"""amem-daily writes coverage snapshots and consumption records (data-use plan R4, coverage_daily).

The deviations, score and sync steps, end to end against a fresh database per test (test_scoring_support `sdb`):
synthetic transcripts, the fake writer and Jev of the deviations tests, the Jev fixtures of the score tests, and a
fake sink behind the real `sync.run`. Nothing hosted, no model.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import cli, cli_integrations, coverage as CV, coverage_daily as COV, daily, deviations, distill
from agent_memory import questions, scoring
from agent_memory.integrations import sync
from agent_memory.integrations.evidence import Health
from test_daily_support import LOG_LINE_RE, DailyIO, daily_io  # noqa: F401
from test_daily_sync import CONFIGURED
from test_deviations_support import (  # noqa: F401
    FakeJev,
    FakeWriter,
    Home,
    envelope,
    fake_jev,
    finding,
    make_all_kinds,
    setup_home,
    writer,
)
from test_scoring_support import ScoringDb, add_run, sdb  # noqa: F401

NOW = datetime(2026, 10, 15, 9, 20, 0, tzinfo=timezone(timedelta(hours=2)))
KEY = "2026-10-15T09:20:00+0200"
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
INGEST_LINE = re.compile(r"deviations: sessions: (\d+) files, \d+ new, \d+ updated, (\d+) skipped")


@pytest.fixture
def live(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> ScoringDb:  # noqa: F811
    monkeypatch.setenv(deviations.LIVE_ENV, "1")
    monkeypatch.setenv("AMEM_QUESTIONS_DIR", str(questions.QUESTIONS_DIR))  # deviation.toml
    return sdb


def snaps(url: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from coverage_snapshots order by window_end, dataset")
        for r in cur.fetchall():
            out.setdefault(r["dataset"], []).append(r)
    return out


def uses(url: str, job: str = COV.DAILY_JOB) -> dict[str, dict[str, Any]]:
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from routine_consumption where job = %s", (job,))
        rows = cur.fetchall()
    out = {r["dataset"]: r for r in rows}
    assert len(out) == len(rows)
    return out


def counts(url: str) -> tuple[int, int]:
    with psycopg.connect(url) as conn:
        a = conn.execute("select count(*) from coverage_snapshots").fetchone()[0]
        b = conn.execute("select count(*) from routine_consumption").fetchone()[0]
    return a, b


def edge_runs(url: str) -> None:
    """t-routine states with Jev fixtures (four) and one without (left unscored, `missing`)."""
    for i, (status, summary) in enumerate((("fail", "edge equal"), ("fail", "edge below"),
                                           ("pass", "edge score low"), ("pass", "all confident"),
                                           ("fail", "a state nobody recorded"))):
        add_run(url, "t-routine", status, summary, T0 + timedelta(hours=i))


# --------------------------------------------------------------------------- deviations step


def test_deviation_scan_records_match_the_jobs_own_output(live: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                         setup_home: Home, writer: Any, fake_jev: FakeJev,  # noqa: F811
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    make_all_kinds(setup_home)
    writer(default=envelope({"deviations": [finding()]}))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS" and r.coverage_notes == []
    rep = r.deviations
    (line,) = daily_io.log_lines()
    assert " deviations=5 deviation_sessions=5 " in line and "coverage_problems" not in line
    files, skipped = map(int, INGEST_LINE.search(capsys.readouterr().err).groups())

    u = uses(live.url)[COV.SESSIONS]
    assert (u["run_key"], u["status"], u["findings"]) == (KEY, "findings", rep.found)
    assert (u["queried"], u["considered"], u["used"]) == (6, len(rep.outcomes), 5)
    assert u["skipped"] == {"kind_eval": 1}                     # the eval clone: never scanned
    assert u["window_start"] == NOW - timedelta(days=deviations.DEFAULT_DAYS) and u["window_end"] == NOW
    assert u["analysis_version"] == f"deviations-scan:deviation:writer={distill.WRITER_MODEL}"
    assert len(u["evidence_refs"]) == 5 and all(x.startswith("amem:deviation/") for x in u["evidence_refs"])
    with psycopg.connect(live.url) as conn:                     # every ref names a stored deviation
        ids = [x.split("/", 1)[1] for x in u["evidence_refs"]]
        assert conn.execute("select count(*) from deviations where id::text = any(%s)", (ids,)).fetchone()[0] == 5

    s = snaps(live.url)
    (dev,) = s[COV.DEVIATIONS]
    assert (dev["status"], dev["eligible"], dev["collected"], dev["pending"], dev["rejected"]) == ("complete", 5, 5, 0, 0)
    (ses,) = s[COV.SESSIONS]
    assert (ses["status"], ses["eligible"], ses["collected"]) == ("complete", files - skipped, files - skipped)
    assert ses["window_start"] == u["window_start"] and ses["window_end"] == u["window_end"]

    # the guard: findings, never all clear; the datasets the night needs are named the same way
    with psycopg.connect(live.url) as conn:
        v = CV.verdict(conn, job=COV.DAILY_JOB, run_key=KEY, required=[COV.SESSIONS])
    assert (v.conclusion, v.findings, v.gaps) == ("findings", 5, ())
    assert COV.DEVIATIONS == "deviations:deviations" and COV.SESSIONS == "sessions:sessions"


def test_a_rerun_under_the_same_run_key_adds_no_rows(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                                     writer: Any, fake_jev: FakeJev) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    edge_runs(live.url)
    writer(default=envelope({"deviations": [finding()]}))
    daily.run_daily(push=False, now=NOW)
    first = counts(live.url)
    assert first[0] >= 3 and first[1] >= 2
    daily.run_daily(push=False, now=NOW)                        # deviations not due: they record nothing
    assert counts(live.url) == first
    assert uses(live.url)[COV.SESSIONS]["status"] == "findings"  # the run's real record is kept
    daily_io.state.unlink()                                     # due again under the same key: updated in place
    daily.run_daily(push=False, now=NOW)
    assert counts(live.url) == first


def test_backlog_is_not_collected_and_over_limit(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                                 writer: Any, fake_jev: FakeJev,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    make_all_kinds(setup_home)
    writer(default=envelope({"deviations": []}))
    monkeypatch.setattr(daily, "DEVIATIONS_LIMIT", 3)
    r = daily.run_daily(push=False, now=NOW)
    assert len(r.deviations.outcomes) == 3 and r.deviations.found == 0
    (dev,) = snaps(live.url)[COV.DEVIATIONS]
    assert (dev["status"], dev["eligible"], dev["collected"], dev["pending"]) == ("not_collected", 5, 3, 2)
    assert "2 eligible session(s) not scanned yet" in dev["detail"]
    u = uses(live.url)[COV.SESSIONS]
    assert (u["status"], u["queried"], u["considered"], u["used"], u["findings"]) == ("used_no_finding", 6, 3, 3, 0)
    assert u["skipped"] == {"kind_eval": 1, "over_limit": 2}
    v = CV.evaluate([COV.DEVIATIONS], {COV.DEVIATIONS: dev}, {COV.DEVIATIONS: u})
    assert not v.all_clear and v.gaps[0].distinction == "not_collected"


def test_an_empty_window_is_no_eligible_data(live: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                             writer: Any) -> None:  # noqa: F811
    w = writer(default=envelope({"deviations": []}))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS" and r.deviations is not None and r.deviations.outcomes == [] and w.prompts == []
    s = snaps(live.url)
    assert s[COV.SESSIONS][0]["status"] == "no_eligible_data" and s[COV.DEVIATIONS][0]["status"] == "no_eligible_data"
    assert s[COV.RUNS][0]["status"] == "no_eligible_data"     # no runs of a question routine either
    got = uses(live.url)
    assert {k: v["status"] for k, v in got.items()} == {COV.SESSIONS: "analysis_not_run", COV.RUNS: "analysis_not_run",
        "command_timings:execution_spans": "analysis_not_run",
        "test_timings:test_timing_runs": "analysis_not_run",
        "reports_checkpoints:rsi_checkpoints": "analysis_not_run"}
    statuses = {r["status"] for rows in s.values() for r in rows} | {v["status"] for v in got.values()}
    assert not statuses & {"not_collected", "analysis_failed", "used_no_finding"}
    with psycopg.connect(live.url) as conn:                     # the guard says incomplete, never all clear
        v = CV.verdict(conn, job=COV.DAILY_JOB, run_key=KEY, required=[COV.SESSIONS])
    assert v.conclusion == "incomplete" and [g.distinction for g in v.gaps] == ["no_eligible_data"]


def test_a_failed_step_is_analysis_failed(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                          writer: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("AMEM_PROJECTS_DIR", str(setup_home.root / "nowhere"))
    r = daily.run_daily(push=False, now=NOW)
    assert r.failures[0].startswith("deviations: FileNotFoundError")
    u = uses(live.url)[COV.SESSIONS]
    assert (u["status"], u["used"], u["findings"]) == ("analysis_failed", 0, 0)
    assert u["detail"].startswith("deviations: FileNotFoundError: no projects folder")
    assert COV.SESSIONS not in snaps(live.url)                  # nothing was collected, nothing claimed


def test_every_session_failing_is_analysis_failed(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                                  writer: Any, fake_jev: FakeJev) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    writer(default=distill.WriterError("exit 1: overloaded"))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL" and r.deviations.errors == 5
    u = uses(live.url)[COV.SESSIONS]
    assert (u["status"], u["considered"], u["used"], u["skipped"]) == ("analysis_failed", 5, 0,
                                                                        {"kind_eval": 1, "scan_error": 5})
    assert "5 of 5 session(s) failed; first: writer: exit 1: overloaded" in u["detail"]
    (dev,) = snaps(live.url)[COV.DEVIATIONS]
    assert (dev["status"], dev["collected"], dev["pending"]) == ("not_collected", 0, 5)


def test_switch_off_writes_and_reads_nothing(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                            writer: Any, fake_jev: FakeJev,  # noqa: F811
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    make_all_kinds(setup_home)
    edge_runs(live.url)
    writer(default=envelope({"deviations": [finding()]}))
    monkeypatch.setenv(CV.WRITE_SWITCH, "0")
    monkeypatch.setattr(COV, "scan_states", lambda *a, **k: pytest.fail("read while switched off"))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS" and r.coverage == [] and r.coverage_notes == []
    assert counts(live.url) == (0, 0)


# --------------------------------------------------------------------------- score step


def test_score_records_match_the_scored_runs(sdb: ScoringDb, daily_io: DailyIO) -> None:  # noqa: F811
    edge_runs(sdb.url)
    r = daily.run_daily(push=False, now=NOW)
    assert (r.scored, r.missing) == (4, 1)
    u = uses(sdb.url)[COV.RUNS]
    assert (u["status"], u["queried"], u["considered"], u["used"], u["findings"]) == ("used_no_finding", 5, 5, 4, 0)
    assert u["skipped"] == {"jev_fixture_missing": 1} and u["analysis_version"].startswith("jev-score:")
    (s,) = snaps(sdb.url)[COV.RUNS]
    assert (s["status"], s["eligible"], s["unmeasured_fields"]) == ("measurement_unavailable", 5, ["scores"])
    assert s["detail"] == "1 run(s) without scores" and s["window_start"] == T0 and s["window_end"] == NOW
    assert (u["window_start"], u["window_end"]) == (s["window_start"], s["window_end"])


def test_every_routine_failing_is_analysis_failed(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    edge_runs(sdb.url)

    def boom(**_k: Any) -> scoring.ScoreReport:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(scoring, "score_runs", boom)
    r = daily.run_daily(push=False, now=NOW)
    assert r.failures == ["score t-routine: RuntimeError: connection lost"]
    u = uses(sdb.url)[COV.RUNS]
    assert (u["status"], u["detail"]) == ("analysis_failed", "score t-routine: RuntimeError: connection lost")
    (s,) = snaps(sdb.url)[COV.RUNS]
    assert (s["status"], s["eligible"]) == ("measurement_unavailable", 5)       # the runs are there, unscored


def test_a_coverage_write_failure_is_a_note_only(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    edge_runs(sdb.url)

    def broken(*_a: Any, **_k: Any) -> str:
        raise RuntimeError("coverage table gone")

    monkeypatch.setattr(CV, "record_consumption", broken)
    monkeypatch.setattr(CV, "record_snapshot", broken)
    assert cli.main(["daily", "--no-push"]) == 0                # the exit code does not change
    (line,) = daily_io.log_lines()
    assert " amem-daily PASS scored=4 " in line and " coverage_problems=8 " in line
    assert re.fullmatch(LOG_LINE_RE, line.replace("coverage_problems=8 ", ""))   # otherwise the house line
    assert "coverage routine_runs:runs: RuntimeError: coverage table gone" in capsys.readouterr().err
    assert counts(sdb.url) == (0, 0)


def test_log_line_differs_only_by_the_note() -> None:
    r = daily.DailyResult(scored=2)
    plain = daily.log_lines("ts", r, 3)
    noted = daily.log_lines("ts", dataclasses.replace(r, coverage_notes=["coverage x: boom"]), 3)
    assert plain == ["ts amem-daily PASS scored=2 escalations=0 new=0 push=none tasks=0 3s"]
    assert noted == ["ts amem-daily PASS scored=2 escalations=0 new=0 push=none tasks=0 coverage_problems=1 3s"]


# --------------------------------------------------------------------------- sync step (real sync.run, fake sink)


class FakeSink:
    """A ClickHouse stand-in: one batch per table; `refuse` / `fail` name tables whose push is refused / errors."""

    name = "clickhouse"

    def __init__(self, rows: dict[str, int], refuse: set[str] = frozenset(), fail: set[str] = frozenset(),
                 healthy: bool = True) -> None:
        self.rows, self.refuse, self.fail, self.healthy = rows, refuse, fail, healthy

    def health(self) -> Health:
        return Health(self.healthy, "" if self.healthy else "connection refused")

    def batches(self, conn: Any, watermarks: Any, *, replay: bool) -> Iterator[sync.Batch]:
        for i, (table, n) in enumerate(self.rows.items()):
            yield sync.Batch(table, [{"id": f"{table}-{k}"} for k in range(n)], f"2026-10-15T07:00:00Z|{i}")

    def push(self, batch: sync.Batch) -> None:
        if batch.table in self.refuse:
            raise sync.Refused("content rejected", rows=batch.rows)
        if batch.table in self.fail:
            raise RuntimeError("HTTP 503")


@pytest.fixture
def one_sink(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> ScoringDb:  # noqa: F811
    for k, v in CONFIGURED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AMEM_SYNC_LANGFUSE", "off")
    monkeypatch.setenv("AMEM_SYNC_PASTDEV", "off")
    monkeypatch.delenv("AMEM_SYNC_CLICKHOUSE", raising=False)
    return sdb


def test_sync_records_what_was_delivered_per_table(one_sink: ScoringDb, monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Any) -> None:
    sink = FakeSink({"sessions": 3, "deviations": 2, "runs": 2, "scores": 0}, refuse={"deviations"}, fail={"runs"})
    monkeypatch.setattr(cli_integrations, "sink_for", lambda name, *a, **k: sink)
    r = daily.DailyResult()
    daily.step_sync(r, state_file=tmp_path / "s.json", now=NOW)
    (rep,) = r.synced
    assert (rep.status, rep.rows, rep.dead_letters, rep.failed) == ("partial", 3, 2, 1)
    daily.step_coverage(r)
    assert r.coverage_notes == []
    s, u = snaps(one_sink.url), uses(one_sink.url)
    ses, dev, run, sco = (f"{fam}:{t}.clickhouse" for fam, t in (("sessions", "sessions"), ("deviations", "deviations"),
                                                                   ("routine_runs", "runs"), ("routine_runs", "scores")))
    got = {ds: (s[ds][0]["status"], s[ds][0]["eligible"], s[ds][0]["collected"], s[ds][0]["rejected"],
                s[ds][0]["pending"]) for ds in (ses, dev, run, sco)}
    assert got == {ses: ("measurement_unavailable", 3, 3, 0, 0),      # delivered; readback never claimed
                   dev: ("not_collected", 2, 0, 2, 0), run: ("not_collected", 2, 0, 0, 2),
                   sco: ("no_eligible_data", 0, 0, 0, 0)}
    assert s[ses][0]["unmeasured_fields"] == ["readback"] and "not read back" in s[ses][0]["detail"]
    assert {ds: (u[ds]["status"], u[ds]["queried"], u[ds]["considered"], u[ds]["used"], u[ds]["skipped"])
            for ds in (ses, dev, run, sco)} == {
        ses: ("used_no_finding", 3, 3, 3, {}), dev: ("read_excluded", 2, 2, 0, {"dead_letter": 2}),
        run: ("analysis_failed", 2, 2, 0, {"push_failed": 2}), sco: ("analysis_not_run", 0, 0, 0, {})}
    assert sum(u[ds]["used"] for ds in (ses, dev, run, sco)) == rep.rows   # the job's own row count
    assert {x["run_key"] for x in u.values()} == {KEY}


def test_sync_coverage_keeps_recovery_and_reconciliation_attempts():
    report = sync.SyncReport('clickhouse', tables={
        'sessions': {'seen': 2, 'delivered': 2},
        'sessions:lookback': {'seen': 3, 'delivered': 3},
        'sessions:reconcile': {'seen': 1, 'delivered': 1}})
    records = COV.sync_records(run_key=KEY, now=NOW, sink='clickhouse', report=report)
    snapshots = [r for r in records if isinstance(r, CV.Snapshot)]
    assert len(snapshots) == 1 and snapshots[0].collected == 6
    assert 'including recovery and reconciliation' in snapshots[0].denominator
    assert snapshots[0].unmeasured_fields == ('readback',)


def test_an_unavailable_sink_fails_every_table_it_covers(one_sink: ScoringDb, monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Any) -> None:
    monkeypatch.setattr(cli_integrations, "sink_for", lambda name, *a, **k: FakeSink({}, healthy=False))
    r = daily.DailyResult()
    daily.step_sync(r, state_file=tmp_path / "s.json", now=NOW)
    daily.step_coverage(r)
    u = uses(one_sink.url)
    assert len(u) == len(COV.sink_tables("clickhouse")) >= 5
    assert {x["status"] for x in u.values()} == {"analysis_failed"}
    assert all(x["detail"] == "sink unavailable: connection refused" for x in u.values())
    assert snaps(one_sink.url) == {}                                    # nothing read, nothing measured
    first = counts(one_sink.url)
    assert COV.write(r.coverage) == [] and counts(one_sink.url) == first   # the same records again: no new rows


def test_sink_tables_map_to_registered_families() -> None:
    for sink in sync.SINKS:
        tables = COV.sink_tables(sink)
        assert tables, sink
        for t in tables:
            assert COV.sink_dataset(t, sink), (sink, t)
