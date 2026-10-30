"""Cloud wake is bounded separately from analysis. All transports are offline stand-ins."""
from contextlib import contextmanager
import threading

import pytest

from agent_memory import rsi_evidence_pack as P, rsi_evidence_providers as EP
from agent_memory.integrations import clickhouse as CH, evidence as E, http


class Clock:
    at = 0.0

    def __call__(self):
        return self.at

    def sleep(self, seconds):
        self.at += seconds


class CloudRead:
    source = 'ch'
    readiness_timeout_s = CH.WAKE_TIMEOUT_S

    def __init__(self, clock, *, wake=25, health=None, slow_query=False):
        self.clock, self.wake, self.health_result = clock, wake, health or E.Health(True)
        self.health_calls = self.query_calls = 0
        self.slow_query = slow_query
        self.current_budget = None
        self.budgets = []

    @contextmanager
    def read_budget(self, seconds):
        previous = self.current_budget
        self.current_budget = seconds
        self.budgets.append(seconds)
        try:
            yield
        finally:
            self.current_budget = previous

    def health(self):
        self.health_calls += 1
        self.clock.at += self.wake
        return self.health_result

    def named_queries(self):
        return {n: E.NamedQuery(n, {}, 'fixed read') for n in ('gap', 'weekly')}

    def query(self, name, params):
        self.query_calls += 1
        if self.slow_query:
            self.clock.at += self.current_budget
            raise TimeoutError('analytical read timed out')
        self.clock.at += .1
        return []


def run(monkeypatch, read, names=('gap', 'weekly')):
    monkeypatch.setattr(P.time, 'sleep', read.clock.sleep)
    runner = P._Runner({'ch': read}, 20, read.clock)
    return runner, runner.phase([P._Step('ch', n, {}, 'clickhouse') for n in names])


def test_wake_longer_than_old_outer_deadline_preserves_analytical_budget(monkeypatch):
    read = CloudRead(Clock())
    runner, done = run(monkeypatch, read)
    assert all(result.status == 'ok' for _, result in done)
    assert read.health_calls == 1 and read.query_calls == 2
    assert runner.used['ch'] == pytest.approx(.2)
    assert read.budgets[0] == CH.WAKE_TIMEOUT_S
    assert read.budgets[1:] == pytest.approx([(10 - .5) / 3, (19.9 - .5) / 3])
    assert runner.log[0]['phase'] == 'readiness'
    assert runner.log[0]['elapsed_ms'] == 25000
    assert [row['elapsed_ms'] for row in runner.log[1:]] == [100, 100]
    # The same runner reuses readiness in later phases.
    runner.phase([P._Step('ch', 'gap', {}, 'clickhouse')])
    assert read.health_calls == 1 and len([x for x in runner.log if x.get('phase') == 'readiness']) == 1


def test_readiness_cap_stops_before_any_query(monkeypatch):
    read = CloudRead(Clock(), wake=CH.WAKE_TIMEOUT_S)
    runner, done = run(monkeypatch, read)
    assert all(result.status == 'unavailable' and result.detail == 'readiness: timeout' for _, result in done)
    assert read.health_calls == 1 and read.query_calls == 0
    assert runner.log[0]['failure_kind'] == 'timeout'
    assert runner.used['ch'] == 0
    runner.phase([P._Step('ch', 'gap', {}, 'clickhouse')])
    assert read.health_calls == 1 and read.query_calls == 0


@pytest.mark.parametrize('health,cause', [(E.Health(False, 'private body', False, None, 401), 'auth'),
    (E.Health(False, 'private body', False, None, 403), 'auth'),
    (E.Health(False, 'private body', False, 'tls'), 'tls'),
    (E.Health(False, 'private body'), 'health_rejected')])
def test_permanent_readiness_failures_are_not_retried_or_leaked(monkeypatch, health, cause):
    read = CloudRead(Clock(), wake=0, health=health)
    runner, done = run(monkeypatch, read)
    assert read.health_calls == 1 and read.query_calls == 0
    assert all(result.detail == f'readiness: {cause}' for _, result in done)
    assert runner.log[0]['failure_kind'] == cause
    assert 'private body' not in str(runner.log)


def test_temporary_health_recovery_resets_cache_once_then_keeps_it(monkeypatch):
    read = CloudRead(Clock(), wake=0)
    outcomes = iter([E.Health(False, 'HTTP 503', True), E.Health(True)])
    def health():
        read.health_calls += 1
        return next(outcomes)
    read.health = health
    runner, done = run(monkeypatch, read)
    assert all(result.status == 'ok' for _, result in done)
    assert read.health_calls == 2 and read.query_calls == 2
    assert runner.log[0]['attempts'] == 2
    assert runner.log[0]['elapsed_ms'] == 250
    assert runner.used['ch'] == pytest.approx(.2)


