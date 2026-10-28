from datetime import UTC, datetime, timedelta
import hashlib

import psycopg
from psycopg.types.json import Jsonb

from agent_memory import production_feedback as F, rsi_approval as AP, rsi_night as N, rsi_outcome as O
from agent_memory import replay_evidence as RE
from test_rsi_night import env, run, seed_ab  # noqa: F401
from rsi_night_support import KEY_A,RULE_A,candidate,rows
from test_production_feedback import complete


def test_nightly_reconsiders_new_verified_generation_without_erasing_attempts(env):
    seed_ab(env)
    now=datetime.now(UTC)
    path=env.home/'.claude'/'CLAUDE.md'
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    rule='claude-md:global:'+RULE_A[0]
    with psycopg.connect(env.url) as c:
        cid=AP.upsert_candidate(c,dedupe_key=KEY_A,source='deviations',kind='instruction',
                                target='~/.claude/CLAUDE.md',rule_key=rule,metrics={'n':10})
        c.execute("update rsi_candidates set status='tested',last_considered_at=%s,meta=%s where id=%s",
                  (now-timedelta(days=1),Jsonb({'attempts':2,'history':[{'verdict':'WORSE','night':'previous'}],
                                               'last_tested_instruction_sha256':'f'*64}),cid))
        change=O.Change('edit','0f81e263-a3b0-4b1d-b3b9-a1bc027eaac5','claude-md:global',RULE_A[0],
                        now-timedelta(days=10),'Closing','~/.claude/CLAUDE.md','0'*64,sha)
        outcome=O.Outcome(change,10,sessions_before=10,sessions_after=10,deviations_before=1,deviations_after=8,
                          rate_before=.1,rate_after=.8,p_value=.02,verdict='worse',observed_days=10)
        observation=F.observation(outcome,now,complete(outcome))
        c.execute("insert into rsi_evidence(system,kind,subject_refs,metrics,external_ref,observed_at,valid_until) "
                  "values('supabase',%s,%s,%s,%s,%s,%s)",
                  (F.KIND,observation['subject_refs'],Jsonb(observation['metrics']),observation['external_ref'],
                   observation['observed_at'],observation['valid_until']))
    report=run(env)
    result=candidate(env.url,KEY_A)
    assert any('advisory reconsideration' in note for note in report['notes'])
    assert result['meta']['attempts']==3 and result['meta']['generation_attempts']==1
    assert result['meta']['history'][0]['verdict']=='WORSE'
    assert result['meta']['production_feedback_generation']==observation['metrics']['generation']
    assert result['meta']['last_tested_instruction_sha256']==sha
    assert rows(env.url,"select count(*) from rsi_decisions")[0][0]==0
    assert all(status[0]!='promoted' for status in rows(env.url,"select status from rsi_proposals"))


def test_expected_replay_population_does_not_treat_missing_eval_rows_as_empty(env):
    with psycopg.connect(env.url) as c:
        c.execute("insert into rsi_experiments(id,night,kind,status,meta) values('exp-20261026-missing','2026-10-26',"
                  "'edit_instruction','evaluated',%s)",
                  (Jsonb({'provider_results':[{'provider':'claude','arms':{'A':{'runs':3},'B':{'runs':3}}}]}),))
        now=datetime.now(UTC)
        records=RE.night_records(c,night='2026-10-26',start=now-timedelta(hours=1),end=now)
    snapshots={x.dataset:x for x in records if hasattr(x,'eligible')}
    assert snapshots['experiments:replay-evidence'].eligible==1
    assert snapshots['experiments:replay-evidence'].resolved_status()=='not_collected'
    native=snapshots['experiments:native-attempt-sessions']
    assert native.eligible==6 and native.collected==0 and native.resolved_status()=='not_collected'
