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
    night._merge_health({'clickhouse':'skipped'},[{'step':'ch.rule_gap','status':'unavailable','detail':'policy-excluded: withheld'}])
    assert night.health['clickhouse']=='ok' and night.health_reasons=={}
    assert night.report['connection_recovery']['checks'][0]['status']=='excluded'


def test_historical_labels_do_not_rewrite_raw_health_or_hide_transport_failures():
    from agent_memory.rsi_brief import service_labels
    report={'health':{'langfuse':'unavailable','clickhouse':'error'},
            'experiments':[{'langfuse_export':{'status':'partial','refused':8,'failed':0,'uncertain':0}}],
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
