"""Real PostgreSQL checks: fresh nonce, no stale successes, restricted entrypoints."""
import uuid
import psycopg


def test_request_coalesces_and_only_current_completed_nonce_can_succeed(pg_url):
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live'")
        def start(): return conn.execute("select public.amem_connection_diagnostic_start()").fetchone()[0]
        def result(ident): return conn.execute("select public.amem_connection_probe_result(%s)",(ident,)).fetchone()[0]
        first=start()
        assert start()['request_id']==first['request_id']
        assert result(first['request_id'])['pending'] is True
        assert result(str(uuid.uuid4()))['check']['status']=='unknown'
        assert str(conn.execute("select public.amem_connection_probe_pending()").fetchone()[0])==first['request_id']
        assert conn.execute("select public.amem_connection_probe_finish(%s,true,74,'ready')",(first['request_id'],)).fetchone()[0] is True
        assert result(first['request_id'])['check']['status']=='healthy'
        second=start()
        assert second['request_id']!=first['request_id']
        assert conn.execute("select public.amem_connection_probe_finish(%s,true,74,'ready')",(first['request_id'],)).fetchone()[0] is False
        assert result(first['request_id'])['check']['status']=='unknown'
        assert result(second['request_id'])['pending'] is True
        conn.execute("update public.rsi_checkpoints set started_at=now()-interval '241 seconds' where job='connections:clickhouse-probe' and run_key='live'")
        assert result(second['request_id'])['pending'] is False
        assert result(second['request_id'])['check']['status']=='unknown'
        for role in ('amem_app','rsi_reader','rsi_writer'):
            for fn in ('public.amem_connection_diagnostic_start()','public.amem_connection_probe_result(uuid)'):
                assert not conn.execute("select has_function_privilege(%s,%s,'execute')",(role,fn)).fetchone()[0]
        conn.rollback()


def test_saved_status_preserves_time_failure_and_never_starts_queries(pg_url):
    with psycopg.connect(pg_url) as c:
        c.execute("delete from rsi_checkpoints where job='connections:clickhouse-probe'")
        def current(): return c.execute('select public.amem_connection_probe_current()').fetchone()[0]
        assert current()['request_id'] is None
        assert current()['pending'] is False
        request=c.execute('select public.amem_connection_diagnostic_start()').fetchone()[0]
        pending=current()
        assert pending['requested_at']==request['requested_at']
        assert pending['check']['checked_at']==request['requested_at']
        assert current()['request_id']==request['request_id']
        c.execute("select public.amem_connection_probe_finish(%s,false,20000,'timeout')",(request['request_id'],))
        failed=current()
        assert failed['check']['status']=='unhealthy'
        assert '20-second' in failed['check']['message']
        c.execute("update rsi_checkpoints set started_at=now()-interval '6 minutes',finished_at=now()-interval '5 minutes' where job='connections:clickhouse-probe'")
        assert current()['check']['status']=='unhealthy'
        assert 'timed out' in current()['check']['message']
        assert current()['request_id']==request['request_id']
        c.execute("update rsi_checkpoints set status='running',finished_at=null where job='connections:clickhouse-probe'")
        expired=current()
        assert expired['pending'] is False
        assert expired['check']['status']=='unknown'
        assert '240 seconds' in expired['check']['message']
        assert expired['check']['checked_at']==expired['requested_at']
        for role in ('amem_app','rsi_reader','rsi_writer'):
            assert not c.execute("select has_function_privilege(%s,'public.amem_connection_probe_current()','execute')",(role,)).fetchone()[0]
        c.rollback()


def test_claim_once_and_langfuse_pipeline_result(pg_url):
    with psycopg.connect(pg_url) as c:
        c.execute("delete from rsi_checkpoints where job='connections:clickhouse-probe'")
        request=c.execute('select public.amem_connection_diagnostic_start()').fetchone()[0]
        ident=request['request_id']
        assert str(c.execute('select public.amem_connection_probe_claim()').fetchone()[0])==ident
        assert c.execute('select public.amem_connection_probe_claim()').fetchone()[0] is None
        assert c.execute("select public.amem_connection_langfuse_result(%s)",(ident,)).fetchone()[0]['pending']
        c.execute("select public.amem_connection_probe_finish(%s,true,100,'ready')",(ident,))
        assert c.execute('select public.amem_connection_diagnostic_start()').fetchone()[0]['request_id']==ident
        assert c.execute("select public.amem_connection_langfuse_finish(%s,'rejected',120)",(ident,)).fetchone()[0]
        result=c.execute("select public.amem_connection_langfuse_result(%s)",(ident,)).fetchone()[0]
        assert result['check']['status']=='unhealthy'
        assert 'spans or scores' in result['check']['message']
        assert not result['pending']
        assert not c.execute("select public.amem_connection_langfuse_finish(%s,'ok',120)",(ident,)).fetchone()[0]
        c.rollback()


def test_legacy_checks_cannot_be_relabelled_or_trigger_new_diagnostic_writes(pg_url):
    with psycopg.connect(pg_url) as c:
        c.execute("delete from rsi_checkpoints where job='connections:clickhouse-probe'")
        old=c.execute('select public.amem_connection_probe_start()').fetchone()[0]
        assert c.execute('select public.amem_connection_probe_claim()').fetchone()[0] is None
        saved=c.execute('select public.amem_connection_probe_current()').fetchone()[0]
        assert saved['check']['status']=='unknown' and saved['pending'] is False
        assert 'basic query access only' in saved['check']['message']
        fresh=c.execute('select public.amem_connection_diagnostic_start()').fetchone()[0]
        assert fresh['request_id']!=old['request_id']
        assert not c.execute("select public.amem_connection_probe_finish(%s,true,10,'ok')",(fresh['request_id'],)).fetchone()[0]
        c.rollback()
