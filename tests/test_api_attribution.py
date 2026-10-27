from copy import deepcopy

import psycopg
import pytest

from agent_memory import execution_timing as T, timing_store
from synth_support import fresh_db
from test_execution_timing import add_session, otlp


def attr(key, value):
    return {"key": key, "value": {"stringValue": value}}


def test_scope_identity_is_observed_without_inheriting_collector_actor():
    payload = otlp()
    resource = payload["resourceLogs"][0]
    resource["resource"]["attributes"] = []
    resource["scopeLogs"][0]["scope"] = {"attributes": [attr("thread.id", "scope-thread")]}
    one, = T.otlp_spans(payload)
    assert one.session_id == "codex:scope-thread"
    assert one.meta["identity_source"] == "otel_attribute:thread.id"
    assert one.method_version == 3


@pytest.mark.parametrize("attributes,reason", [
    ([], "explicit_thread_id_missing"),
    ([attr("thread.id", "")], "invalid_explicit_thread_id"),
    ([attr("thread.id", "another-thread")], "conflicting_explicit_thread_ids"),
])
def test_missing_or_conflicting_identity_stays_unassigned(attributes, reason):
    payload = otlp()
    resource = payload["resourceLogs"][0]
    if reason != "conflicting_explicit_thread_ids":
        resource["resource"]["attributes"] = []
    resource["scopeLogs"][0]["logRecords"][0]["attributes"] += attributes
    one, = T.otlp_spans(payload)
    assert one.session_id is None
    assert one.meta["identity_unavailable"] == reason
    assert one.elapsed_ms == 125.5


def test_request_identity_is_stable_when_explicit_actor_arrives_later():
    payload = otlp()
    resource = payload["resourceLogs"][0]
    record = resource["scopeLogs"][0]["logRecords"][0]
    record["attributes"].append(attr("request.id", "native-request-1"))
    unassigned = deepcopy(payload)
    unassigned["resourceLogs"][0]["resource"]["attributes"] = []
    before, = T.otlp_spans(unassigned)
    after, = T.otlp_spans(payload)
    assert before.span_id == after.span_id
    assert before.session_id is None and after.session_id == "codex:thread"
    assert before.elapsed_ms == after.elapsed_ms
    assert "PRIVATE" not in str(after.record()) and "TOP-SECRET" not in str(after.record())
    corrected = deepcopy(payload)
    corrected['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]['attributes'] = [
        attr('event.name','codex.api_request'),attr('duration_ms','200'),attr('request.id','native-request-1')]
    assert T.otlp_spans(corrected)[0].span_id == after.span_id


def test_exact_late_assignment_is_idempotent_and_cannot_reassign_actor(pg_url):
    with fresh_db(pg_url) as db:
        payload = otlp()
        record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        record["attributes"].append(attr("request.id", "native-request-late"))
        anonymous = deepcopy(payload)
        anonymous["resourceLogs"][0]["resource"]["attributes"] = []
        before, = T.otlp_spans(anonymous)
        after, = T.otlp_spans(payload)
        add_session(db, "codex:thread")
        assert timing_store.upsert([before], db_url=db) == (1, 0)
        assert timing_store.upsert([after], db_url=db) == (0, 1)
        assert timing_store.upsert([after], db_url=db) == (0, 0)
        collision = deepcopy(after)
        collision.session_id = "codex:someone-else"
        assert timing_store.upsert([collision], db_url=db) == (0, 0)
        with psycopg.connect(db) as conn:
            sid, meta = conn.execute("select session_id,meta from execution_spans where span_id=%s",
                                     (after.span_id,)).fetchone()
            assert sid == "codex:thread"
            assert "identity_unavailable" not in meta
            assert conn.execute("select count(*) from execution_spans").fetchone()[0] == 1
            assert conn.execute("select api_seconds from sessions where session_id=%s", (sid,)).fetchone()[0] == pytest.approx(.1255)


