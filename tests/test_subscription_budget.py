import pytest

from agent_memory.rsi_budget import Budget, BudgetConfig, BudgetConfigError, BudgetExhausted, Usage

MODEL = "claude-opus-5-5"

def budget(**extra):
    return Budget(BudgetConfig.from_dict({"max_usd": 0.1, "model_calls": {MODEL: 2, "jev-1.14.0": 1},
        "subscription_models": [MODEL], "max_subscription_tokens": 1000, **extra}))

def test_subscription_is_not_an_api_bill_or_free_usage():
    b = budget()
    b.call(MODEL, "replay", lambda: ("ok", Usage(tokens_in=100, tokens_out=20, usd=99)))
    c = b.costs()
    assert c["total_usd"] == 0 and c["total_usd_scope"] == "api-only"
    assert c["subscription"] == {"records": 1, "tokens_in": 100, "tokens_out": 20,
                                 "billed_usd": None, "allowance_used": None, "usage_complete": True}
    assert c["by_basis"]["subscription"]["kind"] == "subscription"
    b.call("jev-1.14.0", "triage", lambda: ("ok", Usage(usd=.01)))
    assert b.spent_usd() == .01

def test_subscription_limits_calls_and_tokens():
    b = budget(max_subscription_tokens=120)
    b.call(MODEL, "replay", lambda: ("ok", Usage(tokens_in=100, tokens_out=20)))
    with pytest.raises(BudgetExhausted, match="subscription_tokens"):
        b.take_call(MODEL, "replay")
    b = budget()
    for _ in range(2):
        b.call(MODEL, "replay", lambda: ("ok", Usage(tokens_in=10, tokens_out=2)))
    with pytest.raises(BudgetExhausted, match="calls:"):
        b.take_call(MODEL, "replay")

def test_missing_subscription_usage_stops_next_call():
    b = budget()
    b.call(MODEL, "replay", lambda: ("ok", Usage()))
    assert b.costs()["subscription"]["usage_complete"] is False
    with pytest.raises(BudgetExhausted, match="subscription_usage_unknown"):
        b.take_call(MODEL, "replay")

@pytest.mark.parametrize("extra", [{"max_subscription_tokens": 0}, {"subscription_models": [MODEL, MODEL]},
                                  {"subscription_models": ["stub"]}, {"subscription_models": [42]}])
def test_invalid_subscription_budget_refuses(extra):
    with pytest.raises(BudgetConfigError):
        budget(**extra)