def test_cloud_query_timeout_does_not_inherit_wake_allowance(monkeypatch):
    read = CloudRead(Clock(), slow_query=True)
    runner, done = run(monkeypatch, read)
    assert all(result.status == 'error' for _, result in done)
    assert read.health_calls == 1  # analytical retries do not repeat Cloud wake
    assert read.query_calls == 6
    assert read.clock.at == 45  # 25s readiness plus the original 20s query budget
    assert runner.used['ch'] == 20


@pytest.mark.parametrize('blocked', ['policy', 'params', 'unknown'])
def test_forbidden_queries_do_not_contact_cloud_health(monkeypatch, blocked):
    read = CloudRead(Clock())
    if blocked == 'policy':
        def preflight(*args):
            raise E.Unavailable('policy-excluded: private')
        read._preflight_query = preflight
    names = ('missing',) if blocked == 'unknown' else ('gap',)
    if blocked == 'params':
        read.named_queries = lambda: {'gap': E.NamedQuery('gap', {'required': '.+'}, 'read', ('required',))}
    _, done = run(monkeypatch, read, names)
    assert done[0][1].status in ('unavailable', 'bad-params', 'unknown-query')
    assert read.health_calls == read.query_calls == 0


def test_outer_readiness_cap_never_starts_an_overlapping_or_late_query():
    gate = threading.Event()
    exited = threading.Event()
    read = CloudRead(Clock(), wake=0)
    read.readiness_timeout_s = .02
    def health():
        read.health_calls += 1
        gate.wait(2)
        exited.set()
        return E.Health(True)
    read.health = health
    runner = P._Runner({'ch': read}, .02)
    step = P._Step('ch', 'gap', {}, 'clickhouse')
    try:
        assert runner.phase([step])[0][1].detail == 'readiness: timeout'
        assert 'ch' in runner.dead
        assert runner.phase([step])[0][1].status == 'error'
        assert read.health_calls == 1 and read.query_calls == 0
    finally:
        gate.set()
        assert exited.wait(1)
        # Join the actual worker so cleanup is proved, not inferred from a flag.
        for thread in threading.enumerate():
            if thread.name == 'evidence-pack-ch':
                thread.join(1)
                assert not thread.is_alive()
    assert read.query_calls == 0
    assert len([row for row in runner.log if row.get('phase') == 'readiness']) == 1
    assert 'ch' not in runner.readiness  # no late readiness cache or success after the outer timeout


@pytest.mark.parametrize('cloud,block,policy,configured,expected', [
    (True, '', True, True, CH.WAKE_TIMEOUT_S), (False, '', True, True, 0),
    (True, 'switched off', True, True, 0), (True, '', False, True, 0),
    (True, '', True, False, 0)])
def test_readiness_allowance_only_for_configured_approved_cloud(cloud, block, policy, configured, expected):
    client = EP.ReadOnlyClickHouseClient(CH.Config('https://fixture.clickhouse.cloud' if configured else '',
                                                  'reader', 'fixture-key', cloud=cloud), meter=EP.Meter())
    gated = EP.GatedRetriever('clickhouse', CH.ClickHouseRetriever(client), EP.ProviderPolicy(policy, ('*',)),
                              block=block)
    assert gated.readiness_timeout_s == expected


def test_authenticated_cloud_health_transport_has_wake_cap_but_select_does_not(monkeypatch):
    requests = []
    def request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return http.Response(200, b'{"ok":1}\n', {})
    monkeypatch.setattr(CH.http, 'request', request)
    meter = EP.Meter()
    client = EP.ReadOnlyClickHouseClient(CH.Config('https://fixture.clickhouse.cloud', 'reader', 'fixture-key',
                                                  cloud=True), meter=meter)
    with client.read_budget(CH.WAKE_TIMEOUT_S):
        client.ping()
    with client.read_budget(3):
        client.select(EP.CH_HEALTH_SQL, {})
    assert meter.calls == 2
    first, second = [row[2] for row in requests]
    assert 119 < first['timeout'] <= CH.WAKE_TIMEOUT_S
    assert first['retries'] == 0 and first['total_timeout'] <= CH.WAKE_TIMEOUT_S
    assert first['headers']['X-ClickHouse-User'] == 'reader'
    assert first['headers']['X-ClickHouse-Key'] == 'fixture-key'
    assert first['body'] == EP.CH_HEALTH_SQL.encode()
    assert second['timeout'] <= 3 and second['retries'] == 0
    assert requests[0][0] == 'POST' and '/ping' not in requests[0][1]


def test_readiness_health_preserves_safe_transport_cause(monkeypatch):
    client = EP.ReadOnlyClickHouseClient(CH.Config('https://fixture.clickhouse.cloud', cloud=True), meter=EP.Meter())
    def rejected():
        raise http.HttpError(None, 'private provider text', retryable=False, failure_kind='tls')
    monkeypatch.setattr(client, 'ping', rejected)
    health = CH.ClickHouseRetriever(client).health()
    assert health.failure_kind == 'tls' and not health.retryable
