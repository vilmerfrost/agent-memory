"""Own state shapes (agent_memory/states.py): canary selftest, evals-parent, dispatch friction.

Runs here are synthetic: shaped like real rows, with made-up values.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_memory import jev, scoring, states
from agent_memory.questions import QUESTIONS_DIR, QuestionCache

CANARY = "canary-simplitics-selftest"


def _canary(status: str, summary: str | None, failures: list[str]) -> dict[str, Any]:
    return {"routine": CANARY, "status": status, "summary": summary, "failure_reason": None,
            "meta": {"failures": failures, "duration_s": 3}, "id": "ignored"}


def test_canary_planted_scenario_tags_failures_and_cuts_the_temp_dir() -> None:
    run = _canary("fail", "pass=6 fail=3 warn=2 3s report=/var/folders/zz/T/tmp.AbC123/report-fake-real-host.md", [
        "QPI disabled: QPI_ENABLED=true (docker compose config)",
        "AME host not real: host localhost",
        "connect test: TCP connect to localhost:50001 succeeded",
        "compose run: no AME POST: exit 1, crashed: PermissionError: denied",
    ])
    assert scoring.format_state(run) == "\n".join([
        "routine: canary-simplitics-selftest",
        "status: fail",
        "scenario: fake-real-host",
        "expected: FAIL on the planted checks: QPI disabled, AME host not real, connect test",
        "summary: pass=6 fail=3 warn=2 3s report=report-fake-real-host.md",
        "failure_reason: (none)",
        "planted_failed: 3 of 4 failure lines; other: 1",
        "failures:",
        "- [planted] QPI disabled: QPI_ENABLED=true (docker compose config)",
        "- [planted] AME host not real: host localhost",
        "- [planted] connect test: TCP connect to localhost:50001 succeeded",
        "- [other] compose run: no AME POST: exit 1, crashed: PermissionError: denied",
    ])


def test_canary_state_does_not_depend_on_the_temp_dir() -> None:
    a = _canary("fail", "pass=8 fail=1 3s report=/tmp/tmp.aaa/report-env-local.md", ["no .env.local: present"])
    b = _canary("fail", "pass=8 fail=1 3s report=/var/folders/x/T/tmp.bbb/report-env-local.md",
                ["no .env.local: present"])
    assert scoring.format_state(a) == scoring.format_state(b)
    assert "- [planted] no .env.local: present" in scoring.format_state(a)


@pytest.mark.parametrize(("scenario", "check"), [
    ("setup-log-missing", "setup log: missing or empty: ~/x/worktree-setup.log"),
    ("setup-log-noport", "setup log ports: no numeric PORT= value (none found)"),
    ("setup-log-secret-pattern", "setup log has no secrets: 2 secret pattern(s)"),
    ("dirty-tree", "wt-clean: worktree kept: KEEP canary-x — 1 uncommitted change(s)"),
])
def test_canary_check_names_match_exactly(scenario: str, check: str) -> None:
    state = scoring.format_state(_canary("fail", f"pass=1 fail=1 report=/t/report-{scenario}.md", [check]))
    assert f"- [planted] {check}" in state
    # A check of another scenario is not planted here: "setup log" != "setup log ports".
    other = scoring.format_state(_canary("fail", "pass=1 fail=1 report=/t/report-setup-log-missing.md",
                                         ["setup log ports: no numeric PORT= value"]))
    assert "- [other] setup log ports" in other


def test_canary_clean_missed_unknown_and_aggregate_runs() -> None:
    clean = scoring.format_state(_canary("pass", "pass=9 fail=0 warn=1 3s report=/t/report-clean.md", []))
    assert "scenario: clean\nexpected: PASS with no failures" in clean
    assert "planted_failed: 0 of 0 failure lines; other: 0\nfailures: (none)" in clean

    missed = scoring.format_state(_canary("pass", "pass=9 fail=0 report=/t/report-env-local.md", []))
    assert "expected: FAIL on the planted checks: no .env.local" in missed
    assert "planted_failed: 0 of 0" in missed

    unknown = scoring.format_state(_canary("fail", "report=/t/report-brand-new.md", ["x: y"]))
    assert "scenario: brand-new\nexpected: (unknown scenario: not in the planted-check list)" in unknown
    assert "- x: y" in unknown and "[other]" not in unknown and "planted_failed" not in unknown

    aggregate = scoring.format_state(_canary("pass", "caught=7/7 problems=0 34s", []))
    assert "scenario: (none: no report-<scenario>.md in the summary" in aggregate
    assert "expected: PASS, with every scenario caught" in aggregate


def test_question_file_points_at_the_planted_list_and_scenario_parse() -> None:
    text = (QUESTIONS_DIR / f"{CANARY}.toml").read_text(encoding="utf-8")
    assert "CANARY_PLANTED" in text
    assert states.canary_scenario("x report=/a/b/report-dirty-tree.md") == "dirty-tree"
    assert states.canary_scenario(None) is None


def test_evals_state_explains_that_failure_lines_are_checks_that_did_not_hold() -> None:
    run = {"routine": "evals-parent", "status": "fail", "summary": "pass=5 fail=1 inconclusive=0 547s report=~/r.md",
           "failure_reason": None, "meta": {"failures": ["e-dod: never reported DONE"]}}
    lines = scoring.format_state(run).split("\n")
    assert lines[:6] == ["routine: evals-parent", "status: fail",
                         "summary: pass=5 fail=1 inconclusive=0 547s report=~/r.md", "failure_reason: (none)",
                         "failures:", "- e-dod: never reported DONE"]
    assert lines[6] == f"legend: {states.EVALS_LEGEND}"
    assert "did NOT hold" in lines[6] and len(lines) == 7


def test_dispatch_friction_lists_only_signals_that_cost_rounds() -> None:
    assert states.dispatch_friction({}) == []
    assert states.dispatch_friction({
        "dod_first_pass": True, "integration_first_pass": None, "denials": 0, "nudges": 0, "follow_ups": 0,
        "escalations": 0, "merge_conflicts": 0, "clarifying_questions": 3,
    }) == []  # questions before the work started are not friction
    assert states.dispatch_friction({
        "dod_first_pass": False, "integration_first_pass": False, "denials": 6, "nudges": 1, "follow_ups": 2,
        "escalations": 1, "merge_conflicts": 1, "clarifying_questions": 1,
    }) == ["dod_first_pass=false", "integration_first_pass=false", "denials=6", "nudges=1", "follow_ups=2",
           "escalations=1", "merge_conflicts=1"]
    # Mistyped values are not friction (tasks_ingest turns them into null anyway).
    assert states.dispatch_friction({"denials": True, "nudges": "2", "dod_first_pass": None}) == []


def test_dispatch_state_explains_brief_gaps_and_tool_errors() -> None:
    state = scoring.format_state({"routine": "dispatch", "status": "pass", "summary": "s",
                                  "meta": {"repo": "r", "final_status": "DONE", "dod_first_pass": True,
                                           "workers": [{"name": "w", "brief_gaps": ["dod"],
                                                        "errors": {"nonzero-exit": 2}}]}})
    assert "\nfriction: (none)\n" in state
    legend = state.split("\n")[-1]
    assert "brief_gaps = template sections missing from the brief" in legend
    assert "errors and tool_errors = failed tool calls during the work" in legend


@pytest.mark.parametrize("routine", [CANARY, "evals-parent", "dispatch"])
def test_own_shapes_build_fixture_keys_with_the_shipped_questions(routine: str) -> None:
    qset = QuestionCache(QUESTIONS_DIR).get(routine)
    assert qset is not None
    state = scoring.format_state({"routine": routine, "status": "fail", "summary": "x", "meta": {}})
    assert state == scoring.format_state({"routine": routine, "status": "fail", "summary": "x", "meta": {}})
    assert len(jev.fixture_key(state, qset.jev_questions())) == 32
