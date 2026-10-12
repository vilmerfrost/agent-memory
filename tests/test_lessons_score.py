"""`amem lessons score`: pass rate with a lesson vs the same-scope baseline; retire on --apply."""

from __future__ import annotations

import json
import uuid

import psycopg

from agent_memory import lessons, store
from conftest import run_amem


def _runs(repo: str, status: str, n: int) -> list[uuid.UUID]:
    return [store.insert_run(f"score-{repo}", status, meta={"repo": repo}) for _ in range(n)]


def _use(pg_url: str, slug: str, run_id: uuid.UUID | None, outcome: str | None = None) -> None:
    with psycopg.connect(pg_url) as conn:
        conn.execute(
            "insert into lesson_uses (lesson_id, run_id, context, outcome) "
            "select id, %s, 'test', %s from lessons where slug = %s",
            (run_id, outcome, slug),
        )


def _score(env: dict[str, str], repo: str, *extra: str) -> dict[str, dict]:
    result = run_amem("lessons", "score", "--repo", repo, "--min-uses", "3", *extra, env=env)
    assert result.returncode == 0, result.stderr
    return {row["slug"]: row for row in map(json.loads, result.stdout.splitlines())}


def test_score_insufficient_keep_retire_review(db_env: dict[str, str], pg_url: str) -> None:
    repo = f"score-{uuid.uuid4().hex[:8]}"
    slug = {k: f"{repo}-{k}" for k in ("retire", "keep", "few", "promoted")}
    lessons.upsert([
        lessons.Lesson(slug=slug["retire"], title="t retire", rule="r", scope_repo=repo, status="trial"),
        lessons.Lesson(slug=slug["keep"], title="t keep", rule="r", scope_repo=repo, status="kept"),
        lessons.Lesson(slug=slug["few"], title="t few", rule="r", scope_repo=repo, status="trial"),
        lessons.Lesson(slug=slug["promoted"], title="t promoted", rule="r", scope_repo=repo, status="promoted"),
    ])
    _runs(repo, "pass", 3)  # baseline runs with no lesson
    _runs(repo, "skip", 2)  # skips never count
    for run in _runs(repo, "fail", 3):
        _use(pg_url, slug["retire"], run)
    _use(pg_url, slug["retire"], run)  # the same run injected twice counts once
    for run in _runs(repo, "pass", 3):
        _use(pg_url, slug["keep"], run)
    (few_run,) = _runs(repo, "pass", 1)
    _use(pg_url, slug["few"], few_run)
    _use(pg_url, slug["few"], None, outcome="fail")  # explicit outcome, no run
    _use(pg_url, slug["few"], None)  # unknown outcome: not a use for scoring
    for run in _runs(repo, "error", 3):
        _use(pg_url, slug["promoted"], run)

    dry = _score(db_env, repo)
    assert set(dry) == set(slug.values())

    r = dry[slug["retire"]]
    # Baseline: 3 pass + 3 keep-pass + 1 few-pass + 3 promoted-error = 7/10.
    assert (r["uses"], r["with_pass_rate"], r["baseline_runs"], r["baseline_pass_rate"]) == (3, 0.0, 10, 0.7)
    assert (r["decision"], r["applied"], r["status"]) == ("retire", False, "trial")

    k = dry[slug["keep"]]
    assert (k["uses"], k["with_pass_rate"], k["baseline_pass_rate"], k["decision"]) == (3, 1.0, 0.4, "keep")

    f = dry[slug["few"]]
    assert (f["uses"], f["uses_pass"], f["decision"]) == (2, 1, "insufficient")

    p = dry[slug["promoted"]]
    assert (p["uses"], p["with_pass_rate"], p["decision"]) == (3, 0.0, "review")  # never auto-retired

    assert lessons.get_lesson(slug["retire"]).status == "trial"  # dry run wrote nothing

    applied = _score(db_env, repo, "--apply")
    assert (applied[slug["retire"]]["applied"], applied[slug["retire"]]["status"]) == (True, "retired")
    assert lessons.get_lesson(slug["retire"]).status == "retired"
    for k in ("keep", "few", "promoted"):
        assert applied[slug[k]]["applied"] is False
    assert lessons.get_lesson(slug["promoted"]).status == "promoted"

    after = _score(db_env, repo)
    assert slug["retire"] not in after  # retired lessons are no longer scored


def test_score_too_few_uses_never_retires(db_env: dict[str, str], pg_url: str) -> None:
    repo = f"score-{uuid.uuid4().hex[:8]}"
    s = f"{repo}-lonely"
    lessons.upsert([lessons.Lesson(slug=s, title="t", rule="r", scope_repo=repo)])
    _runs(repo, "pass", 5)
    for run in _runs(repo, "fail", 2):
        _use(pg_url, s, run)
    rows = _score(db_env, repo, "--apply")
    assert (rows[s]["uses"], rows[s]["decision"], rows[s]["applied"]) == (2, "insufficient", False)
    assert lessons.get_lesson(s).status == "trial"


def test_score_no_baseline(db_env: dict[str, str], pg_url: str) -> None:
    repo = f"score-{uuid.uuid4().hex[:8]}"
    s = f"{repo}-alone"
    lessons.upsert([lessons.Lesson(slug=s, title="t", rule="r", scope_repo=repo)])
    for run in _runs(repo, "fail", 3):
        _use(pg_url, s, run)
    rows = _score(db_env, repo, "--apply")
    assert (rows[s]["decision"], rows[s]["baseline_runs"], rows[s]["baseline_pass_rate"]) == ("no_baseline", 0, None)


def test_score_apply_respects_require_remote(db_env: dict[str, str]) -> None:
    result = run_amem("lessons", "score", "--apply", env=dict(db_env, AMEM_REQUIRE_REMOTE="1"))
    assert result.returncode == 3
