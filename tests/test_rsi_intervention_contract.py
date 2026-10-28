"""Exact declared interventions; no model, hosted API, or production writes."""
import json
from types import SimpleNamespace
from pathlib import Path
import pytest
from agent_memory import edits_sections as ES, rsi_night as N, rsi_relevance as REL
from agent_memory.rsi_budget import Budget, BudgetConfig, Usage

OLD = '# Rules\n\n## Summary\n\n- Keep the summary short.\n\n## Other\n\n- Leave this alone.\n'
AFTER = '## Summary\n\n- Write a two-line summary.\n'
TARGET = '~/.claude/CLAUDE.md'

@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / 'agent-config' / 'improve').mkdir(parents=True)
    (home / '.claude' / 'CLAUDE.md').write_text(OLD)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('AMEM_AGENT_CONFIG_DIR', str(home / 'agent-config'))


def hyp(**change):
    edit = dict(section_id='summary', before=ES.section_text(OLD, 'summary'), after=AFTER)
    edit.update(change)
    return N.Hypothesis('Use a two-line summary', 'Make the output precise', TARGET, edit=edit)


def reply(section):
    return json.dumps(dict(new_section=section,topic='summary shape',why='use the declared output shape',
        summary='changes only the summary shape',kind='contract',kind_reason='one required output shape'))


def implement(h, answer):
    calls=[]
    def writer(prompt):
        calls.append(prompt)
        return answer,Usage(tokens_in=10,tokens_out=10)
    budget=Budget(BudgetConfig.from_dict({'max_usd':0,'model_calls':{'claude-opus-5-5':1},
        'subscription_models':['claude-opus-5-5'],'max_subscription_tokens':10000}))
    result=N.WriterImplementer({},call=writer).implement(h,old_text=OLD,experiment_id='exp-test',baseline_id='bl-test',
        budget=budget,context={'rule_key':'dev:summary'})
    return result,calls


def test_exact_declared_replacement_is_the_only_writer_intervention(monkeypatch):
    monkeypatch.setattr(N.ET,'pinned_sections',lambda *a:False)
    got,calls=implement(hyp(),reply(AFTER))
    assert got.implementer=='writer' and got.contract_verified
    assert got.text==ES.splice(OLD,'summary',AFTER)
    assert len(calls)==1 and 'Exact declared replacement' in calls[0]
    assert '## Other\n\n- Leave this alone.' in got.text


def test_extra_reporting_requirement_is_drift_not_intended_evidence(monkeypatch):
    monkeypatch.setattr(N.ET,'pinned_sections',lambda *a:False)
    got,calls=implement(hyp(),reply(AFTER+'- Announce this check in every reply.\n'))
    assert got.implementer=='stub-fallback' and not got.contract_verified
    assert got.fallback_reason=='writer drift: output differs from the declared edit'
    assert len(calls)==1


@pytest.mark.parametrize('change,reason', [({'before':'stale text'},'baseline is stale'),
    ({'section_id':'other'},'another rule section')])
def test_stale_or_other_section_contract_never_calls_writer(change,reason):
    got,calls=implement(hyp(**change),reply(AFTER))
    assert calls==[] and reason in got.fallback_reason


def test_pinned_declared_section_never_calls_writer(monkeypatch):
    monkeypatch.setattr(N.ET,'pinned_sections',lambda *a:True)
    got,calls=implement(hyp(),reply(AFTER))
    assert calls==[] and 'section pin' in got.fallback_reason


def test_subsection_artifact_requires_declared_behavior_coverage():
    cases=[SimpleNamespace(id='summary',targets=[dict(path=TARGET,kind='instruction',behavior='summary',sections=['summary'])]),
        SimpleNamespace(id='other',targets=[dict(path=TARGET,kind='instruction',behavior='other',sections=['other'])]),
        SimpleNamespace(id='legacy',targets=[dict(path=TARGET,kind='instruction',behavior='unspecified')])]
    evidence=REL.select({'files':[dict(path=TARGET,kind='instruction',rule_id='rsi-section-summary')]},cases)
    assert evidence['eligible_cases']=={'summary':[TARGET]}
    missing=REL.select({'files':[dict(path=TARGET,kind='instruction',rule_id='rsi-section-uncovered')]},cases)
    assert missing['eligible_cases']=={} and missing['uncovered_targets']==[TARGET]


