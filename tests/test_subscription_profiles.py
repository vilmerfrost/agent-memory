import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_memory import config, rsi_night as N, rsi_schedule as S, rsi_replay as R, rsi_subscription as SUB
from agent_memory.rsi_budget import Budget, BudgetConfig
from rsi_replay_support import HostSandbox, setup_root


@pytest.mark.parametrize("provider,model,calls", [("claude","claude-opus-5-5",8),("codex","gpt-6.1-sol",9)])
def test_profiles_pin_subscriptions_models_high_and_accounting(provider, model, calls):
    cfg = N.load_config(config.REPO_ROOT / f"config/rsi-night.subscription-{provider}.json")
    rc = N.SandboxReplayer(cfg.stages,cfg.replay).replay_config()
    rc.validate()
    assert (rc.agent,rc.model,rc.effort,rc.auth_mode) == (provider,model,"high","subscription")
    assert (rc.judge_model,rc.judge_effort,rc.judge_auth_mode) == ("gpt-6.1-sol","high","subscription")
    assert cfg.hypothesizer["model"] == "claude-opus-5-5" and cfg.hypothesizer["effort"] == "high"
    assert cfg.budget.call_cap(model) == calls
    assert S.paid_stages(cfg) == ["replay_jev","triage"]
    plan = S.paid_plan(cfg)
    # 1 hypothesis + 1 section writer + 6 agent runs + 3 judgments
    assert sum(x["count"] for x in plan["calls"] if x["billing_source"] == "subscription") == 11
    assert plan["worst_case_usd"] == .06
    assert "No API fallback" in S.render_plan(plan)


def test_config_cannot_mislabel_api_usage_as_subscription(tmp_path):
    raw=json.loads((config.REPO_ROOT / "config/rsi-night.subscription-claude.json").read_text())
    raw["replay"]["auth_mode"]="api"
    path=tmp_path / "night.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(N.ConfigError,match="API role"):
        N.load_config(path)


def test_config_rejects_subscription_api_key_fallback(tmp_path):
    raw=json.loads((config.REPO_ROOT / "config/rsi-night.subscription-claude.json").read_text())
    raw["replay"]["credentials"]="keychain:rsi-sandbox/anthropic"
    path=tmp_path / "night.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(N.ConfigError,match="API credentials"):
        N.load_config(path)


@pytest.mark.parametrize("provider,model", [("claude","claude-opus-5-5"),("codex","gpt-6.1-sol")])
def test_subscription_replay_routes_both_arms_through_isolated_runner(tmp_path,monkeypatch,provider,model):
    import hashlib
    from agent_memory import rsi_subscription_config as SC
    root,bid,h=setup_root(tmp_path,monkeypatch,"closing-line")
    calls=[]
    def run(p,prompt,**kw):
        calls.append((p,kw["model"],kw["effort"],kw.get("tools_allowed",True)))
        if kw.get("tools_allowed",True):
            path=kw["arm_home"] / ".claude/CLAUDE.md"
            read={"path":"~/.claude/CLAUDE.md","sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                  "success":True,"full_read":True,"source":"sandbox-bridge"}
            # Emulate a task completion; actual objective checks still run via HostSandbox inside the test container.
            for file in kw["workspace"].glob("*.md"):
                file.write_text(file.read_text().replace("teh","the"))
            return SUB.SubscriptionResult("Done",{"input_tokens":100,"output_tokens":10},
                {"verified_reads":[read],"billing_source":"subscription"},[],.01)
        return SUB.SubscriptionResult('{"winner":"tie","reason":"same checked result"}',
              {"input_tokens":30,"output_tokens":10},{},[],.01)
    monkeypatch.setattr(SUB,"run",run)
    monkeypatch.setattr(SC,"auth_for",lambda *a,**kw: None)
    cfg=R.ReplayConfig(agent=provider,model=model,effort="high",reps=3,profile=provider,auth_mode="subscription",auth={"home":str(tmp_path/'auth')},
        judge="codex",judge_model="gpt-6.1-sol",judge_effort="high",judge_auth_mode="subscription",judge_auth={"home":str(tmp_path/'judge')},jev="none")
    caps={"claude-opus-5-5":7,"gpt-6.1-sol":9}
    budget=Budget(BudgetConfig.from_dict({"max_usd":0,"model_calls":caps,"subscription_models":list(caps),
       "max_subscription_tokens":10000,"max_sandbox_runs":64}))
    result=R.run_replay(h,R.load_cases(["docs-typo"]),cfg,root=root,experiment_id="exp-20261017-01",baseline_id=bid,
                        budget=budget,sandbox=HostSandbox(root))
    assert len(calls)==9, result.verdict.reasons
    assert all(p==provider and m==model and e=="high" for p,m,e,t in calls[:6])
    assert all(p=="codex" and m=="gpt-6.1-sol" and e=="high" and t is False for p,m,e,t in calls[6:])
    assert all(run.triggered and run.runtime["billing_source"]=="subscription" for run in result.runs)
    assert budget.costs()["subscription"]["records"]==9
    assert budget.costs()["subscription"]["tokens_in"]==690
    assert budget.spent_usd()==0
    assert not result.verdict.promotable
    assert result.evidence_dir.name == provider
    assert result.eval_path.name == f"sandbox-replay-{provider}.json"
