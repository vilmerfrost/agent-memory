"""Deterministic delayed/multipage Langfuse reads; no live HTTP or model calls."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_memory.integrations import evidence as E, http, langfuse as L

EXP = "exp-20261025-63a57bf4"
NOW = datetime(2026, 10, 27, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.at = 100.0
        self.waits = []

    def __call__(self):
        return self.at

    def sleep(self, value):
        self.waits.append(value)
        self.at += value


def response(data):
    return http.Response(200, json.dumps(data).encode(), {})


class API:
    def __init__(self, clock, traces=12, delay=.1, page_size=100, subject="mixed", scores=True):
        self.clock, self.delay, self.page_size, self.subject, self.has_scores = clock, delay, page_size, subject, scores
        self.ids = [f"{i+1:032x}" for i in range(traces)]
        self.calls = []

    def __call__(self, method, url, **kwargs):
        u = urlsplit(url)
        p = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append({"path": u.path, "params": p, "at": self.clock(), **kwargs})
        self.clock.at += self.delay
        if u.path == L.HEALTH_PATH:
            return response({"data": [{"id": "fixture"}]})
        if u.path.startswith(L.DATASETS_PATH):
            raise http.HttpError(404, "fixture has no dataset")
        if u.path == L.OBSERVATIONS_PATH:
            rows = [{"traceId": tid, "id": f"root-{i}", "parentObservationId": None, "sessionId": EXP,
                     "metadata": {"arm": "A" if i % 2 == 0 else "B"}}
                    for i, tid in enumerate(self.ids)]
        elif u.path == L.SCORES_PATH:
            assert p["traceId"].split(",") == self.ids[:L.MAX_SCORED_TRACES]
            assert p["fields"] == "subject"
            assert p["fromTimestamp"] and p["toTimestamp"]
            rows = []
            if self.has_scores:
                for i, tid in enumerate(p["traceId"].split(",")):
                    subject = {"kind": "trace", "id": tid} if self.subject == "trace" or i % 2 == 0 else \
                              {"kind": "observation", "id": f"obs-{i}", "traceId": tid}
                    if self.subject == "foreign":
                        subject = {"kind": "trace", "id": "f"*32}
                    if self.subject == "session":
                        subject = {"kind": "session", "id": EXP}
                    rows.append({"id": f"score-{i}", "name": "check_passed", "dataType": "BOOLEAN",
                                 "value": i % 3 != 0, "subject": subject})
        else:
            raise AssertionError(u.path)
        start = int(p.get("cursor", 0))
        size = min(self.page_size, int(p.get("limit", 100)))
        cursor = str(start+size) if start+size < len(rows) else None
        return response({"data": rows[start:start+size], "meta": {"cursor": cursor}})


def client(clock, *, timeout=10, interval=0):
    return L.LangfuseClient("https://cloud.langfuse.com", "pk-fixture", "sk-fixture", timeout=timeout,
                            retries=2, clock=clock, sleep=clock.sleep, min_interval=interval)


def test_twelve_traces_use_one_bound_bulk_score_query_inside_existing_budget(monkeypatch):
    clock = Clock()
    api = API(clock)
    monkeypatch.setattr(L.http, "request", api)
    c = client(clock, interval=L.CLOUD_MIN_INTERVAL_S)
    r = L.LangfuseRetriever(c, now=lambda: NOW)
    start = clock()
    with r.read_budget(20):
        result = E.run_query(r, "experiment_scores", {"experiment_id": EXP})
    assert result.status == "ok"
    assert len(api.calls) == 4  # health, dataset lookup, roots, one scores page; old path needed 15
    assert sum(x["path"] == L.SCORES_PATH for x in api.calls) == 1
    assert clock()-start == pytest.approx(6.4) and clock()-start < 20
    assert sum(e.metrics["traces"] for e in result.refs) == 12
    assert sum(e.metrics["check_passed_n"] for e in result.refs) == 12
    assert all(e.metrics["population_complete"] for e in result.refs)
    assert all(call["retries"] == 0 for call in api.calls)
    assert all(0 < call["timeout"] <= 10 for call in api.calls)


def test_multipage_bulk_scores_preserve_both_arms_without_per_trace_fanout(monkeypatch):
    clock = Clock()
    api = API(clock, page_size=4, delay=.3)
    monkeypatch.setattr(L.http, "request", api)
    result = E.run_query(L.LangfuseRetriever(client(clock), now=lambda: NOW), "experiment_scores", {"experiment_id": EXP})
    assert result.status == "ok" and len(result.refs) == 2
    assert sum(x.metrics["traces"] for x in result.refs) == 12
    assert sum(x.metrics["check_passed_n"] for x in result.refs) == 12
    score_calls = [x for x in api.calls if x["path"] == L.SCORES_PATH]
    assert len(score_calls) == 3
    assert [x["params"].get("cursor") for x in score_calls] == [None, "4", "8"]
    assert len({x["params"]["traceId"] for x in score_calls}) == 1


def test_trace_selection_cap_remains_explicit_not_a_complete_population(monkeypatch):
    clock = Clock()
    api = API(clock, traces=30)
    monkeypatch.setattr(L.http, "request", api)
    result = E.run_query(L.LangfuseRetriever(client(clock), now=lambda: NOW), "experiment_scores", {"experiment_id": EXP})
    assert result.status == "ok"
    assert sum(x.metrics["traces"] for x in result.refs) == L.MAX_SCORED_TRACES
    assert all(x.metrics["truncated"] and x.metrics["population_complete"] is False for x in result.refs)


@pytest.mark.parametrize("subject", ["foreign", "session"])
def test_scores_must_bind_to_the_selected_root_traces(monkeypatch, subject):
    clock = Clock()
    api = API(clock, subject=subject)
    monkeypatch.setattr(L.http, "request", api)
    result = E.run_query(L.LangfuseRetriever(client(clock), now=lambda: NOW), "experiment_scores", {"experiment_id": EXP})
    assert result.status == "error" and result.detail == "IncompleteRead" and not result.refs


def test_missing_score_measurements_are_unknown_not_zero_rates(monkeypatch):
    clock = Clock()
    api = API(clock, scores=False)
    monkeypatch.setattr(L.http, "request", api)
    result = E.run_query(L.LangfuseRetriever(client(clock), now=lambda: NOW), "experiment_scores", {"experiment_id": EXP})
    assert result.status == "ok" and result.refs
    assert all(x.metrics["check_passed_n"] == 0 and "check_passed_rate" not in x.metrics for x in result.refs)


def test_malformed_successful_dataset_lookup_is_not_treated_as_absent(monkeypatch):
    clock = Clock()
    api = API(clock)

    def malformed(method, url, **kwargs):
        if urlsplit(url).path.startswith(L.DATASETS_PATH):
            return response({"unexpected": "shape"})
        return api(method, url, **kwargs)
    monkeypatch.setattr(L.http, "request", malformed)
    result = E.run_query(L.LangfuseRetriever(client(clock), now=lambda: NOW), "experiment_scores", {"experiment_id": EXP})
    assert result.status == "error" and result.detail == "IncompleteRead" and not result.refs
    assert not any(c["path"] == L.SCORES_PATH for c in api.calls)


def test_shared_read_deadline_includes_health_throttle_and_pages(monkeypatch):
    clock = Clock()
    api = API(clock, page_size=4, delay=.4)
    monkeypatch.setattr(L.http, "request", api)
    r = L.LangfuseRetriever(client(clock, interval=2.1), now=lambda: NOW)
    start = clock()
    with r.read_budget(5):
        result = E.run_query(r, "experiment_scores", {"experiment_id": EXP})
    assert result.status == "error" and result.detail == "timeout (ReadDeadline)" and not result.refs
    assert clock()-start < 5
    assert len(api.calls) == 3
    assert not any(x["path"] == L.SCORES_PATH for x in api.calls)


def test_late_page_is_rejected_and_no_further_request_starts(monkeypatch):
    clock = Clock()
    c = client(clock)
    calls = []

    def late(method, url, **kw):
        calls.append(kw)
        clock.at += 6
        return response({"data": [{"id": "first"}], "meta": {"cursor": "more"}})
    monkeypatch.setattr(L.http, "request", late)
    with c.read_budget(5), pytest.raises(L.ReadDeadline):
        c.scores(traceId="a"*32)
    assert len(calls) == 1 and calls[0]["timeout"] == 5 and calls[0]["retries"] == 0


def test_read_context_disables_retry_but_preserves_unbounded_client_retry_setting(monkeypatch):
    clock = Clock()
    c = client(clock)
    retries = []

    def fail(method, url, **kw):
        retries.append(kw["retries"])
        raise http.HttpError(503, "fixture unavailable")
    monkeypatch.setattr(L.http, "request", fail)
    with c.read_budget(), pytest.raises(http.HttpError):
        c.scores(traceId="a"*32)
    with pytest.raises(http.HttpError):
        c.scores(traceId="a"*32)
    assert retries == [0, 2]


@pytest.mark.parametrize("repeat", [False, True])
def test_unfinished_or_repeated_cursor_never_looks_complete(monkeypatch, repeat):
    clock = Clock()
    c = client(clock)
    calls = []

    def page(method, url, **kw):
        calls.append(url)
        return response({"data": [{"id": str(len(calls))}], "meta": {"cursor": "same"}})
    monkeypatch.setattr(L.http, "request", page)
    with c.read_budget(), pytest.raises(L.IncompleteRead, match="repeated|page cap"):
        c.scores(max_pages=3 if repeat else 1, traceId="a"*32)
    assert len(calls) == 2 if repeat else len(calls) == 1


def test_existence_probe_can_intentionally_stop_after_one_row(monkeypatch):
    clock = Clock()
    c = client(clock)
    monkeypatch.setattr(L.http, "request", lambda *a, **kw: response(
        {"data": [{"traceId": "a"*32}], "meta": {"cursor": "more"}}))
    assert c.trace_exists("a"*32)
    with pytest.raises(L.IncompleteRead):
        c.observations(max_pages=1, traceId="a"*32, limit=1)


def test_nested_budget_cannot_extend_deadline_or_leak_after_context(monkeypatch):
    clock = Clock()
    c = client(clock)
    calls = []
    monkeypatch.setattr(L.http, "request", lambda *a, **kw: calls.append(kw) or response({"data": []}))
    with c.read_budget(2):
        clock.at += 1.5
        with c.read_budget(20):
            c.scores(traceId="a"*32)
    c.scores(traceId="a"*32)
    assert calls[0]["timeout"] == .5 and calls[0]["retries"] == 0
    assert calls[1]["timeout"] == 10 and calls[1]["retries"] == 2


def test_read_budget_cannot_be_used_to_export(monkeypatch):
    clock = Clock()
    c = client(clock)
    monkeypatch.setattr(L.http, "request", lambda *a, **kw: pytest.fail("write must not reach HTTP"))
    with c.read_budget(), pytest.raises(L.LangfuseError, match="non-GET"):
        c.export_spans({"resourceSpans": []})


def test_export_budget_bounds_ingestion_without_enabling_evidence_writes(monkeypatch):
    from agent_memory.integrations import langfuse as L, http
    clock = [0.0]
    seen = []
    def request(method, url, **kw):
        seen.append((method, kw['retries'], kw['total_timeout']))
        clock[0] += .1 if len(seen) == 1 else .15
        return http.Response(200, b'{}', {})
    monkeypatch.setattr(L.http, 'request', request)
    client = L.LangfuseClient('http://127.0.0.1', 'test', 'test', clock=lambda: clock[0], min_interval=0)
    with client.export_budget(.2), pytest.raises(L.ReadDeadline):
        client.export_spans({})
        client.create_scores([])
    assert seen == [('POST', 0, .2), ('POST', 0, .1)]
    # Export scope ends; the original evidence read scope still refuses ingestion.
    with client.read_budget(1), pytest.raises(L.LangfuseError, match='non-GET'):
        client.create_scores([])