def test_section_pin_is_checked_before_triage_hypothesis_or_replay(tmp_path,monkeypatch,pg_url):
    from rsi_night_support import adapters,clean_db,make_home,seed_rule,RULE_A
    from agent_memory import config
    clean_db(pg_url);make_home(tmp_path,monkeypatch)
    box=tmp_path/'sandbox';box.mkdir();monkeypatch.setenv('RSI_SANDBOX_DIR',str(box))
    seed_rule(pg_url,RULE_A,'pin')
    monkeypatch.setattr(N.ET,'pinned_sections',lambda *a:True)
    class MustNotCall:
        def assess(self,*a,**kw):raise AssertionError('pinned candidate reached triage')
        def reply(self,*a,**kw):raise AssertionError('pinned candidate reached hypothesis generation')
    ad=adapters(implementer=N.WriterImplementer({},call=lambda _: (_ for _ in ()).throw(AssertionError('writer called'))),
        triage=MustNotCall(),hypothesizer=MustNotCall())
    runner=N.Night(N.load_config(config.REPO_ROOT/'config/rsi-night.subscription.json'),ad,night='2026-10-28',db_url=pg_url,install_signals=False)
    runner._test_cases=lambda *a,**kw:['explicit-fixture-coverage']
    result=runner.run()
    assert result['experiments']==[] and result['costs']['by_model']=={}
    assert result['candidates'] and all(not c['selected'] for c in result['candidates'])
    assert any('pinned' in c['reason'] for c in result['candidates'])
    assert ad.replayer.calls==[]


def test_hypothesis_cannot_name_another_heading_than_its_declared_edit():
    from dataclasses import replace
    got,calls=implement(replace(hyp(),what='Add the new line under Other heading'),reply(AFTER))
    assert calls==[] and 'placement conflicts' in got.fallback_reason


@pytest.mark.parametrize('export_status',['ok','partial','failed'])
def test_current_experiment_export_follows_evaluation_ingestion_and_is_optional(tmp_path,monkeypatch,pg_url,export_status):
    from rsi_night_support import adapters,clean_db,make_home,seed_rule,RULE_A,FakeReplayer
    from agent_memory import config
    clean_db(pg_url);make_home(tmp_path,monkeypatch)
    box=tmp_path/'sandbox';box.mkdir();monkeypatch.setenv('RSI_SANDBOX_DIR',str(box))
    seed_rule(pg_url,RULE_A,'export')
    seen=[]
    def exporter(conn,exp_id,*,budget_s):
        assert conn.autocommit and 0 < budget_s <= 90
        assert conn.execute('select count(*) from rsi_eval_results where experiment_id=%s',(exp_id,)).fetchone()[0] >= 2
        seen.append(exp_id)
        return {'experiment_id':exp_id,'status':export_status,'rows':1,'delivered_rows':1,
                'visible_rows':None,'readback_verified':False,'detail':'bounded local fixture'}
    ad=adapters(replayer=FakeReplayer('NO_EFFECT'));ad.exporter=exporter
    result=N.Night(N.load_config(config.REPO_ROOT/'config/rsi-night.json'),ad,night='2026-10-28',db_url=pg_url,install_signals=False).run()
    actual=[e for e in result['experiments'] if e.get('experiment_id')]
    assert seen == [e['experiment_id'] for e in actual]
    assert actual[0]['verdict']=='NO_EFFECT' and actual[0]['langfuse_export']['readback_verified'] is False
    assert result['status']==('ok' if export_status=='ok' else 'partial')


def test_invalid_declared_heading_is_refused_before_writer():
    got,calls=implement(hyp(after='## Other\n\n- Change the other rule.\n'),reply(AFTER))
    assert calls==[] and 'declared edit violates' in got.fallback_reason


def test_legacy_unbound_hypothesis_cannot_reach_writer_or_replay(tmp_path,monkeypatch,pg_url):
    from rsi_night_support import adapters,clean_db,make_home,seed_rule,RULE_A
    from agent_memory import config
    clean_db(pg_url);make_home(tmp_path,monkeypatch)
    box=tmp_path/'sandbox';box.mkdir();monkeypatch.setenv('RSI_SANDBOX_DIR',str(box))
    seed_rule(pg_url,RULE_A,'legacy')
    ad=adapters(implementer=N.WriterImplementer({},call=lambda _: (_ for _ in ()).throw(AssertionError('writer called'))))
    runner=N.Night(N.load_config(config.REPO_ROOT/'config/rsi-night.subscription.json'),ad,night='2026-10-28',db_url=pg_url,install_signals=False)
    runner._test_cases=lambda *a,**kw:['explicit-fixture-coverage']
    result=runner.run()
    assert result['experiments']==[] and ad.replayer.calls==[]
    assert result['candidates'][0]['hypotheses_accepted']==0
    assert any(r['code']=='bad_edit_contract' for r in result['candidates'][0]['hypotheses_rejected'])
