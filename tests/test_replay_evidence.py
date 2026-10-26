import json
from pathlib import Path

import psycopg
import pytest

from agent_memory import attempt_links as AL, replay_evidence as RE, rsi_artifact as RA, sessions_ingest as SI
from synth_support import fresh_db
from test_rsi_approval import make_artifact
from test_rsi_subscription import response
from agent_memory import rsi_subscription as S


@pytest.fixture(scope="module")
def db(pg_url):
    with fresh_db(pg_url) as url:
        yield url


def attempt(exp="exp-20261026-native", native="native-client-a"):
    return dict(experiment_id=exp,profile="claude",arm="A",case="pepe-lock",rep=0,
                run_id=exp+"-claude-pepe-lock-A-r0",replay_record="replays/test/runs/run.json",
                agent="claude",native_session_id=native,model="claude-opus-5-5",
                started_at="2026-10-26T20:00:00Z",duration_s=1.5,error=False)


def test_native_session_observation_is_real_scoped_and_idempotent(db):
    a=attempt()
    with psycopg.connect(db) as c:
        assert AL.record_runner(c,[a])==1
        assert AL.record_runner(c,[a])==0
        sid,env,exp,meta,byte_count=c.execute("select session_id,env,experiment_id,meta,transcript_bytes "
                                            "from sessions where session_id=%s",(a['native_session_id'],)).fetchone()
        assert sid==a['native_session_id'] and env=='sandbox' and exp==a['experiment_id']
        assert meta['transcript_available'] is False and meta['acceptance']=='unverified' and byte_count==0
        row=c.execute("select attempt_session_id,session_id from rsi_attempts where run_id=%s",(a['run_id'],)).fetchone()
        assert row[0]!=row[1] and row[1]==sid


def test_missing_native_id_does_not_fabricate_or_use_nearby_session(db):
    a=attempt("exp-20261026-unknown",None)
    with psycopg.connect(db) as c:
        before=c.execute("select count(*) from sessions").fetchone()[0]
        AL.record_runner(c,[a])
        sid,ev=c.execute("select session_id,evidence from rsi_attempts where run_id=%s",(a['run_id'],)).fetchone()
        assert sid is None and ev['native_identity_unavailable']=='client_id_not_recorded'
        assert c.execute("select count(*) from sessions").fetchone()[0]==before


def test_failed_client_keeps_only_its_explicit_native_identity():
    events=[json.loads(line) for line in response().splitlines()]
    for row in events:
        row['session_id']='failed-native-session'
    events[-1]['is_error']=True
    with pytest.raises(S.SubscriptionError,match='client_run_failed') as err:
        S._parse_claude('\n'.join(json.dumps(x) for x in events),model=S.MODELS['claude'],tools_allowed=False)
    assert err.value.native_session_id=='failed-native-session'


def test_native_collision_never_relabels_a_production_session(db):
    a=attempt("exp-20261026-conflict","existing-prod")
    with psycopg.connect(db) as c:
        c.execute("insert into sessions(session_id,kind,project_dir,env) values('existing-prod','chat','repo','prod')")
        AL.record_runner(c,[a])
        assert c.execute("select env from sessions where session_id='existing-prod'").fetchone()[0]=='prod'
        assert c.execute("select session_id from rsi_attempts where run_id=%s",(a['run_id'],)).fetchone()[0] is None


def test_normal_transcript_ingest_keeps_proven_sandbox_scope(db):
    a=attempt("exp-20261026-retain","native-with-transcript")
    with psycopg.connect(db) as c:
        AL.record_runner(c,[a])
    row=SI.SessionRow(a['native_session_id'],'headless',None,'normal-native-project',None,None,100,[],[],tool='claude')
    SI.upsert([row],db_url=db)
    with psycopg.connect(db) as c:
        env,exp,meta=c.execute("select env,experiment_id,meta from sessions where session_id=%s",(a['native_session_id'],)).fetchone()
        assert env=='sandbox' and exp==a['experiment_id'] and meta['transcript_available'] is True


def test_all_verdict_eval_ingestion_and_identity_validation(db,tmp_path):
    # Existing artifact builder provides exact canonical bytes and a bound static
    # result. Add a non-improving replay result: this must persist without a proposal.
    _,adir=make_artifact(tmp_path,[("~/.claude/CLAUDE.md",b"old",b"new")],exp="exp-20261026-evals")
    m,h,_=RA.load_artifact(adir)
    ev=adir/'evals'/'replay.json'
    ev.parent.mkdir(exist_ok=True)
    ev.write_text(json.dumps(dict(format='rsi-eval/1',artifact_hash=h,experiment_id=m['experiment_id'],
                                  baseline_id=m['baseline_id'],suite='sandbox-replay-claude',suite_version='1',
                                  model='claude-opus-5-5',effort='high',verdict='worse',results={})))
    with psycopg.connect(db) as c:
        first=RE.register_evaluations(c,adir,m['experiment_id'])
        assert first['evaluations_added']>=1
        assert RE.register_evaluations(c,adir,m['experiment_id'])['evaluations_added']==0
        assert c.execute("select count(*) from rsi_proposals where artifact_hash=%s",(h,)).fetchone()[0]==0
        with pytest.raises(RA.ArtifactError,match='another experiment'):
            RE.register_evaluations(c,adir,'exp-20261026-other')
    bad=json.loads(ev.read_text());bad['baseline_id']='bl-wrong';ev.write_text(json.dumps(bad))
    with psycopg.connect(db) as c,pytest.raises(RA.ArtifactError,match='binding mismatch'):
        RE.register_evaluations(c,adir,m['experiment_id'])
