"""The daily `amem daily` deviations step: off unless AMEM_DEVIATIONS_LIVE=1, runs when the
last scan is >= 20 hours old (AMEM_DEVIATIONS_EVERY_HOURS), adds `deviations=...` to the log line only when it ran."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from agent_memory import daily, deviations, distill, questions
from test_daily_support import LOG_LINE_RE, DailyIO, daily_io  # noqa: F401
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
from test_scoring_support import ScoringDb, sdb  # noqa: F401

NOW = datetime(2026, 10, 15, 9, 20, 0, tzinfo=timezone(timedelta(hours=2)))
Writer = Callable[..., FakeWriter]
FIELDS = re.compile(r" deviations=\d+ deviation_sessions=\d+ deviations_edit=\d+ deviations_enforce=\d+ "
                    r"deviations_leave=\d+ deviation_errors=\d+ deviation_skipped=\d+ ")


@pytest.fixture
def live(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> ScoringDb:  # noqa: F811
    monkeypatch.setenv(deviations.LIVE_ENV, "1")
    monkeypatch.setenv("AMEM_QUESTIONS_DIR", str(questions.QUESTIONS_DIR))  # deviation.toml
    return sdb


def _state(io: DailyIO) -> dict[str, object]:
    return json.loads(io.state.read_text())


def test_off_without_live_flag(sdb: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                               writer: Writer, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    w = writer()
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS" and r.deviations is None and w.prompts == []
    (line,) = daily_io.log_lines()
    assert re.fullmatch(LOG_LINE_RE, line)  # the house line is unchanged
    assert "deviations: off (AMEM_DEVIATIONS_LIVE is not 1)" in capsys.readouterr().err
    assert daily.DEVIATIONS_KEY not in _state(daily_io)


def test_daily_gating(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                       writer: Writer, fake_jev: FakeJev, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    make_all_kinds(setup_home)
    w = writer(default=envelope({"deviations": [finding()]}))

    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS", r.failures
    assert r.deviations is not None and r.deviations.found == 5
    (line,) = daily_io.log_lines()
    assert " push=none tasks=0 deviations=5 deviation_sessions=5 deviations_edit=5 deviations_enforce=0 " \
           "deviations_leave=0 deviation_errors=0 " in line
    state = _state(daily_io)
    assert state[daily.DEVIATIONS_KEY] == "2026-10-15T09:20:00+0200" and state["pushed_run_ids"] == []
    assert len(w.prompts) == 5

    # 19 hours later: not due. The escalations step's save_state kept the timestamp.
    r = daily.run_daily(push=False, now=NOW + timedelta(hours=19))
    assert r.verdict == "PASS" and r.deviations is None
    assert not FIELDS.search(daily_io.log_lines()[-1])
    assert "deviations: not due (last scan 2026-10-15T09:20:00+0200, every 20 hours)" in capsys.readouterr().err
    assert len(w.prompts) == 5

    # The next morning (24 hours later): due again (nothing new to scan in these fixtures).
    r = daily.run_daily(push=False, now=NOW + timedelta(hours=24))
    assert r.verdict == "PASS" and r.deviations is not None and r.deviations.outcomes == []
    assert FIELDS.search(daily_io.log_lines()[-1])
    assert _state(daily_io)[daily.DEVIATIONS_KEY] == "2026-10-16T09:20:00+0200"


def test_session_errors_fail_the_step_only(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                           writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    ids = make_all_kinds(setup_home)
    writer(distill.WriterError("timed out after 300s"), default=envelope({"deviations": []}))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL"
    assert r.failures == [f"deviations: 1 error; first: session {ids['chat']}: writer: timed out after 300s"]
    head, fail = daily_io.log_lines()
    assert " deviation_errors=1 " in head and fail.endswith("amem-daily-fail " + r.failures[0])
    assert daily.DEVIATIONS_KEY in _state(daily_io)  # the scan finished; the failed session is retried later


def test_skipped_item_is_a_counted_warning_not_a_fail(live: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                      setup_home: Home, writer: Writer,  # noqa: F811
                                                      fake_jev: FakeJev,  # noqa: F811
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    # 2026-10-17 09:20: one item with `"followed_grade": null` made the routine FAIL and dropped
    # the session's valid items. Now the valid items are stored, each bad one is counted and named
    # (a null and an out-of-range grade alike).
    ids = make_all_kinds(setup_home)
    writer(default=envelope({"deviations": [finding(followed=None), finding(turn=2), finding(turn=3, result_=1.5)]}))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS", r.failures
    assert r.deviations is not None and (r.deviations.found, r.deviations.errors, r.deviations.skipped) == (5, 0, 10)
    (line,) = daily_io.log_lines()
    assert FIELDS.search(line) and " deviation_errors=0 deviation_skipped=10 " in line
    err = capsys.readouterr().err
    assert (f"deviations: invalid item skipped: session {ids['worker']}: "
            "deviation 0: followed_grade must be a number in 0..1, got None") in err
    assert (f"deviations: invalid item skipped: session {ids['worker']}: "
            "deviation 2: result_grade must be a number in 0..1, got 1.5") in err
    assert err.count("deviations: invalid item skipped: ") == 10
    assert "errors=0 skipped=10" in err  # the scan summary line


def test_deviation_errors_do_not_stop_the_edits_step(live: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                     setup_home: Home, writer: Writer,  # noqa: F811
                                                     fake_jev: FakeJev,  # noqa: F811
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # Scope of a deviations failure: that session only; the step fails, later steps still run.
    from agent_memory import edits

    ids = make_all_kinds(setup_home)
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    runs: list[object] = []
    monkeypatch.setattr(edits, "run", lambda **kw: runs.append(kw) or edits.Report())
    writer(envelope({"deviations": [finding(followed=None)]}), default=envelope({"deviations": [finding()]}))
    r = daily.run_daily(push=False, now=NOW)
    assert r.failures == [f"deviations: 1 error; first: session {ids['chat']}: writer output: "
                          "deviation 0: followed_grade must be a number in 0..1, got None"]
    assert r.deviations is not None and (r.deviations.found, r.deviations.errors) == (4, 1)
    assert len(runs) == 1 and r.edits is not None
    head, _fail = daily_io.log_lines()
    assert " deviation_errors=1 deviation_skipped=0 edits=0 " in head
    assert daily.DEVIATIONS_KEY in _state(daily_io)


def test_crash_before_the_scan_is_a_failed_step(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_PROJECTS_DIR", str(setup_home.root / "nowhere"))
    r = daily.run_daily(push=False, now=NOW)
    assert len(r.failures) == 1 and r.failures[0].startswith("deviations: FileNotFoundError: no projects folder")
    head, _fail = daily_io.log_lines()
    assert not FIELDS.search(head)
    assert daily.DEVIATIONS_KEY not in _state(daily_io)  # retried tomorrow
    assert daily_io.state.exists()  # escalations still ran before it


def test_save_state_keeps_other_keys(daily_io: DailyIO) -> None:  # noqa: F811
    daily_io.state.parent.mkdir(parents=True, exist_ok=True)
    daily_io.state.write_text(json.dumps({"pushed_run_ids": ["a"], daily.DEVIATIONS_KEY: "x"}))
    daily.save_state(daily_io.state, {"b"}, NOW)
    state = _state(daily_io)
    assert state["pushed_run_ids"] == ["b"] and state[daily.DEVIATIONS_KEY] == "x"


# ---------------------------------------------------------------- wait for the database, catch up


class _Flaky:
    """store.connect that fails `fails` times with a DNS error, then connects for real."""

    def __init__(self, fails: int) -> None:
        self.fails, self.calls = fails, 0
        self.real = daily.store.connect

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        if self.calls <= self.fails:
            raise psycopg.OperationalError("failed to resolve host 'db.example': [Errno 8] nodename nor servname")
        return self.real(*args, **kwargs)


def test_wait_for_db_retries_until_it_answers(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                              monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(daily, "sleep", sleeps.append)
    flaky = _Flaky(fails=2)
    monkeypatch.setattr(daily.store, "connect", flaky)
    assert daily.wait_for_db() is True
    assert flaky.calls == 3 and sleeps == [daily.DB_WAIT_SECONDS] * 2
    assert "db: not reachable (OperationalError: failed to resolve host" in capsys.readouterr().err


def test_wait_for_db_gives_up_after_the_last_try(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(daily, "sleep", sleeps.append)
    monkeypatch.setattr(daily.store, "connect", _Flaky(fails=99))
    assert daily.wait_for_db(tries=3) is False
    assert len(sleeps) == 2  # no sleep after the last try
    assert "db: not reachable after 3 tries" in capsys.readouterr().err


def test_daily_waits_for_the_database_after_wake(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                                 writer: Writer, fake_jev: FakeJev,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-10-14: launchd ran the job right after wake; DNS was not back and every step failed.
    make_all_kinds(setup_home)
    writer(default=envelope({"deviations": []}))
    monkeypatch.setattr(daily.store, "connect", _Flaky(fails=3))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS", r.failures
    assert r.deviations is not None and len(r.deviations.outcomes) == 5
    assert daily.DEVIATIONS_KEY in _state(daily_io)


def test_lost_database_before_the_scan_is_retried_once(live: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                       setup_home: Home, writer: Writer,  # noqa: F811
                                                       fake_jev: FakeJev, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    make_all_kinds(setup_home)
    writer(default=envelope({"deviations": []}))
    real_scan, calls = deviations.scan, []

    def scan(**kwargs: object) -> deviations.ScanReport:
        calls.append(kwargs)
        if len(calls) == 1:
            raise psycopg.OperationalError("failed to resolve host 'db.example'")
        return real_scan(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(daily.deviations, "scan", scan)
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS", r.failures
    assert len(calls) == 2 and r.deviations is not None and len(r.deviations.outcomes) == 5
    assert "the database answers again, retrying once" in capsys.readouterr().err
    assert daily.DEVIATIONS_KEY in _state(daily_io)


def test_lost_database_twice_fails_the_step(live: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                            setup_home: Home, monkeypatch: pytest.MonkeyPatch) -> None:
    make_all_kinds(setup_home)
    calls: list[object] = []

    def scan(**kwargs: object) -> deviations.ScanReport:
        calls.append(kwargs)
        raise psycopg.OperationalError("failed to resolve host 'db.example'")

    monkeypatch.setattr(daily.deviations, "scan", scan)
    r = daily.run_daily(push=False, now=NOW)
    assert len(calls) == 2
    assert r.failures == ["deviations: OperationalError: failed to resolve host 'db.example'"]
    assert daily.DEVIATIONS_KEY not in _state(daily_io)  # tomorrow's run catches up


def test_daily_scans_up_to_the_daily_limit(live: ScoringDb, daily_io: DailyIO, setup_home: Home,  # noqa: F811
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    make_all_kinds(setup_home)
    seen: list[object] = []
    monkeypatch.setattr(daily.deviations, "scan",
                        lambda **kw: seen.append(kw["limit"]) or deviations.ScanReport())
    daily.run_daily(push=False, now=NOW)
    assert seen == [daily.DEVIATIONS_LIMIT] and daily.DEVIATIONS_LIMIT > deviations.DEFAULT_LIMIT
