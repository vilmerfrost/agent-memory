"""rsi_verdict.decide: the deterministic sandbox-replay verdict (every verdict, edge cases) and the policy file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_memory import rsi_verdict as V

POLICY = V.VerdictPolicy()


def machine(*cases: tuple, **kw) -> V.ArmPair:
    """cases: (case, pairs, a_pass, b_pass[, a_score, b_score])."""
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


IMPROVED = machine(("c1", 5, 0, 5))
EQUAL = machine(("c1", 5, 5, 5))


@pytest.mark.parametrize(("name", "m", "j", "e", "verdict", "reason"), [
    # 1-4: preconditions, budget, errors, missing runs -> INCONCLUSIVE
    ("refusal", machine(("c1", 5, 0, 5), refusals=("stale-baseline: x",)), judge(b=5), jev(), "INCONCLUSIVE",
     "stale-baseline"),
    ("not-exact", machine(("c1", 5, 0, 5), refusals=("artifact-not-exact: ~/.claude/settings.json",)), judge(b=5),
     jev(), "INCONCLUSIVE", "artifact-not-exact"),
    ("budget", machine(("c1", 5, 0, 5), budget_stop="sandbox_runs"), judge(b=5), jev(), "INCONCLUSIVE",
     "budget-exhausted:sandbox_runs"),
    ("run-error", machine(("c1", 5, 0, 5), errors=("exp-c1-B-r0: timeout",)), judge(b=5), jev(), "INCONCLUSIVE",
     "run-errors:1>0"),
    ("missing-runs", machine(("c1", 5, 0, 5), runs_done=9), judge(b=5), jev(), "INCONCLUSIVE", "missing-runs:9/10"),
    ("no-cases", machine(), None, None, "INCONCLUSIVE", "no-cases"),
    ("missing-arm", machine(("c1", 5, 0, 5), ("c2", 0, 0, 0)), judge(b=5), jev(), "INCONCLUSIVE", "missing-arm:c2"),
    ("too-few", machine(("c1", 4, 0, 4)), judge(b=4), jev(), "INCONCLUSIVE", "too-few-pairs:4<5"),
    ("jev-harness", IMPROVED, judge(b=5), jev(harness=0.7), "INCONCLUSIVE", "jev-harness-problem"),
    # 5: a regression by the margin anywhere -> WORSE, even next to an improvement and a judge that likes B
    ("worse", machine(("c1", 5, 5, 0)), judge(a=5), jev("worse"), "WORSE", "regressed:c1 A 5/5 -> B 0/5"),
    ("worse-at-margin", machine(("c1", 5, 5, 3)), judge(b=5), jev(), "WORSE", "regressed:c1 A 5/5 -> B 3/5"),
    ("worse-beats-better", machine(("c1", 5, 0, 5), ("c2", 5, 5, 3)), judge(b=10), jev(), "WORSE", "regressed:c2"),
    ("worse-without-judge-or-jev", machine(("c1", 5, 5, 1)), None, None, "WORSE", "regressed:c1"),
    # 6: improvement by the margin needs judge and Jev agreement
    ("improves", IMPROVED, judge(b=5), jev(), "IMPROVES", "improved:c1 A 0/5 -> B 5/5"),
    ("improves-at-margin", machine(("c1", 5, 3, 5)), judge(b=5), jev(), "IMPROVES", "improved:c1 A 3/5 -> B 5/5"),
    ("judge-missing", IMPROVED, None, jev(), "INCONCLUSIVE", "judge-missing"),
    ("judge-none-mode", IMPROVED, V.JudgeResult(mode="none"), jev(), "INCONCLUSIVE", "judge-missing"),
    ("judge-incomplete", IMPROVED, judge(b=4, errors=1), jev(), "INCONCLUSIVE", "judge-incomplete:4/5"),
    ("judge-disagrees", IMPROVED, judge(a=3, b=2), jev(), "INCONCLUSIVE", "judge-disagrees"),
    ("jev-missing", IMPROVED, judge(b=5), None, "INCONCLUSIVE", "jev-missing"),
    ("jev-fixture-missing", IMPROVED, judge(b=5), jev(status="missing"), "INCONCLUSIVE", "jev-missing"),
    ("jev-disagrees", IMPROVED, judge(b=5), jev("no_effect"), "INCONCLUSIVE", "jev-disagrees:no_effect"),
    ("jev-low-confidence", IMPROVED, judge(b=5), jev(confidence=0.49), "INCONCLUSIVE", "jev-low-confidence"),
    # 7: equal checks (inside the margin): never WORSE or IMPROVES
    ("no-effect", EQUAL, judge(tie=5), jev("no_effect"), "NO_EFFECT", "checks-equal"),
    ("no-effect-no-judge", EQUAL, None, None, "NO_EFFECT", "checks-equal"),
    # both arms score 0 everywhere: the checks cannot tell the arms apart
    ("harness-dead-all-fail", machine(("c1", 5, 0, 0)), judge(tie=5), None, "INCONCLUSIVE", "harness-dead:c1"),
    ("judge-prefers-baseline", EQUAL, judge(a=5), jev("worse"), "INCONCLUSIVE", "judge-only-preference:b_winrate 0.00"),
    ("judge-only-preference", EQUAL, judge(b=5), jev(), "INCONCLUSIVE", "judge-only-preference:b_winrate 1.00"),
])
def test_decision_table(name, m, j, e, verdict, reason):
    v = V.decide(m, j, e, POLICY)
    assert v.verdict == verdict, (name, v.reasons)
    assert any(reason in r for r in v.reasons), (name, v.reasons)
    assert v.promotable is (verdict == "IMPROVES")
    assert v.eval_verdict == V.EVAL_VERDICT[verdict]


# ---------------------------------------------------------------- noise margin, both ways


def test_one_flipped_run_is_noise_not_worse():
    """A 5/5, B 4/5: one run's flip. Was WORSE under policy/1 (any 0.01 drop); now NO_EFFECT."""
    v = V.decide(machine(("c1", 5, 5, 4)), judge(a=1, tie=4), jev("worse"), POLICY)
    assert v.verdict == "NO_EFFECT", v.reasons
    assert "within-noise:c1 A 5/5 -> B 4/5" in v.reasons and "score-margin:2" in v.reasons


