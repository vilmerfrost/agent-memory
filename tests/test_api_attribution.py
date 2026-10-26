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
    assert one.method_version == 2


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
