"""Budget prices (Mission 005 G2 follow-up): the versioned price table, fail-closed booking of paid calls that
report no cost (Codex from tokens, Jev per call), the USD cap over them, and one cost booked once per call.
Unit tests and HostSandbox replays: no Docker, no model, no network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rsi_replay_support import HostSandbox, setup_root

from agent_memory import rsi_budget as BU
from agent_memory import rsi_replay as R
from agent_memory.rsi_budget import Budget, BudgetConfig, BudgetExhausted, PriceConfigError, Usage

EXP = "exp-20261017-01"
LUNA, JEV = "gpt-6-luna", "jev-1.14.0"
# G2 judge call r1 (Codex exec --json turn.completed): input 12979 of which 12976 cache writes, output 427.
G2_JUDGE = {"input_tokens": 12979, "cached_input_tokens": 0, "cache_write_input_tokens": 12976, "output_tokens": 427,
            "reasoning_output_tokens": 388}
G2_JUDGE_USD = (3 * 0.10 + 12976 * 0.125 + 427 * 0.50) / 1e6


def budget(**kw: Any) -> Budget:
    return Budget(BudgetConfig.from_dict({"max_usd": 1.0, "model_calls": {LUNA: 3, JEV: 1}, **kw}))


# ---------------------------------------------------------------- the table


def test_shipped_price_table_is_versioned_labelled_and_fail_closed() -> None:
    t = BU.load_prices()
    raw = json.loads(BU.PRICES_PATH.read_text())
    assert t.describe() == {"format": "rsi-prices/1", "version": "2026-10-17", "sha256": t.sha256}
    assert len(t.sha256) == 64
    assert "not verified" in raw["_note"].lower()
    for name, entry in raw["models"].items():                    # every assumed price says where it came from
        assert "not verified" in entry["source"], name
    luna, named = t.price(LUNA)
    assert named and luna.basis == "tokens" and luna.input_per_mtok > 0 and luna.output_per_mtok > 0
    jev, named = t.price(JEV)
    assert named and jev.basis == "call" and jev.per_call_usd > 0
    default, named = t.price("some-new-paid-model")
    assert not named and default.unknown_usage_usd >= 1.0          # deliberately high: stops a budget early
    assert default.input_per_mtok >= 10 * luna.input_per_mtok and default.output_per_mtok >= 10 * luna.output_per_mtok


def test_codex_tokens_are_priced_per_part_and_reasoning_is_not_counted_twice() -> None:
    t = BU.load_prices()
    usd, basis = t.book(LUNA, tokens_in=12979, tokens_out=427, tokens_cache_write=12976)
    assert basis == "table" and usd == pytest.approx(G2_JUDGE_USD)
    usd_all_plain, _ = t.book(LUNA, tokens_in=12979, tokens_out=427)
    assert usd_all_plain == pytest.approx((12979 * 0.10 + 427 * 0.50) / 1e6)
    cached, _ = t.book(LUNA, tokens_in=1000, tokens_out=0, tokens_cached=1000)
    assert cached == pytest.approx(1000 * 0.01 / 1e6)
    # Parts larger than the input cannot make a negative or larger-than-input count.
    odd, _ = t.book(LUNA, tokens_in=100, tokens_out=0, tokens_cached=80, tokens_cache_write=80)
    assert odd == pytest.approx((80 * 0.01 + 20 * 0.125) / 1e6)


def test_book_order_reported_then_table_then_default_then_unknown_usage() -> None:
    t = BU.load_prices()
    assert t.book("claude-haiku-5-5", reported_usd=0.0029, tokens_in=55000) == (0.0029, "reported")
    assert t.book(LUNA, reported_usd=0.5, tokens_in=10) == (0.5, "reported")       # a provider's number wins
    assert t.book(JEV) == (0.01, "call")
    assert t.book(JEV, tokens_in=10_000) == (0.01, "call")                        # flat, whatever the tokens
    usd, basis = t.book("gpt-7-mystery", tokens_in=1000, tokens_out=100)
    assert basis == "default" and usd == pytest.approx((1000 * 15 + 100 * 75) / 1e6)
    assert t.book("gpt-7-mystery") == (1.0, "unknown-usage")
    assert t.book(LUNA) == (0.05, "unknown-usage")                                 # its own fallback
    for free in ("stub", "stub-agent", "jev-fixture", "hypothesizer-fixture", "implementer-stub", "none"):
        assert t.book(free, tokens_in=10 ** 6, tokens_out=10 ** 6) == (0.0, "free")
    assert t.book(None) == (0.0, "none") and t.book(None, reported_usd=0.1) == (0.1, "reported")


@pytest.mark.parametrize("mutate, match", [
    (lambda d: d.update(format="rsi-prices/2"), "format"),
    (lambda d: d.pop("version"), "version"),
    (lambda d: d.update(surprise=1), "unknown keys"),
    (lambda d: d["models"][JEV].update(per_call_usd=0), "per_call_usd > 0"),
    (lambda d: d["models"][LUNA].update(input_per_mtok=0), "input_per_mtok > 0"),
    (lambda d: d["models"][LUNA].update(output_per_mtok=-1), "0..1000"),
    (lambda d: d["models"][LUNA].update(per_call_usd=0.1), "basis 'call'"),
    (lambda d: d["models"][JEV].update(input_per_mtok=1.0), "basis 'tokens'"),
    (lambda d: d["models"][LUNA].update(unknown_usage_usd=0), "unknown_usage_usd must be > 0"),
    (lambda d: d["models"][LUNA].update(basis="flat"), "basis must be"),
    (lambda d: d["models"].update({"stub-agent": {"basis": "call", "per_call_usd": 1}}), "free stand-ins"),
    (lambda d: d["default"].pop("unknown_usage_usd"), "default must be"),
    (lambda d: d.update(default={"basis": "call", "per_call_usd": 1.0, "unknown_usage_usd": 1.0}), "default must be"),
])
def test_a_bad_price_table_refuses(tmp_path: Path, mutate: Any, match: str) -> None:
    d = json.loads(BU.PRICES_PATH.read_text())
    mutate(d)
    p = tmp_path / "prices.json"
    p.write_text(json.dumps(d))
    with pytest.raises(PriceConfigError, match=match):
        BU.load_prices(p)


def test_a_missing_price_table_refuses_the_budget(tmp_path: Path) -> None:
    with pytest.raises(PriceConfigError, match="unreadable"):
        BU.load_prices(tmp_path / "nope.json")


# ---------------------------------------------------------------- the budget books them


def test_codex_judge_call_is_priced_from_tokens_and_booked_once() -> None:
    b = budget()
    b.take_call(LUNA, stage="replay-judge")
    b.take_sandbox_run("J-r0")
    b.record_call(LUNA, stage="replay-judge", tokens_in=12979, tokens_out=427, tokens_cache_write=12976, usd=0.0)
    b.record_run("J-r0", duration_s=9.7, model=LUNA, exit_code=0, cost_on_call=True,
                 tokens_in=12979, tokens_out=427, usd=0.0)        # whatever a caller passes: the call holds the cost
    (call,) = b.calls
    (run,) = b.runs
    assert call["price_basis"] == "table" and call["usd"] == pytest.approx(G2_JUDGE_USD)
    assert run["price_basis"] == "on-call" and run["usd"] == 0.0 and run["tokens_in"] == run["tokens_out"] == 0
    assert b.spent_usd() == round(G2_JUDGE_USD, 6)
    assert b.costs()["by_model"][LUNA] == {"calls": 1, "tokens_in": 12979, "tokens_out": 427,
                                           "usd": round(G2_JUDGE_USD, 6)}
    assert b.costs()["prices"]["version"] == "2026-10-17"


def test_jev_call_is_booked_at_its_flat_price() -> None:
    b = budget()
    b.take_call(JEV, stage="replay-jev")
    b.record_call(JEV, stage="replay-jev", duration_s=0.49)
    assert (b.calls[0]["usd"], b.calls[0]["price_basis"]) == (0.01, "call") and b.spent_usd() == 0.01


def test_usd_cap_refuses_and_stops_on_codex_and_jev_spend() -> None:
    b = budget(max_usd=0.015)
    b.take_call(JEV, stage="replay-jev")                # table estimate 0.01 fits under 0.015
    b.record_call(JEV, stage="replay-jev")
    with pytest.raises(BudgetExhausted) as e:           # no configured estimate: the table's 0.05 Codex fallback
        b.take_call(LUNA, stage="replay-judge")
    assert e.value.kind == "usd" and "estimate 0.0500" in str(e.value)
    b2 = budget(max_usd=0.05, cost_estimate_usd={LUNA: 0.0001})
    b2.take_call(LUNA, stage="replay-judge")
    b2.record_call(LUNA, stage="replay-judge", tokens_in=1_000_000, tokens_out=0)    # $0.10, over the cap
    with pytest.raises(BudgetExhausted) as e:
        b2.take_call(LUNA, stage="replay-judge")
    assert e.value.kind == "usd" and b2.exhausted == "usd"
    with pytest.raises(BudgetExhausted):
        b2.take_sandbox_run("next")                     # every later stage stops too
    b3 = budget(max_usd=0.019, model_calls={JEV: 5})
    b3.take_call(JEV, stage="replay-jev")
    b3.record_call(JEV, stage="replay-jev")
    with pytest.raises(BudgetExhausted) as e:           # 0.01 booked + 0.01 estimate > 0.019
        b3.take_call(JEV, stage="replay-jev")
    assert e.value.kind == "usd"


def test_unknown_paid_model_is_never_booked_at_zero() -> None:
    b = Budget(BudgetConfig.from_dict({"max_usd": 100.0, "default_model_calls": 5}))
    assert b.estimate("gpt-7-mystery") == 1.0 and b.estimate("stub-agent") == 0.0
    b.record_call("gpt-7-mystery", stage="x")
    b.record_call("gpt-7-mystery", stage="x", tokens_in=100, tokens_out=10)
    b.record_call("gpt-7-mystery", stage="x", ok=False, error="timeout")             # a failed call is booked too
    b.record_run("r1", model="codex", exit_code=1)                                   # a run of an unpriced model
    b.record_run("r2", model="stub-agent", exit_code=0)
    b.record_run("chk", model=None, exit_code=0)
    assert [c["price_basis"] for c in b.calls] == ["unknown-usage", "default", "unknown-usage"]
    assert all(c["usd"] > 0 for c in b.calls) and b.runs[0]["usd"] == 1.0
    assert (b.runs[1]["usd"], b.runs[1]["price_basis"]) == (0.0, "free")
    assert (b.runs[2]["usd"], b.runs[2]["price_basis"]) == (0.0, "none")
    # A configured estimate of 0 for a paid model does not switch the pre-check off.
    b2 = Budget(BudgetConfig.from_dict({"max_usd": 0.5, "model_calls": {"opus": 1}, "cost_estimate_usd": {"opus": 0}}))
    with pytest.raises(BudgetExhausted) as e:
        b2.take_call("opus", "s")
    assert e.value.kind == "usd"


def test_budget_call_passes_cache_parts_to_the_price() -> None:
    b = budget()
    b.call(LUNA, "replay-judge", lambda: ("ok", Usage(tokens_in=12979, tokens_out=427, tokens_cache_write=12976)))
    assert b.calls[0]["usd"] == pytest.approx(G2_JUDGE_USD) and b.calls[0]["tokens_cache_write"] == 12976


def test_a_price_basis_from_the_caller_is_kept_only_for_a_priced_run() -> None:
    b = budget()
    b.record_run("A-r0", model="claude-haiku-5-5", usd=0.05, price_basis="cli-cap")
    b.record_run("A-r1", model="claude-haiku-5-5", usd=0.002)
    assert [(r["usd"], r["price_basis"]) for r in b.runs] == [(0.05, "cli-cap"), (0.002, "reported")]


# ---------------------------------------------------------------- the replay books them (HostSandbox)


CLAUDE_OUT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "ok",
                         "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 5, "costUSD": 0.001}}})


def judge_out(usage: dict) -> str:
    return "\n".join(json.dumps(e) for e in [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": '{"winner": "tie", "reason": "r"}'}},
        {"type": "turn.completed", "usage": usage}])


def live_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, b: Budget, judge_usage: dict,
                agent_stdout: str = CLAUDE_OUT) -> tuple[R.ReplayResult, list[str]]:
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    creds = tmp_path / "dummy-creds.txt"
    creds.write_text("ANTHROPIC_API_KEY=sk-ant-api03-DUMMY-NOT-A-KEY\nCODEX_API_KEY=sk-proj-DUMMY-NOT-A-KEY\n")

    def fault(rid: str, arm: str) -> dict | None:
        if arm in "AB":
            return {"exit_code": 0, "stdout": agent_stdout}
        if arm == "J":
            return {"exit_code": 0, "stdout": judge_out(judge_usage)}
        return None
    sb = HostSandbox(root, fault=fault)
    monkeypatch.setattr(sb, "net_status", lambda: {"internal": {"internal": True}, "proxy_running": True})
    asked: list[str] = []

    def ask(state: str, qs: Any) -> dict:
        asked.append(state)
        return {"effect": SimpleNamespace(value="no_effect", confidence=0.8),
                "harness_problem": SimpleNamespace(value=0.1), "evidence_strength": SimpleNamespace(value=0.5)}
    monkeypatch.setattr(R.J, "mode", lambda: "live")
    monkeypatch.setattr(R.J, "ask", ask)
    cfg = R.ReplayConfig(agent="claude", model="claude-haiku-5-5", reps=3, judge="codex", judge_model=LUNA, jev="live",
                         credentials=f"env-file:{creds}", judge_credentials=f"env-file:{creds}",
                         judge_credential_name="CODEX_API_KEY", max_budget_usd=0.05)
    res = R.run_replay(h, R.load_cases(["report-todos"]), cfg, root=root, experiment_id=EXP, sandbox=sb, budget=b)
    return res, asked


def test_live_replay_books_codex_from_tokens_jev_per_call_and_each_judge_once(tmp_path, monkeypatch):
    b = budget(max_sandbox_runs=15)
    res, asked = live_replay(tmp_path, monkeypatch, b, G2_JUDGE)
    assert len(asked) == 1 and res.budget_stop is None
    judge_calls = [c for c in b.calls if c["stage"] == "replay-judge"]
    jev_calls = [c for c in b.calls if c["stage"] == "replay-jev"]
    assert len(judge_calls) == 3 and all(c["price_basis"] == "table" for c in judge_calls)
    assert all(c["usd"] == pytest.approx(G2_JUDGE_USD) and c["tokens_cache_write"] == 12976 for c in judge_calls)
    assert [(c["usd"], c["price_basis"]) for c in jev_calls] == [(0.01, "call")]
    j_runs = [r for r in b.runs if "-J-" in r["run_id"]]
    assert len(j_runs) == 3 and all(r["usd"] == 0 and r["tokens_in"] == 0 and r["price_basis"] == "on-call"
                                    for r in j_runs)
    agent_runs = [r for r in b.runs if r["model"] == "claude-haiku-5-5"]
    assert len(agent_runs) == 6 and all((r["usd"], r["price_basis"]) == (0.001, "reported") for r in agent_runs)
    want = 6 * 0.001 + 3 * G2_JUDGE_USD + 0.01
    assert b.spent_usd() == pytest.approx(want, abs=1e-6)
    assert b.costs()["by_model"][LUNA]["tokens_in"] == 3 * 12979                     # not 6 x: no double count
    t = res.totals
    assert t["cost_usd"] == pytest.approx(0.006) and t["judge_cost_usd"] == pytest.approx(3 * G2_JUDGE_USD, abs=1e-6)
    assert t["jev_cost_usd"] == 0.01 and t["model_cost_usd"] == pytest.approx(want, abs=1e-6)
    assert t["cost_bases"] == ["call", "reported", "table"]


def test_live_replay_stops_when_codex_spend_passes_the_cap(tmp_path, monkeypatch):
    # The pre-call estimate lets the first judge call in; its real tokens ($0.10) pass the $0.05 cap.
    b = budget(max_usd=0.05, max_sandbox_runs=15, cost_estimate_usd={LUNA: 0.0001, JEV: 0.01})
    res, asked = live_replay(tmp_path, monkeypatch, b, {"input_tokens": 1_000_000, "output_tokens": 0})
    assert res.budget_stop == "usd" and res.verdict.verdict == "INCONCLUSIVE"
    assert len([c for c in b.calls if c["stage"] == "replay-judge"]) == 1 and asked == []
    assert res.totals["judge_cost_usd"] == pytest.approx(0.1)


def test_live_replay_refuses_the_jev_call_that_would_pass_the_cap(tmp_path, monkeypatch):
    # Agents $0.006 + judges about $0.0055; Jev's flat $0.01 would pass the $0.02 cap: refused before the call.
    b = budget(max_usd=0.02, max_sandbox_runs=15, cost_estimate_usd={LUNA: 0.003})
    res, asked = live_replay(tmp_path, monkeypatch, b, G2_JUDGE)
    assert asked == [] and res.budget_stop == "usd" and res.verdict.verdict == "INCONCLUSIVE"
    assert not [c for c in b.calls if c["stage"] == "replay-jev"] and res.totals["jev_cost_usd"] == 0.0


def test_a_codex_agent_without_a_model_is_priced_as_an_unnamed_paid_model(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    out = judge_out({"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100})
    sb = HostSandbox(root, fault=lambda rid, arm: {"exit_code": 0, "stdout": out} if arm in "AB" else None)
    monkeypatch.setattr(sb, "net_status", lambda: {"internal": {"internal": True}, "proxy_running": True})
    b = Budget(BudgetConfig.from_dict({"max_usd": 5.0, "max_sandbox_runs": 4}))
    cfg = R.ReplayConfig(agent="codex", model=None, reps=1, judge="stub", jev="none")
    res = R.run_replay(h, R.load_cases(["report-todos"]), cfg, root=root, experiment_id=EXP, sandbox=sb, budget=b)
    want = (600 * 15 + 400 * 15 + 100 * 75) / 1e6                  # the default has no cached-input discount
    assert [(r.cost_basis, r.tokens_cached) for r in res.runs] == [("default", 400)] * 2
    assert all(r.cost_usd == pytest.approx(want) for r in res.runs)
    agent_runs = [r for r in b.runs if r["model"] == "codex"]
    assert len(agent_runs) == 2 and all(r["usd"] == pytest.approx(want) and r["price_basis"] == "default"
                                        for r in agent_runs)
    assert b.spent_usd() == pytest.approx(2 * want)


def test_a_claude_run_with_no_usage_books_its_cli_cap(tmp_path, monkeypatch):
    b = budget(max_usd=1.0, max_sandbox_runs=15)
    res, _ = live_replay(tmp_path, monkeypatch, b, G2_JUDGE, agent_stdout="")
    assert all((r.cost_usd, r.cost_basis) == (0.05, "cli-cap") for r in res.runs)
    assert all(r["price_basis"] == "cli-cap" and r["usd"] == 0.05 for r in b.runs if r["model"] == "claude-haiku-5-5")
    assert res.verdict.verdict == "INCONCLUSIVE"                                    # unreadable output: run errors
