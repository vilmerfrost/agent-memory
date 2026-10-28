import uuid
import psycopg


def test_manual_claim_replay_completion_and_expiry(pg_url):
    with psycopg.connect(pg_url) as c:
        c.execute('delete from amem_private.model_connection_tests')
        def start(provider, id): return c.execute('select public.amem_model_test_start(%s,%s)', (provider,id)).fetchone()[0]
        def claim(): return c.execute('select public.amem_model_test_claim()').fetchone()[0]
        def status(): return c.execute('select public.amem_model_test_status()').fetchone()[0]
        def finish(id, token, result): return c.execute('select public.amem_model_test_finish(%s,%s,%s::jsonb)',(id, token, result)).fetchone()[0]
        id=str(uuid.uuid4())
        assert start('codex',id)['accepted']
        assert start('codex',str(uuid.uuid4()))['accepted'] is False
        assert start('codex',id)['request_id']==id
        job=claim()
        assert job['request_id']==id
        assert claim() is None
        result='{"code":"ok","model":"gpt-6.1-sol","latency_ms":24,"tokens_in":5,"tokens_out":3}'
        assert not finish(id,str(uuid.uuid4()),result)
        assert finish(id,job['claim_id'],result)
        assert finish(id,job['claim_id'],result)
        assert status()[0]['state']=='verified'
        assert not finish(id,job['claim_id'],result.replace('24','25'))
        c.execute("update amem_private.model_connection_tests set requested_at=now()-interval '16 minutes',finished_at=now()-interval '16 minutes'")
        assert status()[0]['state']=='stale'
        assert start('codex',str(uuid.uuid4()))['accepted']
        job=claim()
        c.execute("update amem_private.model_connection_tests set requested_at=now()-interval '181 seconds'")
        assert claim() is None
        assert status()[0]['state']=='unknown'
        assert not finish(job['request_id'],job['claim_id'],result)
        for role in ('amem_app','rsi_reader','rsi_writer'):
            assert not c.execute("select has_function_privilege(%s,'public.amem_model_test_start(text,uuid)','execute')",(role,)).fetchone()[0]
        assert c.execute("select has_function_privilege('amem_app','public.amem_model_test_claim()','execute')").fetchone()[0]
        c.rollback()


def test_independent_provider_and_strict_result(pg_url):
    with psycopg.connect(pg_url) as c:
        c.execute('delete from amem_private.model_connection_tests')
        for provider in ('codex','claude'):
            c.execute('select public.amem_model_test_start(%s,%s)',(provider,str(uuid.uuid4())))
        claims=[c.execute('select public.amem_model_test_claim()').fetchone()[0] for _ in range(2)]
        assert {j['provider'] for j in claims}=={'codex','claude'}
        assert c.execute('select public.amem_model_test_claim()').fetchone()[0] is None
        job=next(j for j in claims if j['provider']=='claude')
        import json
        base=dict(code='ok',model='claude-opus-5-5',latency_ms=3,tokens_in=1,tokens_out=1)
        for bad in ({**base,'model':'other'}, {**base,'tokens_in':0,'tokens_out':0}, {**base,'prompt':'secret'}, {**base,'latency_ms':-1}, {**base,'tokens_out':None}):
            assert c.execute('select public.amem_model_test_finish(%s,%s,%s::jsonb)',(job['request_id'],job['claim_id'],json.dumps(bad))).fetchone()[0] is False
        c.rollback()


def test_two_workers_cannot_claim_the_same_request(pg_url):
    from concurrent.futures import ThreadPoolExecutor
    with psycopg.connect(pg_url,autocommit=True) as c:
        c.execute('delete from amem_private.model_connection_tests')
        ident=str(uuid.uuid4())
        c.execute("select public.amem_model_test_start('codex',%s)",(ident,))
        def claim(_):
            with psycopg.connect(pg_url,autocommit=True) as other:
                return other.execute('select public.amem_model_test_claim()').fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs=list(pool.map(claim,range(2)))
        assert sum(j is not None for j in jobs)==1
        c.execute("update amem_private.model_connection_tests set requested_at=now()-interval '6 minutes'")
        next_id=str(uuid.uuid4())
        c.execute("select public.amem_model_test_start('codex',%s)",(next_id,))
        c.execute("update amem_private.model_connection_tests set requested_at=now()-interval '6 minutes'")
        replay=c.execute("select public.amem_model_test_start('codex',%s)",(ident,)).fetchone()[0]
        assert replay['accepted'] is False
        assert replay['request_id']==next_id
        c.execute('delete from amem_private.model_connection_tests')
        c.execute('delete from amem_private.model_connection_receipts')
