"""Real PostgreSQL checks: fresh nonce, no stale successes, restricted entrypoints."""
import uuid
import psycopg


def test_request_coalesces_and_only_current_completed_nonce_can_succeed(pg_url):
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live'")
        def start(): return conn.execute("select public.amem_connection_probe_start()").fetchone()[0]
        def result(ident): return conn.execute("select public.amem_connection_probe_result(%s)",(ident,)).fetchone()[0]
        first=start()
        assert start()['request_id']==first['request_id']
        assert result(first['request_id'])['pending'] is True
        assert result(str(uuid.uuid4()))['check']['status']=='unknown'
        assert str(conn.execute("select public.amem_connection_probe_pending()").fetchone()[0])==first['request_id']
        assert conn.execute("select public.amem_connection_probe_finish(%s,true,74,'ok')",(first['request_id'],)).fetchone()[0] is True
        assert result(first['request_id'])['check']['status']=='healthy'
        second=start()
        assert second['request_id']!=first['request_id']
        assert conn.execute("select public.amem_connection_probe_finish(%s,true,74,'ok')",(first['request_id'],)).fetchone()[0] is False
        assert result(first['request_id'])['check']['status']=='unknown'
        assert result(second['request_id'])['pending'] is True
        conn.execute("update public.rsi_checkpoints set started_at=now()-interval '91 seconds' where job='connections:clickhouse-probe' and run_key='live'")
        assert result(second['request_id'])['pending'] is False
        assert result(second['request_id'])['check']['status']=='unknown'
        for role in ('amem_app','rsi_reader','rsi_writer'):
            for fn in ('public.amem_connection_probe_start()','public.amem_connection_probe_result(uuid)'):
                assert not conn.execute("select has_function_privilege(%s,%s,'execute')",(role,fn)).fetchone()[0]
        conn.rollback()
