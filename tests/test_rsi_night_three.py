"""Three experiments per night (Vilmer, 2026-10-25): the scheduled subscription profile's real budget, with fakes
that book one experiment's worst case (calls, sandbox runs, subscription tokens and wall clock, from the paid
plan's `room`). The night must run 3 experiments and stop at the experiment cap, never at a call, token or
wall-clock cap. Same disposable Postgres, temp HOME and temp RSI_SANDBOX_DIR as tests/test_rsi_night.py; no model,
no Docker, no hosted database.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_memory import config, rsi_night as N, rsi_schedule as S
from agent_memory.rsi_budget import Budget, Usage
from rsi_night_support import (RULE_A, RULE_B, FakeReplayer, adapters, candidate, clean_db, make_home, rows, seed_rule,
                               strip_agent_env)

NIGHT = "2026-10-18"
PROFILE = config.REPO_ROOT / "config" / "rsi-night.subscription.json"
OPUS, SOL, JEV = "claude-opus-5-5", "gpt-6.1-sol", "jev-1.14.0"
RULE_C = ("claude-md-global--closing-line-c", "Closing line", 4, 0.3)
RULE_D = ("claude-md-global--explaining-d", "Explaining", 2, 0.5)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home, sandbox=sandbox, tmp=tmp_path)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def worst_case() -> dict[str, Any]:
    """One experiment at its worst case, from the scheduled profile's paid plan."""
    plan = S.paid_plan(N.load_config(PROFILE))
    return plan["room"]


class LiveCostTriage:
    """Books one live Jev call per candidate (as JevTriage live does) and answers `experiment`."""

    health = "live"

    def __init__(self, clock: Clock, overhead_s: float) -> None:
        self.clock, self.overhead_s, self.calls = clock, overhead_s, 0

    def assess(self, c: Any, *, state: str, budget: Budget) -> N.TriageAnswers:
        if self.calls == 0:
            self.clock.t += self.overhead_s          # the night's whole overhead, charged up front
        self.calls += 1
        budget.call(JEV, "triage", lambda: (None, Usage()))
        return N.TriageAnswers("experiment", 0.9, 2.0, 0.0, 2.0, "live")


class OpusHypothesizer:
    """The hypothesizer's one Opus call per candidate, with token usage; two template hypotheses."""

    def __init__(self, tokens: int) -> None:
        self.tokens, self.calls = tokens, 0

    def reply(self, candidate: Any, *, evidence_text: str, targets: Any, cap: int, target_text: str,
              budget: Budget) -> str:
        self.calls += 1
        return budget.call(OPUS, "hypothesize", lambda: (json.dumps(N.template_hypotheses(candidate)),
                                                         Usage(tokens_in=self.tokens - 1000, tokens_out=1000)))


class WorstCaseReplayer(FakeReplayer):
    """A shared-skill replay at its worst case: per provider 3 pairs x 2 arms, each agent run one subscription call
    plus 8 reserved tool containers and 1 check container, 3 Sol judge calls, 1 Jev call; then the clock moves by
    one experiment's full wall-clock bound."""

    def __init__(self, verdicts: Any, clock: Clock, room: dict[str, Any], tokens_per_call: int) -> None:
        super().__init__(verdicts, runs=0, on_replay=self.book)
        self.clock, self.room, self.tokens = clock, room, tokens_per_call

    def book(self, budget: Budget) -> None:
        exp = len(self.calls)
        for model in (OPUS, SOL):
            for i in range(6):
                budget.take_call(model, "replay-agent")
                for k in range(9):
                    rid = f"x{exp}-{model}-{i}-{k}"
                    budget.take_sandbox_run(rid)
                    budget.record_run(rid, duration_s=1.0)
                budget.record_call(model, stage="replay-agent", tokens_in=self.tokens - 500, tokens_out=500)
            for _ in range(3):
                budget.take_call(SOL, "replay-judge")
                budget.record_call(SOL, stage="replay-judge", tokens_in=self.tokens - 500, tokens_out=500)
            budget.take_call(JEV, "replay-jev")
            budget.record_call(JEV, stage="replay-jev")
        self.clock.t += self.room["per_experiment"]["wall_clock_s"]


def seed_four(url: str) -> None:
    for rule, tag in ((RULE_A, "a"), (RULE_B, "b"), (RULE_C, "c"), (RULE_D, "d")):
        seed_rule(url, rule, tag)


