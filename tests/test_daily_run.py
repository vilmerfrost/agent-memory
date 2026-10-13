"""`amem daily` end to end against a fresh database (per-test, see test_scoring_support).

Jev answers come from the hand-written fixtures in tests/fixtures/jev/ (fixture
mode; the SDK client is a tripwire). `gh`, ntfy and osascript are stand-ins
(test_daily_support). The routines log and state file live in tmp_path.
"""

from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
import typesafe_sdk

from agent_memory import cli, jev, scoring
from conftest import run_amem
from test_daily_support import (  # noqa: F401
    CLOUD_LOG,
    LOG_LINE_RE,
    DailyIO,
    FakeGh,
    Push,
    daily_io,
    gh_contents,
)
from test_scoring_support import (  # noqa: F401
    DUMMY_KEY,
    TEST_QUESTIONS,
    FakeJevClient,
    ScoringDb,
    add_run,
    fixture_dir_with,
    live_jev,
    sdb,
    sdk_headers,
)

from agent_memory import daily

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 13, 9, 20, 4, tzinfo=timezone(timedelta(hours=2)))
STAMP = "2026-10-13T09:20:04+0200"
TOPIC = "topic-must-never-print"


def _edge_runs(url: str) -> dict[str, uuid.UUID]:
    """States with fixtures (t-routine): two escalate, two do not; one has no fixture."""
    return {
        "edge equal": add_run(url, "t-routine", "fail", "edge equal", T0),
        "edge below": add_run(url, "t-routine", "fail", "edge below", T0 + timedelta(hours=1)),
        "edge score low": add_run(url, "t-routine", "pass", "edge score low", T0 + timedelta(hours=2)),
        "all confident": add_run(url, "t-routine", "pass", "all confident", T0 + timedelta(hours=3)),
        "unrecorded": add_run(url, "t-routine", "fail", "a state nobody recorded", T0 + timedelta(hours=4)),
    }


def _count(url: str, sql: str) -> int:
    with psycopg.connect(url) as conn:
        row = conn.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def _questions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *routines: str) -> None:
    """A question dir where each routine gets a copy of t-routine.toml."""
    qdir = tmp_path / "questions"
    qdir.mkdir()
    for name in routines:
        shutil.copy(TEST_QUESTIONS / "t-routine.toml", qdir / f"{name}.toml")
    monkeypatch.setenv("AMEM_QUESTIONS_DIR", str(qdir))


# ---------------------------------------------------------------- happy path + push memory


def test_all_steps_then_push_only_new_ids_and_prune(sdb: ScoringDb, daily_io: DailyIO) -> None:  # noqa: F811
    gh = daily_io.use_gh(FakeGh(0, gh_contents(CLOUD_LOG)))
    push = daily_io.use_push(Push(status=200))
    runs = _edge_runs(sdb.url)
    notify = {"NTFY_TOPIC": TOPIC}

    # 1. ingest + score + push the two open escalations.
    r = daily.run_daily(notify=notify, now=NOW)
    assert r.failures == [] and r.verdict == "PASS"
    assert (r.ingested, r.scored, r.missing) == (2, 4, 1)  # a missing fixture is not a failure
    assert (r.escalations, r.new, r.push) == (2, 2, "phone")
    assert len(gh.calls) == 1
    assert _count(sdb.url, "select count(*) from runs where source = 'cloud'") == 2
    escalated = {str(runs["edge below"]), str(runs["edge score low"])}
    assert set(daily_io.pushed_ids()) == escalated
    (post,) = push.posts
    assert post["url"] == f"https://ntfy.sh/{TOPIC}"
    assert post["headers"] == {"Title": "amem: 2 new runs to label", "Priority": "default", "Tags": "label"}
    assert post["body"].startswith("2 new escalations (2 open): t-routine 2.")
    # The body names counts and routines only: no summaries, failures or ids.
    for secret in ("edge", "recorded", *escalated):
        assert secret not in post["body"] and secret not in post["headers"]["Title"]
    (line,) = daily_io.log_lines()
    assert line.startswith(f"{STAMP} amem-daily PASS scored=4 escalations=2 new=2 push=phone ")
    assert re.fullmatch(LOG_LINE_RE, line)

    # 2. Same open set: nothing new, no push. Ingest is idempotent.
    r = daily.run_daily(notify=notify, now=NOW)
    assert (r.ingested, r.scored, r.escalations, r.new, r.push) == (0, 0, 2, 0, "none")
    assert len(push.posts) == 1

    # 3. A label closes one escalation: its id is pruned from the state.
    scoring.set_label(runs["edge below"], "down", None, db_url=sdb.url)
    r = daily.run_daily(notify=notify, now=NOW)
    assert (r.escalations, r.new, r.push) == (1, 0, "none")
    assert daily_io.pushed_ids() == [str(runs["edge score low"])]

    # 4. A new escalation: one push, for the new id only.
    fresh = add_run(sdb.url, "t-routine", "fail", "edge below", T0 + timedelta(days=1))
    r = daily.run_daily(notify=notify, now=NOW)
    assert (r.scored, r.escalations, r.new, r.push) == (1, 2, 1, "phone")
    assert push.posts[-1]["body"].startswith("1 new escalation (2 open): t-routine 1.")
    assert set(daily_io.pushed_ids()) == {str(runs["edge score low"]), str(fresh)}
    assert len(daily_io.log_lines()) == 4 and push.macs == []