def test_one_flipped_run_is_noise_not_improves():
    """The mirror: A 4/5, B 5/5, with a judge and Jev that both like B. Still NO_EFFECT."""
    v = V.decide(machine(("c1", 5, 4, 5)), judge(b=5), jev(), POLICY)
    assert v.verdict == "INCONCLUSIVE" and "judge-only-preference:b_winrate 1.00" in v.reasons
    v = V.decide(machine(("c1", 5, 4, 5)), judge(b=2, tie=3), jev(), POLICY)
    assert v.verdict == "NO_EFFECT", v.reasons
    assert "within-noise:c1 A 4/5 -> B 5/5" in v.reasons


@pytest.mark.parametrize(("a", "b", "verdict"), [
    (5, 3, "WORSE"), (5, 4, "NO_EFFECT"), (4, 5, "NO_EFFECT"), (3, 5, "IMPROVES"),
    (2, 4, "IMPROVES"), (4, 2, "WORSE"), (1, 2, "NO_EFFECT"), (2, 1, "NO_EFFECT"),
])
def test_margin_is_symmetric(a, b, verdict):
    # judge (b_winrate 0.7: agrees, not a strong preference) and Jev lean to B everywhere, so only the checks and
    # the margin decide the direction
    assert V.decide(machine(("c1", 5, a, b)), judge(b=2, tie=3), jev(), POLICY).verdict == verdict


def test_judge_alone_never_makes_worse_or_improves():
    for wr_judge in (judge(a=5), judge(b=5), judge(a=4, tie=1), judge(b=4, tie=1), judge(tie=5)):
        for m in (EQUAL, machine(("c1", 5, 2, 2)), machine(("c1", 5, 5, 4)), machine(("c1", 5, 4, 5))):
            for e in (jev("worse"), jev("improves"), None):
                assert V.decide(m, wr_judge, e, POLICY).verdict in ("NO_EFFECT", "INCONCLUSIVE")


def test_judge_b_winrate_zero_with_equal_checks_is_not_worse():
    # 2026-10-27 claude (exp-20261027-7c731284): A 1/3, B 1/3, judge 0/3 for B -> was WORSE (judge-prefers-baseline)
    v = V.decide(machine(("c1", 5, 2, 2)), judge(a=5), jev("worse", 0.77), POLICY)
    assert v.verdict != "WORSE" and v.verdict == "INCONCLUSIVE"
    assert v.reasons == ("checks-equal", "judge-only-preference:b_winrate 0.00")


def test_judge_and_jev_cannot_rescue_a_worse_case():
    v = V.decide(machine(("c1", 5, 5, 2)), judge(b=5), jev("improves", 0.99), POLICY)
    assert v.verdict == "WORSE" and v.reasons == ("regressed:c1 A 5/5 -> B 2/5", "score-margin:2")


# ---------------------------------------------------------------- scores


