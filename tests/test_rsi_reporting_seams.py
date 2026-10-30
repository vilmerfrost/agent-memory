"""Selection and readiness evidence must survive the saved-night report boundary."""
from types import SimpleNamespace

from agent_memory import rsi_night as N


def test_collect_retains_selection_accounting_without_private_labels():
    selection = {'stored': {'read': 50, 'selected': 26, 'excluded': {'delta_limit': 24}},
                 'langfuse': {'read': 4, 'selected': 4, 'excluded': {}},
                 'private-provider-label': {'read': 1, 'selected': 1, 'excluded': {}}}
    delta = SimpleNamespace(since='2026-10-29T00:00:00+00:00', until='2026-10-30T00:00:00+00:00',
                            health={}, steps=[], finding_selection=selection)
    n = N.Night.__new__(N.Night)
    n.conn, n.report = object(), {'checkpoint': {}}
    n.ad = SimpleNamespace(evidence=SimpleNamespace(delta=lambda *a, **k: delta))
    n._poll = lambda: None
    n._previous_checkpoint = lambda: delta.since
    n._merge_health = lambda *a: None
    n._cov = lambda: SimpleNamespace(delta=lambda *a, **k: None)
    n._check_skill_refresh = lambda *a: None
    n._sightings = lambda *a: []
    n._attach_history = lambda *a: None
    n._pool = lambda *a: []
    n._triage = lambda *a: None
    n._collect()
    assert n.report['delta']['finding_selection'] == {k: selection[k] for k in ('stored', 'langfuse')}
    assert n.report['delta']['finding_selection']['stored']['selected'] == 26


def test_recovery_report_retains_safe_readiness_phase_and_cause():
    n = N.Night.__new__(N.Night)
    n.health, n.health_reasons = {k: 'skipped' for k in N.PROVIDERS}, {}
    n.report = {'connection_recovery': {'checks': []}}
    n._merge_health({'clickhouse': 'unavailable'}, [
        {'step': 'ch.readiness', 'status': 'unavailable', 'phase': 'readiness',
         'failure_kind': 'tls', 'budget_s': 120, 'attempts': 1, 'elapsed_ms': 12},
        {'step': 'ch.readiness', 'status': 'unavailable', 'phase': 'private phase text',
         'failure_kind': 'private provider text', 'budget_s': float('inf')},
        {'step': 'ch.readiness', 'status': 'unavailable', 'phase': [],
         'failure_kind': {}, 'budget_s': True}], 'S1 delta')
    first, second, third = n.report['connection_recovery']['checks']
    assert (first['phase'], first['failure_kind'], first['budget_s']) == ('readiness', 'tls', 120)
    assert not {'phase', 'failure_kind', 'budget_s'} & second.keys()
    assert not {'phase', 'failure_kind', 'budget_s'} & third.keys()


def test_inconsistent_selection_is_not_reported_as_verified_accounting():
    assert N.report_finding_selection({'stored': {'read': 50, 'selected': 26, 'excluded': {}}}) == {}
    assert N.report_finding_selection({'stored': {'read': True, 'selected': 1, 'excluded': {}}}) == {}
