from datetime import datetime, timezone
from types import SimpleNamespace
import json
import pytest

from agent_memory.integrations import egress, langfuse as L
from agent_memory import rsi_evidence_providers as P, rsi_evidence_pack as EP, rsi_night as N

EXP = 'exp-20261028-bebfd909'
RULE = 'agent:worker:final-report'


@pytest.fixture
def denied(monkeypatch):
    monkeypatch.setattr(egress, '_team', egress.Deny(('repo',)))
    monkeypatch.delenv(egress.DENY_ENV, raising=False)


def run(case='claude.worker-final-report', run_id=None):
    return L.SandboxRun(experiment_id=EXP, run_id=run_id or EXP+'-claude-worker-final-report-A-r0', arm='A', case=case, rep=0,
                        started_at=datetime.now(timezone.utc))


def test_bound_shipped_identifiers_avoid_only_partial_word_collision(denied):
    value=run()
    L.check(value)
    body=json.dumps(L.build_spans(value))
    assert value.case in body and value.run_id in body
    assert L.sensitive('worker-final-report')  # free text semantics never changed
    assert egress.Deny(('repo',)).match('report')


@pytest.mark.parametrize('case,rid', [
    ('claude.worker-final-report-extra',None),
    ('claude.worker-final-report',EXP+'-claude-worker-final-report-B-r0'),
    ('rogue.worker-final-report',None),
    ('claude.parent-repo-profile',EXP+'-claude-parent-repo-profile-A-r0'),
])
def test_unknown_mismatched_or_whole_word_private_id_remains_blocked(denied,case,rid):
    with pytest.raises(L.Refused):L.check(run(case,rid))


def test_explicit_substrings_and_exact_private_words_still_block(denied,monkeypatch):
    monkeypatch.setenv(egress.DENY_ENV,'repo')
    with pytest.raises(L.Refused):L.check(run())
    monkeypatch.delenv(egress.DENY_ENV)
    monkeypatch.setattr(egress,'_team',egress.Deny(('report',)))
    with pytest.raises(L.Refused):L.check(run())


def gate():
    g=P.EgressGate(None,{})
    g._deny=egress.Deny(('repo',))
    return g


def retriever(service='clickhouse',subject=None):
    policy=SimpleNamespace(allows=lambda name:True)
    return P.GatedRetriever(service,SimpleNamespace(),policy,gate=gate(),subject_rule=subject)


def test_named_rule_parameter_is_verified_not_arbitrary_text():
    r=retriever()
    r._preflight_query('rule_gap',{'rule':RULE})
    with pytest.raises(P.Unavailable):r._preflight_query('other',{'rule':RULE})
    with pytest.raises(P.Unavailable):r._preflight_query('rule_gap',{'other':RULE})
    with pytest.raises(P.Unavailable):r._preflight_query('rule_gap',{'rule':RULE+'-private'})
    with pytest.raises(P.Unavailable):gate().check_params({'q':RULE})


def test_recall_requires_exact_derived_query_and_known_origin():
    q=EP._recall_query(RULE)
    retriever('pastdev',RULE)._preflight_query('recall',{'query':q})
    with pytest.raises(P.Unavailable):retriever('pastdev',RULE)._preflight_query('recall',{'query':q+' extra'})
    with pytest.raises(P.Unavailable):retriever('pastdev',None)._preflight_query('recall',{'query':q})


def test_night_keeps_policy_exclusions_out_of_transport_reasons():
    night=N.Night.__new__(N.Night)
    night.health={'clickhouse':'ok'};night.health_reasons={};night.report={'connection_recovery':{'checks':[]}}
    night._merge_health({'clickhouse':'error'},[{'step':'ch.rule_gap','status':'unavailable','detail':'policy-excluded: withheld'}])
    assert night.health['clickhouse']=='ok' and night.health_reasons=={}
    assert night.report['connection_recovery']['checks'][0]['status']=='excluded'


def test_historical_labels_do_not_rewrite_raw_health_or_hide_transport_failures():
    from agent_memory.rsi_brief import service_labels
    report={'health':{'langfuse':'unavailable','clickhouse':'error'},
            'experiments':[{'langfuse_export':{'experiment_id':EXP,'status':'partial','refused':8,'failed':0,'uncertain':0}}],
            'health_reasons':{'langfuse':[f'current replay export {EXP}: partial:'],
                              'clickhouse':['ch.rule_gap: unavailable (policy-excluded: withheld)']},
            'connection_recovery':{'checks':[{'source':'clickhouse','status':'excluded'}]}}
    labels=service_labels(report)
    assert labels['langfuse'].startswith('traces withheld locally (8)')
    assert labels['clickhouse'].startswith('queries withheld')
    assert report['health']['langfuse']=='unavailable'
    report['connection_recovery']['checks'].extend([{'source':'langfuse','status':'failed'}, {'source':'clickhouse','status':'failed'}])
    assert service_labels(report)=={}


def test_malformed_export_counts_cannot_claim_local_only_failure():
    from agent_memory.rsi_brief import service_labels
    for invalid in (None,False,'0',1):
        r={'experiments':[{'langfuse_export':{'status':'partial','refused':8,'failed':invalid,'uncertain':0}}]}
        assert 'langfuse' not in service_labels(r)


