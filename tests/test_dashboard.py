"""Real Postgres RLS, restricted columns, undo and idempotent report ingestion."""
import json
import uuid
from pathlib import Path

import psycopg
import pytest

from agent_memory import dashboard as D

MIGRATION = next((Path(__file__).resolve().parents[1] / 'supabase/migrations').glob('*_night_desk.sql')).read_text()
EVIDENCE_MIGRATION = next((Path(__file__).resolve().parents[1] / 'supabase/migrations').glob('*_night_desk_evidence_columns.sql')).read_text()
TITLES_MIGRATION = next((Path(__file__).resolve().parents[1] / 'supabase/migrations').glob('*_night_desk_session_titles.sql')).read_text()
OWNER = str(uuid.UUID(int=6101))
OTHER = str(uuid.UUID(int=6102))
TABLES = ('rsi_nights','report_snapshots','ledger_events','runs','edits','undo_requests',
          'rsi_candidates','rsi_experiments','rsi_proposals','rsi_checkpoints','deviations','sessions','rsi_decisions')


@pytest.fixture
def owner_db(pg_url):
    with psycopg.connect(pg_url) as c:
        for role in ('anon','authenticated'):
            c.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{role}') then create role {role}; end if; end $$")
        c.execute('create schema if not exists auth')
        c.execute("create or replace function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$")
        c.execute('grant usage on schema auth,public to authenticated,anon')
        c.execute(MIGRATION)
        c.execute(EVIDENCE_MIGRATION)
        c.execute(TITLES_MIGRATION)
        c.execute('insert into amem_private.dashboard_owner values (true,%s) on conflict (singleton) do update set uid=excluded.uid', (OWNER,))
        c.execute("insert into rsi_nights values ('rls-test','partial','Test','{}','{}','{}','{}',now(),now()) on conflict do nothing")
        c.execute("insert into report_snapshots(kind,date,summary,report_path) values ('improve','2026-01-01','{}','local') on conflict do nothing")
        c.execute("insert into ledger_events(id,event,summary) values ('rls-test','pin','{}') on conflict do nothing")
        c.execute("insert into runs(routine,status) values ('night-desk-rls','pass')")
        eid = c.execute("insert into edits(instruction_source,rule_id,status,improve_id) values ('test','test','trial','harmless-test') returning id").fetchone()[0]
        c.execute("insert into undo_requests(edit_id) values (%s)", (eid,))
        c.execute("insert into rsi_candidates(dedupe_key,source,kind) values ('rls-test','test','test') on conflict do nothing")
        c.execute("insert into rsi_experiments(id,kind) values ('rls-test','test') on conflict do nothing")
        h = c.execute("select encode(sha256('test'::bytea),'hex')").fetchone()[0]
        c.execute('insert into rsi_artifacts(hash,manifest_canonical,manifest,files,baseline_id) values (%s,\'test\',\'{"format":"rsi-artifact/1"}\',\'{}\',\'test\') on conflict do nothing', (h,))
        pid = c.execute("insert into rsi_proposals(artifact_hash,title,targets) values (%s,'test','{}') returning id", (h,)).fetchone()[0]
        c.execute("insert into rsi_decisions(proposal_id,action,artifact_hash,source,verified,expires_at) values (%s,'reject',%s,'cli','cli-tty',now()+interval '1 day')", (pid,h))
        c.execute("insert into rsi_checkpoints(job,run_key,status) values ('rls-test','test','done') on conflict do nothing")
        c.execute("insert into sessions(session_id,kind,project_dir) values ('rls-test','chat','sensitive') on conflict do nothing")
        c.execute("insert into deviations(source_key,session_id,instruction_source,instruction_ref,what,why,how,followed_grade,result_grade) values ('rls-test','rls-test','test','test','what','why','how',1,1) on conflict do nothing")
        yield c, eid
        c.rollback()


def identity(c, uid, role='authenticated'):
    c.execute('reset role')
    c.execute("select set_config('request.jwt.claim.sub',%s,true)", (uid,))
    c.execute(f'set local role {role}')


