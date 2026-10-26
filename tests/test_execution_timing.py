from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import psycopg
import pytest

from agent_memory import execution_timing as T, sessions_ingest, timing_store
from synth_support import fresh_db

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def span(sid: str = "codex:timing", name: str = "one", *, start: int = 0, end: int | None = 10,
         category: str = "test_full", source: str = "parent.command") -> T.Span:
    return T.Span(name, sid, "codex", category, name, NOW+timedelta(seconds=start),
                  NOW+timedelta(seconds=end) if end is not None else None,
                  (end-start)*1000 if end is not None else None,
                  "passed" if end is not None else "running", "measured", source)


@pytest.fixture(scope="module")
def db(pg_url: str):
    with fresh_db(pg_url) as url:
        yield url


def add_session(db: str, sid: str, parent: str | None = None) -> None:
    with psycopg.connect(db) as conn:
        conn.execute("insert into sessions(session_id,kind,tool,project_dir,started_at,ended_at,duration_seconds,meta) "
                     "values (%s,'chat','codex','timing',%s,%s,60,%s::jsonb) on conflict do nothing",
                     (sid,NOW,NOW+timedelta(seconds=60),json.dumps({"parent_session":parent} if parent else {})))


def test_native_codex_uses_explicit_timing_and_drops_sensitive_fields() -> None:
    events = [{"type":"session_meta","payload":{"id":"thread","subagent_history_start_ordinal":2}},
              {"ordinal":1,"type":"event_msg","payload":{"type":"item_completed","started_at_ms":1000,"completed_at_ms":9000,
                 "item":{"id":"copied","type":"McpToolCall","duration":{"secs":8,"nanos":0}}}},
              {"ordinal":2,"type":"event_msg","payload":{"type":"item_completed","started_at_ms":1000,"completed_at_ms":4000,
                 "item":{"id":"own","type":"CommandExecution","command":"pytest tests/test_x.py --secret=my-secret",
                         "aggregated_output":"my-secret", "duration":{"secs":2,"nanos":500000000},"exit_code":1}}},
              {"ordinal":3,"type":"event_msg","payload":{"type":"item_completed","started_at_ms":4000,"completed_at_ms":5000,
                 "item":{"id":"model","type":"Reasoning","content":"private reasoning"}}}]
    result = T.codex_spans(events+events[2:], "codex:thread")
    assert len(result)==2
    command, model = sorted(result,key=lambda s:s.started_at)
    assert command.elapsed_ms==2500 and command.status=="failed" and command.exit_code==1
    assert command.category=="test_targeted" and command.measurement_kind=="provider_reported"
    assert model.category=="model_output" and model.elapsed_ms==1000
    assert all(s.category!="api" for s in result)
    text=json.dumps([s.record() for s in result])
    assert "my-secret" not in text and "private reasoning" not in text and "copied" not in text


def otlp() -> dict:
    def attr(k,v): return {"key":k,"value":{"stringValue":v}}
    return {"resourceLogs":[{"resource":{"attributes":[attr("conversation.id","thread")]},
             "scopeLogs":[{"logRecords":[{"timeUnixNano":str(int(NOW.timestamp()*1e9)),
                "attributes":[attr("event.name","codex.api_request"),attr("duration_ms","125.5"),
                              attr("authorization","TOP-SECRET"),attr("prompt","PRIVATE-PROMPT")],
                "body":{"stringValue":"PRIVATE-OUTPUT"}}]}]}]}


def test_otel_api_measurements_have_stable_identity_and_no_payload() -> None:
    one, = T.otlp_spans(otlp())
    assert one.session_id=="codex:thread" and one.elapsed_ms==125.5
    assert one.category=="api" and one.measurement_kind=="provider_reported"
    assert one.span_id==T.otlp_spans(otlp())[0].span_id
    text=json.dumps(one.record())
    assert all(x not in text for x in ("TOP-SECRET","PRIVATE-PROMPT","PRIVATE-OUTPUT"))
    assert T.parse_record(one.record())==one


def test_otel_zero_event_clock_uses_observed_clock() -> None:
    payload=otlp()
    record=payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
    record['observedTimeUnixNano']=record['timeUnixNano']
    record['timeUnixNano']='0'
    one,=T.otlp_spans(payload)
    assert one.ended_at==NOW
    assert one.meta['clock']=='provider_observed'
    first_id=one.span_id
    record['observedTimeUnixNano']=str(int((NOW+timedelta(seconds=1)).timestamp()*1e9))
    assert T.otlp_spans(payload)[0].span_id!=first_id


def test_otel_missing_clocks_keep_duration_without_inventing_interval() -> None:
    payload=otlp()
    record=payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
    record['timeUnixNano']='0'
    record['observedTimeUnixNano']='0'
    one,=T.otlp_spans(payload)
    assert one.started_at is None and one.ended_at is None
    assert one.elapsed_ms==125.5 and one.session_id=='codex:thread'
    assert one.meta['clock']=='unavailable'
    assert T.parse_record(one.record())==one


def test_replaying_legacy_zero_clock_record_cannot_restore_epoch_interval() -> None:
    one,=T.otlp_spans(otlp())
    old=one.record()
    old['started_at']='1969-12-31T23:59:59.874500+00:00'
    old['ended_at']='1970-01-01T00:00:00+00:00'
    parsed=T.parse_record(old)
    assert parsed.started_at is None and parsed.ended_at is None
    assert parsed.elapsed_ms==125.5 and parsed.span_id==one.span_id
    assert parsed.meta['clock']=='unavailable'


