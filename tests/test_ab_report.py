"""`ab.report`: arms, outcome sources, verdicts and the low-confidence list.

Each test gets its own fresh database (`sdb`), because the report reads every run in scope.
Runs of routine `t-routine` have a question file (tests/fixtures/questions/t-routine.toml:
threshold 0.7, pass_question `verdict`, pass_options ["ok"]); other routines have none.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from agent_memory import ab, lessons, store
from test_scoring_support import ScoringDb, add_label, add_score, sdb  # noqa: F401

JEV = "t-routine"


def _lesson(url: str, slug: str, repo: str | None = None, status: str = "trial") -> None:
    lessons.upsert([lessons.Lesson(slug=slug, title=f"t {slug}", rule="r", scope_repo=repo, status=status)],
                   db_url=url)


def _runs(url: str, repo: str, status: str, k: int, *, routine: str | None = None,
          started_at: datetime | None = None) -> list[uuid.UUID]:
    return [store.insert_run(routine or f"r-{repo}", status, meta={"repo": repo, "failures": []},
                             started_at=started_at, db_url=url) for _ in range(k)]


def _use(url: str, slug: str, *run_ids: uuid.UUID | None, outcome: str | None = None) -> None:
    with psycopg.connect(url) as conn:
        for run_id in run_ids:
            conn.execute(
                "insert into lesson_uses (lesson_id, run_id, context, outcome) "
                "select id, %s, 'test', %s from lessons where slug = %s",
                (run_id, outcome, slug),
            )


def _jev(url: str, run_id: uuid.UUID, value: str, confidence: float) -> None:
    other = "needs_action" if value == "ok" else "ok"
    add_score(url, run_id, "verdict", "choice", value, None, confidence,
              {value: confidence, other: round(1 - confidence, 4)})


def _report(url: str, **kw) -> dict[str, dict]:
    return {e["slug"]: e for e in ab.report(db_url=url, include_runs=True, **kw)}


def _sources(entry: dict) -> dict[str, tuple[str, str, str]]:
    return {r["run_id"]: (r["arm"], r["outcome"], r["source"]) for r in entry["runs"]}


def test_insufficient_until_both_arms_reach_n(sdb: ScoringDb) -> None:
    _lesson(sdb.url, "few", "repo-a")
    _use(sdb.url, "few", *_runs(sdb.url, "repo-a", "pass", 2))
    _runs(sdb.url, "repo-a", "fail", 10)

    e = _report(sdb.url, n=3)["few"]
    assert (e["with"]["runs"], e["without"]["runs"], e["verdict"]) == (2, 10, "insufficient")
    assert (e["with"]["pass_rate"], e["without"]["pass_rate"], e["diff"]) == (1.0, 0.0, 1.0)
    assert _report(sdb.url, n=2)["few"]["verdict"] == "WIN"
    assert _report(sdb.url, n=11)["few"]["verdict"] == "insufficient"  # the without arm is short too


def test_win_and_loss(sdb: ScoringDb) -> None:
    _lesson(sdb.url, "good", "repo-w")
    _use(sdb.url, "good", *_runs(sdb.url, "repo-w", "pass", 4), *_runs(sdb.url, "repo-w", "fail", 1))
    _runs(sdb.url, "repo-w", "pass", 2)
    _runs(sdb.url, "repo-w", "error", 3)  # error counts as fail

    _lesson(sdb.url, "bad", "repo-l")
    _use(sdb.url, "bad", *_runs(sdb.url, "repo-l", "fail", 3))
    _runs(sdb.url, "repo-l", "pass", 3)

    got = _report(sdb.url, n=3)
    w = got["good"]
    assert (w["with"]["runs"], w["with"]["pass"], w["without"]["runs"], w["without"]["pass"]) == (5, 4, 5, 2)
    assert (w["diff"], w["verdict"]) == (0.4, "WIN")
    assert w["with"]["sources"] == {"label": 0, "jev": 0, "status": 5}
    lo, hi = w["with"]["ci95"]
    assert lo < 0.8 < hi
    assert w["z"] == pytest.approx(1.291, abs=1e-3)

    b = got["bad"]
    assert (b["diff"], b["verdict"]) == (-1.0, "LOSS")
    assert b["z"] < -1.96


def test_tie_at_the_margin_edge(sdb: ScoringDb) -> None:
    # 0.4 - 0.3 is 0.10000000000000003 in floats: still exactly the margin, so TIE.
    _lesson(sdb.url, "edge-up", "repo-up")
    _use(sdb.url, "edge-up", *_runs(sdb.url, "repo-up", "pass", 4), *_runs(sdb.url, "repo-up", "fail", 6))
    _runs(sdb.url, "repo-up", "pass", 3)
    _runs(sdb.url, "repo-up", "fail", 7)

    _lesson(sdb.url, "edge-down", "repo-down")
    _use(sdb.url, "edge-down", *_runs(sdb.url, "repo-down", "pass", 3), *_runs(sdb.url, "repo-down", "fail", 7))
    _runs(sdb.url, "repo-down", "pass", 4)
    _runs(sdb.url, "repo-down", "fail", 6)

    at_edge = _report(sdb.url, n=10, margin=0.1)
    assert (at_edge["edge-up"]["diff"], at_edge["edge-up"]["verdict"]) == (0.1, "TIE")
    assert (at_edge["edge-down"]["diff"], at_edge["edge-down"]["verdict"]) == (-0.1, "TIE")

    inside = _report(sdb.url, n=10, margin=0.09)
    assert inside["edge-up"]["verdict"] == "WIN"
    assert inside["edge-down"]["verdict"] == "LOSS"


def test_label_beats_jev_and_confident_jev_beats_status(sdb: ScoringDb) -> None:
    _lesson(sdb.url, "src", "repo-s")
    labeled, confident_fail, confident_pass, plain = (_runs(sdb.url, "repo-s", s, 1, routine=JEV)[0]
                                                      for s in ("fail", "pass", "fail", "pass"))
    _jev(sdb.url, labeled, "ok", 0.95)       # Jev says pass with high confidence ...
    add_label(sdb.url, labeled, "down")      # ... but the human label says fail: label wins
    _jev(sdb.url, confident_fail, "needs_action", 0.9)  # status pass, confident Jev says fail
    _jev(sdb.url, confident_pass, "ok", 0.7)             # status fail, Jev at the threshold says pass
    _use(sdb.url, "src", labeled, confident_fail)

    e = _report(sdb.url, n=1)["src"]
    assert _sources(e) == {
        str(labeled): ("with", "fail", "label"),
        str(confident_fail): ("with", "fail", "jev"),
        str(confident_pass): ("without", "pass", "jev"),
        str(plain): ("without", "pass", "status"),
    }
    assert e["with"]["sources"] == {"label": 1, "jev": 1, "status": 0}
    assert e["low_confidence"] == []  # 0.7 is not below the 0.7 threshold


def test_low_confidence_jev_falls_back_to_status_and_is_listed(sdb: ScoringDb) -> None:
    _lesson(sdb.url, "low", "repo-c")
    shaky_with, shaky_without, shaky_labeled = (_runs(sdb.url, "repo-c", "pass", 1, routine=JEV)[0]
                                                for _ in range(3))
    _jev(sdb.url, shaky_with, "needs_action", 0.55)    # below 0.7: ignored, status pass decides
    _jev(sdb.url, shaky_without, "needs_action", 0.6)
    _jev(sdb.url, shaky_labeled, "needs_action", 0.4)
    add_label(sdb.url, shaky_labeled, "up")            # already labeled: not listed
    _use(sdb.url, "low", shaky_with)

    e = _report(sdb.url, n=1)["low"]
    assert _sources(e)[str(shaky_with)] == ("with", "pass", "status")
    assert _sources(e)[str(shaky_without)] == ("without", "pass", "status")
    assert _sources(e)[str(shaky_labeled)] == ("without", "pass", "label")
    assert e["low_confidence"] == [
        {"run_id": str(shaky_with), "routine": JEV, "arm": "with", "status": "pass",
         "confidence": 0.55, "threshold": 0.7},
        {"run_id": str(shaky_without), "routine": JEV, "arm": "without", "status": "pass",
         "confidence": 0.6, "threshold": 0.7},
    ]


def test_scores_without_a_question_file_are_ignored(sdb: ScoringDb) -> None:
    _lesson(sdb.url, "nofile", "repo-n")
    (run,) = _runs(sdb.url, "repo-n", "fail", 1, routine="no-such-routine")
    _jev(sdb.url, run, "ok", 0.99)
    e = _report(sdb.url, n=1)["nofile"]
    assert _sources(e)[str(run)] == ("without", "fail", "status")
    assert e["low_confidence"] == []


def test_skip_runs_are_excluded_from_both_arms(sdb: ScoringDb) -> None:
    _lesson(sdb.url, "skips", "repo-k")
    skipped_used = _runs(sdb.url, "repo-k", "skip", 2)
    _use(sdb.url, "skips", *skipped_used)
    add_label(sdb.url, skipped_used[0], "up")  # a label does not bring a skip run back
    _runs(sdb.url, "repo-k", "skip", 3)
    _use(sdb.url, "skips", *_runs(sdb.url, "repo-k", "pass", 1))
    _runs(sdb.url, "repo-k", "fail", 1)

    e = _report(sdb.url, n=1)["skips"]
    assert (e["with"]["runs"], e["without"]["runs"]) == (1, 1)
    assert not {str(r) for r in skipped_used} & set(_sources(e))


def test_scope_matches_lessons_score(sdb: ScoringDb) -> None:
    repo = "repo-scope"
    _lesson(sdb.url, "scoped", repo)
    _lesson(sdb.url, "global", None, status="kept")
    _lesson(sdb.url, "gone", repo, status="retired")
    # In scope: meta.repo = repo, or no meta.repo and routine = repo.
    in_meta = _runs(sdb.url, repo, "pass", 3) + _runs(sdb.url, repo, "fail", 2)
    by_routine = [store.insert_run(repo, s, db_url=sdb.url) for s in ("pass", "error", "pass")]
    elsewhere = _runs(sdb.url, "repo-other", "fail", 4)
    _runs(sdb.url, repo, "skip", 2)
    _use(sdb.url, "scoped", in_meta[0], in_meta[3], by_routine[1])
    _use(sdb.url, "scoped", in_meta[0])  # injected twice: counts once
    _use(sdb.url, "global", in_meta[1], elsewhere[0], elsewhere[1])

    report = _report(sdb.url, n=1)
    assert set(report) == {"scoped", "global"}  # active lessons only, like `lessons score`
    score = {row["slug"]: row for row in lessons.score(1, db_url=sdb.url)}
    for slug in ("scoped", "global"):
        e, s = report[slug], score[slug]
        assert (e["with"]["runs"], e["with"]["pass"]) == (s["uses"], s["uses_pass"]), slug
        assert (e["without"]["runs"], e["without"]["pass"]) == (s["baseline_runs"], s["baseline_pass"]), slug
    assert (report["scoped"]["with"]["runs"], report["scoped"]["without"]["runs"]) == (3, 5)
    assert (report["global"]["with"]["runs"], report["global"]["without"]["runs"]) == (3, 9)

    only = _report(sdb.url, n=1, repo=repo)
    assert set(only) == {"scoped"}  # --repo filters lessons by scope, as `lessons score --repo`
    assert set(_report(sdb.url, n=1, slug="gone")) == {"gone"}  # an explicit slug: any status


def test_since_cuts_both_arms(sdb: ScoringDb) -> None:
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = old + timedelta(days=30)
    _lesson(sdb.url, "recent", "repo-t")
    _use(sdb.url, "recent", *_runs(sdb.url, "repo-t", "fail", 2, started_at=old),
         *_runs(sdb.url, "repo-t", "pass", 1, started_at=new))
    _runs(sdb.url, "repo-t", "pass", 3, started_at=old)
    _runs(sdb.url, "repo-t", "fail", 1, started_at=new)

    e = _report(sdb.url, n=1, since=new - timedelta(days=1))["recent"]
    assert (e["with"]["runs"], e["with"]["pass"], e["without"]["runs"], e["without"]["pass"]) == (1, 1, 1, 0)
    assert e["since"] == (new - timedelta(days=1)).isoformat()


def test_unknown_lesson_raises(sdb: ScoringDb) -> None:
    with pytest.raises(ab.AbError, match="no lesson with Id 'nope'"):
        ab.report(slug="nope", db_url=sdb.url)


def test_statistics() -> None:
    assert ab.wilson(0, 0) is None
    assert ab.wilson(5, 10) == (0.2366, 0.7634)
    assert ab.wilson(10, 10) == (0.7225, 1.0)
    assert ab.z_score(3, 3, 3, 3) == 0.0
    assert ab.z_score(0, 0, 1, 2) is None
    assert ab.verdict(9, 9, 10, 0, n=10, margin=0.1) == "insufficient"
    assert ab.verdict(10, 6, 10, 5, n=10, margin=0.1) == "TIE"
    assert ab.verdict(10, 7, 10, 5, n=10, margin=0.1) == "WIN"
    assert ab.verdict(10, 3, 10, 5, n=10, margin=0.1) == "LOSS"