@pytest.mark.parametrize('table', TABLES)
def test_each_policy_owner_reads_nonowner_sees_nothing(owner_db, table):
    c, _ = owner_db
    identity(c,OWNER)
    column = 'session_id' if table == 'sessions' else 'id' if table == 'edits' else '*'
    assert c.execute(f'select {column} from public.{table}').fetchone() is not None
    identity(c,OTHER)
    assert c.execute(f'select {column} from public.{table}').fetchall() == []
    identity(c,'')
    assert c.execute(f'select {column} from public.{table}').fetchall() == []
    identity(c,'','anon')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute(f'select {column} from public.{table}')


def test_owner_cannot_read_sensitive_columns_or_write(owner_db):
    c, _ = owner_db
    identity(c,OWNER)
    assert c.execute('select * from dashboard_sessions').fetchone()
    for query in ('select meta from sessions','select undo_token_hash from edits',
                  "insert into rsi_nights (night,status,headline,brief,report,costs,checkpoint) values ('bad','ok','bad','{}','{}','{}','{}')",
                  'delete from ledger_events','select * from amem_private.dashboard_owner'):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction():
                c.execute(query)


def test_session_titles_are_owner_only_and_readonly(owner_db):
    c, _ = owner_db
    query = 'select session_id,title,tool from sessions'
    identity(c, OWNER)
    assert c.execute(query).fetchone() is not None
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute("update sessions set title='x'")
    identity(c, OTHER)
    assert c.execute(query).fetchall() == []
    identity(c, '', 'anon')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute(query)


def test_edit_evidence_columns_are_owner_only_and_readonly(owner_db):
    c, _ = owner_db
    query = 'select deviation_ids,instruction_sha_before,instruction_sha_after from edits'
    identity(c, OWNER)
    assert c.execute(query).fetchone() is not None
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute("update edits set deviation_ids='{}'")
    identity(c, OTHER)
    assert c.execute(query).fetchall() == []
    identity(c, '', 'anon')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute(query)


def test_undo_owner_nonowner_unknown_and_already_undone(owner_db):
    c, eid = owner_db
    identity(c,OTHER)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute('select amem_request_undo_owner(%s)', (eid,))
    identity(c,OWNER)
    assert c.execute('select amem_request_undo_owner(%s)', (uuid.uuid4(),)).fetchone() == ('nothing to undo',)
    assert c.execute('select amem_request_undo_owner(%s)', (eid,)).fetchone() == ('queued',)
    assert c.execute('select amem_request_undo_owner(%s)', (eid,)).fetchone() == ('queued',)
    c.execute('reset role')
    c.execute("update undo_requests set result='reverted',handled_at=now() where edit_id=%s", (eid,))
    c.execute("update edits set status='reverted' where id=%s", (eid,))
    identity(c,OWNER)
    assert c.execute('select amem_request_undo_owner(%s)', (eid,)).fetchone() == ('already undone',)


def test_report_costs_missing_values_secret_counts_and_idempotence(pg_url,tmp_path):
    reports = tmp_path/'reports'; reports.mkdir()
    (reports/'evals-2026-10-24.json').write_text(json.dumps({'results':[{'id':'a','status':'PASS','cost_usd':4.12,'prompt':'do not copy'}, {'id':'b','status':'FAIL','cost_usd':3.39}]}))
    for name,cost in [('a',.57),('b',.62)]:
        (reports/f'skill-evals-2026-10-24-{name}.json').write_text(json.dumps({'skills':{name:{'status':'PASS','fixtures':[{'id':name,'cost_usd':cost}]}}}))
    (reports/'secret-scan-2026-10-24.json').write_text(json.dumps({'findings':{'repo':[{'secret':'never-store-me'}]},'new':[{'secret':'never-store-me'}]}))
    ledger=tmp_path/'ledger.jsonl'; ledger.write_text(json.dumps({'event':'pin','id':'x','t':'2026-10-24T12:00:00Z','token':'never-store-me'})+'\n')
    D.sync_reports(reports,ledger,db_url=pg_url)
    D.sync_reports(reports,ledger,db_url=pg_url)
    with psycopg.connect(pg_url) as c:
        rows = dict(c.execute("select kind,cost_usd from report_snapshots where date='2026-10-24'"))
        assert float(rows['evals-parent']) == 7.51 and float(rows['skill-evals']) == 1.19
        assert rows['secret-scan'] is None
        data = c.execute("select summary from report_snapshots where kind='secret-scan' and date='2026-10-24'").fetchone()[0]
        assert data['findings']==1 and data['new']==1 and 'never-store-me' not in json.dumps(data)
        assert c.execute("select count(*) from ledger_events where summary->>'id'='x'").fetchone()==(1,)
        assert 'never-store-me' not in json.dumps(c.execute("select summary from ledger_events where summary->>'id'='x'").fetchone()[0])


