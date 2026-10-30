"""Bounded S1 selection and honest read/selected/excluded accounting. No network or paid calls."""
from collections import Counter
from datetime import UTC, datetime, timedelta

from agent_memory import night_coverage as NC, rsi_evidence_pack as P
from agent_memory.integrations.evidence import EvidenceRef, QueryResult


def finding(source, index, *, status='ok'):
    return P.Finding(source, f'amem:evidence/{index:08x}-0000-4000-8000-000000000000', None,
                     {'n': 3}, 3, status, via={'supabase': 'sb.rsi_evidence', 'clickhouse': 'ch.query', 'langfuse': 'lf.query'}[source])


def test_supported_round_robin_is_bounded_deterministic_and_accounts_for_every_drop():
    groups = {'stored': [finding('supabase', i) for i in range(50)],
              'clickhouse': [finding('clickhouse', i + 100) for i in range(20)],
              'langfuse': [finding('langfuse', i + 200) for i in range(4)]}
    chosen, counts = P._select_delta(groups, 50)
    assert len(chosen) == 50
    assert Counter(f.source for f in chosen) == {'supabase': 26, 'clickhouse': 20, 'langfuse': 4}
    assert P._select_delta(dict(reversed(list(groups.items()))), 50) == (chosen, counts)
    assert counts['stored'] == {'read': 50, 'selected': 26, 'excluded': {'delta_limit': 24}}
    for row in counts.values():
        assert row['read'] == row['selected'] + sum(row['excluded'].values())


def test_unsupported_provider_does_not_displace_support_and_zero_cap_is_honest():
    groups = {'stored': [finding('supabase', i) for i in range(50)],
              'langfuse': [finding('langfuse', 100, status='unlinked')]}
    chosen, counts = P._select_delta(groups, 50)
    assert all(f.status == 'ok' for f in chosen)
    assert counts['langfuse']['excluded'] == {'delta_limit': 1}
    chosen, counts = P._select_delta(groups, 0)
    assert chosen == [] and sum(x['read'] for x in counts.values()) == 51
    assert sum(sum(x['excluded'].values()) for x in counts.values()) == 51


def test_delta_assembly_retains_live_sources_checkpoint_and_read_denominators(monkeypatch):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    start, boundary = now - timedelta(days=2), now - timedelta(days=1)
    subject = 'amem:experiment/exp-20261029-fixture'
    rows = [(f'{i:08x}-0000-4000-8000-000000000000', boundary, 'supabase', 'metric', None,
             [subject], {'n': 3}, boundary, None) for i in range(50)]

    def page(conn, kind, since, until, limit):
        return (rows, boundary, False) if kind == 'findings' else ([], None, False)

    def phase(runner, steps):
        done = []
        for retr, source, n in [('ch', 'clickhouse', 20), ('lf', 'langfuse', 4)]:
            st = P._Step(retr, 'fixture', {}, source)
            evs = [EvidenceRef(f'amem:evidence/{i + n:08x}-0000-4000-8000-000000000000', retr,
                               'metric', [subject], {'n': 3}) for i in range(n)]
            runner.log.append({'step': st.name, 'status': 'ok', 'found': n})
            done.append((st, QueryResult(retr, 'ok', evs)))
        return done

    monkeypatch.setattr(P, '_page', page)
    monkeypatch.setattr(P, 'existing', lambda conn, subjects: {subject})
    monkeypatch.setattr(P._Runner, 'phase', phase)
    delta = P.delta_since(start, conn=object(), now=now, retrievers={'ch': None, 'lf': None})
    assert delta.next_checkpoint == boundary.isoformat()
    assert len(delta.findings) == 50 and delta.finding_selection['stored']['read'] == 50
    assert Counter(f.source for f in delta.findings)['langfuse'] == 4
    ledger = NC.Ledger('fixture', 'fixture')
    ledger.delta(delta, since=start, until=now, before=start.isoformat(), next_checkpoint=delta.next_checkpoint)
    for f in delta.findings:
        ledger.finding(f, 'sighted')
    ledger.finished(NC.EVIDENCE)
    assert not ledger.errors
    use = ledger.uses[NC.EVIDENCE]
    assert (use.queried, use.considered, use.used) == (50, 26, 26)
    assert use.skipped == {'delta_limit': 24}
    assert ledger.snaps[NC.EVIDENCE]['collected'] == 50
    for source, n in [('clickhouse', 20), ('langfuse', 4)]:
        p = ledger.providers[source]
        assert (p.rows, p.considered, p.used) == (n, n, n)


def test_stored_paging_carries_unread_rows_forward_without_claiming_excluded_rows_used(pg_url):
    import psycopg
    from synth_support import fresh_db

    now = datetime(2026, 10, 30, tzinfo=UTC)
    start = now - timedelta(hours=1)
    subject = 'amem:experiment/exp-20261029-fixture'
    with fresh_db(pg_url) as url, psycopg.connect(url) as conn:
        conn.execute("insert into rsi_experiments (id, kind, created_at) values (%s, 'fixture', %s)",
                     (subject.split('/', 1)[1], start - timedelta(hours=1)))
        for i in range(65):
            conn.execute("insert into rsi_evidence (system, kind, subject_refs, metrics, created_at) "
                         "values ('manual', 'metric', %s, '{\"n\":3}', %s)",
                         ([subject], start + timedelta(seconds=i + 1)))
        first = P.delta_since(start, conn=conn, now=now)
        assert len(first.findings) == 50 and first.truncated['findings']
        assert first.next_checkpoint == (start + timedelta(seconds=50)).isoformat()
        # Selection is a separate bounded operation: exclusion must not move the query cursor backwards.
        first.findings, first.finding_selection = P._select_delta(
            {'stored': first.findings,
             'clickhouse': [finding('clickhouse', 100 + i) for i in range(20)],
             'langfuse': [finding('langfuse', 200 + i) for i in range(4)]}, 50)
        ledger = NC.Ledger('fixture', 'fixture')
        ledger.delta(first, since=start, until=now, before=start.isoformat(),
                     next_checkpoint=first.next_checkpoint)
        for f in first.findings:
            ledger.finding(f, 'sighted')
        ledger.finished(NC.EVIDENCE)
        assert ledger.uses[NC.EVIDENCE].used == 26
        assert ledger.uses[NC.EVIDENCE].skipped == {'delta_limit': 24}
        records, _ = ledger.records(status='ok', reason='', conn=conn)
        snapshot = next(r for r in records if isinstance(r, NC.CV.Snapshot) and r.dataset == NC.EVIDENCE)
        assert (snapshot.eligible, snapshot.collected) == (65, 50)
        second = P.delta_since(first.next_checkpoint, conn=conn, now=now)
        assert len(second.findings) == 15 and not second.truncated['findings']
        assert second.next_checkpoint == (now - timedelta(seconds=60)).isoformat()
        stored_first = {f.ref for f in first.findings if f.via == 'sb.rsi_evidence'}
        assert stored_first.isdisjoint(f.ref for f in second.findings)
