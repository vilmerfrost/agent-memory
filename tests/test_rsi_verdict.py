"""rsi_verdict.decide: the deterministic sandbox-replay verdict (every verdict, edge cases) and the policy file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_memory import rsi_verdict as V

POLICY = V.VerdictPolicy()


def machine(*cases: tuple[str, int, int, int], **kw) -> V.ArmPair:
    pairs = sum(c[1] for c in cases)
    kw.setdefault("runs_expected", 2 * pairs)
    kw.setdefault("runs_done", 2 * pairs)
    return V.ArmPair(cases=tuple(V.CaseStats(*c) for c in cases), **kw)


def judge(a: int = 0, b: int = 0, tie: int = 0, unfired: int = 0, errors: int = 0, mode: str = "stub") -> V.JudgeResult:
    return V.JudgeResult(mode=mode, judged=a + b + tie + unfired, wins_a=a, wins_b=b, ties=tie, unfired_b=unfired,
                         errors=errors)


def jev(effect: str = "improves", confidence: float = 0.9, harness: float = 0.05, status: str = "ok") -> V.JevResult:
    return V.JevResult(mode="fixture", status=status, effect=effect, confidence=confidence, harness_problem=harness,
                       evidence_strength=1.5)


IMPROVED = machine(("c1", 3, 0, 3))
EQUAL = machine(("c1", 3, 3, 3))


@pytest.mark.parametrize(("name", "m", "j", "e", "verdict", "reason"), [
    # 1-4: preconditions, budget, errors, missing runs -> INCONCLUSIVE
    ("refusal", machine(("c1", 3, 0, 3), refusals=("stale-baseline: x",)), judge(b=3), jev(), "INCONCLUSIVE",
     "stale-baseline"),
    ("not-exact", machine(("c1", 3, 0, 3), refusals=("artifact-not-exact: ~/.claude/settings.json",)), judge(b=3),
     jev(), "INCONCLUSIVE", "artifact-not-exact"),
    ("budget", machine(("c1", 3, 0, 3), budget_stop="sandbox_runs"), judge(b=3), jev(), "INCONCLUSIVE",
     "budget-exhausted:sandbox_runs"),
    ("run-error", machine(("c1", 3, 0, 3), errors=("exp-c1-B-r0: timeout",)), judge(b=3), jev(), "INCONCLUSIVE",
     "run-errors:1>0"),
    ("missing-runs", machine(("c1", 3, 0, 3), runs_done=5), judge(b=3), jev(), "INCONCLUSIVE", "missing-runs:5/6"),
    ("no-cases", machine(), None, None, "INCONCLUSIVE", "no-cases"),
    ("missing-arm", machine(("c1", 3, 0, 3), ("c2", 0, 0, 0)), judge(b=3), jev(), "INCONCLUSIVE", "missing-arm:c2"),
    ("too-few", machine(("c1", 2, 0, 2)), judge(b=2), jev(), "INCONCLUSIVE", "too-few-pairs:2<3"),
    ("jev-harness", IMPROVED, judge(b=3), jev(harness=0.7), "INCONCLUSIVE", "jev-harness-problem"),
    # 5: any regression -> WORSE, even next to an improvement and a judge that likes B
    ("worse", machine(("c1", 3, 3, 0)), judge(a=3), jev("worse"), "WORSE", "regressed:c1 A 3/3 -> B 0/3"),
    ("worse-beats-better", machine(("c1", 3, 0, 3), ("c2", 3, 3, 2)), judge(b=6), jev(), "WORSE", "regressed:c2"),
    ("worse-without-judge-or-jev", machine(("c1", 3, 3, 1)), None, None, "WORSE", "regressed:c1"),
    # 6: improvement needs judge and Jev agreement
    ("improves", IMPROVED, judge(b=3), jev(), "IMPROVES", "improved:c1 A 0/3 -> B 3/3"),
    ("judge-missing", IMPROVED, None, jev(), "INCONCLUSIVE", "judge-missing"),
    ("judge-none-mode", IMPROVED, V.JudgeResult(mode="none"), jev(), "INCONCLUSIVE", "judge-missing"),
    ("judge-incomplete", IMPROVED, judge(b=2, errors=1), jev(), "INCONCLUSIVE", "judge-incomplete:2/3"),
    ("judge-disagrees", IMPROVED, judge(a=2, b=1), jev(), "INCONCLUSIVE", "judge-disagrees"),
    ("jev-missing", IMPROVED, judge(b=3), None, "INCONCLUSIVE", "jev-missing"),
    ("jev-fixture-missing", IMPROVED, judge(b=3), jev(status="missing"), "INCONCLUSIVE", "jev-missing"),
    ("jev-disagrees", IMPROVED, judge(b=3), jev("no_effect"), "INCONCLUSIVE", "jev-disagrees:no_effect"),
    ("jev-low-confidence", IMPROVED, judge(b=3), jev(confidence=0.49), "INCONCLUSIVE", "jev-low-confidence"),
    # 7: equal checks
    ("no-effect", EQUAL, judge(tie=3), jev("no_effect"), "NO_EFFECT", "checks-equal"),
    ("no-effect-no-judge", EQUAL, None, None, "NO_EFFECT", "checks-equal"),
    ("no-effect-all-fail", machine(("c1", 3, 0, 0)), judge(tie=3), None, "NO_EFFECT", "checks-equal"),
    ("judge-prefers-baseline", EQUAL, judge(a=3), jev("worse"), "WORSE", "judge-prefers-baseline"),
    ("judge-only-preference", EQUAL, judge(b=3), jev(), "INCONCLUSIVE", "judge-only-preference"),
])
def test_decision_table(name, m, j, e, verdict, reason):
    v = V.decide(m, j, e, POLICY)
    assert v.verdict == verdict, (name, v.reasons)
    assert any(reason in r for r in v.reasons), (name, v.reasons)
    assert v.promotable is (verdict == "IMPROVES")
    assert v.eval_verdict == V.EVAL_VERDICT[verdict]


def test_every_verdict_is_reachable_and_only_improves_passes():
    seen = {V.decide(m, j, e, POLICY).verdict for m, j, e in [
        (IMPROVED, judge(b=3), jev()), (EQUAL, judge(tie=3), None), (machine(("c1", 3, 3, 0)), None, None),
        (machine(("c1", 1, 0, 1)), None, None)]}
    assert seen == set(V.VERDICTS)
    passing = {"pass", "improves"}                 # agent-config rsi_artifact.PASSING
    assert [k for k, ev in V.EVAL_VERDICT.items() if ev in passing] == ["IMPROVES"]


@pytest.mark.parametrize(("b_wins", "ties", "a_wins", "verdict"), [
    (0, 3, 0, "IMPROVES"),          # all ties: b_winrate 0.5 == judge_min_b_winrate -> still agrees
    (1, 1, 1, "IMPROVES"),          # 0.5 again
    (0, 2, 1, "INCONCLUSIVE"),      # 0.333 < 0.5
])
def test_judge_threshold_boundary_for_improves(b_wins, ties, a_wins, verdict):
    assert V.decide(IMPROVED, judge(a=a_wins, b=b_wins, tie=ties), jev(), POLICY).verdict == verdict


@pytest.mark.parametrize(("j", "verdict"), [
    (judge(a=1, tie=1, b=0, unfired=2), "NO_EFFECT"),     # 0.375: unfired B wins count as ties
    (judge(a=3, b=1), "WORSE"),                            # 0.25 == 1 - judge_strong
    (judge(b=3, a=1), "INCONCLUSIVE"),                     # 0.75 == judge_strong
    (judge(b=2, a=1, tie=1), "NO_EFFECT"),                 # 0.625
])
def test_equal_checks_judge_boundaries(j, verdict):
    m = machine(("c1", 4, 2, 2))
    assert V.decide(m, j, None, POLICY).verdict == verdict


def test_min_pairs_boundary_and_policy_switches():
    exactly = machine(("c1", 3, 0, 3))
    assert V.decide(exactly, judge(b=3), jev(), POLICY).verdict == "IMPROVES"
    lenient = V.VerdictPolicy(min_pairs=1, require_judge=False, require_jev=False)
    assert V.decide(machine(("c1", 1, 0, 1)), None, None, lenient).verdict == "IMPROVES"
    tolerant = V.VerdictPolicy(max_error_runs=1)
    m = machine(("c1", 3, 0, 3), errors=("x: timeout",))
    assert V.decide(m, judge(b=3), jev(), tolerant).verdict == "IMPROVES"
    # A tiny delta below the policy threshold is no change at all.
    coarse = V.VerdictPolicy(min_improve_delta=0.5, min_worse_delta=0.5)
    assert V.decide(machine(("c1", 3, 1, 2)), judge(tie=3), None, coarse).verdict == "NO_EFFECT"


def test_decide_is_pure_and_reports_its_inputs():
    a = V.decide(IMPROVED, judge(b=3), jev(), POLICY)
    b = V.decide(IMPROVED, judge(b=3), jev(), POLICY)
    assert a == b
    m = a.inputs["machine"]
    assert m["pairs"] == 3 and m["cases"] == [{"case": "c1", "pairs": 3, "a_pass": 0, "b_pass": 3,
                                                "a_rate": 0.0, "b_rate": 1.0}]
    assert a.inputs["judge"]["b_winrate"] == 1.0 and a.inputs["jev"]["effect"] == "improves"
    assert a.inputs["policy"]["version"] == V.POLICY_FORMAT
    assert json.loads(json.dumps(a.to_json()))["verdict"] == "IMPROVES"


def test_shipped_policy_file_is_the_default_and_hashed():
    p = V.load_policy()
    assert p.version == V.POLICY_FORMAT
    assert {k: v for k, v in p.describe().items() if k != "sha256"} == \
        {k: v for k, v in V.VerdictPolicy().describe().items() if k != "sha256"}
    assert len(p.sha256) == 64


@pytest.mark.parametrize(("body", "message"), [
    ({"version": "rsi-verdict-policy/0"}, "version"),
    ({"version": V.POLICY_FORMAT, "min_pairs": -1}, "bad value for min_pairs"),
    ({"version": V.POLICY_FORMAT, "require_jev": "yes"}, "bad value for require_jev"),
    ({"version": V.POLICY_FORMAT, "judge_min_b_winrate": 1.5}, "bad value for judge_min_b_winrate"),
    ({"version": V.POLICY_FORMAT, "approve_self": True}, "unknown keys"),
    ({"version": V.POLICY_FORMAT, "sha256": "0" * 64}, "unknown keys"),
])
def test_bad_policy_files_are_refused(tmp_path: Path, body, message):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(body))
    with pytest.raises(V.PolicyError, match=message):
        V.load_policy(p)
    with pytest.raises(V.PolicyError, match="unreadable"):
        V.load_policy(tmp_path / "missing.json")