def test_malformed_and_incomplete_reports_are_reported(tmp_path,pg_url):
    (tmp_path/'evals-2026-10-24.json').write_text('{unfinished')
    ledger=tmp_path/'ledger'; ledger.write_text('{unfinished')
    result=D.sync_reports(tmp_path,ledger,db_url=pg_url)
    assert result['errors']==['evals-2026-10-24.json'] and result['ledger_events']==0


def test_partial_case_cost_is_not_labelled_complete(tmp_path):
    path = tmp_path / 'evals-2026-10-24.json'
    summary, cost = D.snapshot('evals-parent', [(path, {'results': [
        {'id': 'priced', 'cost_usd': 1.25}, {'id': 'unpriced', 'status': 'FAIL'}]})])
    assert cost == 1.25 and summary['cost_complete'] is False
    assert all(case['report'] == path.name for case in summary['cases'])
    summary, cost = D.snapshot('evals-parent', [(path, {'cost_usd': 0, 'results': [{'id': 'skipped', 'status': 'SKIP'}]})])
    assert cost == 0 and summary['cost_complete'] is True


def test_ledger_preserves_change_identity_reason_and_usage_and_upgrades_existing_rows(pg_url, tmp_path):
    import hashlib
    change = 'dashboard-context-test'
    records = [
        {'event': 'applied', 'id': change, 't': '2026-10-20T12:00:00Z', 'target': '~/example.md',
         'rule_id': 'example-rule', 'topic': 'Example change', 'why': 'Clarify the task', 'status': 'trial'},
        {'event': 'eval-cost', 'id': change, 't': '2026-10-20T13:00:00Z',
         'cost': {'sessions': 2, 'tokens': {'input': 100, 'total': 100, 'secret': 'never-store'}, 'secret': 'never-store'},
         'window': {'start': '2026-10-20T12:00:00Z', 'end': '2026-10-20T12:10:00Z'}},
        {'event': 'reverted', 'id': change, 't': '2026-10-21T12:00:00Z', 'status': 'reverted',
         'revert_reason': 'phone undo', 'secret': 'never-store'},
    ]
    ledger = tmp_path / 'ledger.jsonl'
    ledger.write_text(''.join(json.dumps(r) + '\n' for r in records))
    digest = hashlib.sha256(json.dumps(records[2], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with psycopg.connect(pg_url) as c:
        c.execute("insert into ledger_events(id,event,summary) values (%s,'reverted',%s) on conflict do nothing",
                  (digest, psycopg.types.json.Jsonb({'id': change, 'event': 'reverted'})))
        created = c.execute('select created_at from ledger_events where id=%s', (digest,)).fetchone()[0]
    D.sync_reports(tmp_path, ledger, db_url=pg_url)
    with psycopg.connect(pg_url) as c:
        rows = dict(c.execute("select event,summary from ledger_events where summary->>'id'=%s", (change,)))
        assert rows['applied']['why'] == 'Clarify the task'
        assert rows['reverted']['revert_reason'] == 'phone undo'
        assert rows['reverted']['change_context']['target'] == '~/example.md'
        assert rows['eval-cost']['cost'] == {'sessions': 2, 'tokens': {'input': 100, 'total': 100}}
        assert 'cost_usd' not in rows['eval-cost']['cost']
        assert 'never-store' not in json.dumps(rows)
        version = c.execute('select xmin::text,created_at from ledger_events where id=%s', (digest,)).fetchone()
        assert version[1] == created
    D.sync_reports(tmp_path, ledger, db_url=pg_url)
    with psycopg.connect(pg_url) as c:
        assert c.execute('select xmin::text,created_at from ledger_events where id=%s', (digest,)).fetchone() == version
        assert c.execute("select count(*) from ledger_events where summary->>'id'=%s", (change,)).fetchone() == (3,)
