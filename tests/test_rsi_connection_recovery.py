"""Deterministic read recovery; no providers, model calls or production writes."""
from __future__ import annotations

import threading
from contextlib import contextmanager

import pytest

from agent_memory import rsi_evidence_pack as P
from agent_memory.integrations import evidence as E, http


class Clock:
    def __init__(self):
        self.at = 0.0

    def __call__(self):
        return self.at

    def sleep(self, seconds):
        self.at += seconds


class Read:
    source = 'ch'

    def __init__(self, failures=(), *, health=None, blocked=False, clock=None):
        self.failures = list(failures)
        self.health_result = health or E.Health(True)
        self.blocked, self.clock = blocked, clock
        self.calls = self.health_calls = 0
        self.budgets = []

    def _preflight_query(self, name, params):
        if self.blocked:
            raise E.Unavailable('policy-excluded: local egress rule')

    def health(self):
        self.health_calls += 1
        return self.health_result

    def named_queries(self):
        return {'read': E.NamedQuery('read', {}, 'fixed read')}

    @contextmanager
    def read_budget(self, seconds):
        self.budgets.append(seconds)
        yield

    def query(self, name, params):
        self.calls += 1
        if self.clock:
            self.clock.at += 0.1
        if self.failures:
            raise self.failures.pop(0)
        return []


def execute(monkeypatch, read, *, retr='ch', budget=2):
    clock = read.clock or Clock()
    monkeypatch.setattr(P.time, 'sleep', clock.sleep)
    runner = P._Runner({retr: read}, budget, clock)
    step = P._Step(retr, 'read', {}, P.SERVICE[retr])
    result = runner._query(retr, runner.r[retr], step, budget)
    return result, runner.observations[id(step)], clock


def test_temporary_read_recovers_and_records_attempts(monkeypatch):
    read = Read([http.HttpError(503, 'service unavailable')], clock=Clock())
    result, check, clock = execute(monkeypatch, read)
    assert result.status == 'ok'
    assert (check['recovery_status'], check['attempts']) == ('recovered', 2)
    assert check['elapsed_ms'] == 450
    assert read.health_calls == 2
    assert read.budgets == pytest.approx([.5, .7])
    assert clock.at < 2


@pytest.mark.parametrize('failure', [http.HttpError(401, 'denied'), http.HttpError(403, 'denied'),
                                    http.HttpError(429, 'rate limited'), ValueError('bad response')])
def test_no_retry_for_auth_rate_limit_or_malformed_data(monkeypatch, failure):
    read = Read([failure])
    result, check, _ = execute(monkeypatch, read)
    assert result.status == 'error'
    assert check['recovery_status'] == 'failed'
    assert check['attempts'] == read.calls == 1


def test_policy_is_checked_before_health_or_query(monkeypatch):
    read = Read(blocked=True)
    result, check, _ = execute(monkeypatch, read)
    assert result.status == 'unavailable'
    assert check['recovery_status'] == 'excluded'
    assert read.health_calls == read.calls == 0


def test_pastdev_recall_never_retried(monkeypatch):
    read = Read([TimeoutError()])
    _, check, _ = execute(monkeypatch, read, retr='pd')
    assert check['attempts'] == read.calls == 1


def test_persistent_failure_stops_after_two_retries(monkeypatch):
    read = Read([ConnectionError()] * 5)
    _, check, _ = execute(monkeypatch, read)
    assert check['recovery_status'] == 'failed'
    assert check['attempts'] == read.calls == 3


def test_shared_deadline_does_not_expand_for_recovery(monkeypatch):
    read = Read([ConnectionError()] * 5, clock=Clock())
    _, check, clock = execute(monkeypatch, read, budget=.3)
    assert read.calls == check['attempts'] == 1
    assert clock.at == .1


def test_failed_health_is_rechecked_before_query(monkeypatch):
    read = Read()
    outcomes = iter([E.Health(False, 'HTTP 503', True), E.Health(True)])
    read.health = lambda: next(outcomes)
    result, check, _ = execute(monkeypatch, read)
    assert result.status == 'ok'
    assert check['recovery_status'] == 'recovered'
    assert read.calls == 1