def test_record_validation_rejects_false_durations_and_unsafe_metadata() -> None:
    record=span().record()
    for bad in (float("nan"),float("inf"),-1,True,"1"):
        assert T.parse_record({**record,"elapsed_ms":bad}) is None
    assert T.parse_record({**record,"ended_at":(NOW-timedelta(seconds=1)).isoformat()}) is None
    parsed=T.parse_record({**record,"meta":{"prompt":"secret","command":"secret","workers":4}})
    assert parsed.meta=={"workers":4}


def test_completed_events_are_idempotent_and_cannot_be_downgraded(db: str) -> None:
    add_session(db,"codex:timing")
    assert timing_store.upsert([span(end=None)],db_url=db)==(1,0)
    assert timing_store.upsert([span()],db_url=db)==(0,1)
    assert timing_store.upsert([span(end=None),span()],db_url=db)==(0,0)
    with psycopg.connect(db) as conn:
        assert conn.execute("select status,elapsed_ms from execution_spans where span_id='one'").fetchone()==('passed',10000)


def test_union_and_parent_totals_do_not_double_count_overlaps(db: str) -> None:
    for sid,parent in (("codex:union",None),("codex:child-a","codex:union"),("codex:child-b","codex:union")):
        add_session(db,sid,parent)
    a,b=span("codex:union","union-a"),span("codex:union","union-b",start=2,end=7,category="tool")
    b.parent_span_id=a.span_id
    timing_store.upsert([a,b],db_url=db)
    result=timing_store.summary("codex:union",db_url=db)
    assert float(result['observed_wall_seconds'])==10
    assert float(result['unclassified_seconds'])==50
    assert result['category_union_seconds']=={'test_full':10,'tool':5}
    assert result['category_times_may_overlap'] is True
    assert result['descendant_sessions']==2 and result['cumulative_descendant_elapsed_seconds']==120
    assert result['api_measured_seconds'] is None and result['api_seconds'] is None
    assert T.union_seconds([a,b])==10


def test_codex_api_totals_only_come_from_api_measurements(db: str) -> None:
    add_session(db,"codex:thread")
    api,=T.otlp_spans(otlp())
    model=span("codex:thread","not-api",category="model_output")
    timing_store.upsert([model],db_url=db)
    assert timing_store.summary("codex:thread",db_url=db)['api_seconds'] is None
    timing_store.upsert([api],db_url=db)
    assert timing_store.summary("codex:thread",db_url=db)['api_seconds']==pytest.approx(.1255)


def test_api_duration_without_clock_is_stored_without_wall_time(db: str) -> None:
    payload=otlp()
    record=payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
    record['timeUnixNano']='0'
    api,=T.otlp_spans(payload)
    api.session_id='codex:clockless';api.span_id='clockless-api'
    add_session(db,api.session_id)
    timing_store.upsert([api],db_url=db)
    result=timing_store.summary(api.session_id,db_url=db)
    assert result['api_seconds']==pytest.approx(.1255)
    assert result['observed_wall_seconds'] is None
    assert float(result['window_seconds'])==60


def test_api_event_before_its_session_is_reflected_when_the_session_arrives(db: str) -> None:
    api,=T.otlp_spans(otlp())
    api.session_id='codex:late-api';api.span_id='late-api-event'
    timing_store.upsert([api],db_url=db)
    row=sessions_ingest.SessionRow('codex:late-api','chat',None,'timing',NOW,NOW,0,[],[],tool='codex')
    sessions_ingest.upsert([row],db_url=db)
    assert timing_store.summary('codex:late-api',db_url=db)['api_seconds']==pytest.approx(.1255)


def test_estimated_intervals_are_retained_but_not_counted_as_observed(db: str) -> None:
    add_session(db,'codex:estimated')
    estimate=span('codex:estimated','estimated-api',category='api',end=120)
    estimate.measurement_kind='estimated'
    timing_store.upsert([estimate],db_url=db)
    result=timing_store.summary('codex:estimated',db_url=db)
    assert result['observed_wall_seconds'] is None
    assert result['api_measured_seconds'] is None and result['api_seconds'] is None
    assert result['estimated_spans']==1
    assert float(result['window_seconds'])==60


def test_spool_replay_keeps_partial_tail_for_later(db: str,tmp_path:Path) -> None:
    p=tmp_path/'timing-events.jsonl'
    record=span('codex:spool','spool')
    p.write_text(json.dumps(record.record())+'\n{')
    result=timing_store.ingest_spools([p],db_url=db)
    assert (result.new,result.records,result.incomplete)==(1,1,1)
    assert timing_store.ingest_spools([p],db_url=db).new==0
    assert p.read_text().endswith('{')


def test_timing_table_enforces_rls_and_has_no_public_grants(db: str) -> None:
    with psycopg.connect(db) as conn:
        assert conn.execute("select relrowsecurity from pg_class where oid='public.execution_spans'::regclass").fetchone()==(True,)
        assert conn.execute("select count(*) from information_schema.role_table_grants where table_name='execution_spans' and grantee='PUBLIC'").fetchone()==(0,)
