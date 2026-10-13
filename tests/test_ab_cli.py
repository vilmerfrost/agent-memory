"""`amem ab report` through the real CLI: text table, JSON lines, guards and exit codes."""

from __future__ import annotations

import json
import uuid

import psycopg

from agent_memory import lessons, store
from conftest import run_amem
from test_scoring_support import ScoringDb, add_score, sdb  # noqa: F401


def _seed(url: str) -> uuid.UUID:
    """Lesson `cli-lesson` in repo `cli-repo`: with 3/3 pass, without 1/3 pass. Returns a shaky run."""
    lessons.upsert([lessons.Lesson(slug="cli-lesson", title="t", rule="r", scope_repo="cli-repo")], db_url=url)
    with_runs = [store.insert_run("t-routine", "pass", meta={"repo": "cli-repo"}, db_url=url) for _ in range(3)]
    for status in ("pass", "fail", "fail"):
        store.insert_run("t-routine", status, meta={"repo": "cli-repo"}, db_url=url)
    add_score(url, with_runs[0], "verdict", "choice", "needs_action", None, 0.5,
              {"needs_action": 0.5, "ok": 0.5})
    with psycopg.connect(url) as conn:
        for run_id in with_runs:
            conn.execute("insert into lesson_uses (lesson_id, run_id) "
                         "select id, %s from lessons where slug = 'cli-lesson'", (run_id,))
    return with_runs[0]


def test_text_report(sdb: ScoringDb) -> None:
    shaky = _seed(sdb.url)
    result = run_amem("ab", "report", "--n", "3", env=sdb.env)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert out.startswith("cli-lesson  [trial, scope: cli-repo]  verdict WIN  (n>=3, margin 0.10)")
    assert "  with         3     3  1.00  0.44-1.00       0    0       0      3" in out
    assert "  without      3     1  0.33  0.06-0.79       0    0       0      3" in out
    assert "  diff +0.67  z +1.73" in out
    assert f"    {shaky}  t-routine  conf 0.50 < 0.7  with  status pass" in out


def test_json_report_and_memory_switch_has_no_effect(sdb: ScoringDb) -> None:
    shaky = _seed(sdb.url)
    plain = run_amem("ab", "report", "--n", "3", "--json", env=sdb.env)
    off = run_amem("ab", "report", "--n", "3", "--json", env=dict(sdb.env, AMEM_MEMORY="off"))
    assert plain.returncode == off.returncode == 0, plain.stderr + off.stderr
    assert plain.stdout == off.stdout
    (row,) = [json.loads(line) for line in plain.stdout.splitlines()]
    assert (row["slug"], row["verdict"], row["n"], row["margin"]) == ("cli-lesson", "WIN", 3, 0.1)
    assert (row["with"]["pass_rate"], row["without"]["pass_rate"], row["diff"]) == (1.0, 0.3333, 0.6667)
    assert [low["run_id"] for low in row["low_confidence"]] == [str(shaky)]
    assert "runs" not in row

    detailed = run_amem("ab", "report", "--n", "3", "--json", "--runs", env=sdb.env)
    runs = json.loads(detailed.stdout)["runs"]
    assert sorted((r["arm"], r["outcome"], r["source"]) for r in runs) == [
        ("with", "pass", "status")] * 3 + [("without", "fail", "status")] * 2 + [("without", "pass", "status")]
    assert run_amem("ab", "report", "--runs", env=sdb.env).returncode == 2  # --runs needs --json

    tight = run_amem("ab", "report", "--lesson", "cli-lesson", "--n", "4", "--json", env=sdb.env)
    assert json.loads(tight.stdout)["verdict"] == "insufficient"


def test_unknown_lesson_and_empty_db(sdb: ScoringDb) -> None:
    empty = run_amem("ab", "report", env=sdb.env)
    assert (empty.returncode, empty.stdout) == (0, "")
    assert "no lessons to report" in empty.stderr
    missing = run_amem("ab", "report", "--lesson", "nope", env=sdb.env)
    assert missing.returncode == 1
    assert "no lesson with Id 'nope'" in missing.stderr


def test_refuses_rest_backend(sdb: ScoringDb) -> None:
    env = dict(sdb.env, AMEM_BACKEND="rest", AMEM_SUPABASE_URL="https://example.invalid")
    result = run_amem("ab", "report", env=env)
    assert result.returncode == 1
    assert "needs the Postgres backend in V1 (backend=rest)" in result.stderr


def test_require_remote_refuses_a_local_db(sdb: ScoringDb) -> None:
    result = run_amem("ab", "report", env=dict(sdb.env, AMEM_REQUIRE_REMOTE="1"))
    assert result.returncode == 3
    assert "AMEM_REQUIRE_REMOTE=1" in result.stderr