def test_timeout_does_not_spawn_overlapping_retry_threads():
    gate = threading.Event()
    read = Read()
    calls = []

    def slow(name, params):
        calls.append(1)
        gate.wait(2)
        return []

    read.query = slow
    runner = P._Runner({'ch': read}, .03)
    try:
        result = runner.phase([P._Step('ch', 'read', {}, 'clickhouse')])
        assert result[0][1].status == 'error'
        assert 'ch' in runner.dead
        again = runner.phase([P._Step('ch', 'read', {}, 'clickhouse')])
        assert again[0][1].status == 'error'
        assert len(calls) == 1
    finally:
        gate.set()


def test_no_authoritative_evidence_defers_candidate_before_model_call():
    from contextlib import nullcontext
    from types import SimpleNamespace
    from agent_memory import rsi_night as N

    night = N.Night.__new__(N.Night)
    night.conn = object()  # no lookup needed: the candidate has no amem references
    night._evidence, night._evidence_text = {}, {}
    night.health = {'clickhouse': 'unavailable'}
    night.notes, night.health_reasons = [], {}
    night.report = {'candidates': [{'key': 'candidate-a', 'selected': True}]}
    night.degraded = False
    night._interruptible = nullcontext
    night._merge_health = lambda *args: None
    night._cov = lambda: SimpleNamespace(pack=lambda *a, **k: None)
    night.budget = SimpleNamespace(record_provider=lambda *a: None)
    updates = []
    night._update_candidate = lambda *a, **k: updates.append(k)
    pack = SimpleNamespace(health={'clickhouse': 'unavailable'}, findings=[])
    night.ad = SimpleNamespace(evidence=SimpleNamespace(pack=lambda c: pack, render=lambda p: ''),
                               hypothesizer=object())  # would fail if invoked
    night._investigate({'id': 'id-a', 'dedupe_key': 'candidate-a', 'meta': {}, 'evidence_refs': []})
    assert updates[0]['status'] == 'deferred'
    assert night.report['candidates'][0]['skip_reason'] == 'required evidence unavailable'
    assert not night.report['candidates'][0]['selected']
    assert night.degraded


def test_later_failure_does_not_hide_earlier_recovery():
    from agent_memory import rsi_night as N
    night = N.Night.__new__(N.Night)
    night.health = {k: 'skipped' for k in N.PROVIDERS}
    night.health_reasons = {}
    night.report = {'connection_recovery': {'checks': []}}
    night._merge_health({'clickhouse': 'ok'}, [{'step': 'ch.read', 'status': 'ok',
                        'recovery_status': 'recovered', 'attempts': 2}], 'S1 delta')
    night._merge_health({'clickhouse': 'error'}, [{'step': 'ch.read', 'status': 'error'}], 'candidate B')
    assert night.health['clickhouse'] == 'error'
    assert [c['status'] for c in night.report['connection_recovery']['checks']] == ['recovered', 'failed']
    assert night.report['connection_recovery']['checks'][1]['attempts'] is None


def test_retry_after_is_not_ignored(monkeypatch):
    monkeypatch.setattr(http, '_read_attempt', lambda *a, **k: http.Response(503, b'busy', {'Retry-After': '10'}))
    with pytest.raises(http.HttpError) as error:
        http.request('GET', 'http://127.0.0.1/', retries=0, total_timeout=1)
    assert error.value.retryable is False


@pytest.mark.parametrize('field,value', [('attempts', True), ('attempts', 0), ('elapsed_ms', float('nan')),
                                       ('checked_at', 'yesterday'), ('source', 'unrecognized')])
def test_recovery_contract_rejects_invalid_evidence(field, value):
    from agent_memory.rsi_night import connection_recovery_problems
    row = dict(source='clickhouse', stage='S1 delta', step='ch.read', status='recovered', attempts=2,
               checked_at='2026-10-28T00:30:00Z', elapsed_ms=450, reason='retry succeeded', impact='read completed')
    row[field] = value
    assert connection_recovery_problems({'format': 'rsi-connection-recovery/1', 'checks': [row]})


def test_exhausted_budget_starts_no_read_and_keeps_attempt_count_unknown(monkeypatch):
    read = Read()
    result, check, _ = execute(monkeypatch, read, budget=0)
    assert result.status == 'error'
    assert read.calls == read.health_calls == 0
    assert check['attempts'] is None


