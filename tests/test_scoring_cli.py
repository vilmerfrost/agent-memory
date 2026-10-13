"""`amem score | escalations | calibrate | drift` end to end against a fresh database.

Jev answers come from hand-written fixtures in tests/fixtures/jev/ (fixture mode;
the SDK client is a tripwire). Question set: tests/fixtures/questions/t-routine.toml.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
import typesafe_sdk

from agent_memory import cli, jev, scoring
from agent_memory.questions import QuestionCache
from conftest import run_amem
from test_scoring_support import (  # noqa: F401
    DUMMY_KEY,
    JEV_FIXTURES,
    SAMPLE_LOG,
    TEST_QUESTIONS,
    FakeJevClient,
    ScoringDb,
    add_label,
    add_run,
    add_score,
    fixture_dir_with,
    live_jev,
    sdb,
    sdk_headers,
    t_routine_fixture_path,
)

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def _edge_runs(url: str) -> dict[str, uuid.UUID]:
    """Runs whose states have fixtures (see each fixture's `_state`), plus two that cannot be scored."""
    return {
        "edge equal": add_run(url, "t-routine", "fail", "edge equal", T0),
        "edge below": add_run(url, "t-routine", "fail", "edge below", T0 + timedelta(hours=1)),
        "edge score low": add_run(url, "t-routine", "pass", "edge score low", T0 + timedelta(hours=2)),
        "all confident": add_run(url, "t-routine", "pass", "all confident", T0 + timedelta(hours=3)),
        "unrecorded": add_run(url, "t-routine", "fail", "a state nobody recorded", T0 + timedelta(hours=4)),
        "no questions": add_run(url, "t-noq", "fail", "routine without a question file", T0),
    }


# --------------------------------------------------------------------------- score


def test_score_happy_path_missing_fixture_and_skip(sdb: ScoringDb) -> None:
    runs = _edge_runs(sdb.url)
    result = run_amem("score", env=sdb.env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "scored 4 runs, skipped 1 (no questions), 1 missing fixtures"
    # The missing fixture is reported with its run and key, not a crash.
    assert f"missing fixture: run {runs['unrecorded']} (t-routine)" in result.stderr
    assert "no Jev fixture" in result.stderr

    with psycopg.connect(sdb.url) as conn:
        rows = conn.execute(
            "select question, kind, value, value_num, confidence, probabilities, jev_mode "
            "from scores where run_id = %s order by question", (runs["edge below"],)
        ).fetchall()
        assert rows == [
            ("flag", "noul", "0.4", 0.4, None, {}, "fixture"),
            ("severity", "score", "1.0", 1.0, 0.9, {"0": 0.05, "1": 0.9, "2": 0.05}, "fixture"),
            ("verdict", "choice", "needs_action", None, 0.69, {"ok": 0.31, "needs_action": 0.69}, "fixture"),
        ]
        unscored = conn.execute(
            "select count(*) from runs r where not exists (select 1 from scores s where s.run_id = r.id)"
        ).fetchone()
        assert unscored == (2,)

    # Re-running scores nothing new; the unrecorded run is retried and still missing.
    again = run_amem("score", env=sdb.env)
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip() == "scored 0 runs, skipped 1 (no questions), 1 missing fixtures"


def test_score_limit_routine_and_since(sdb: ScoringDb) -> None:
    _edge_runs(sdb.url)
    # Newest first: "unrecorded" (missing) then "all confident" use up the limit of 2.
    limited = run_amem("score", "--routine", "t-routine", "--limit", "2", env=sdb.env)
    assert limited.stdout.strip() == "scored 1 runs, skipped 0 (no questions), 1 missing fixtures"
    since = run_amem("score", "--since", (T0 + timedelta(hours=1)).isoformat(), env=sdb.env)
    # edge below + edge score low scored; unrecorded missing again; edge equal is before --since.
    assert since.stdout.strip() == "scored 2 runs, skipped 0 (no questions), 1 missing fixtures"
    rest = run_amem("score", env=sdb.env)
    assert rest.stdout.strip() == "scored 1 runs, skipped 1 (no questions), 1 missing fixtures"


def test_score_never_calls_live_jev(sdb: ScoringDb) -> None:
    assert jev.mode() == "fixture"
    env = dict(sdb.env, TYPESAFE_API_KEY="")  # no key -> fixture mode even with the flag
    env["AMEM_JEV_LIVE"] = "1"
    add_run(sdb.url, "t-routine", "fail", "a state nobody recorded", T0)
    result = run_amem("score", env=env)
    assert result.returncode == 0
    assert "1 missing fixtures" in result.stdout


# --------------------------------------------------------------------------- score: per-run Jev failures


def _unscored(url: str) -> set[uuid.UUID]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "select r.id from runs r where not exists (select 1 from scores s where s.run_id = r.id)"
        ).fetchall()
    return {r[0] for r in rows}


def test_score_counts_corrupt_fixture_as_failed_and_scores_the_rest(sdb: ScoringDb, tmp_path: Path) -> None:
    """Real CLI, fixture mode: a non-JSON fixture, an answer the SDK models reject and an
    out-of-range confidence are failed runs; older and newer runs are still scored."""
    good = (JEV_FIXTURES / "b0e8c12441d8978e3928493c8fa1bd34.json").read_text()
    bad_confidence = json.loads(good)
    bad_confidence["answers"]["verdict"]["confidence"] = 2
    bad_shape = json.loads(good)
    bad_shape["answers"]["verdict"] = {"type": "choice", "confidence": "high"}
    fixtures = fixture_dir_with(tmp_path, {
        ("fail", "corrupt json"): '{"answers": {"verdict": ',
        ("fail", "bad confidence"): json.dumps(bad_confidence),
        ("fail", "bad shape"): json.dumps(bad_shape),
    })
    runs = {  # scored newest first; failing runs sit between runs that score
        "edge equal": add_run(sdb.url, "t-routine", "fail", "edge equal", T0),
        "corrupt json": add_run(sdb.url, "t-routine", "fail", "corrupt json", T0 + timedelta(minutes=30)),
        "edge below": add_run(sdb.url, "t-routine", "fail", "edge below", T0 + timedelta(hours=1)),
        "bad confidence": add_run(sdb.url, "t-routine", "fail", "bad confidence", T0 + timedelta(minutes=90)),
        "edge score low": add_run(sdb.url, "t-routine", "pass", "edge score low", T0 + timedelta(hours=2)),
        "bad shape": add_run(sdb.url, "t-routine", "fail", "bad shape", T0 + timedelta(minutes=150)),
        "all confident": add_run(sdb.url, "t-routine", "pass", "all confident", T0 + timedelta(hours=3)),
        "unrecorded": add_run(sdb.url, "t-routine", "fail", "a state nobody recorded", T0 + timedelta(hours=4)),
    }
    failing = {runs["corrupt json"], runs["bad confidence"], runs["bad shape"]}
    env = dict(sdb.env, AMEM_JEV_FIXTURES_DIR=str(fixtures))

    result = run_amem("score", env=env)
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert result.stdout.strip() == "scored 4 runs, skipped 0 (no questions), 1 missing fixtures, 3 failed"
    corrupt_path = t_routine_fixture_path(fixtures, "fail", "corrupt json")
    shape_path = t_routine_fixture_path(fixtures, "fail", "bad shape")
    assert (f"amem: jev error: run {runs['corrupt json']} (t-routine): fixture {corrupt_path} "
            "is unreadable or not JSON: JSONDecodeError") in result.stderr
    assert (f"amem: jev error: run {runs['bad shape']} (t-routine): fixture {shape_path} "
            "has an invalid answer: ValidationError") in result.stderr
    assert (f"amem: jev error: run {runs['bad confidence']} (t-routine): "
            "answer for 'verdict' has confidence 2.0 outside 0..1") in result.stderr
    # The missing fixture stays its own category, not a failure.
    assert f"missing fixture: run {runs['unrecorded']} (t-routine)" in result.stderr
    # Runs before and after the failing ones were scored; the failed ones were not marked scored.
    assert _unscored(sdb.url) == failing | {runs["unrecorded"]}

    # The failed runs are retried next time and fail again (never silently skipped).
    again = run_amem("score", env=env)
    assert again.returncode == 1
    assert again.stdout.strip() == "scored 0 runs, skipped 0 (no questions), 1 missing fixtures, 3 failed"
    # Once the fixtures are fixed, the same runs score.
    for status_summary in (("fail", "corrupt json"), ("fail", "bad confidence"), ("fail", "bad shape")):
        t_routine_fixture_path(fixtures, *status_summary).write_text(good)
    fixed = run_amem("score", env=env)
    assert fixed.returncode == 0, fixed.stderr
    assert fixed.stdout.strip() == "scored 3 runs, skipped 0 (no questions), 1 missing fixtures"
    assert _unscored(sdb.url) == {runs["unrecorded"]}


def test_score_counts_sdk_error_as_failed_and_scores_the_rest(sdb: ScoringDb, live_jev: type[FakeJevClient],
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    """Live mode with the SDK client faked: a 429 and a connection error raised by the real
    typesafe_sdk exception classes are failed runs; the runs around them are scored."""
    runs = {
        "edge equal": add_run(sdb.url, "t-routine", "fail", "edge equal", T0),
        "edge below": add_run(sdb.url, "t-routine", "fail", "edge below", T0 + timedelta(hours=1)),
        "edge score low": add_run(sdb.url, "t-routine", "pass", "edge score low", T0 + timedelta(hours=2)),
        "all confident": add_run(sdb.url, "t-routine", "pass", "all confident", T0 + timedelta(hours=3)),
    }
    live_jev.errors = {
        "edge below": typesafe_sdk.TypeSafeRateLimitError(429, {"error": "rate limited"}, sdk_headers(),
                                                          endpoint="POST https://api.typesafe.ai/v1/system-one"),
        "edge score low": typesafe_sdk.TypeSafeAPIConnectionError("connection reset by peer"),
    }

    assert cli.main(["score"]) == 1
    out = capsys.readouterr()
    assert out.out.strip() == "scored 2 runs, skipped 0 (no questions), 0 missing fixtures, 2 failed"
    assert live_jev.calls == ["all confident", "edge score low", "edge below", "edge equal"]  # newest first, no stop
    assert (f"amem: jev error: run {runs['edge below']} (t-routine): Jev API error: TypeSafeRateLimitError: "
            "POST https://api.typesafe.ai/v1/system-one: 429 rate limited") in out.err
    assert (f"amem: jev error: run {runs['edge score low']} (t-routine): Jev API error: "
            "TypeSafeAPIConnectionError: connection reset by peer") in out.err
    assert DUMMY_KEY not in out.out + out.err
    assert _unscored(sdb.url) == {runs["edge below"], runs["edge score low"]}
    with psycopg.connect(sdb.url) as conn:
        modes = conn.execute("select distinct jev_mode from scores").fetchall()
    assert modes == [("live",)]

    # The API recovers: the failed runs are retried and scored.
    live_jev.errors = {}
    assert cli.main(["score"]) == 0
    assert capsys.readouterr().out.strip() == "scored 2 runs, skipped 0 (no questions), 0 missing fixtures"
    assert _unscored(sdb.url) == set()


@pytest.mark.parametrize("error", [
    typesafe_sdk.TypeSafeBadRequestError(400, {"error": "bad state"}, sdk_headers()),
    typesafe_sdk.TypeSafeAuthenticationError(401, {"error": f"invalid key {DUMMY_KEY}"}, sdk_headers()),
    typesafe_sdk.TypeSafeRateLimitError(429, None, sdk_headers()),
    typesafe_sdk.TypeSafeInternalServerError(503, "upstream down", sdk_headers()),
    typesafe_sdk.TypeSafeAPIResponseValidationError(200, {}, sdk_headers(), "answers.verdict.confidence"),
    typesafe_sdk.TypeSafeAPIConnectionError("name resolution failed"),
    typesafe_sdk.TypeSafeAPITimeoutError(30.0),
    ConnectionResetError("reset below the SDK"),
], ids=lambda e: type(e).__name__)
def test_jev_ask_wraps_sdk_error_as_jev_error(live_jev: type[FakeJevClient], error: BaseException) -> None:
    live_jev.errors = {"edge below": error}
    state = "routine: t-routine\nstatus: fail\nsummary: edge below\nfailure_reason: (none)\nfailures: (none)"
    qset = QuestionCache(TEST_QUESTIONS).get("t-routine")
    assert qset is not None
    with pytest.raises(jev.JevError) as caught:
        jev.ask(state, qset.jev_questions())
    assert not isinstance(caught.value, jev.JevFixtureMissing)
    assert caught.value.__cause__ is error
    assert type(error).__name__ in str(caught.value)
    assert DUMMY_KEY not in str(caught.value)  # a server echoing the key is masked too
    assert "\n" not in str(caught.value)


# --------------------------------------------------------------------------- escalations


def test_escalations_threshold_edge_and_labels(sdb: ScoringDb) -> None:
    runs = _edge_runs(sdb.url)
    assert run_amem("score", env=sdb.env).returncode == 0

    result = run_amem("escalations", env=sdb.env)
    assert result.returncode == 0, result.stderr
    rows = _lines(result.stdout)
    # Newest first. "edge equal" (confidence == threshold 0.7) and "all confident" stay out.
    assert [r["run_id"] for r in rows] == [str(runs["edge score low"]), str(runs["edge below"])]
    by_id = {r["run_id"]: r for r in rows}
    low = by_id[str(runs["edge below"])]
    assert low["low"] == [{"question": "verdict", "kind": "choice", "value": "needs_action", "confidence": 0.69,
                           "jev_confidence": 0.69}]
    assert low["threshold"] == 0.7
    assert low["jev_verdict"] == "down"
    assert low["routine"] == "t-routine" and low["summary"] == "edge below"
    score_low = by_id[str(runs["edge score low"])]
    # A score question escalates too when the file lists it in escalate_on (t-routine does).
    assert [x["question"] for x in score_low["low"]] == ["severity"]
    assert score_low["jev_verdict"] == "up"

    # A labeled run is no longer an escalation.
    assert run_amem("label", str(runs["edge below"]), "down", env=sdb.env).returncode == 0
    after = _lines(run_amem("escalations", env=sdb.env).stdout)
    assert [r["run_id"] for r in after] == [str(runs["edge score low"])]
    assert _lines(run_amem("escalations", "--routine", "t-noq", env=sdb.env).stdout) == []


# --------------------------------------------------------------------------- calibrate


def _labeled(sdb: ScoringDb, conf: float, jev_label: str, human: str, i: int) -> None:
    run = add_run(sdb.url, "t-routine", "fail", f"cal {i}", T0 + timedelta(minutes=i))
    probs = {jev_label: conf, ("needs_action" if jev_label == "ok" else "ok"): round(1 - conf, 4)}
    add_score(sdb.url, run, "verdict", "choice", jev_label, None, conf, probs)
    add_score(sdb.url, run, "severity", "score", "1.0", 1.0, 0.9)
    add_label(sdb.url, run, human)


def test_calibrate_with_too_few_labels(sdb: ScoringDb) -> None:
    for i, (conf, jv, human) in enumerate([(0.9, "ok", "up"), (0.8, "needs_action", "down"), (0.6, "ok", "down")]):
        _labeled(sdb, conf, jv, human, i)
    add_run(sdb.url, "t-routine", "pass", "unlabeled", T0)  # not counted
    result = run_amem("calibrate", env=sdb.env)
    assert result.returncode == 0, result.stderr
    rows = _lines(result.stdout)
    assert rows == [{
        "routine": "t-routine", "pass_question": "verdict", "labels": 3, "agree": 2, "agreement": 0.6667,
        "jev_up_label_up": 1, "jev_up_label_down": 1, "jev_down_label_up": 0, "jev_down_label_down": 1,
        "threshold": 0.7, "suggested_threshold": None, "note": "not enough labels (3 < 10)",
    }]


def test_calibrate_with_enough_labels_suggests_threshold(sdb: ScoringDb) -> None:
    confs = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.92, 0.95, 0.98]
    for i, conf in enumerate(confs):
        if conf == 0.50:
            _labeled(sdb, conf, "needs_action", "up", i)  # disagree
        elif conf == 0.55:
            _labeled(sdb, conf, "ok", "down", i)  # disagree
        else:
            _labeled(sdb, conf, "ok" if i % 2 else "needs_action", "up" if i % 2 else "down", i)
    result = run_amem("calibrate", "--routine", "t-routine", env=sdb.env)
    assert result.returncode == 0, result.stderr
    (row,) = _lines(result.stdout)
    assert row["labels"] == 12 and row["agree"] == 10
    assert row["agreement"] == pytest.approx(0.8333)
    assert row["jev_down_label_up"] == 1 and row["jev_up_label_down"] == 1
    # >= 0.50: 10/12 agree (83%); >= 0.55: 10/11 (91%) -> lowest level reaching 90%.
    assert row["suggested_threshold"] == 0.55
    assert row["note"] is None
    # A stricter target with the same labels finds the next level.
    strict = _lines(run_amem("calibrate", "--target", "0.95", "--min-labels", "5", env=sdb.env).stdout)
    assert strict[0]["suggested_threshold"] == 0.6


def test_calibrate_unknown_routine_fails(sdb: ScoringDb) -> None:
    result = run_amem("calibrate", "--routine", "t-noq", env=sdb.env)
    assert result.returncode == 1
    assert "no question file" in result.stderr


# --------------------------------------------------------------------------- drift

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def _drift_run(sdb: ScoringDb, routine: str, status: str, days_ago: float, p_ok: float | None = None) -> None:
    run = add_run(sdb.url, routine, status, f"{routine} {status}", NOW - timedelta(days=days_ago))
    if p_ok is not None:
        label = "ok" if p_ok >= 0.5 else "needs_action"
        add_score(sdb.url, run, "verdict", "choice", label, None, max(p_ok, 1 - p_ok),
                  {"ok": p_ok, "needs_action": round(1 - p_ok, 4)})


def test_drift_flags_with_sufficient_samples(sdb: ScoringDb) -> None:
    # t-routine: baseline 6 runs all pass, Jev P(ok) 0.9; window 5 runs, 2 pass, P(ok) 0.3.
    for d in (8, 10, 12, 15, 20, 30):
        _drift_run(sdb, "t-routine", "pass", d, 0.9)
    for d, status in zip((1, 2, 3, 4, 5), ("pass", "fail", "fail", "pass", "fail")):
        _drift_run(sdb, "t-routine", status, d, 0.3)
    # steady: no drop, never flagged.
    for d in (8, 9, 10, 11, 12, 1, 2, 3, 4, 5):
        _drift_run(sdb, "steady", "pass", d)
    _drift_run(sdb, "steady", "skip", 1)  # skip runs are left out of the pass rate
    _drift_run(sdb, "old", "fail", 40)  # outside window + baseline

    result = run_amem("drift", "--now", NOW.isoformat(), env=sdb.env)
    assert result.returncode == 0, result.stderr
    rows = _lines(result.stdout)
    assert [(r["routine"], r["metric"]) for r in rows] == [("t-routine", "pass_rate"), ("t-routine", "jev_pass")]
    rate, jp = rows
    assert (rate["window_n"], rate["baseline_n"]) == (5, 6)
    assert rate["window_value"] == 0.4 and rate["baseline_value"] == 1.0 and rate["drop"] == 0.6
    assert jp["window_value"] == 0.3 and jp["baseline_value"] == 0.9 and jp["drop"] == 0.6
    assert "0 skipped" in result.stderr

    # A larger delta than the drop flags nothing.
    calm = run_amem("drift", "--now", NOW.isoformat(), "--delta", "0.7", env=sdb.env)
    assert _lines(calm.stdout) == []


def test_drift_skips_small_samples(sdb: ScoringDb) -> None:
    # Same drop, but only 2 runs in the window: below the default minimum of 5.
    for d in (8, 10, 12, 15, 20, 30):
        _drift_run(sdb, "t-small", "pass", d)
    for d in (1, 2):
        _drift_run(sdb, "t-small", "fail", d)
    result = run_amem("drift", "--now", NOW.isoformat(), "--window", "7d", "--baseline", "28d", env=sdb.env)
    assert result.returncode == 0, result.stderr
    assert _lines(result.stdout) == []
    assert "1 skipped (fewer than 5 samples" in result.stderr
    # The minimum is configurable.
    loose = run_amem("drift", "--now", NOW.isoformat(), "--min-runs", "2", "--routine", "t-small", env=sdb.env)
    rows = _lines(loose.stdout)
    assert [(r["routine"], r["metric"], r["drop"]) for r in rows] == [("t-small", "pass_rate", 1.0)]


def test_naive_now_and_since_are_read_as_utc(sdb: ScoringDb) -> None:
    for d in (8, 10, 12, 15, 20, 30):
        _drift_run(sdb, "t-small", "pass", d)
    for d in (0.5, 1, 2):
        _drift_run(sdb, "t-small", "fail", d)
    naive_now = NOW.replace(tzinfo=None).isoformat()  # 2026-10-12T00:00:00, no offset
    aware = run_amem("drift", "--now", NOW.isoformat(), "--min-runs", "2", env=sdb.env)
    naive = run_amem("drift", "--now", naive_now, "--min-runs", "2", env=sdb.env)
    assert naive.returncode == 0, naive.stderr
    assert "Traceback" not in naive.stderr
    assert _lines(naive.stdout) == _lines(aware.stdout) != []
    # The library call is safe too, not only the CLI parser.
    flags, _ = scoring.drift(now=NOW.replace(tzinfo=None), window=timedelta(days=7), baseline=timedelta(days=28),
                             min_runs=2, db_url=sdb.url)
    assert json.loads(json.dumps(flags, default=str)) == _lines(aware.stdout)

    # --since without an offset is UTC too: a run at exactly T0 is in, one a second earlier is out.
    add_run(sdb.url, "t-routine", "fail", "edge below", T0)
    add_run(sdb.url, "t-routine", "fail", "edge equal", T0 - timedelta(seconds=1))
    since = run_amem("score", "--routine", "t-routine", "--since", T0.replace(tzinfo=None).isoformat(),
                     env=sdb.env)
    assert since.returncode == 0, since.stderr
    assert since.stdout.strip() == "scored 1 runs, skipped 0 (no questions), 0 missing fixtures"


# --------------------------------------------------------------------------- demo flow


def test_demo_flow_on_sample_log(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented demo: shipped questions + the 5 config-check fixtures."""
    env = {k: v for k, v in sdb.env.items() if k != "AMEM_QUESTIONS_DIR"}
    monkeypatch.delenv("AMEM_QUESTIONS_DIR")
    ingest = run_amem("ingest", "routines-log", str(SAMPLE_LOG), env=env)
    assert ingest.returncode == 0, ingest.stderr

    scored = run_amem("score", "--routine", "config-check", "--limit", "5", env=env)
    assert scored.returncode == 0, scored.stderr
    assert scored.stdout.strip() == "scored 5 runs, skipped 0 (no questions), 0 missing fixtures"

    esc = _lines(run_amem("escalations", env=env).stdout)
    # config-check escalates on the verdict only: the 2026-10-12 run's low urgency (0.66) no longer pushes.
    assert [(r["started_at"], [x["question"] for x in r["low"]]) for r in esc] == [
        ("2026-10-11T13:48:26+00:00", ["verdict"]),
    ]
    assert run_amem("label", esc[0]["run_id"], "down", "--note", "LAN exposure is real", env=env).returncode == 0
    assert len(_lines(run_amem("escalations", env=env).stdout)) == 0

    cal = {r["routine"]: r for r in _lines(run_amem("calibrate", env=env).stdout)}
    assert sorted(cal) == ["canary-simplitics-selftest", "config-check", "dispatch", "evals-parent", "routine-audit"]
    assert cal["config-check"]["labels"] == 1 and cal["config-check"]["agree"] == 1
    assert cal["config-check"]["note"] == "not enough labels (1 < 10)"
