from datetime import UTC, datetime, timedelta
import json
import uuid

import psycopg
import pytest

from agent_memory import coverage as CV, telemetry_analysis as A
from synth_support import fresh_db

NOW=datetime(2026,10,27,8,tzinfo=UTC)


@pytest.fixture(scope="module")
def db(pg_url):
    with fresh_db(pg_url) as url:
        yield url


def test_empty_window_is_not_an_invented_all_clear(db):
    report=A.run(db_url=db,now=NOW-timedelta(days=60))
    assert report['span_coverage']['stored']==0 and report['test_coverage']['stored']==0
    assert report['findings']==[]
    assert 'Check coverage' in A.render(report)
    assert all(r.resolved_status() in ('no_eligible_data','analysis_not_run') for r in A.records(report,run_key='empty'))


def test_nonempty_analysis_has_refs_limits_and_explicit_denominators(db):
    with psycopg.connect(db) as c:
        for sid,meta in (('analysis-parent',{}),('analysis-worker',{'parent_session':'analysis-parent'})):
            c.execute("insert into sessions(session_id,kind,project_dir,tool,started_at,ended_at,duration_seconds,meta,outcome) "
                      "values(%s,'chat','repo','claude',%s,%s,60,%s,'shipped')",
                      (sid,NOW-timedelta(hours=3),NOW-timedelta(hours=2),json.dumps(meta)))
        for span_id,sid,duration in (('analysis-cmd','analysis-parent',3000),('analysis-api',None,None)):
            c.execute("insert into execution_spans(span_id,session_id,tool,category,operation,started_at,ended_at,elapsed_ms,"
                      "status,measurement_kind,source) values(%s,%s,'claude','command','Bash',%s,%s,%s,'passed','observed','claude.tool')",
                      (span_id,sid,NOW-timedelta(hours=3),NOW-timedelta(hours=3)+timedelta(seconds=3),duration))
        for i in range(2):
            rid=str(uuid.uuid4())
            c.execute("""insert into test_timing_runs(run_id,session_id,parent_span_id,repo,head,dirty,selection,workers,
                started_at,ended_at,elapsed_ms,exit_code,collection_errors,worker_errors,tests_reported,tests_complete,outcomes,log_ref)
                values(%s,'analysis-parent','analysis-cmd','repo','head',false,'full',4,%s,%s,1000,%s,0,0,1,1,'{}','local.log')""",
                (rid,NOW-timedelta(hours=2-i),NOW-timedelta(hours=2-i)+timedelta(seconds=1),i))
            c.execute("insert into test_timing_cases(run_id,test_key,nodeid,setup_ms,call_ms,teardown_ms,total_ms,outcome,complete) "
                      "values(%s,%s,'tests/test_x.py::test_y',500,300,200,1000,%s,true)",
                      (rid,'a'*64,'passed' if i==0 else 'failed'))
    report=A.run(db_url=db,now=NOW)
    assert report['span_coverage']['stored']==2 and report['span_coverage']['session_resolved']==1
    assert report['test_coverage']['stored']==2 and len(report['repeat_groups'])==1
    kinds={f['kind'] for f in report['findings']}
    assert {'recurring_operation','measurement_gap','slow_test_observation','repeat_test_review','parent_time_review',
            'completion_evidence_review'} <= kinds
    assert all(f['evidence_refs'] and f['limitation'] and f['advisory_only'] for f in report['findings'])
    records=A.records(report,run_key='nonempty')
    for r in records:
        r.validate()
    with psycopg.connect(db) as c:
        assert CV.record_best_effort(c,records)==[]
        assert c.execute("select count(*) from routine_consumption where run_key='nonempty'").fetchone()[0]==3
        assert c.execute("select status from coverage_snapshots where dataset='command_timings:execution_spans'").fetchone()[0]=='collected_unlinked'


def test_report_save_contains_no_full_test_logs_or_commands(db,tmp_path):
    report=A.run(db_url=db,now=NOW)
    path=A.save(report,tmp_path)
    assert json.loads(path.read_text())['format']==A.FORMAT
    text=path.with_suffix('.md').read_text()
    assert 'cumulative' in text and 'not wall time' in text and 'Evidence:' in text
    assert 'unnecessary reruns' in text  # explicitly warns against that interpretation


def test_morning_review_distinguishes_recent_stale_and_missing(db,tmp_path):
    report=A.run(db_url=db,now=NOW)
    A.save(report,tmp_path)
    current=A.daily_review(tmp_path,now=NOW+timedelta(hours=1))
    assert current['status']=='recorded' and current['scope']=='stored_rows_only'
    assert current['stored_spans']==2 and all(f['evidence_refs'] for f in current['findings'])
    assert A.daily_review(tmp_path,now=NOW+timedelta(hours=27))['status']=='stale'
    assert A.daily_review(tmp_path/'missing',now=NOW)['status']=='unavailable'


def test_catalog_traffic_is_not_a_fake_session_link_or_inference_time(pg_url):
    from agent_memory.execution_timing import Span
    from agent_memory import timing_store
    with fresh_db(pg_url) as url:
        one=Span('catalog-control',None,'codex','unclassified','codex.api_request',
                 NOW-timedelta(seconds=1),NOW-timedelta(milliseconds=1),999,'passed','provider_reported','codex.otel',
                 meta={'api_scope':'client_control','endpoint':'models'})
        timing_store.upsert([one],db_url=url)
        report=A.run(db_url=url,now=NOW)
        q=report['span_coverage']
        assert q['stored']==1 and q['client_control']==1 and q['session_expected']==0
        assert q['session_resolved']==0 and q['api_events']==0 and q['api_unassigned']==0
        assert not any(f['kind']=='measurement_gap' for f in report['findings'])
        snap=next(r for r in A.records(report,run_key='control') if isinstance(r,CV.Snapshot) and r.dataset=='command_timings:execution_spans')
        assert snap.eligible==0 and snap.linked==0 and snap.resolved_status()=='no_eligible_data'
        assert 'client-control' in snap.denominator
