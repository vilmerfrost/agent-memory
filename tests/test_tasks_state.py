"""`scoring.format_state`: the dispatch branch, and proof that every other routine's
state (and so every recorded Jev fixture key) is byte-identical to before."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_memory import jev, routines_log, scoring
from agent_memory.questions import QUESTIONS_DIR, QuestionCache
from agent_memory.tasks_ingest import parse_parent

TESTS_DIR = Path(__file__).resolve().parent
JEV_FIXTURES = TESTS_DIR / "fixtures" / "jev"
TEST_QUESTIONS = TESTS_DIR / "fixtures" / "questions"
SAMPLE_LOG = TESTS_DIR / "fixtures" / "routines.sample.log"
TASKS = TESTS_DIR / "fixtures" / "tasks"


def _frozen_format_state(run: dict[str, Any]) -> str:
    """format_state as it was before the dispatch branch (commit 0127933). Do not edit."""
    import re

    ws = re.compile(r"\s+")

    def clean(value: Any) -> str:
        if value is None:
            return "(none)"
        text = ws.sub(" ", str(value)).strip()
        return text or "(none)"

    meta = run.get("meta") or {}
    failures = meta.get("failures") or []
    lines = [
        f"routine: {clean(run.get('routine'))}",
        f"status: {clean(run.get('status'))}",
        f"summary: {clean(run.get('summary'))}",
        f"failure_reason: {clean(run.get('failure_reason'))}",
    ]
    if failures:
        lines.append("failures:")
        lines.extend(f"- {clean(f)}" for f in failures)
    else:
        lines.append("failures: (none)")
    return "\n".join(lines)


def _state_to_run(state: str) -> dict[str, Any]:
    """Inverse of the generic state format (enough for the recorded fixtures)."""
    lines = state.split("\n")
    fields = dict(line.split(": ", 1) for line in lines[:4])
    failures = [line[2:] for line in lines[5:]] if lines[4] == "failures:" else []
    none = lambda v: None if v == "(none)" else v  # noqa: E731
    return {"routine": fields["routine"], "status": fields["status"], "summary": none(fields["summary"]),
            "failure_reason": none(fields["failure_reason"]), "meta": {"failures": failures}}


def _recorded() -> list[tuple[Path, str]]:
    out = []
    for path in sorted(JEV_FIXTURES.glob("*.json")):
        state = json.loads(path.read_text(encoding="utf-8")).get("_state")
        if state is not None:
            out.append((path, state))
    return out


def test_existing_fixture_keys_are_unchanged() -> None:
    recorded = _recorded()
    assert len(recorded) >= 8
    caches = [QuestionCache(QUESTIONS_DIR), QuestionCache(TEST_QUESTIONS)]
    for path, state in recorded:
        run = _state_to_run(state)
        assert scoring.format_state(run) == state
        qset = next(q for q in (c.get(run["routine"]) for c in caches) if q is not None)
        assert jev.fixture_key(scoring.format_state(run), qset.jev_questions()) == path.stem


def _non_dispatch_runs() -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for r in routines_log.parse_file(SAMPLE_LOG).runs:
        runs.append({"routine": r.routine, "status": r.status, "summary": r.summary,
                     "failure_reason": None, "meta": r.meta})
    # Odd shapes, and dispatch-like meta on other routines: still the generic state.
    runs += [
        {"routine": "config-check", "status": "fail", "summary": " a\n\tb ", "failure_reason": "x\ny",
         "meta": {"failures": ["one\ntwo", ""], "repo": "r", "workers": [{"name": "w"}]}},
        {"routine": "dispatch-like", "status": "error", "summary": None, "failure_reason": None, "meta": None},
        {"routine": "Dispatch", "status": "pass", "summary": "", "meta": {"repo": "r"}},
        {},
    ]
    return runs


def test_generic_state_is_byte_identical_to_the_frozen_copy() -> None:
    runs = _non_dispatch_runs()
    assert len(runs) > 10
    for run in runs:
        assert scoring.format_state(run) == _frozen_format_state(run)


# ---------------------------------------------------------------- dispatch state


def _dispatch_run(task_id: str) -> dict[str, Any]:
    r = next(r for r in parse_parent(TASKS).runs if r.meta["task_id"] == task_id)
    # What scoring reads back from the database (candidate_runs): JSON round trip.
    return {"id": "ignored", "routine": "dispatch", "status": r.status, "summary": r.summary,
            "failure_reason": r.failure_reason, "meta": json.loads(json.dumps(r.meta)),
            "started_at": r.started_at}


def test_dispatch_state_for_a_blocked_task() -> None:
    state = scoring.format_state(_dispatch_run("t-d2dce9a006"))
    assert state == "\n".join([
        "routine: dispatch",
        "repo: junk-orchestra",
        "status: fail",
        "final_status: BLOCKED",
        "summary: junk-orchestra: BLOCKED, 1 worker, dod_first_pass=false, denials=1 (heredoc), "
        "reasons=denied-command",
        "failure_reason: denied-command",
        "signals: dod_first_pass=false integration_first_pass=(none) follow_ups=0 nudges=0 denials=1 "
        "escalations=1 merge_conflicts=0 clarifying_questions=0",
        "workers:",
        "- worker-d: final_status=BLOCKED first_status=BLOCKED denials=1 denied_shapes=heredoc:1 "
        "errors=isolation-refusal:1 tool_errors=1 repeat_cmds=0 reasons=denied-command "
        "brief_gaps=vilmer-words,dod,siblings,skills,standing-rules lessons_read_first=false",
        "  checkpoint: (none)",
    ])


def test_dispatch_state_carries_excerpts_and_no_ids() -> None:
    runs = [_dispatch_run(r.meta["task_id"]) for r in parse_parent(TASKS).runs]
    for run in runs:
        state = scoring.format_state(run)
        assert state == scoring.format_state(json.loads(json.dumps(run, default=str)))  # deterministic
        assert state.startswith(f"routine: dispatch\nrepo: {run['meta']['repo']}\n")
        assert run["meta"]["task_id"] not in state and run["meta"]["session"] not in state
        for w in run["meta"]["workers"]:
            assert f"- {w['name']}: final_status=" in state
            if w["checkpoint_excerpt"]:
                first_words = " ".join(w["checkpoint_excerpt"].split()[:6])
                assert first_words in state
    # Each line of the state is one logical line: excerpts are collapsed to one line.
    snap = next(run for run in runs if any(w["name"] == "worker-dev-snapshot" for w in run["meta"]["workers"]))
    lines = scoring.format_state(snap).split("\n")
    assert sum(line.startswith("  checkpoint: ### ") for line in lines) >= 1


def test_dispatch_state_without_workers() -> None:
    state = scoring.format_state({"routine": "dispatch", "status": "error", "summary": None, "meta": {}})
    assert state.split("\n")[-1] == "workers: (none)"
    assert "repo: (none)" in state


def test_dispatch_questions_build_a_fixture_key() -> None:
    qset = QuestionCache(QUESTIONS_DIR).get("dispatch")
    assert qset is not None
    assert [q.name for q in qset.questions] == ["verdict", "brief_quality", "main_cause", "lesson_worthy"]
    assert qset.threshold == 0.7 and qset.pass_question == "verdict" and qset.pass_options == ["clean"]
    assert list(qset.spec("verdict").options) == ["clean", "struggled", "failed"]
    assert list(qset.spec("main_cause").options) == ["brief_gap", "tool_denial", "test_failure", "scope", "none"]
    assert qset.spec("brief_quality").kind == "score"
    key = jev.fixture_key(scoring.format_state(_dispatch_run("t-d2dce9a006")), qset.jev_questions())
    assert len(key) == 32


@pytest.mark.parametrize("routine", ["config-check", "t-routine"])
def test_generic_routines_never_take_the_dispatch_branch(routine: str) -> None:
    run = _dispatch_run("t-d2dce9a006") | {"routine": routine}
    assert scoring.format_state(run) == _frozen_format_state(run)
