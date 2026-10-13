"""The `amem daily` distill step: runs after scoring, off unless AMEM_DISTILL_LIVE=1,
a failure is a failed step and never stops the escalations step after it."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from agent_memory import daily, distill
from test_daily_support import LOG_LINE_RE, DailyIO, daily_io  # noqa: F401
from test_distill_support import (  # noqa: F401
    ACCEPT,
    NEW,
    REJECT,
    FakeWriter,
    add_dispatch,
    answer,
    envelope,
    jev_dir,
    later,
    no_real_writer,
    writer,
)
from test_scoring_support import ScoringDb, sdb  # noqa: F401

NOW = datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))
Writer = Callable[..., FakeWriter]


def _count(url: str, table: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(f"select count(*) from {table}").fetchone()
    assert row is not None
    return int(row[0])


def test_daily_distill_step_success(sdb: ScoringDb, daily_io: DailyIO, jev_dir: Path,  # noqa: F811
                                    writer: Writer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(distill.LIVE_ENV, "1")
    good = add_dispatch(sdb.url, started_at=later(1), summary="good")
    dull = add_dispatch(sdb.url, summary="dull")
    answer(jev_dir, sdb.url, good, *ACCEPT)
    answer(jev_dir, sdb.url, dull, *REJECT)
    writer(envelope(NEW))

    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS", r.failures
    assert r.distilled is not None and (r.distilled.new, r.distilled.rejected) == (1, 1)
    (line,) = daily_io.log_lines()
    assert " push=none distilled=2 lessons_new=1 lessons_merged=0 lessons_rejected=1 distill_errors=0 " in line
    assert re.fullmatch(r"\S+ amem-daily PASS .* \d+s", line)
    assert _count(sdb.url, "lessons") == 1 and _count(sdb.url, "lesson_candidates") == 2
    assert daily_io.state.exists()  # the escalations step ran after distill

    # Next day: nothing new to consider.
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS" and r.distilled is not None and r.distilled.outcomes == []


def test_daily_distill_errors_fail_the_step_only(sdb: ScoringDb, daily_io: DailyIO, jev_dir: Path,  # noqa: F811
                                                 writer: Writer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(distill.LIVE_ENV, "1")
    run = add_dispatch(sdb.url)
    answer(jev_dir, sdb.url, run, *ACCEPT)
    writer(distill.WriterError("timed out after 120s"))

    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL"
    assert r.failures == [f"distill: 1 error; first: run {run}: writer: timed out after 120s"]
    head, fail = daily_io.log_lines()
    assert " amem-daily FAIL " in head and " distilled=1 lessons_new=0 lessons_merged=0 " in head
    assert " distill_errors=1 " in head
    assert fail.endswith(f" amem-daily-fail distill: 1 error; first: run {run}: writer: timed out after 120s")
    assert daily_io.state.exists()  # escalations still ran


def test_daily_distill_crash_is_a_failed_step(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(distill.LIVE_ENV, "1")

    def boom(**_kwargs: object) -> distill.Report:
        raise RuntimeError("connection lost")

    monkeypatch.setattr(distill, "distill", boom)
    r = daily.run_daily(push=False, now=NOW)
    assert r.failures == ["distill: RuntimeError: connection lost"]
    (head, _fail) = daily_io.log_lines()
    assert "distilled=" not in head  # no counts when the step never ran
    assert daily_io.state.exists()


def test_daily_distill_is_off_without_live_flag(sdb: ScoringDb, daily_io: DailyIO, jev_dir: Path,  # noqa: F811
                                                writer: Writer, capsys: pytest.CaptureFixture[str]) -> None:
    run = add_dispatch(sdb.url)
    answer(jev_dir, sdb.url, run, *ACCEPT)
    fake = writer(envelope(NEW))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS" and r.distilled is None
    (line,) = daily_io.log_lines()
    assert re.fullmatch(LOG_LINE_RE, line)  # the house line is unchanged
    assert fake.prompts == [] and _count(sdb.url, "lesson_candidates") == 0
    assert "distill: off (AMEM_DISTILL_LIVE is not 1)" in capsys.readouterr().err
