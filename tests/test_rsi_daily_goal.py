"""Daily minimum/target under the real subscription caps. All adapters and stores are local fakes."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from agent_memory import config, jev, questions, rsi_brief as BR, rsi_night as N
from rsi_night_support import RULE_A, RULE_B, FakeReplayer, FakeStager, adapters, rows, seed_rule
from test_rsi_night_three import env as env, seed_four, PROFILE, NIGHT, RULE_C, RULE_D


class Advice:
    health = "fixture"

    def __init__(self, action="wait", *, reason="hard_to_test", mode="fixture"):
        self.action, self.reason, self.mode = action, reason, mode
        self.calls = []

    def assess(self, candidate, *, state, budget):
        self.calls.append(candidate["dedupe_key"])
        return N.TriageAnswers(self.action, 0.1, 1.0, 1.0, 1.0, self.mode, self.reason)


def run(env, *, advice=None, replayer=None, hypothesizer=None, stager=None, cfg=None):
    cfg = cfg or N.load_config(PROFILE)
    ad = adapters(triage=advice or Advice(), replayer=replayer or FakeReplayer("NO_EFFECT"),
                  hypothesizer=hypothesizer, stager=stager)
    rep = N.Night(cfg, ad, night=NIGHT, db_url=env.url, install_signals=False).run()
    assert N.validate_report(rep) == [], N.validate_report(rep)
    return rep, ad


def test_all_five_wait_recommendations_still_run_three_experiments(env):
    seed_four(env.url)
    seed_rule(env.url, ("claude-md-global--fifth", "Fifth", 1, 0.2), "e")
    rep, ad = run(env)
    assert len(ad.triage.calls) == 5 and len(ad.replayer.calls) == 3
    assert rep["experiment_goal"] == {"minimum": 1, "target": 3, "attempted": 3, "replayed": 3,
                                      "minimum_met": True, "target_met": True, "shortfall_reasons": []}
    selected = [c for c in rep["candidates"] if c["selected"]]
    assert len(selected) == 3
    assert all(c["triage"]["action"] == "wait" and c["triage"]["reason"] == "hard_to_test" for c in selected)
    assert all("scheduled exploration despite wait" in c["reason"] for c in selected)
    assert len(rows(env.url, "select 1 from rsi_proposals")) == 0
    assert len(rows(env.url, "select 1 from rsi_experiments")) == 3


@pytest.mark.parametrize("count", [1, 2])
def test_one_or_two_testable_candidates_meet_the_minimum_without_inventing_a_third(env, count):
    seed_rule(env.url, RULE_A, "a")
    if count == 2:
        seed_rule(env.url, RULE_B, "b")
    rep, ad = run(env)
    assert len(ad.replayer.calls) == count
    assert rep["experiment_goal"]["minimum_met"] and not rep["experiment_goal"]["target_met"]
    assert "fewer permitted candidates than the daily target" in rep["experiment_goal"]["shortfall_reasons"]


@pytest.mark.parametrize("blocked", ["empty", "out_of_scope", "uncovered"])
def test_minimum_never_overrides_scope_pins_or_missing_cases(env, blocked):
    # A file pin no longer blocks (2026-10-29: a pinned file is suggest-only and its ideas are ranked like any other,
    # tests/test_rsi_night_suggest.py); a target the night cannot reach at all still does.
    if blocked == "out_of_scope":
        import psycopg
        with psycopg.connect(env.url) as conn:
            conn.execute("insert into rsi_candidates(dedupe_key,source,kind,target) values "
                         "('repo','deviations','edit_instruction','~/Projects/demo/AGENTS.md')")
    elif blocked == "uncovered":
        import psycopg
        p = env.home / ".agents/skills/uncovered/SKILL.md"
        p.parent.mkdir(parents=True)
        p.write_text("# Uncovered instruction")
        with psycopg.connect(env.url) as conn:
            conn.execute("insert into rsi_candidates(dedupe_key,source,kind,target) values "
                         "('uncovered','deviations','edit_instruction','~/.agents/skills/uncovered/SKILL.md')")
    rep, ad = run(env)
    assert not ad.replayer.calls and not ad.triage.calls
    assert rep["status"] == "partial"
    assert rep["experiment_goal"]["replayed"] == 0 and not rep["experiment_goal"]["minimum_met"]
    assert "no permitted candidate with applicable replay cases" in rep["experiment_goal"]["shortfall_reasons"]
    assert "minimum 1, target 3" in rep["notes"][0]


def test_invalid_hypotheses_use_other_assessed_candidates_without_more_triage_calls(env):
    seed_four(env.url)

    class FirstInvalid(N.FixtureHypothesizer):
        calls = 0

        def reply(self, *args, **kw):
            self.calls += 1
            return '{"hypotheses":[]}' if self.calls == 1 else super().reply(*args, **kw)

    rep, ad = run(env, hypothesizer=FirstInvalid())
    assert len(ad.triage.calls) == 4 and ad.hypothesizer.calls == 4 and len(ad.replayer.calls) == 3
    assert rep["experiment_goal"]["target_met"]
    assert any("backfill unused daily experiment slot" in c["reason"] for c in rep["candidates"])


@pytest.mark.parametrize("failure", ["invalid-hypotheses", "stage-refused", "zero-runs"])
def test_preparation_or_zero_run_results_cannot_claim_the_daily_minimum(env, failure):
    seed_four(env.url)

    class Invalid(N.FixtureHypothesizer):
        def reply(self, *args, **kw):
            return '{"hypotheses":[]}'

    rep, ad = run(env, hypothesizer=Invalid() if failure == "invalid-hypotheses" else None,
                  stager=FakeStager("pinned") if failure == "stage-refused" else None,
                  replayer=FakeReplayer("NO_EFFECT", runs=0) if failure == "zero-runs" else None)
    assert not rep["experiment_goal"]["minimum_met"] and rep["experiment_goal"]["replayed"] == 0
    assert rep["status"] == "partial"
    assert rep["experiment_goal"]["attempted"] <= 3
    assert len(rows(env.url, "select 1 from rsi_proposals")) == 0


def test_existing_budget_can_stop_the_goal_without_being_raised(env):
    seed_four(env.url)
    cfg = N.load_config(PROFILE)
    cfg = replace(cfg, budget=replace(cfg.budget, free_model_calls=0))
    rep, ad = run(env, cfg=cfg)
    assert rep["status"] == "budget_exhausted"
    assert not rep["experiment_goal"]["minimum_met"] and not ad.replayer.calls
    assert "budget_exhausted" in rep["experiment_goal"]["shortfall_reasons"]


def test_every_future_triage_answer_has_an_explicit_reason_question_in_the_same_call(monkeypatch):
    qs = questions.load_file(config.REPO_ROOT / "questions/rsi-triage.toml", "rsi-triage").jev_questions()
    assert len([q for q in qs if q.name == "reason"]) == 1
    assert set(next(q for q in qs if q.name == "reason").criteria) == set(N.TRIAGE_REASONS)
    from agent_memory.rsi_budget import Budget
    budget = Budget(N.load_config().budget)
    seen = []

    def ask(state, questions):
        seen.append(questions)
        return {"action": jev.JevResult("action", "choice", "wait", 0.9, {"experiment": 0.1}),
                **{k: jev.JevResult(k, "score", 1, 0.9) for k in ("impact", "risk", "testability")},
                "reason": jev.JevResult("reason", "choice", "prior_worse", 0.9)}

    monkeypatch.setattr(jev, "_ask_fixture", ask)
    answer = N.JevTriage("fixture").assess({}, state="synthetic candidate", budget=budget)
    assert len(seen) == 1 and answer.reason == "prior_worse"
    assert N.triage_explanation(answer) == "An earlier experiment made the result worse."


def test_missing_legacy_reason_stays_unknown_instead_of_being_inferred_from_scores():
    answer = N.TriageAnswers("wait", 0.01, 0, 2, 0, "live")
    assert N.triage_explanation(answer) == "Jev did not provide a reason."


@pytest.mark.parametrize("field", ["action", "reason"])
def test_unrecognized_jev_labels_do_not_become_valid_report_evidence(field, monkeypatch):
    from agent_memory.rsi_budget import Budget
    answers = {"action": jev.JevResult("action", "choice", "wait", 0.9, {"experiment": 0.1}),
               **{k: jev.JevResult(k, "score", 1, 0.9) for k in ("impact", "risk", "testability")},
               "reason": jev.JevResult("reason", "choice", "prior_worse", 0.9)}
    answers[field] = jev.JevResult(field, "choice", "unknown-label", 0.9)
    monkeypatch.setattr(jev, "_ask_fixture", lambda *_: answers)
    triage = N.JevTriage("fixture")
    answer = triage.assess({}, state="synthetic candidate", budget=Budget(N.load_config().budget))
    assert triage.health == "error"
    if field == "action":
        assert answer is None
    else:
        assert answer.reason is None and N.triage_explanation(answer) == "Jev did not provide a reason."


def test_morning_brief_preserves_both_advice_and_actual_exploration_decision(env):
    seed_rule(env.url, RULE_A, "a")
    rep, _ = run(env)
    assert BR.validate_report(rep) == []
    brief = BR.build_brief(rep, BR.Db(), artifacts_root=env.sandbox / "artifacts")
    text = BR.render_markdown(brief)
    assert "triage wait (fixture)" in text and "A short replay may not measure this problem well." in text
    assert "scheduled exploration despite wait" in text and "minimum 1, target 3" in text


@pytest.mark.parametrize("minimum,target", [(True, 3), (-1, 3), (2, 1), (1, 4), (1, 2.5)])
def test_invalid_cadence_settings_do_not_change_budget_caps(tmp_path, minimum, target):
    data = json.loads(PROFILE.read_text())
    data["triage"].update(minimum_experiments=minimum, target_experiments=target)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(data))
    with pytest.raises(N.ConfigError, match="experiment goal"):
        N.load_config(path)


@pytest.mark.parametrize("change", [{"replayed": True}, {"replayed": 4}, {"minimum_met": False},
                                   {"shortfall_reasons": ["false warning"]}])
def test_report_rejects_inconsistent_cadence_evidence(change):
    goal = {"minimum": 1, "target": 3, "attempted": 3, "replayed": 3,
            "minimum_met": True, "target_met": True, "shortfall_reasons": [], **change}
    assert N.experiment_goal_problems(goal)


def test_goal_cannot_claim_replays_that_are_absent_from_the_experiment_records():
    goal = {"minimum": 1, "target": 3, "attempted": 1, "replayed": 1,
            "minimum_met": True, "target_met": False, "shortfall_reasons": ["only one candidate"]}
    assert N.experiment_goal_problems(goal, [])
    assert N.experiment_goal_problems(goal, [{"experiment_id": "exp", "replay_runs": 0}])
    assert N.experiment_goal_problems(goal, [{"experiment_id": "exp", "replay_runs": True}])
    assert not N.experiment_goal_problems(goal, [{"experiment_id": "exp", "replay_runs": 2}])