def test_partial_credit_scores_decide_not_the_pass_bits():
    # No run of either arm passes every scenario (a_pass = b_pass = 0), but B scores 5.0 against A's 3.0.
    m = machine(("c1", 5, 0, 0, 3.0, 5.0))
    v = V.decide(m, judge(b=4, tie=1), jev(), POLICY)
    assert v.verdict == "IMPROVES" and v.reasons[0] == "improved:c1 A 3/5 -> B 5/5"
    # and not harness-dead: a run that passes some scenarios is alive
    assert V.decide(machine(("c1", 5, 0, 0, 0.6, 0.4)), judge(tie=5), None, POLICY).verdict == "NO_EFFECT"
    # partial scores below the margin are noise; at the margin they move, the same both ways
    assert V.decide(machine(("c1", 5, 1, 1, 3.5, 2.0)), judge(tie=5), None, POLICY).verdict == "NO_EFFECT"
    assert V.decide(machine(("c1", 5, 1, 1, 4.25, 2.25)), judge(tie=5), None, POLICY).verdict == "WORSE"
    assert V.decide(machine(("c1", 5, 1, 1, 2.25, 4.25)), judge(b=5), jev(), POLICY).verdict == "IMPROVES"
    v = V.decide(machine(("c1", 5, 2, 2, 2.5, 4.75)), judge(b=5), jev(), POLICY)
    assert v.reasons[0] == "improved:c1 A 2.5/5 -> B 4.75/5"


def test_records_without_scores_behave_via_check_passed():
    old = V.CaseStats("c1", 5, 3, 5)
    assert (old.a_score, old.b_score) == (3.0, 5.0)
    assert V.CaseStats("c1", 5, 3, 5, 3.0, 5.0) == old
    assert V.decide(machine(("c1", 5, 3, 5)), judge(b=5), jev(), POLICY).verdict == "IMPROVES"
    # run level: check_score, then check_scenarios, then the pass bit
    assert V.run_check_score(True) == 1.0 and V.run_check_score(False) == 0.0 and V.run_check_score(None) == 0.0
    assert V.run_check_score(False, 0.75) == 0.75
    assert V.run_check_score(False, None, {"passed": 2, "total": 3}) == pytest.approx(2 / 3)
    assert V.run_check_score(True, None, {"passed": 3, "total": 3}) == 1.0
    # an unusable score falls through to the next source
    for bad in (1.5, -0.1, True, "0.5", float("nan")):
        assert V.run_check_score(True, bad) == 1.0
    for bad in ({"passed": 4, "total": 3}, {"passed": 0, "total": 0}, {"passed": True, "total": 1}, [1, 2]):
        assert V.run_check_score(False, None, bad) == 0.0


def test_case_scores_must_fit_the_pairs():
    with pytest.raises(ValueError, match="b_score"):
        V.CaseStats("c1", 5, 0, 0, 0.0, 5.5)
    with pytest.raises(ValueError, match="a_score"):
        V.CaseStats("c1", 5, 0, 0, -1.0, 0.0)
    with pytest.raises(ValueError, match="a_score"):
        V.CaseStats("c1", 5, 0, 0, float("nan"), 0.0)


def test_harness_dead_reads_scores():
    v = V.decide(machine(("c1", 5, 0, 0, 0.0, 0.0), ("c2", 5, 0, 0)), judge(b=5, tie=5), jev(), POLICY)
    assert v.verdict == "INCONCLUSIVE" and v.reasons == ("harness-dead:c1,c2",)
    off = V.VerdictPolicy(harness_dead_inconclusive=False)
    assert V.decide(machine(("c1", 5, 0, 0)), judge(tie=5), None, off).verdict == "NO_EFFECT"


# ---------------------------------------------------------------- reachability, boundaries, policy


def test_every_verdict_is_reachable_and_only_improves_passes():
    seen = {V.decide(m, j, e, POLICY).verdict for m, j, e in [
        (IMPROVED, judge(b=5), jev()), (EQUAL, judge(tie=5), None), (machine(("c1", 5, 5, 0)), None, None),
        (machine(("c1", 1, 0, 1)), None, None)]}
    assert seen == set(V.VERDICTS)
    passing = {"pass", "improves"}                 # agent-config rsi_artifact.PASSING
    assert [k for k, ev in V.EVAL_VERDICT.items() if ev in passing] == ["IMPROVES"]


@pytest.mark.parametrize(("b_wins", "ties", "a_wins", "verdict"), [
    (0, 5, 0, "IMPROVES"),          # all ties: b_winrate 0.5 == judge_min_b_winrate -> still agrees
    (2, 1, 2, "IMPROVES"),          # 0.5 again
    (1, 2, 2, "INCONCLUSIVE"),      # 0.4 < 0.5
])
def test_judge_threshold_boundary_for_improves(b_wins, ties, a_wins, verdict):
    assert V.decide(IMPROVED, judge(a=a_wins, b=b_wins, tie=ties), jev(), POLICY).verdict == verdict


