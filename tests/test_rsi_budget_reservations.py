"""Money reservations are atomic; policy experiment slots are a different limit. No network/model calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from agent_memory.rsi_budget import (Budget, BudgetConfig, BudgetConfigError, BudgetExhausted,
                                     Price, PriceTable, Usage)
from agent_memory import rsi_night as N

MODEL = "paid-fixture-model"


def budget(*, estimate: float = .11, cap: float = 1, retries: int = 0) -> Budget:
    return Budget(BudgetConfig.from_dict({"max_usd": cap, "model_calls": {MODEL: 20},
                                         "cost_estimate_usd": {MODEL: estimate}, "model_retries": retries}),
                  prices=PriceTable({MODEL: Price("call", per_call_usd=.11)},
                                    Price("tokens", input_per_mtok=15, output_per_mtok=75, unknown_usage_usd=1),
                                    version="fixture/1"))


def test_six_affordable_units_under_one_dollar_without_raising_experiment_policy() -> None:
    b = budget()
    for _ in range(6):
        b.call(MODEL, "fixture-experiment-cost-unit", lambda: (None, Usage(usd=.11)))
    assert b.spent_usd() == .66 and b.reserved_usd() == 0
    assert b.snapshot()["remaining_usd"] == .34
    # Six affordable charges are not authority to run six experiments: that remains a separate policy cap.
    assert b.cfg.max_experiments == 1


def test_conservative_estimate_releases_the_full_reservation_on_each_settlement() -> None:
    b = budget(estimate=.4)
    for _ in range(6):
        rid = b.take_call(MODEL, "example")
        assert b.reserved_usd() == .4
        b.record_call(MODEL, stage="example", usd=.11, reservation_id=rid)
        assert b.reserved_usd() == 0
    assert b.spent_usd() == .66
    assert all(c["reservation"]["released_usd"] == .4 for c in b.calls)
    assert all(c["reservation"]["settled_booked_usd"] == .11 for c in b.calls)


def test_inflight_reservations_cannot_oversubscribe_concurrently() -> None:
    b = budget(estimate=.6)
    gate = Barrier(2)

    def reserve() -> str:
        gate.wait()
        try:
            return b.take_call(MODEL, "concurrent")
        except BudgetExhausted as exc:
            return exc.kind

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert results.count("usd") == 1
    assert b.spent_usd() == 0 and b.reserved_usd() == .6
    refusal = b.decisions[-1]
    assert refusal["remaining_usd"] == .4 and refusal["reserved_usd"] == .6
    rid = next(x for x in results if x != "usd")
    b.record_call(MODEL, stage="concurrent", usd=.1, reservation_id=rid)
    assert b.spent_usd() == .1 and b.reserved_usd() == 0


def test_explicit_reservations_settle_out_of_order_once() -> None:
    b = budget()
    first, second = b.take_call(MODEL, "same"), b.take_call(MODEL, "same")
    b.record_call(MODEL, stage="same", usd=.05, reservation_id=second)
    b.record_call(MODEL, stage="same", usd=.08, reservation_id=first)
    assert [c["reservation"]["id"] for c in b.calls] == [second, first]
    assert b.spent_usd() == .13 and b.reserved_usd() == 0
    with pytest.raises(BudgetConfigError, match="already settled"):
        b.record_call(MODEL, stage="same", usd=.08, reservation_id=first)
    assert b.spent_usd() == .13


def test_an_unstarted_cancel_releases_money_but_not_the_attempt_slot() -> None:
    b = budget(estimate=.8)
    rid = b.take_call(MODEL, "unstarted")
    b.cancel_call(rid)
    assert b.reserved_usd() == b.spent_usd() == 0
    assert b.snapshot()["calls_taken"] == {MODEL: 1}
    assert b.decisions[-1]["decision"] == "cancelled_unstarted"
    with pytest.raises(BudgetConfigError):
        b.cancel_call(rid)


def test_failed_calls_and_retries_each_settle_once() -> None:
    b = budget(estimate=.4, retries=1)
    calls = []

    def run():
        calls.append(1)
        if len(calls) == 1:
            exc = TimeoutError("partial provider result")
            exc.usage = Usage(usd=.05)
            raise exc
        return "done", Usage(usd=.1)

    assert b.call(MODEL, "retry", run, retry_on=(TimeoutError,)) == "done"
    assert [c["attempt"] for c in b.calls] == [1, 2]
    assert [c["ok"] for c in b.calls] == [False, True]
    assert b.spent_usd() == .15 and b.reserved_usd() == 0


def test_reserved_price_version_survives_a_later_table_change() -> None:
    b = budget()
    rid = b.take_call(MODEL, "versioned")
    b.prices = PriceTable({MODEL: Price("call", per_call_usd=.3)}, b.prices.default, version="fixture/2")
    b.record_call(MODEL, stage="versioned", reservation_id=rid)
    assert b.spent_usd() == .11
    assert b.calls[0]["reservation"]["prices"]["version"] == "fixture/1"
    b.call(MODEL, "new-version", lambda: (None, Usage()))
    assert b.spent_usd() == .41


def test_unknown_usage_and_unknown_price_are_never_zero_cost() -> None:
    b = budget()
    b.cfg = BudgetConfig.from_dict({"max_usd": 1, "default_model_calls": 2})
    b.take_call("unknown-model", "unknown")
    b.record_call("unknown-model", stage="unknown", ok=False)
    assert b.spent_usd() == 1 and b.reserved_usd() == 0
    assert b.calls[0]["price_basis"] == "unknown-usage"
    with pytest.raises(BudgetExhausted):
        b.take_call("unknown-model", "next")


def test_provider_charge_over_an_estimate_is_visible_and_stops_later_work() -> None:
    b = budget(estimate=.1)
    b.call(MODEL, "variable-provider", lambda: (None, Usage(usd=1.1)))
    with pytest.raises(BudgetExhausted, match="spent 1.1000"):
        b.check()
    # A local estimate cannot retroactively undo an unbounded provider charge. Do not pretend it stayed under cap.
    assert b.spent_usd() == 1.1 and b.snapshot()["remaining_usd"] == 0 and b.reserved_usd() == 0


def test_sandbox_money_reservation_is_released_when_recorded() -> None:
    b = budget()
    b.cfg = BudgetConfig.from_dict({"max_usd": 1, "max_sandbox_runs": 4, "sandbox_run_estimate_usd": .6})
    b.take_sandbox_run("first")
    with pytest.raises(BudgetExhausted):
        b.take_sandbox_run("second")
    b.record_run("first", usd=.1)
    assert b.reserved_usd() == 0 and b.spent_usd() == .1
    b.take_sandbox_run("second")


def test_nested_budget_failure_settles_unknown_started_call_once() -> None:
    b = budget()
    with pytest.raises(BudgetExhausted):
        b.call(MODEL, "nested", lambda: (_ for _ in ()).throw(BudgetExhausted("sandbox_runs")))
    assert len(b.calls) == 1 and not b.calls[0]["ok"] and b.reserved_usd() == 0


def test_admission_does_not_round_small_booked_charges_away() -> None:
    b = budget(estimate=.0000004, cap=.0000006)
    b.prices = PriceTable({MODEL: Price("call", per_call_usd=.0000004)}, b.prices.default, version="tiny/1")
    b.call(MODEL, "tiny-reported", lambda: (None, Usage(usd=.0000004)))
    assert b.snapshot()["booked_usd"] == .0000004
    with pytest.raises(BudgetExhausted):
        b.take_call(MODEL, "second")


def test_increased_fixed_price_overrides_an_outdated_lower_estimate() -> None:
    b = budget()
    b.call(MODEL, "prior", lambda: (None, Usage(usd=.5)))
    b.prices = PriceTable({MODEL: Price("call", per_call_usd=.6)}, b.prices.default, version="fixture/2")
    with pytest.raises(BudgetExhausted):
        b.take_call(MODEL, "new-price")
    assert b.spent_usd() == .5 and b.reserved_usd() == 0


def test_policy_deferral_records_available_cash_without_claiming_it_was_exhausted() -> None:
    b = budget()
    b.call(MODEL, "already-booked", lambda: (None, Usage(usd=.07)))
    b.take_experiment()
    n = N.Night.__new__(N.Night)
    n.budget, n._evidence = b, {}
    e = n._skipped_entry({"dedupe_key": "skill:demo"},
                         N.Hypothesis("Rewrite the report", "fixed shape", "~/.agents/skills/demo/SKILL.md"), [],
                         policy="one_experiment_per_candidate")
    assert e["status"] == "deferred_policy" and e["experiment_id"] is None
    assert e["skip_reason"] == "one_experiment_per_candidate"
    assert e["budget_decision"]["booked_usd"] == .07
    assert e["budget_decision"]["remaining_usd"] == .93
    assert e["budget_decision"]["resource"] is None