def test_failed_push_does_not_mark_ids(sdb: ScoringDb, daily_io: DailyIO) -> None:  # noqa: F811
    push = daily_io.use_push(Push(status=500))
    _edge_runs(sdb.url)
    r = daily.run_daily(notify={"NTFY_TOPIC": TOPIC}, now=NOW)
    assert (r.verdict, r.new, r.push) == ("PASS", 2, "mac")  # the Mac fallback is not a failure
    assert daily_io.pushed_ids() == []
    assert len(push.posts) == 1 and len(push.macs) == 1

    # Next day ntfy works: the same ids are still new, pushed, and marked.
    push.status = 200
    r = daily.run_daily(notify={"NTFY_TOPIC": TOPIC}, now=NOW)
    assert (r.new, r.push) == (2, "phone")
    assert len(daily_io.pushed_ids()) == 2

    # No topic at all: Mac only, never marked.
    add_run(sdb.url, "t-routine", "fail", "edge below", T0 + timedelta(days=2))
    r = daily.run_daily(notify={}, now=NOW)
    assert (r.new, r.push) == (1, "mac")
    assert len(daily_io.pushed_ids()) == 2 and len(push.posts) == 2


# ---------------------------------------------------------------- failures


def test_one_routine_jev_error_does_not_stop_others(sdb: ScoringDb, daily_io: DailyIO, tmp_path: Path,  # noqa: F811
                                                    monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    # Routines are scored in name order: t-crash, t-other, then t-routine.
    _questions(tmp_path, monkeypatch, "t-crash", "t-other", "t-routine")
    _edge_runs(sdb.url)
    add_run(sdb.url, "t-other", "fail", "anything", T0)
    add_run(sdb.url, "t-crash", "fail", "anything", T0)
    real_ask, real_score = jev.ask, scoring.score_runs

    def ask(state: str, questions: list[jev.Question]) -> dict[str, jev.JevResult]:
        if state.startswith("routine: t-other\n"):
            raise jev.JevError("upstream 500")
        return real_ask(state, questions)

    def score_runs(**kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("routine") == "t-crash":
            raise RuntimeError("connection lost")
        return real_score(**kwargs)

    monkeypatch.setattr(jev, "ask", ask)
    monkeypatch.setattr(scoring, "score_runs", score_runs)
    daily_io.notify_env.parent.mkdir(parents=True)
    daily_io.notify_env.write_text(f"NTFY_TOPIC={TOPIC}\n")

    assert cli.main(["daily", "--no-push"]) == 1
    out = capsys.readouterr()
    lines = daily_io.log_lines()
    assert re.fullmatch(LOG_LINE_RE, lines[0])
    assert " amem-daily FAIL scored=4 escalations=2 new=2 push=none " in lines[0]
    ts = lines[0].split(" ", 1)[0]
    assert lines[1:] == [
        f"{ts} amem-daily-fail score t-crash: RuntimeError: connection lost",
        f"{ts} amem-daily-fail score t-other: 1 Jev error; first: upstream 500",
    ]
    assert out.out.splitlines() == lines  # stdout repeats what was logged
    assert TOPIC not in out.out + out.err
    # --no-push: nothing pushed, nothing marked; t-routine was still scored.
    assert daily_io.pushed_ids() == []
    assert _count(sdb.url, "select count(distinct run_id) from scores") == 4


def _four_runs(url: str) -> dict[str, uuid.UUID]:
    """The four t-routine states with recorded answers (no missing fixture)."""
    return {
        "edge equal": add_run(url, "t-routine", "fail", "edge equal", T0),
        "edge below": add_run(url, "t-routine", "fail", "edge below", T0 + timedelta(hours=1)),
        "edge score low": add_run(url, "t-routine", "pass", "edge score low", T0 + timedelta(hours=2)),
        "all confident": add_run(url, "t-routine", "pass", "all confident", T0 + timedelta(hours=3)),
    }


def _scored_ids(url: str) -> set[uuid.UUID]:
    with psycopg.connect(url) as conn:
        return {r[0] for r in conn.execute("select distinct run_id from scores").fetchall()}


def test_daily_jev_error_from_sdk_is_a_failed_step_and_other_runs_score(  # noqa: F811
        sdb: ScoringDb, daily_io: DailyIO, live_jev: type[FakeJevClient]) -> None:  # noqa: F811
    """Live mode, SDK client faked: a 5xx on one run (the real typesafe_sdk class) fails the
    score step for that run only; the runs before and after it are scored and escalated."""
    runs = _four_runs(sdb.url)
    live_jev.errors = {"edge score low": typesafe_sdk.TypeSafeInternalServerError(
        503, {"error": "upstream unavailable"}, sdk_headers())}

    r = daily.run_daily(push=False, now=NOW)
    assert live_jev.calls == ["all confident", "edge score low", "edge below", "edge equal"]
    assert r.verdict == "FAIL" and r.scored == 3
    assert r.failures == [
        "score t-routine: 1 Jev error; first: Jev API error: TypeSafeInternalServerError: 503 upstream unavailable",
    ]
    assert _scored_ids(sdb.url) == {runs["all confident"], runs["edge below"], runs["edge equal"]}
    assert r.escalations == 1  # edge below; edge score low is not scored yet
    lines = daily_io.log_lines()
    assert " amem-daily FAIL scored=3 escalations=1 new=1 push=none " in lines[0]
    assert lines[1].endswith(" amem-daily-fail score t-routine: 1 Jev error; first: Jev API error: "
                             "TypeSafeInternalServerError: 503 upstream unavailable")
    assert all(DUMMY_KEY not in line for line in lines)

    # Next day the API answers: the failed run is retried and scored.
    live_jev.errors = {}
    r = daily.run_daily(push=False, now=NOW)
    assert (r.verdict, r.scored, r.escalations) == ("PASS", 1, 2)


def test_daily_jev_error_from_corrupt_fixture_is_a_failed_step(  # noqa: F811
        sdb: ScoringDb, daily_io: DailyIO, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    runs = _four_runs(sdb.url)
    corrupt = add_run(sdb.url, "t-routine", "fail", "corrupt json", T0 + timedelta(minutes=90))
    monkeypatch.setenv("AMEM_JEV_FIXTURES_DIR",
                       str(fixture_dir_with(tmp_path, {("fail", "corrupt json"): "not json at all"})))

    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL" and r.scored == 4
    (failure,) = r.failures
    assert failure.startswith("score t-routine: 1 Jev error; first: fixture ")
    assert "is unreadable or not JSON: JSONDecodeError" in failure
    assert _scored_ids(sdb.url) == set(runs.values())
    assert corrupt not in _scored_ids(sdb.url)
    assert r.escalations == 2


def test_ingest_failure_logs_and_parses_back(sdb: ScoringDb, daily_io: DailyIO) -> None:  # noqa: F811
    daily_io.use_gh(FakeGh(1, "", "gh: Bad credentials (HTTP 401)"))
    _edge_runs(sdb.url)
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL" and r.scored == 4  # scoring still ran
    assert daily_io.log_lines() == [
        f"{STAMP} amem-daily FAIL scored=4 escalations=2 new=2 push=none tasks=0 {daily_io.log_lines()[0].split()[-1]}",
        f"{STAMP} amem-daily-fail ingest: FetchError: gh api failed: gh: Bad credentials (HTTP 401)",
    ]

    # `amem ingest routines-log` reads the lines back as routine amem-daily.
    result = run_amem("ingest", "routines-log", str(daily_io.routines_log), env=sdb.env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ingested 1 new, 0 already present, 0 unparsed"
    with psycopg.connect(sdb.url) as conn:
        rows = conn.execute(
            "select routine, status, started_at, meta from runs where routine = 'amem-daily'").fetchall()
    ((routine, status, started_at, meta),) = rows
    assert (routine, status, started_at) == ("amem-daily", "fail", NOW)
    assert meta["scored"] == 4 and meta["escalations"] == 2 and meta["new"] == 2 and meta["push"] == "none"
    assert isinstance(meta["duration_s"], int)
    assert meta["failures"] == ["ingest: FetchError: gh api failed: gh: Bad credentials (HTTP 401)"]


def test_require_remote_refuses_every_step(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    gh = daily_io.use_gh(FakeGh(0, gh_contents(CLOUD_LOG)))
    _edge_runs(sdb.url)
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")  # AMEM_DB_URL is the local test container
    assert cli.main(["daily"]) == 1
    lines = daily_io.log_lines()
    assert " amem-daily FAIL scored=0 escalations=0 new=0 push=none " in lines[0]
    assert [line.split(" ", 2)[2] for line in lines[1:]] == [
        "ingest: AMEM_REQUIRE_REMOTE=1 refused a local or empty database",
        "score: AMEM_REQUIRE_REMOTE=1 refused a local or empty database",
        "escalations: AMEM_REQUIRE_REMOTE=1 refused a local or empty database",
    ]
    assert gh.calls == []
    assert _count(sdb.url, "select count(*) from scores") == 0
    assert _count(sdb.url, "select count(*) from runs where source = 'cloud'") == 0
    assert not daily_io.state.exists()


def test_live_flag_without_key_and_bad_state_fail(sdb: ScoringDb, daily_io: DailyIO,  # noqa: F811
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    _edge_runs(sdb.url)
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")  # no TYPESAFE_API_KEY: Jev stays in fixture mode
    daily_io.state.parent.mkdir(parents=True)
    daily_io.state.write_text("{not json")
    r = daily.run_daily(push=False, now=NOW)
    assert r.scored == 4  # fixture mode, never live
    assert r.failures == [
        "score: AMEM_JEV_LIVE=1 but TYPESAFE_API_KEY is empty; Jev ran in fixture mode",
        "state: unreadable amem-daily-state.json (JSONDecodeError); starting empty",
    ]
    assert daily_io.pushed_ids() == []  # rewritten as a valid state file
