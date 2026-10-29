from types import SimpleNamespace
import pytest
from agent_memory import client_readiness as R, rsi_subscription as S
from agent_memory.rsi_budget import Budget, BudgetConfig


def cfg():
    c={'auth_mode':'subscription','model':S.MODELS['claude'],'effort':'high','auth':{'home':'/fake'}}
    x={'auth_mode':'subscription','model':S.MODELS['codex'],'effort':'high','auth':{'home':'/fake2'}}
    return SimpleNamespace(stages={'hypothesizer':'claude-cli','implementer':'writer','replay_agent':'claude','judge':'codex'},
                           hypothesizer=c,implementer=c,replay={**c,'provider_profiles':{'claude':c,'codex':x},
                           **{'judge_'+k:v for k,v in x.items()}})


def budget():
    return Budget(BudgetConfig.from_dict({'max_usd':1,'model_calls':dict.fromkeys(S.MODELS.values(),8),
                                         'subscription_models':list(S.MODELS.values()),'max_subscription_tokens':10000}))


def test_all_metadata_precedes_any_probe_and_usage_recorded(monkeypatch):
    events=[]
    monkeypatch.setattr(R,'auth_for',lambda *a,**k:None)
    monkeypatch.setattr(S,'preflight',lambda p,**k:events.append(('metadata',p)) or {})
    def run(p,prompt,**k):
        events.append(('probe',p))
        if k['tools_allowed']:
            nonce=prompt.split('printf ')[1].split(' >')[0]
            (k['workspace']/'readiness.txt').write_text(nonce)
        return SimpleNamespace(text='OK',usage={'input_tokens':10,'output_tokens':2},evidence={})
    monkeypatch.setattr(S,'run',run)
    b=budget()
    receipts=R.check(cfg(),b)
    assert len(receipts)==4
    assert all(e[0]=='metadata' for e in events[:4])
    assert all(e[0]=='probe' for e in events[4:])
    assert sum(v['calls'] for v in b.costs()['by_model'].values())==4


def test_metadata_failure_runs_no_inference(monkeypatch):
    monkeypatch.setattr(R,'auth_for',lambda *a,**k:None)
    def fail(*a,**k): raise S.SubscriptionError('unsupported_protocol',inference_started=False)
    monkeypatch.setattr(S,'preflight',fail)
    monkeypatch.setattr(S,'run',lambda *a,**k:pytest.fail('inference started'))
    b=budget()
    with pytest.raises(S.SubscriptionError,match='unsupported_protocol'): R.check(cfg(),b)
    assert b.costs()['by_model']=={}


def test_unstarted_probe_does_not_create_unknown_usage(monkeypatch):
    monkeypatch.setattr(R,'auth_for',lambda *a,**k:None)
    monkeypatch.setattr(S,'preflight',lambda *a,**k:{})
    def fail(*a,**k): raise S.SubscriptionError('subscription_auth_required',inference_started=False)
    monkeypatch.setattr(S,'run',fail)
    b=budget()
    with pytest.raises(S.SubscriptionError,match='subscription_auth_required'): R.check(cfg(),b)
    b.check()
    assert b.reserved_usd()==0
    assert not b.calls


def test_unused_writer_config_does_not_create_probe():
    c=cfg();c.stages['hypothesizer']='stub';c.stages['implementer']='stub'
    selected=R.roles(c)
    assert not any(p=='claude' and not tools for p,settings,tools in selected)


def test_failed_tool_probe_is_accounted_as_failed(monkeypatch):
    monkeypatch.setattr(R,'auth_for',lambda *a,**k:None)
    monkeypatch.setattr(S,'preflight',lambda *a,**k:{})
    monkeypatch.setattr(S,'run',lambda *a,**k:SimpleNamespace(text='OK',usage={'input_tokens':10,'output_tokens':2},evidence={}))
    b=budget()
    with pytest.raises(S.SubscriptionError,match='client_tool_probe_failed'): R.check(cfg(),b)
    assert b.calls[-1]['ok'] is False
    assert b.calls[-1]['tokens_in']==10


def test_judge_preinference_failure_keeps_original_cause_without_unknown_usage(tmp_path,monkeypatch):
    from agent_memory import rsi_replay as replay, rsi_subscription_config as sc
    obj=replay._Replay.__new__(replay._Replay)
    obj.stage_dir=tmp_path
    obj.cfg=SimpleNamespace(judge_model=S.MODELS['codex'],judge='codex',judge_auth_mode='subscription',
                            judge_effort='high',judge_auth={})
    obj.budget=budget();obj.judge_costs={};obj._timeout=lambda _:30
    monkeypatch.setattr(sc,'auth_for',lambda *a,**k:None)
    def fail(*a,**k): raise S.SubscriptionError('codex_permission_contract_mismatch',inference_started=False)
    monkeypatch.setattr(S,'run',fail)
    with pytest.raises(S.SubscriptionError,match='codex_permission_contract_mismatch'):
        obj.live_judge('case',0,'prompt')
    obj.budget.check()
    assert obj.budget.calls==[]
    assert obj.budget.decisions[-1]['decision']=='cancelled_unstarted'
