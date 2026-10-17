"""rsi_budget (Mission 005, brief Part D): per-model call caps, the USD cap, bounded retries, sandbox runs,
experiments, the wall clock and cost records. Pure unit tests: no database, no model, no sandbox."""

from __future__ import annotations

import json

import pytest

from agent_memory import rsi_night
from agent_memory.rsi_budget import Budget, BudgetConfig, BudgetConfigError, BudgetExhausted, Usage


def cfg(**kw: object) -> BudgetConfig:
    return BudgetConfig.from_dict({"model_calls": {"opus": 2}, "cost_estimate_usd": {"opus": 1.0}, "max_usd": 3.0, **kw})


def test_shipped_config_is_lean_and_spends_nothing() -> None:
    c = rsi_night.load_config()
    b = c.budget
    assert (b.max_hypotheses, b.max_experiments, b.max_usd) == (2, 1, 0.0)
    assert c.stages == {"triage": "fixture", "hypothesizer": "fixture", "implementer": "stub", "replay_agent": "stub",
                        "judge": "stub", "replay_jev": "fixture"}
    bud = Budget(b)
    with pytest.raises(BudgetExhausted) as e:                  # a paid model is refused until a human raises max_usd
        bud.take_call("opus", "hypothesize")
    assert e.value.kind == "usd"
    with pytest.raises(BudgetExhausted) as e:                  # a model the config does not name: cap 0
        bud.take_call("gpt-unknown", "hypothesize")
    assert e.value.kind == "calls:gpt-unknown"
    bud.take_call("jev-fixture", "triage")                     # free stand-ins are capped, not refused


@pytest.mark.parametrize("bad", [{"max_hypotheses": 3}, {"max_hypotheses": 0}, {"model_retries": 5},
                                 {"max_usd": -1}, {"wall_clock_s": 10 ** 9}, {"model_calls": {"opus": -1}},
                                 {"surprise": 1}, {"max_experiments": True}])
def test_config_ranges_are_enforced(bad: dict) -> None:
    with pytest.raises(BudgetConfigError):
        BudgetConfig.from_dict(bad)


def test_per_model_call_cap_and_records() -> None:
    b = Budget(cfg())
    for _ in range(2):
        assert b.call("opus", "hypothesize", lambda: ("ok", Usage(100, 20, 0.5))) == "ok"
    with pytest.raises(BudgetExhausted) as e:
        b.call("opus", "hypothesize", lambda: ("never", Usage()))
    assert e.value.kind == "calls:opus" and b.exhausted == "calls:opus"
    c = b.costs()
    assert c["by_model"]["opus"] == {"calls": 2, "tokens_in": 200, "tokens_out": 40, "usd": 1.0}
    assert c["total_usd"] == 1.0 and c["sandbox_runs"] == 0


def test_usd_cap_before_and_after_a_call() -> None:
    b = Budget(cfg(max_usd=1.5))
    b.call("opus", "s", lambda: (1, Usage(usd=1.4)))
    with pytest.raises(BudgetExhausted) as e:                   # estimate 1.0 would pass the cap
        b.take_call("opus", "s")
    assert e.value.kind == "usd"
    b2 = Budget(cfg(max_usd=1.5, cost_estimate_usd={"opus": 0.1}))
    b2.call("opus", "s", lambda: (1, Usage(usd=2.0)))            # the real cost overran the estimate
    with pytest.raises(BudgetExhausted) as e:
        b2.check()
    assert e.value.kind == "usd"


def test_retries_are_bounded_and_each_is_a_counted_call() -> None:
    n = {"calls": 0}

    def flaky() -> tuple[str, Usage]:
        n["calls"] += 1
        raise TimeoutError("slow")
    b = Budget(cfg(model_retries=1, model_calls={"opus": 10}))
    with pytest.raises(TimeoutError):
        b.call("opus", "s", flaky, retry_on=(TimeoutError,))
    assert n["calls"] == 2 and [c["attempt"] for c in b.calls] == [1, 2] and not any(c["ok"] for c in b.calls)
    n["calls"] = 0
    with pytest.raises(TimeoutError):                           # retries=5 cannot exceed the config's 1
        b.call("opus", "s", flaky, retry_on=(TimeoutError,), retries=5)
    assert n["calls"] == 2
    n["calls"] = 0
    with pytest.raises(ValueError):                             # not a retryable error: one attempt
        b.call("opus", "s", lambda: (_ for _ in ()).throw(ValueError("bad")), retry_on=(TimeoutError,))
    b3 = Budget(cfg(model_retries=2, model_calls={"opus": 2}))
    with pytest.raises(BudgetExhausted):                        # the cap ends retries too
        b3.call("opus", "s", flaky, retry_on=(TimeoutError,))


def test_budget_exhausted_inside_a_call_is_never_retried() -> None:
    n = {"calls": 0}

    def inner() -> tuple[str, Usage]:
        n["calls"] += 1
        raise BudgetExhausted("sandbox_runs")
    b = Budget(cfg(model_retries=2, model_calls={"opus": 10}))
    with pytest.raises(BudgetExhausted):
        b.call("opus", "s", inner, retry_on=(Exception,))
    assert n["calls"] == 1


def test_sandbox_runs_experiments_and_wall_clock() -> None:
    t = [0.0]
    b = Budget(cfg(max_sandbox_runs=2, max_experiments=1, wall_clock_s=100), clock=lambda: t[0])
    b.take_sandbox_run("r1")
    b.record_run("r1", duration_s=3.0, tokens_in=5, usd=0.25, model="claude-x")
    b.take_sandbox_run("r2")
    with pytest.raises(BudgetExhausted) as e:
        b.take_sandbox_run("r3")
    assert e.value.kind == "sandbox_runs"
    b.take_experiment()
    with pytest.raises(BudgetExhausted) as e:
        b.take_experiment()
    assert e.value.kind == "experiments" and b.experiments_left() == 0
    assert b.remaining_s() == 100
    t[0] = 101.0
    assert b.remaining_s() == 0
    with pytest.raises(BudgetExhausted) as e:
        b.take_call("jev-fixture", "triage")
    assert e.value.kind == "wall_clock"
    c = b.costs()
    assert c["sandbox_runs"] == 1 and c["total_usd"] == 0.25 and c["by_model"]["claude-x"]["usd"] == 0.25


def test_mark_and_since_give_one_experiments_cost() -> None:
    b = Budget(cfg(model_calls={"opus": 5}))
    b.call("opus", "s", lambda: (1, Usage(usd=0.5)))
    m = b.mark()
    b.call("opus", "s", lambda: (1, Usage(tokens_in=7, usd=0.25)))
    b.take_sandbox_run("x")
    b.record_run("x", usd=0.1)
    s = b.since(m)
    assert s["usd"] == 0.35 and s["sandbox_runs"] == 1 and s["tokens_in"] == 7 and len(s["calls"]) == 1
    json.dumps(s)                                               # stored in rsi_experiments.meta


def test_hypothesis_cap_is_two_whatever_the_config() -> None:
    assert Budget(cfg(max_hypotheses=2)).hypothesis_cap() == 2
    assert Budget(cfg(max_hypotheses=1)).hypothesis_cap() == 1
