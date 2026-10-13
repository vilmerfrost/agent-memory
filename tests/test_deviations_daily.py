"""The daily `amem daily` deviations step: off unless AMEM_DEVIATIONS_LIVE=1, runs when the
last scan is >= 20 hours old (AMEM_DEVIATIONS_EVERY_HOURS), adds `deviations=...` to the log line only when it ran."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

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
                    r"deviations_leave=\d+ deviation_errors=\d+ ")


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
    assert _state(daily_io)[daily.DEVIATIONS_KEY] == "2026-10-22T09:20:00+0200"


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