def night(env: SimpleNamespace, *, verdicts: Any = "NO_EFFECT") -> tuple[dict[str, Any], Any, Clock]:
    cfg = N.load_config(PROFILE)
    room = worst_case()
    clock = Clock()
    # 19 subscription calls per experiment (1 hypothesis + 12 agent runs + 6 judgments) share the per-experiment
    # token allowance, so 3 experiments use 95% of the night's stop.
    tokens = int(room["per_experiment"]["subscription_tokens"] * 0.95) // 19
    ad = adapters(triage=LiveCostTriage(clock, room["per_night"]["overhead_s"]), hypothesizer=OpusHypothesizer(tokens),
                  replayer=WorstCaseReplayer(verdicts, clock, room, tokens))
    rep = N.Night(cfg, ad, night=NIGHT, db_url=env.url, clock=clock, install_signals=False).run()
    assert N.validate_report(rep) == [], N.validate_report(rep)
    return rep, ad, clock


def test_three_experiments_fill_the_night_and_stop_at_the_experiment_cap(env: SimpleNamespace) -> None:
    seed_four(env.url)
    rep, ad, clock = night(env, verdicts=["NO_EFFECT", "WORSE", "INCONCLUSIVE"])
    assert rep["status"] == "ok", rep["notes"]
    assert not any("budget exhausted" in n for n in rep["notes"]), rep["notes"]
    tested = [e for e in rep["experiments"] if e["experiment_id"]]
    assert [e["status"] for e in tested] == ["not_promotable"] * 3
    assert len({e["candidate_key"] for e in tested}) == 3                    # one experiment per candidate
    waiting = [e for e in rep["experiments"] if not e["experiment_id"]]
    assert [e["status"] for e in waiting] == ["skipped_budget"] * 3          # each candidate's 2nd hypothesis
    assert {e["candidate_key"] for e in waiting} == {e["candidate_key"] for e in tested}
    cands = rep["candidates"]
    assert len(cands) == 4 and sum(c["selected"] for c in cands) == 3
    (rest,) = [c for c in cands if not c["selected"]]
    assert rest["reason"].startswith("deferred: rank") and "experiment slots" in rest["reason"]
    assert candidate(env.url, rest["key"])["status"] == "deferred"
    for e in tested:                                                       # the 2nd hypothesis waits, pending
        assert len(candidate(env.url, e["candidate_key"])["meta"]["pending_hypotheses"]) == 1
    assert len(rows(env.url, "select 1 from rsi_experiments")) == 3
    # every per-night limit had room: the experiment cap is what ended the night
    c, b = rep["costs"], rep["budget"]
    assert b["max_experiments"] == 3 and ad.hypothesizer.calls == 3 and ad.triage.calls == 4
    assert c["by_model"][OPUS]["calls"] == 21 <= b["model_calls"][OPUS]
    assert c["by_model"][SOL]["calls"] == 36 <= b["model_calls"][SOL]
    assert c["by_model"][JEV]["calls"] == 4 + 6 <= b["model_calls"][JEV]
    assert c["sandbox_runs"] == 324 <= b["max_sandbox_runs"]
    used = c["subscription"]["tokens_in"] + c["subscription"]["tokens_out"]
    assert 0.9 * b["max_subscription_tokens"] < used < b["max_subscription_tokens"]
    assert clock.t > 3 * worst_case()["per_experiment"]["wall_clock_s"] and clock.t < b["wall_clock_s"]
    assert c["total_usd"] == pytest.approx(0.10) and c["total_usd"] <= b["max_usd"]


def test_one_covered_candidate_runs_exactly_one_experiment(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    rep, ad, _ = night(env, verdicts="IMPROVES")
    assert rep["status"] == "ok", rep["notes"]
    assert [(e["status"], bool(e["experiment_id"])) for e in rep["experiments"]] == [("staged", True),
                                                                                      ("skipped_budget", False)]
    assert ad.hypothesizer.calls == 1 and len(ad.replayer.calls) == 1
    assert rep["costs"]["by_model"][OPUS]["calls"] == 7 and rep["costs"]["sandbox_runs"] == 108
    assert len(rows(env.url, "select 1 from rsi_proposals where status = 'staged'")) == 1


def test_two_covered_candidates_leave_the_third_slot_empty(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    seed_rule(env.url, RULE_B, "b")
    rep, ad, _ = night(env)
    assert rep["status"] == "ok", rep["notes"]
    assert sum(1 for e in rep["experiments"] if e["experiment_id"]) == 2 and ad.hypothesizer.calls == 2
    assert all(c["selected"] for c in rep["candidates"])