@pytest.mark.parametrize(("j", "verdict"), [
    (judge(a=1, tie=1, b=0, unfired=2), "NO_EFFECT"),     # 0.375: unfired B wins count as ties
    (judge(a=3, b=1), "INCONCLUSIVE"),                     # 0.25 == 1 - judge_strong (was WORSE under policy/1)
    (judge(b=3, a=1), "INCONCLUSIVE"),                     # 0.75 == judge_strong
    (judge(b=2, a=1, tie=1), "NO_EFFECT"),                 # 0.625
])
def test_equal_checks_judge_boundaries(j, verdict):
    m = machine(("c1", 5, 2, 2))
    assert V.decide(m, j, None, POLICY).verdict == verdict


def test_min_pairs_boundary_and_policy_switches():
    exactly = machine(("c1", 5, 0, 5))
    assert V.decide(exactly, judge(b=5), jev(), POLICY).verdict == "IMPROVES"
    # min_pairs counts all cases' pairs together
    assert V.decide(machine(("c1", 3, 0, 3), ("c2", 2, 0, 2)), judge(b=5), jev(), POLICY).verdict == "IMPROVES"
    lenient = V.VerdictPolicy(min_pairs=1, min_score_margin=1.0, require_judge=False, require_jev=False)
    assert V.decide(machine(("c1", 1, 0, 1)), None, None, lenient).verdict == "IMPROVES"
    tolerant = V.VerdictPolicy(max_error_runs=1)
    m = machine(("c1", 5, 0, 5), errors=("x: timeout",))
    assert V.decide(m, judge(b=5), jev(), tolerant).verdict == "IMPROVES"
    # A difference below the policy margin is no change at all.
    coarse = V.VerdictPolicy(min_score_margin=3.0)
    assert V.decide(machine(("c1", 5, 2, 4)), judge(tie=5), None, coarse).verdict == "NO_EFFECT"
    assert V.decide(machine(("c1", 5, 2, 5)), judge(b=5), jev(), coarse).verdict == "IMPROVES"


def test_decide_is_pure_and_reports_its_inputs():
    a = V.decide(IMPROVED, judge(b=5), jev(), POLICY)
    b = V.decide(IMPROVED, judge(b=5), jev(), POLICY)
    assert a == b
    m = a.inputs["machine"]
    assert m["pairs"] == 5 and m["cases"] == [{"case": "c1", "pairs": 5, "a_pass": 0, "b_pass": 5,
                                                "a_rate": 0.0, "b_rate": 1.0, "a_score": 0.0, "b_score": 5.0}]
    assert a.inputs["judge"]["b_winrate"] == 1.0 and a.inputs["jev"]["effect"] == "improves"
    assert a.inputs["policy"] == {"version": V.POLICY_FORMAT, "sha256": "", "min_pairs": 5, "min_score_margin": 2.0}
    assert json.loads(json.dumps(a.to_json()))["verdict"] == "IMPROVES"


def test_shipped_policy_file_is_the_default_and_hashed():
    p = V.load_policy()
    assert p.version == V.POLICY_FORMAT == "rsi-verdict-policy/2"
    assert {k: v for k, v in p.describe().items() if k != "sha256"} == \
        {k: v for k, v in V.VerdictPolicy().describe().items() if k != "sha256"}
    assert (p.min_pairs, p.min_score_margin) == (5, 2.0)
    assert len(p.sha256) == 64


@pytest.mark.parametrize(("body", "message"), [
    ({"version": "rsi-verdict-policy/0"}, "version"),
    ({"version": "rsi-verdict-policy/1", "min_worse_delta": 0.01}, "version"),
    ({"version": V.POLICY_FORMAT, "min_worse_delta": 0.01}, "unknown keys"),
    ({"version": V.POLICY_FORMAT, "min_pairs": -1}, "bad value for min_pairs"),
    ({"version": V.POLICY_FORMAT, "min_score_margin": 0}, "bad value for min_score_margin"),
    ({"version": V.POLICY_FORMAT, "min_score_margin": -2.0}, "bad value for min_score_margin"),
    ({"version": V.POLICY_FORMAT, "min_score_margin": True}, "bad value for min_score_margin"),
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


def test_integer_margin_loads_as_float(tmp_path: Path):
    p = tmp_path / "policy.json"
    p.write_text(json.dumps({"version": V.POLICY_FORMAT, "min_score_margin": 3}))
    assert V.load_policy(p).min_score_margin == 3.0