def test_repeated_query_names_keep_their_own_measured_duration():
    class VariableRead(Read):
        def query(self, name, params):
            self.calls += 1
            self.clock.at += self.calls * .1
            return []
    clock = Clock()
    read = VariableRead(clock=clock)
    runner = P._Runner({'ch': read}, 2, clock)
    steps = [P._Step('ch', 'read', {}, 'clickhouse'), P._Step('ch', 'read', {}, 'clickhouse')]
    assert all(result.status == 'ok' for _, result in runner.phase(steps))
    assert [row['elapsed_ms'] for row in runner.log] == [100, 200]


def test_attempt_timeout_leaves_budget_for_sequential_recovery(monkeypatch):
    class CooperativeTimeout(Read):
        current_budget = None
        in_flight = 0
        maximum_in_flight = 0
        @contextmanager
        def read_budget(self, seconds):
            self.budgets.append(seconds)
            self.current_budget = seconds
            yield
        def query(self, name, params):
            self.calls += 1
            self.in_flight += 1
            self.maximum_in_flight = max(self.maximum_in_flight, self.in_flight)
            try:
                if self.calls == 1:
                    self.clock.at += self.current_budget
                    raise TimeoutError('attempt read deadline')
                self.clock.at += .1
                return []
            finally:
                self.in_flight -= 1
    read = CooperativeTimeout(clock=Clock())
    result, check, clock = execute(monkeypatch, read, budget=20)
    assert result.status == 'ok' and check['recovery_status'] == 'recovered'
    assert check['attempts'] == 2
    assert read.maximum_in_flight == 1 and read.in_flight == 0
    assert read.budgets == pytest.approx([6.5, 6.5])
    assert clock.at == pytest.approx(6.85) and clock.at < 20


def test_all_attempt_timeouts_share_original_deadline(monkeypatch):
    class AlwaysTimeout(Read):
        @contextmanager
        def read_budget(self, seconds):
            self.budgets.append(seconds)
            self.current_budget = seconds
            yield
        def query(self, name, params):
            self.calls += 1
            self.clock.at += self.current_budget
            raise TimeoutError('attempt deadline')
    read = AlwaysTimeout(clock=Clock())
    result, check, clock = execute(monkeypatch, read, budget=20)
    assert result.status == 'error' and check['recovery_status'] == 'failed'
    assert check['attempts'] == read.calls == 3
    assert read.budgets == pytest.approx([6.5, 6.5, 6.5])
    assert clock.at == 20


def test_external_context_is_not_reported_as_missing_authority():
    from datetime import datetime, UTC
    from agent_memory.integrations import refs
    source = 'amem:experiment/exp-20261017-other'
    candidate = P._Cand('candidate-a', 'parent-md:repo-profile', None, (), None, '~/agent-config/parent/PARENT.md')
    ev = E.EvidenceRef('past:'+source, 'pd', 'decision-memory', [source])
    raw = P._Raw(P._Step('pd', 'recall', {}, 'pastdev'), ev, 'pastdev', True)
    finding = P._finding(raw, {source}, P.PackLimits(), candidate,
                         {source: ({'skill:other:rule'}, {'~/.agents/skills/other/SKILL.md'})})
    assert finding.status == 'unlinked' and finding.authority is None
    assert 'context only' in finding.detail and 'authoritative record resolved' in finding.detail
    unknown_scope = P._finding(raw, {source}, P.PackLimits(), candidate, {})
    assert unknown_scope.status == 'unlinked' and unknown_scope.authority is None
    assert 'binding is not recorded' in unknown_scope.detail
    assert 'another rule' not in unknown_scope.detail
    missing = P._evidence_finding(('e1', datetime.now(UTC), 'pastdev', 'decision-memory', 'past:x', [], {}, None, ''), set(), 200)
    assert missing.status == 'unlinked' and 'producer identity required' in missing.detail
    stale = P._evidence_finding(('e2', datetime.now(UTC), 'pastdev', 'decision-memory', 'past:y', [source], {}, None, ''), set(), 200)
    assert stale.status == 'stale' and 'no longer resolve' in stale.detail


def test_langfuse_keeps_its_full_paginated_read_allowance(monkeypatch):
    read = Read([ConnectionError()], clock=Clock())
    result, check, _ = execute(monkeypatch, read, retr='lf', budget=20)
    assert result.status == 'ok' and check['attempts'] == 2
    assert read.budgets == pytest.approx([20, 19.65])
