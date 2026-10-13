"""`amem score --rescore --routine R`: replace the scores of already scored runs, no duplicates.

Fixture mode only. The "new" Jev answers come from a copy of tests/fixtures/jev with one
fixture rewritten (synthetic values).
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from conftest import run_amem
from test_scoring_support import (  # noqa: F401
    JEV_FIXTURES,
    ScoringDb,
    add_label,
    add_run,
    sdb,
    t_routine_fixture_path,
)

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def _runs(url: str) -> dict[str, uuid.UUID]:
    return {
        "edge equal": add_run(url, "t-routine", "fail", "edge equal", T0),
        "edge below": add_run(url, "t-routine", "fail", "edge below", T0 + timedelta(hours=1)),
        "edge score low": add_run(url, "t-routine", "pass", "edge score low", T0 + timedelta(hours=2)),
        "all confident": add_run(url, "t-routine", "pass", "all confident", T0 + timedelta(hours=3)),
    }


def _confident_edge_below(tmp_path: Path) -> Path:
    """Fixtures where Jev is now sure about "edge below" (verdict needs_action, 0.96)."""
    out = tmp_path / "jev-new"
    shutil.copytree(JEV_FIXTURES, out)
    path = t_routine_fixture_path(out, "fail", "edge below")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["answers"]["verdict"] = {"type": "choice", "choice": "needs_action", "confidence": 0.96,
                                  "probabilities": {"ok": 0.02, "needs_action": 0.98}}
    path.write_text(json.dumps(body), encoding="utf-8")
    return out


def _score_rows(url: str) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute("select run_id, question, confidence from scores order by run_id, question").fetchall()


def test_rescore_replaces_scores_and_prints_before_after(sdb: ScoringDb, tmp_path: Path) -> None:
    runs = _runs(sdb.url)
    assert run_amem("score", env=sdb.env).returncode == 0
    assert run_amem("label", str(runs["edge equal"]), "up", env=sdb.env).returncode == 0
    before_rows = _score_rows(sdb.url)
    assert len(before_rows) == 12  # 4 runs x 3 questions

    env = dict(sdb.env, AMEM_JEV_FIXTURES_DIR=str(_confident_edge_below(tmp_path)))
    result = run_amem("score", "--rescore", "--routine", "t-routine", env=env)
    assert result.returncode == 0, result.stderr
    rows = _lines(result.stdout)  # stdout is JSON lines only
    assert [r["run_id"] for r in rows] == [str(runs[k]) for k in
                                           ("all confident", "edge score low", "edge below", "edge equal")]
    below = next(r for r in rows if r["run_id"] == str(runs["edge below"]))
    assert below["before"]["verdict"] == {"value": "needs_action", "confidence": 0.69}
    assert below["after"]["verdict"] == {"value": "needs_action", "confidence": 0.96}
    assert below["escalated_before_old_rule"] is True
    assert below["escalates_before"] is True and below["escalates_after"] is False
    assert below["low_after"] == []
    assert ("amem: rescored 4 runs (t-routine), 0 missing fixtures; escalated before (old rule) 2, "
            "before 2, after 1") in result.stderr

    # Replaced, never duplicated: still 3 rows per run, and only the changed answer differs.
    after_rows = _score_rows(sdb.url)
    assert len(after_rows) == 12
    changed = set(after_rows) - set(before_rows)
    assert changed == {(runs["edge below"], "verdict", 0.96)}
    # The label survives a rescore; escalations follow the new answers.
    esc = _lines(run_amem("escalations", env=sdb.env).stdout)
    assert [r["run_id"] for r in esc] == [str(runs["edge score low"])]
    with psycopg.connect(sdb.url) as conn:
        assert conn.execute("select verdict from labels where run_id = %s", (runs["edge equal"],)).fetchone() == ("up",)

    # Rescoring again with the same answers changes nothing and still never duplicates.
    again = run_amem("score", "--rescore", "--routine", "t-routine", "--limit", "2", env=env)
    assert again.returncode == 0, again.stderr
    assert len(_lines(again.stdout)) == 2
    assert sorted(_score_rows(sdb.url)) == sorted(after_rows)


def test_rescore_keeps_old_scores_when_the_fixture_is_missing(sdb: ScoringDb, tmp_path: Path) -> None:
    runs = _runs(sdb.url)
    assert run_amem("score", env=sdb.env).returncode == 0
    before_rows = _score_rows(sdb.url)
    partial = tmp_path / "jev-partial"
    shutil.copytree(JEV_FIXTURES, partial)
    t_routine_fixture_path(partial, "fail", "edge below").unlink()

    env = dict(sdb.env, AMEM_JEV_FIXTURES_DIR=str(partial))
    result = run_amem("score", "--rescore", "--routine", "t-routine", "--since",
                      (T0 + timedelta(minutes=30)).isoformat(), env=env)
    assert result.returncode == 0, result.stderr
    assert [r["run_id"] for r in _lines(result.stdout)] == [str(runs["all confident"]), str(runs["edge score low"])]
    assert f"missing fixture: run {runs['edge below']} (t-routine)" in result.stderr
    assert "rescored 2 runs (t-routine), 1 missing fixtures" in result.stderr
    assert sorted(_score_rows(sdb.url)) == sorted(before_rows)  # nothing lost, nothing added


def test_rescore_needs_a_routine_with_a_question_file(sdb: ScoringDb) -> None:
    no_routine = run_amem("score", "--rescore", env=sdb.env)
    assert no_routine.returncode == 1
    assert "--rescore needs --routine" in no_routine.stderr
    unknown = run_amem("score", "--rescore", "--routine", "t-noq", env=sdb.env)
    assert unknown.returncode == 1
    assert "no question file for routine 't-noq'" in unknown.stderr


def test_rescore_honors_require_remote(sdb: ScoringDb) -> None:
    env = dict(sdb.env, AMEM_REQUIRE_REMOTE="1")
    result = run_amem("score", "--rescore", "--routine", "t-routine", env=env)
    assert result.returncode == 3
