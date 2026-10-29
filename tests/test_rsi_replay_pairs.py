"""5 A + 5 B per provider, the judge sees the rule under test, unused tool reservations go back (2026-10-28).

No Docker, no model call: planner and prompt are pure functions, the budget is in memory, the night config is
read from config/rsi-night.subscription.json and planned without running anything.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_memory import rsi_relevance as REL
from agent_memory import rsi_replay as R
from agent_memory import rsi_schedule as S
from agent_memory.rsi_budget import HARD_MAX_SANDBOX_RUNS, Budget, BudgetConfig, BudgetConfigError

PATH = "~/agent-config/agents/worker.md"


def case(cid: str, path: str = PATH, sections: tuple[str, ...] = ()) -> R.ReplayCase:
    target = {"path": path, "kind": "instruction", **({"sections": list(sections)} if sections else {})}
    return R.ReplayCase(id=cid, prompt=f"task {cid}", fixture=Path("."), check="true", targets=(target,))


def manifest(*files: dict) -> dict:
    return {"files": [{"kind": "instruction", **f} for f in files]}


def order(cases, reps: int, pairs: int) -> list[tuple[int, str]]:
    cfg = R.ReplayConfig(reps=reps, pairs=pairs)
    return [(rep, c.id) for rep, c in R._Replay.pair_order(SimpleNamespace(cfg=cfg, cases=list(cases)))]


# ---------------------------------------------------------------- planner: exactly 5 pairs per provider


@pytest.mark.parametrize("n, reps, per_case", [
    (1, 5, [5]), (2, 3, [3, 2]), (3, 2, [2, 2, 1]), (6, 1, [1, 1, 1, 1, 1]),
])
def test_plan_pairs_targets_exactly_five_per_provider(n, reps, per_case):
    cases = [case(f"c{i}") for i in range(n)]
    chosen, r, pairs = R.plan_pairs(manifest({"path": PATH}), cases, min_pairs=5, max_pairs=5)
    assert pairs == 5 and r == reps and len(chosen) == min(n, 5)
    ran = order(chosen, r, pairs)
    assert len(ran) == 5 and len(set(ran)) == 5                  # never more than the cap, no pair twice
    assert [sum(cid == c.id for _, cid in ran) for c in chosen] == per_case


def test_plan_pairs_never_exceeds_the_cap_and_the_cap_wins_over_min_pairs():
    cases = [case(f"c{i}") for i in range(2)]
    chosen, r, pairs = R.plan_pairs(manifest({"path": PATH}), cases, min_pairs=5, max_pairs=3)
    assert pairs == 3 and len(order(chosen, r, pairs)) == 3
    # The config's reps is a floor per case, never a way past the pair target
    chosen, r, pairs = R.plan_pairs(manifest({"path": PATH}), cases, min_pairs=5, max_pairs=5, reps=4)
    assert (r, pairs) == (4, 5) and len(order(chosen, r, pairs)) == 5


def test_plan_pairs_keeps_a_covering_case_for_every_changed_target():
    other = "~/agent-config/parent/PARENT.md"
    cases = [case(f"a{i}") for i in range(6)] + [case("z-only", other)]
    chosen, r, pairs = R.plan_pairs(manifest({"path": PATH}, {"path": other}), cases, min_pairs=5, max_pairs=5)
    assert pairs == 5 and "z-only" in [c.id for c in chosen] and len(chosen) == 5


def test_plan_pairs_respects_section_binding():
    sid = "definition-of-done"
    cases = [case("bound-1", sections=(sid,)), case("bound-2", sections=(sid,)), case("other", sections=("x",))]
    chosen, r, pairs = R.plan_pairs(manifest({"path": PATH, "rule_id": REL.section_rule_id(PATH, sid)}), cases,
                                    min_pairs=5, max_pairs=5)
    assert [c.id for c in chosen] == ["bound-1", "bound-2"] and (r, pairs) == (3, 5)


def test_legacy_plan_cases_runs_cases_equally_and_stays_within_the_cap():
    for n in (1, 2, 3, 6):
        cases = [case(f"c{i}") for i in range(n)]
        chosen, r = R.plan_cases(manifest({"path": PATH}), cases, min_pairs=5, max_pairs=5)
        assert len(chosen) * r <= 5 and r >= 1


def test_replay_config_pairs_limits_the_planned_pairs():
    assert R.ReplayConfig(reps=3).planned_pairs(2) == 6
    assert R.ReplayConfig(reps=3, pairs=5).planned_pairs(2) == 5
    with pytest.raises(R.ReplayError):
        R.ReplayConfig(pairs=0).validate()


# ---------------------------------------------------------------- the judge sees the change


EDIT = {"path": PATH, "section_id": "final-report", "heading": "Final report",
        "before": "## Final report\nOld rule.", "after": "## Final report\nList every failed test by name."}
A1 = {"check_passed": True, "final": "done", "diff": "+x"}
A2 = {"check_passed": False, "final": "done too", "diff": "+y"}


def test_judge_prompt_shows_the_rule_under_test_blind_and_without_a_length_preference():
    p = R.judge_prompt("Fix it", A1, A2, EDIT)
    assert "## The rule under test" in p and "Section: Final report" in p
    assert "List every failed test by name." in p and "Old rule." not in p      # the after text, not the before
    assert "follows the rule under test better while staying correct" in p
    assert "check result is strong evidence" in p
    assert "clear, short explanation" not in p                                # no preference for short answers
    assert "Do not reward or penalize an explanation or report for its\nlength" in p   # length counts neither way
    assert "a longer answer that follows the rule and is correct beats a shorter one" in p
    for leak in ("arm", "baseline", "candidate", "Attempt 1 has the rule", "Attempt 2 has the rule"):
        assert leak not in p
    assert p.index("## The rule under test") < p.index("## Attempt 1")


def test_legacy_judge_prompt_has_no_rule_and_no_short_clause():
    p = R.judge_prompt("Fix it", A1, A2)
    assert p == R.judge_prompt("Fix it", A1, A2, None) == R.judge_prompt("Fix it", A1, A2, {"after": " "})
    assert "rule under test" not in p and "short" not in p
    assert "correctness first" in p and "scope discipline" in p
    assert R.JUDGE_PROMPT_VERSION == "2"


def _artifact(tmp_path: Path, before: str, after: str, rule_id: str) -> tuple[dict, Path]:
    adir = tmp_path / "artifact"
    sha = {}
    for side, sub, text in (("before", "before", before), ("after", "files", after)):
        raw = text.encode()
        sha[side] = hashlib.sha256(raw).hexdigest()
        (adir / sub).mkdir(parents=True, exist_ok=True)
        (adir / sub / sha[side]).write_bytes(raw)
    m = {"files": [{"path": PATH, "kind": "instruction", "rule_id": rule_id,
                    "before_sha256": sha["before"], "after_sha256": sha["after"]}]}
    return m, adir


DOC_BEFORE = "# Worker\n\n## Rules\n\nStay in scope.\n\n## Final report\n\nOld rule.\n"
DOC_AFTER = "# Worker\n\n## Rules\n\nStay in scope.\n\n## Final report\n\nList every failed test by name.\n"


def test_declared_edit_is_recovered_from_the_artifacts_verified_bytes(tmp_path):
    m, adir = _artifact(tmp_path, DOC_BEFORE, DOC_AFTER, REL.section_rule_id(PATH, "final-report"))
    e = R.declared_edit(m, adir)
    assert e == {"path": PATH, "section_id": "final-report", "heading": "Final report",
                 "before": "## Final report\n\nOld rule.", "after": "## Final report\n\nList every failed test by name."}
    p = R.judge_prompt("Fix it", A1, A2, e)
    assert "List every failed test by name." in p and "Section: Final report" in p


def test_declared_edit_is_none_for_legacy_or_tampered_artifacts(tmp_path):
    m, adir = _artifact(tmp_path, DOC_BEFORE, DOC_AFTER, "rsi-night-0123456789")
    assert R.declared_edit(m, adir) is None                                   # legacy rule id: no declared edit
    m, adir = _artifact(tmp_path / "t", DOC_BEFORE, DOC_AFTER, REL.section_rule_id(PATH, "final-report"))
    (adir / "files" / m["files"][0]["after_sha256"]).write_text("changed")
    assert R.declared_edit(m, adir) is None                                   # bytes no longer match their hash
    m, adir = _artifact(tmp_path / "u", DOC_BEFORE, DOC_AFTER, REL.section_rule_id(PATH, "no-such-section"))
    assert R.declared_edit(m, adir) is None


# ---------------------------------------------------------------- check scores


def test_check_score_prefers_the_score_then_scenarios_then_the_binary_result():
    assert R.check_score({"check_passed": False, "check_score": 0.75}) == (0.75, {})
    assert R.check_score({"check_passed": False, "check_scenarios": {"passed": 1, "total": 4}}) == \
        (0.25, {"passed": 1, "total": 4})
    assert R.check_score({"check_passed": True}) == (1.0, {})
    assert R.check_score({"check_passed": False, "check_score": 7}) == (0.0, {})
    run = R.RunRecord(run_id="r", arm="A", case="c", rep=0, check_passed=True)
    assert R._score(run) == 1.0
    run.check_score = 0.4
    assert R._score(run) == 0.4


# ---------------------------------------------------------------- reservations


def _budget(cap: int = 20) -> Budget:
    return Budget(BudgetConfig.from_dict({"max_sandbox_runs": cap}))


def test_unused_reservations_are_released_once_used_ones_stay():
    b = _budget()
    ids = [f"run-tool-{i}" for i in range(8)]
    for rid in ids:
        b.take_sandbox_run(rid)
    assert b.snapshot()["sandbox_runs_taken"] == 8
    assert b.release_sandbox_runs(ids[3:], reason="unused tool containers of run") == 5
    snap = b.snapshot()
    assert snap["sandbox_runs_taken"] == 3 and snap["sandbox_runs_released"] == 5
    with pytest.raises(BudgetConfigError):
        b.release_sandbox_runs(ids[3:4], reason="again")                     # never twice
    with pytest.raises(BudgetConfigError):
        b.release_sandbox_runs(["never-taken"], reason="x")                  # never an unknown run
    with pytest.raises(BudgetConfigError):
        b.release_sandbox_runs([ids[0], ids[0]], reason="x")                 # never the same run twice in one call
    b.record_run(ids[0], duration_s=1.0)
    with pytest.raises(BudgetConfigError):
        b.release_sandbox_runs([ids[0]], reason="x")                         # never a run that was recorded
    assert b.snapshot()["sandbox_runs_taken"] == 3 and b.release_sandbox_runs([], reason="x") == 0
    costs = b.costs()["sandbox_releases"]
    assert costs["released_runs"] == 5 and costs["taken_runs"] == 3 and costs["records"][0]["runs"] == 5


def test_released_slots_make_room_and_the_count_never_goes_negative():
    b = _budget(cap=12)
    for n in range(3):           # three runs of 8 reserved, 2 used each: peak 4 + 8 = 12 (24 without releases)
        ids = [f"r{n}-tool-{i}" for i in range(8)]
        for rid in ids:
            b.take_sandbox_run(rid)
        b.release_sandbox_runs(ids[2:], reason="unused")
    assert b.snapshot()["sandbox_runs_taken"] == 6 and b.snapshot()["sandbox_runs_released"] == 18


class _Log:
    def __init__(self, budget):
        self.budget, self.released_tool_runs, self.events = budget, 0, []

    def log(self, event, **kw):
        self.events.append((event, kw))


def _release(tool_calls, result=True):
    b = _budget()
    ids = [f"run-tool-{i}" for i in range(8)]
    for rid in ids:
        b.take_sandbox_run(rid)
    me = _Log(b)
    rec = R.RunRecord(run_id="run", arm="A", case="c", rep=0)
    res = SimpleNamespace(tool_calls=tool_calls) if result else None
    R._Replay.release_unused_tools(me, rec, ids, res)
    return b, me, rec


def test_release_counts_the_containers_the_bridge_started():
    calls = [{"call": 1, "tool": "task_read_arm", "executed": True}, {"call": None, "executed": False},
             {"call": 2, "tool": "task_shell", "executed": True}, {"call": 3, "tool": "task_shell"}]
    b, me, rec = _release(calls)
    assert rec.runtime["tool_containers"] == {"reserved": 8, "used": 3, "released": 5}
    assert b.snapshot()["sandbox_runs_taken"] == 3 and me.released_tool_runs == 5
    b, _, rec = _release([])
    assert rec.runtime["tool_containers"]["released"] == 8 and b.snapshot()["sandbox_runs_taken"] == 0


def test_release_keeps_every_slot_when_use_is_unknown():
    for calls, result in (([], False), ([{"call": "x"}], True), (["not a record"], True)):
        b, me, rec = _release(calls, result)
        assert rec.runtime["tool_containers"]["released"] == 0 and b.snapshot()["sandbox_runs_taken"] == 8


# ---------------------------------------------------------------- the night: budget and room


def test_subscription_night_budget_fits_three_experiments_at_five_pairs_per_provider():
    cfg = S.load_night_config(Path("config/rsi-night.subscription.json")) if hasattr(S, "load_night_config") \
        else None
    if cfg is None:
        from agent_memory import rsi_night as N
        cfg = N.load_config(Path("config/rsi-night.subscription.json"))
    b = cfg.budget
    assert b.max_experiments == 3 and b.max_ab_pairs == 10 and b.max_ab_pairs // 2 == 5
    assert b.max_sandbox_runs == 3 * 2 * 10 * (8 + 1) <= HARD_MAX_SANDBOX_RUNS
    plan = S.paid_plan(cfg)
    assert plan["ab_pairs_per_provider"] == 5 and plan["ab_pairs_per_experiment"] == 10
    checks = {c["limit"]: c for c in plan["room"]["checks"]}
    for limit in ("calls claude-opus-5-5", "calls gpt-6.1-sol", "calls jev-1.14.0", "A/B pairs per experiment",
                  "subscription tokens"):
        assert checks[limit]["fits"], limit
    assert checks["calls claude-opus-5-5"]["need"] == 2 + 3 * (1 + 1 + 10)  # readiness plus experiment calls
    assert checks["calls gpt-6.1-sol"]["need"] == 2 + 3 * (10 + 10)
    assert checks["calls jev-1.14.0"]["need"] == 5 + 3 * 2
    assert checks["sandbox runs"]["need"] == 542
    assert checks["sandbox runs"]["cap"] == 540 and not checks["sandbox runs"]["fits"]
    # The worst-case wall clock is reported honestly, not hidden: over the hard maximum, so not every
    # experiment finishes when every stage runs to its timeout; the night stops at the wall clock.
    room = plan["room"]
    assert not checks["wall clock"]["fits"] and room["worst_case_wall_clock_s"] > room["hard_max_wall_clock_s"]
    assert 1 <= room["worst_case_experiments_in_wall_clock"] < 3
    text = S.render_plan(plan)
    assert "10 A/B pair(s) (5 per provider" in text and "worst-case wall clock" in text
    assert json.loads(Path("config/rsi-night.subscription.json").read_text())["budget"]["max_experiments"] == 3