@pytest.mark.parametrize('endpoint',['/models','models'])
def test_native_catalog_request_is_control_traffic_not_missing_inference_identity(endpoint):
    payload=otlp()
    payload['resourceLogs'][0]['resource']['attributes']=[]
    rec=payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
    # Field shape observed from codex-cli0.163.0 initialize + model/list,
    # without thread/start or turn/start and without a model generation.
    rec['attributes']=[attr('event.name','codex.api_request'),attr('duration_ms','17'),
                       attr('endpoint',endpoint),attr('http.response.status_code','200'),attr('attempt','0'),
                       attr('authorization','TOP-SECRET'),attr('auth.agent_id','PRIVATE-ACTOR')]
    span,=T.otlp_spans(payload)
    assert span.session_id is None and span.category=='unclassified'
    assert span.meta['api_scope']=='client_control' and span.meta['endpoint']=='models'
    assert span.meta['identity_unavailable']=='session_not_applicable'
    assert span.status=='passed' and span.meta['http_status']=='200'
    assert 'TOP-SECRET' not in str(span.record()) and 'PRIVATE-ACTOR' not in str(span.record())


def test_actual_inference_event_metadata_uses_native_identity_and_clock():
    payload=otlp();r=payload['resourceLogs'][0]
    r['resource']['attributes']=[]
    rec=r['scopeLogs'][0]['logRecords'][0]
    rec['timeUnixNano']='0'
    rec['attributes'] += [attr('conversation.id','native-conversation'),attr('model','gpt-6.1-sol'),
                          attr('event.timestamp','2026-10-27T00:30:00Z'),attr('http.response.status_code','429')]
    span,=T.otlp_spans(payload)
    assert span.session_id=='codex:native-conversation' and span.category=='api'
    assert span.meta['api_scope']=='session' and span.status=='failed'
    assert span.ended_at.isoformat()=='2026-10-27T00:30:00+00:00'
    assert span.meta['clock']=='provider_event_timestamp'


def test_unknown_endpoint_and_no_metadata_remains_a_real_gap():
    payload=otlp();payload['resourceLogs'][0]['resource']['attributes']=[]
    rec=payload['resourceLogs'][0]['scopeLogs'][0]['logRecords'][0]
    rec['attributes'].append(attr('endpoint','https://private.invalid/SECRET-TOKEN'))
    span,=T.otlp_spans(payload)
    assert span.category=='api' and span.meta['api_scope']=='unknown' and span.session_id is None
    assert 'SECRET-TOKEN' not in str(span.record())


def test_method_two_anonymous_identity_survives_scope_and_native_status_enrichment(pg_url):
    from datetime import timedelta
    payload=otlp();resource=payload['resourceLogs'][0];resource['resource']['attributes']=[]
    rec=resource['scopeLogs'][0]['logRecords'][0]
    rec['attributes'] += [attr('endpoint','/models'),attr('http.response.status_code','200')]
    current,=T.otlp_spans(payload)
    # Exact original method-2 anonymous key contract, not a timestamp-nearness match.
    expected=T.key(None,'codex.api_request',rec['timeUnixNano'],None,None,125.5,{})
    assert current.span_id==expected
    old=deepcopy(current);old.category='api';old.method_version=2
    old.meta={'duration_source':'otel_duration_ms','clock':'provider_event_end',
              'identity_unavailable':'explicit_thread_id_missing'}
    old.started_at-=timedelta(milliseconds=1)
    with fresh_db(pg_url) as db:
        assert timing_store.upsert([old],db_url=db)==(1,0)
        assert timing_store.upsert([current],db_url=db)==(0,1)
        assert timing_store.upsert([current],db_url=db)==(0,0)
        assert timing_store.upsert([old],db_url=db)==(0,0)  # old spools cannot downgrade stronger semantics
        with psycopg.connect(db) as conn:
            rows=conn.execute('select category,method_version,started_at,meta from execution_spans').fetchall()
            assert len(rows)==1 and rows[0][:3]==('unclassified',3,current.started_at)
            assert rows[0][3]['api_scope']=='client_control'