@pytest.mark.parametrize('reasons', [[], ['HTTP 401 authentication failed'], ['other error'], [None], 'partial'])
def test_local_trace_counts_cannot_hide_unexplained_aggregate_failure(reasons):
    from agent_memory.rsi_brief import service_labels
    report={'health':{'langfuse':'unavailable'},'health_reasons':{'langfuse':reasons},
            'experiments':[{'langfuse_export':{'experiment_id':EXP,'status':'partial','refused':8,'failed':0,'uncertain':0}}]}
    assert 'langfuse' not in service_labels(report)


@pytest.mark.parametrize('kind',['upload_failure','upload_uncertain','unknown'])
def test_explicit_nonlocal_export_kind_wins_over_counts(kind):
    from agent_memory.rsi_brief import service_labels
    report={'health':{'langfuse':'ok'},'experiments':[{'langfuse_export':{
        'experiment_id':EXP,'status':'partial','refused':8,'failed':0,'uncertain':0,'issue_kind':kind}}]}
    assert 'langfuse' not in service_labels(report)


def test_policy_exclusion_does_not_erase_real_query_or_previous_failure():
    night=N.Night.__new__(N.Night)
    night.health={'clickhouse':'ok'};night.health_reasons={};night.report={'connection_recovery':{'checks':[]}}
    excluded={'step':'ch.rule_gap','status':'unavailable','detail':'policy-excluded: withheld'}
    failed={'step':'ch.rule_weekly_rate','status':'error','detail':'HTTP 401'}
    night._merge_health({'clickhouse':'error'},[excluded,failed])
    assert night.health['clickhouse']=='error' and any('HTTP 401' in x for x in night.health_reasons['clickhouse'])
    night._merge_health({'clickhouse':'error'},[excluded])
    assert night.health['clickhouse']=='error'


def test_query_exclusion_label_does_not_hide_unlogged_auth_error():
    from agent_memory.rsi_brief import service_labels
    report={'health':{'clickhouse':'error'},'health_reasons':{'clickhouse':['HTTP 401']},
            'connection_recovery':{'checks':[{'source':'clickhouse','status':'excluded'}]}}
    assert service_labels(report)=={}


def test_clickhouse_mapper_requires_distinct_derived_provenance():
    from agent_memory.integrations import clickhouse as C
    table=next(t for t in C.TABLES if t.name=='deviations')
    raw={'id':'d1','session_id':'s1','instruction_source':'agent:worker','rule_id':'final-report',
         'writer_model':'report-model','_own':'private'}
    deny=egress.Deny(('repo',))
    assert C.map_row(table,raw,1,deny)['rule_id'] is None
    mapped=C.map_row(table,raw,1,derived_deny=deny)
    assert (mapped['instruction_source'],mapped['rule_id'])==('agent:worker','final-report')
    assert mapped['writer_model'] is None  # no exception for other text in a canonical row
    assert C.map_row(table,raw,1,deny,derived_deny=deny)['rule_id'] is None
    assert C.map_row(table,{**raw,'rule_id':'unknown-final-report'},1,derived_deny=deny)['rule_id'] is None
    assert C.map_row(table,raw,1,derived_deny=egress.Deny(('report',)))['rule_id'] is None
    for ownership in ('private','team',None,'unrecognized'):
        scoped={**raw,'instruction_source':'claude-md:project','_own':ownership}
        result=C.map_row(table,scoped,1,derived_deny=deny)
        assert result['instruction_source']=='claude-md:'+('team' if ownership=='team' else 'private')
        assert result['rule_id'] is None


def test_clickhouse_sink_passes_derived_and_explicit_sets_separately(monkeypatch):
    from agent_memory.integrations import clickhouse as C
    table=next(t for t in C.TABLES if t.name=='deviations')
    raw={'id':'d1','session_id':'s1','instruction_source':'agent:worker','rule_id':'final-report',
         '_own':'private','_key':'d1','_changed':datetime.now(timezone.utc)}
    monkeypatch.setattr(egress,'team_names',lambda *a,**k:egress.Deny(('repo',)))
    monkeypatch.setattr(egress,'env_terms',lambda *a,**k:egress.NONE)
    sink=C.ClickHouseSink(SimpleNamespace(cfg=SimpleNamespace(lookback_days=0,cloud=False)),tables=[table])
    monkeypatch.setattr(sink,'_pages',lambda *a,**k:iter([[raw]]))
    assert list(sink.batches(None,{},replay=False))[0].rows[0]['rule_id']=='final-report'
    monkeypatch.setattr(egress,'env_terms',lambda *a,**k:egress.Deny(('repo',)))
    assert list(sink.batches(None,{},replay=False))[0].rows[0]['rule_id'] is None


def test_new_local_refusal_reason_is_explained_without_hiding_other_failures():
    from agent_memory.rsi_brief import service_labels
    report={'health':{'langfuse':'ok'}, 'health_reasons':{'langfuse':[
        'current replay export exp-example: 8 records withheld locally; 12 upload receipts confirmed; readback not verified']},
        'experiments':[{'langfuse_export':{'experiment_id':'exp-example','status':'partial',
            'issue_kind':'local_refusal','refused':8,'failed':0,'uncertain':0}}]}
    assert service_labels(report)['langfuse'].startswith('traces withheld locally (8)')
    report['health_reasons']['langfuse'].append('lf.health: HTTP 401')
    assert 'langfuse' not in service_labels(report)
