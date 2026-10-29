from types import SimpleNamespace
import pytest
from agent_memory import rsi_provider_replay as P, rsi_night as N, config


def test_provider_scope_is_specific_or_shared():
    assert P.required_providers([{"path":"~/.claude/CLAUDE.md"}]) == ["claude"]
    assert P.required_providers([{"path":"~/.codex/AGENTS.md"}]) == ["codex"]
    assert P.required_providers([{"path":"~/.agents/skills/demo/SKILL.md"}]) == ["claude","codex"]
    assert P.required_providers([{"path":"~/.claude/CLAUDE.md"},{"path":"~/.codex/AGENTS.md"}]) == ["claude","codex"]


@pytest.mark.parametrize("values,want", [([],"INCONCLUSIVE"),(["IMPROVES","IMPROVES"],"IMPROVES"),
    (["IMPROVES","NO_EFFECT"],"NO_EFFECT"),(["IMPROVES","WORSE"],"WORSE"),
    (["IMPROVES","INCONCLUSIVE"],"INCONCLUSIVE"),(["WORSE","INCONCLUSIVE"],"INCONCLUSIVE")])
def test_all_required_providers_must_improve(values,want):
    assert P.combined_verdict([SimpleNamespace(verdict=v) for v in values]) == want


def test_shared_profile_reserves_both_provider_workloads():
    from agent_memory import rsi_schedule
    cfg=N.load_config(config.REPO_ROOT/'config/rsi-night.subscription.json')
    plan=rsi_schedule.paid_plan(cfg)
    assert plan['ab_pairs_per_experiment']==10 and plan['ab_pairs_per_provider']==5
    # 3 experiments x (1 hypothesis + 1 section writer + 10 Opus runs + 10 Sol runs + 10 Sol judgments); the room
    # check covers every call cap
    assert sum(r['count'] for r in plan['calls'] if r['billing_source']=='subscription')==4+3*32
    assert plan['worst_case_usd']==.11                       # Jev only: 5 triage + 3 x 2 replay calls
    assert plan['room']['per_experiment']['sandbox_runs']==180
    assert cfg.budget.max_sandbox_runs>=3*180
    assert all(c['fits'] for c in plan['room']['checks'] if c['limit'] not in ('wall clock','sandbox runs'))
    containers=next(c for c in plan['room']['checks'] if c['limit']=='sandbox runs')
    assert (containers['need'],containers['cap'],containers['fits'])==(542,540,False)


@pytest.mark.parametrize("second", ["IMPROVES","INCONCLUSIVE"])
def test_shared_results_are_separate_and_stale_pass_is_replaced(tmp_path,monkeypatch,second):
    from agent_memory import rsi_replay as R, rsi_artifact as RA
    artifact='a'*64
    adir=tmp_path/'artifacts'/artifact/'evals'
    adir.mkdir(parents=True)
    old=adir/'sandbox-replay.json'
    old.write_text('old passing record')
    recorded=[]
    class Box:
        def __init__(self,root): self.artifacts=root/'artifacts'
        def record_eval(self,**kw):
            recorded.append(kw)
            old.write_text(kw['verdict'])
            return old
    rs=SimpleNamespace(Sandbox=Box,SandboxError=RuntimeError,load_artifact=lambda path: ({'files':[{'path':'~/.agents/skills/demo/SKILL.md'}]},artifact,{}))
    monkeypatch.setattr(R,'sandbox_module',lambda:rs)
    monkeypatch.setattr(R,'load_cases',lambda ids=None:[1,2,3])
    monkeypatch.setattr(RA,'sandbox_dir',lambda:tmp_path)
    called=[]
    class Adapter:
        def __init__(self,stages,settings):
            self.provider=stages['replay_agent']
            assert settings['profile']==self.provider and 'provider_profiles' not in settings
        def replay(self,**kw):
            called.append(self.provider)
            return N.ReplayOutcome(verdict='IMPROVES' if self.provider=='claude' else second,
                arms={'A':{'runs':3},'B':{'runs':3}},eval_path='per-provider-record',runs=6,
                setup={'model':self.provider},relevance={'eligible_cases':{'c':['~/rule.md']}})
    monkeypatch.setattr(N,'SandboxReplayer',Adapter)
    result=P.replay({}, {'provider_profiles':{'claude':{},'codex':{}}},artifact_hash=artifact,
        experiment_id='exp-1',baseline_id='bl-1',budget=SimpleNamespace(cfg=SimpleNamespace(max_ab_pairs=6)))
    assert called==['claude','codex'] and result.arms=={}
    assert [x['test_setup']['model'] for x in result.provider_results]==['claude','codex']
    assert result.verdict==second
    assert old.read_text()==('improves' if second=='IMPROVES' else 'inconclusive')
    assert list(adir.glob('sandbox-replay.json.superseded-*'))
    assert recorded[0]['results']['required_providers']==['claude','codex']
