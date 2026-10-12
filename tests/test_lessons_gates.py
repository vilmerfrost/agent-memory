"""Jev gates: `amem lesson propose` (write gate) and `amem gate friction` (improver gate).

All answers come from hand-written fixtures in tests/fixtures/jev/ (no live calls).
"""

from __future__ import annotations

import psycopg

from agent_memory import lessons, store
from conftest import base_env, run_amem

ACCEPT_FAILURE = "worker chained cd && git commit and the isolation check refused it"


def _accept_run() -> str:
    # State must match the fixture: routine, status, summary, failures (no ids, no timestamps).
    run_id = store.insert_run("lessons-propose-accept", "fail", summary="pass=3 fail=1",
                              meta={"failures": [ACCEPT_FAILURE]})
    return str(run_id)


def _reject_run() -> str:
    run_id = store.insert_run("lessons-propose-reject", "fail", summary="network blip",
                              failure_reason="curl: (6) Could not resolve host once")
    return str(run_id)


def _lessons_from_run(pg_url: str, run_id: str) -> list[tuple]:
    with psycopg.connect(pg_url) as conn:
        return conn.execute(
            "select slug, status, scope_repo, scope_task, source_run_ids::text[], title, rule "
            "from lessons where %s = any(source_run_ids)", (run_id,),
        ).fetchall()


def test_propose_accept_stores_trial_lesson(db_env: dict[str, str], pg_url: str) -> None:
    run_id = _accept_run()
    args = ("lesson", "propose", "--run", run_id, "--repo", "agent-memory", "--task", "worker brief")
    result = run_amem(*args, env=db_env)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip()
    assert out.startswith("accept reusable=0.86 (min 0.60) generalizable=0.78 (min 0.50, confidence 0.74)")
    slug = f"lessons-propose-accept-{run_id[:8]}"
    assert out.endswith(f"-> lesson {slug} (stored as trial)")

    (row,) = _lessons_from_run(pg_url, run_id)
    assert row[:5] == (slug, "trial", "agent-memory", "worker brief", [run_id])
    assert ACCEPT_FAILURE in row[5]
    assert row[6].startswith("Before you finish worker brief, check that this does not happen again:")
    lesson = lessons.get_lesson(slug)
    assert lesson is not None and "Jev gate: reusable=0.86, generalizable=0.78" in (lesson.evidence or "")

    again = run_amem(*args, env=db_env)
    assert again.returncode == 0
    assert again.stdout.strip().endswith("(already stored, left unchanged)")
    assert len(_lessons_from_run(pg_url, run_id)) == 1


def test_propose_reject_stores_nothing(db_env: dict[str, str], pg_url: str) -> None:
    run_id = _reject_run()
    result = run_amem("lesson", "propose", "--run", run_id, env=db_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "reject reusable=0.21 (min 0.60) generalizable=0.12 (min 0.50, confidence 0.88)")
    assert _lessons_from_run(pg_url, run_id) == []


def test_propose_thresholds_are_configurable(db_env: dict[str, str], pg_url: str) -> None:
    run_id = _accept_run()
    base = ("lesson", "propose", "--run", run_id, "--repo", "agent-memory", "--task", "worker brief")
    by_flag = run_amem(*base, "--min-reusable", "0.9", env=db_env)
    assert by_flag.stdout.startswith("reject reusable=0.86 (min 0.90)")
    by_env = run_amem(*base, env=dict(db_env, AMEM_PROPOSE_MIN_GENERALIZABLE="0.8"))
    assert by_env.stdout.startswith("reject reusable=0.86 (min 0.60) generalizable=0.78 (min 0.80")
    assert _lessons_from_run(pg_url, run_id) == []


def test_propose_errors(db_env: dict[str, str]) -> None:
    unknown = run_amem("lesson", "propose", "--run", "00000000-0000-0000-0000-000000000000", env=db_env)
    assert unknown.returncode == 1 and "no run with id" in unknown.stderr

    run_id = str(store.insert_run("lessons-propose-unrecorded", "fail", summary="no fixture for me"))
    missing = run_amem("lesson", "propose", "--run", run_id, env=db_env)
    assert missing.returncode == 1
    assert "no Jev fixture" in missing.stderr

    bad = run_amem("lesson", "propose", "--run", "not-a-uuid", env=db_env)
    assert bad.returncode == 2


def test_propose_state_is_deterministic() -> None:
    run = {"routine": "r", "status": "fail", "summary": None, "failure_reason": None, "meta": {}}
    assert lessons.propose_state(run, None, None) == (
        "Routine run: r\nStatus: fail\nRepo: -\nTask: -\nSummary: -\nFailure reason: -\nFailures:\n- none")


def test_friction_accept() -> None:
    result = run_amem("gate", "friction", "--text", "worker refused compound cd command again", "--seen", "3",
                      env=base_env())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "accept confidence=0.88 p_recurring=0.88 threshold=0.50"


def test_friction_reject() -> None:
    result = run_amem("gate", "friction", "--text", "typo in one commit message", "--seen", "1", env=base_env())
    assert result.returncode == 1, result.stderr
    assert result.stdout.strip() == "reject confidence=0.88 p_recurring=0.12 threshold=0.50"


def test_friction_threshold_and_errors() -> None:
    strict = run_amem("gate", "friction", "--text", "worker refused compound cd command again", "--seen", "3",
                      "--min", "0.95", env=base_env())
    assert strict.returncode == 1
    assert strict.stdout.startswith("reject confidence=0.12 p_recurring=0.88 threshold=0.95")

    by_env = run_amem("gate", "friction", "--text", "worker refused compound cd command again", "--seen", "3",
                      env=base_env(AMEM_FRICTION_MIN="0.9"))
    assert by_env.returncode == 1

    missing = run_amem("gate", "friction", "--text", "never recorded", env=base_env())
    assert missing.returncode == 2
    assert "no Jev fixture" in missing.stderr
