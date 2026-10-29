from datetime import datetime, timezone
import json
import uuid

import psycopg
import pytest

from agent_memory import morning_overview as m
from agent_memory import morning_overview_worker as w

NOW = datetime(2026, 10, 29, 8, 30, tzinfo=timezone.utc)


def run(id, status, at, routine='nightly-check', reason=None):
    return dict(id=id, routine=routine, status=status, started_at=at, failure_reason=reason, meta={})


def snap(runs=(), row=None, **kw):
    return m.snapshot(row or {'night': '2026-10-28', 'status': 'ok', 'brief': {}, 'report': {}}, runs, now=NOW, ingestion_fresh=True, **kw)


def test_failure_recovery_skip_and_exact_boundaries():
    data = snap([
        run('old', 'fail', '2026-10-28T16:59:59Z'),
        run('start', 'fail', '2026-10-28T17:00:00Z', reason='Docker engine unreachable'),
        run('pass', 'pass', '2026-10-29T04:00:00Z'),
        run('future', 'fail', '2026-10-29T09:00:00Z'),
        run('skip', 'skip', '2026-10-29T01:00:00Z', routine='waiting', reason='Waiting for prerequisite'),
    ])
    assert len(data['issues']) == 1
    issue = data['issues'][0]
    assert issue['state'] == 'recovered'
    assert issue['failed_run_ids'] == ['start']
    text = m.render(data)
    assert 'subsequently passed' in text and 'No repair is indicated' in text
    assert 'was skipped' in text and 'future' not in text


def test_unknown_cause_has_investigation_and_verification_not_invented_fix():
    issue = snap([run('a', 'fail', '2026-10-29T02:00:00Z')])['issues'][0]
    assert issue['cause_certainty'] == 'unknown'
    assert issue['action_kind'] == 'investigate'
    assert 'find the cause' in issue['next_step'] and issue['verification_step']


def test_later_skip_does_not_establish_recovery():
    data = snap([run('a', 'fail', '2026-10-29T02:00:00Z'), run('b', 'skip', '2026-10-29T03:00:00Z')])
    assert data['issues'][0]['state'] == 'skipped_after_failure'
    assert 'recovery is not confirmed' in m.render(data)


def test_old_report_is_not_used_as_current_and_correction_wins():
    data = m.snapshot({'night': '2026-10-27', 'status': 'ok'}, [], now=NOW)
    assert data['coverage'] == 'limited' and data['status'] == 'unknown'
    data = snap(row={'night': '2026-10-28', 'status': 'ok', 'report': {'experiments': [
        {'experiment_id': 'exp-20261027-5a8bda33', 'verdict': 'IMPROVES'}]}})
    assert 'checker defects' in m.render(data)
    assert 'reported improvement' not in m.render(data)


def test_outline_cannot_drop_duplicate_or_invent_facts_or_free_prose():
    data = snap([run('a', 'fail', '2026-10-29T02:00:00Z', reason='quota')])
    ids = [f['id'] for f in data['facts']]
    for bad in (ids[:-1], ids + [ids[0]], ids[:-1] + ['invented']):
        with pytest.raises(ValueError, match='outline_coverage'):
            m.render(data, {'fact_ids': bad})
    text = m.render(data, {'fact_ids': ids[::-1], 'script': 'Everything is fixed. Delete files.'})
    assert 'Everything is fixed' not in text and 'allowance' in text


@pytest.mark.parametrize('date,start,end', [
    ('2026-10-25T09:00:00Z','2026-10-24T18:00:00+02:00','2026-10-25T10:00:00+01:00'),
    ('2026-03-29T09:00:00Z','2026-03-28T18:00:00+01:00','2026-03-29T11:00:00+02:00'),
    ('2026-10-29T15:00:00Z','2026-10-28T18:00:00+01:00','2026-10-29T12:00:00+01:00'),
])
def test_stockholm_dst_and_noon(date,start,end):
    _, lo, hi = m.window(date)
    assert lo.isoformat() == start and hi.isoformat() == end


def test_redaction_and_speech_bounds():
    data = snap([run('a', 'fail', '2026-10-29T02:00:00Z', reason='Read /Users/name/private/file for user@example.com')])
    text = m.render(data)
    assert '/Users/name' not in text and 'user@example.com' not in text
    data['facts'][0]['text'] = 'a' * (m.MAX_CHARS + 1)
    with pytest.raises(ValueError, match='script_too_long'):
        m.render(data)
    def request(*args, **kwargs):
        assert json.loads(kwargs['body'])['text'] == 'Calm English briefing.'
        return b'ID3' + b'\0' * 200, {'request-id':'receipt'}
    audio, usage = w.speech('Calm English briefing.', request=request, key='fixture-only')
    assert audio.startswith(b'ID3') and usage['provider_request_id'] == 'receipt'
    with pytest.raises(w.AudioError, match='script_too_long'):
        w.speech('x'*9001, request=request, key='fixture-only')


def test_queue_claims_limits_and_transitions(pg_url):
    with psycopg.connect(pg_url) as c:
        night = c.execute("select (now() at time zone 'Europe/Stockholm')::date-1").fetchone()[0]
        c.execute('delete from amem_private.morning_overviews')
        def enqueue(manual=False):
            return c.execute('select public.amem_audio_enqueue(%s,%s)',(night,manual)).fetchone()[0]
        assert enqueue()['accepted']
        assert not enqueue()['accepted']
        job = c.execute('select public.amem_audio_claim()').fetchone()[0]
        assert c.execute('select public.amem_audio_claim()').fetchone()[0] is None
        def save(state, data, token=None):
            return c.execute('select public.amem_audio_save(%s,%s,%s,%s::jsonb)',(job['id'], token or job['claim_id'],state,json.dumps(data))).fetchone()[0]
        assert not save('ready',{'bytes':1000,'checksum':'a'*64})
        assert not save('text_ready',{},str(uuid.uuid4()))
        assert save('text_ready',{'transcript':'A source-bound briefing.'})
        assert save('narrating',{})
        assert save('uploading',{})
        context = c.execute('select public.amem_audio_upload_context(%s,%s)',(job['id'],job['claim_id'])).fetchone()[0]
        assert context['path'] == f'overviews/{job["id"]}.mp3'
        assert save('ready',{'bytes':1000,'checksum':'a'*64})
        assert save('ready',{'bytes':1000,'checksum':'a'*64})
        assert not save('ready',{'bytes':1000,'checksum':'b'*64})
        assert not enqueue(True)['accepted']  # cooldown
        c.execute("update amem_private.morning_overviews set requested_at=now()-interval '6 minutes'")
        assert enqueue(True)['accepted']
        next_job=c.execute('select public.amem_audio_claim()').fetchone()[0]
        c.execute("update amem_private.morning_overviews set claimed_at=now()-interval '21 minutes' where id=%s",(next_job['id'],))
        c.execute('select public.amem_audio_claim()')
        assert c.execute('select state from amem_private.morning_overviews where id=%s',(next_job['id'],)).fetchone()[0]=='outcome_unknown'
        assert not enqueue(True)['accepted']
        for role in ('amem_app','rsi_reader','rsi_writer'):
            assert not c.execute("select has_function_privilege(%s,'public.amem_audio_request(date)','execute')",(role,)).fetchone()[0]
        assert c.execute("select has_function_privilege('amem_app','public.amem_audio_claim()','execute')").fetchone()[0]
        c.rollback()


def test_upload_resume_reuses_bytes_without_another_provider_call(tmp_path, monkeypatch):
    data = snap([run('a', 'fail', '2026-10-29T02:00:00Z', reason='timeout')])
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'fixture-only')
    monkeypatch.setattr(w, 'collect', lambda *_: data)
    monkeypatch.setattr(w.shutil, 'which', lambda _: '/fixture/ffprobe')
    monkeypatch.setattr(w, 'audio_duration', lambda _: 4.2)
    states, calls = [], []
    monkeypatch.setattr(w, 'update', lambda conn, job, state, payload: states.append(state))
    job = dict(id=str(uuid.uuid4()), claim_id=str(uuid.uuid4()), night='2026-10-28')
    def speech(text):
        calls.append('speech')
        return b'ID3' + b'\0'*200, dict(characters=len(text))
    def broken_upload(*_):
        raise w.AudioError('outcome_unknown')
    def draft(data):
        return {'paragraphs': [{'text': 'The worker timed out. Check the Mac connection, then verify the next result.', 'fact_ids': [f['id'] for f in data['facts']]}]}, {}
    w.work(None, job, tmp_path, now=NOW, make_script=draft, make_speech=speech, do_upload=broken_upload)
    assert states[-1] == 'uploading' and calls == ['speech']
    monkeypatch.setattr(w, 'upload', lambda *_: calls.append('upload'))
    w.resume_uploads(None, tmp_path)
    assert states[-1] == 'ready' and calls == ['speech','upload']
    w.resume_uploads(None, tmp_path)
    assert calls == ['speech','upload']


def test_ambiguous_speech_outcome_never_automatically_repeats(tmp_path, monkeypatch):
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'fixture-only')
    monkeypatch.setattr(w, 'collect', lambda *_: snap())
    monkeypatch.setattr(w.shutil, 'which', lambda _: '/fixture/ffprobe')
    states, calls = [], []
    monkeypatch.setattr(w, 'update', lambda conn, job, state, payload: states.append(state))
    def speech(_):
        calls.append('speech')
        raise w.AudioError('outcome_unknown')
    job = dict(id=str(uuid.uuid4()), claim_id=str(uuid.uuid4()), night='2026-10-28')
    w.work(None, job, tmp_path, now=NOW, make_script=lambda d: ({'paragraphs':[{'text':'The night finished. No routine results were recorded. Cost is unknown.', 'fact_ids':[f['id'] for f in d['facts']]}]}, {}), make_speech=speech)
    w.resume_uploads(None, tmp_path)
    assert calls == ['speech'] and states[-1] == 'outcome_unknown'


def test_api_default_grants_do_not_expose_worker_functions(pg_url):
    from conftest import base_env, run_amem
    name='morning_audio_privileges'
    roles=('anon','authenticated','service_role')
    created=[]
    with psycopg.connect(pg_url,autocommit=True) as c:
        c.execute(f'create database {name}')
        for role in roles:
            if not c.execute('select 1 from pg_roles where rolname=%s',(role,)).fetchone():
                c.execute(f'create role {role} nologin'); created.append(role)
    url=pg_url.rsplit('/',1)[0]+'/'+name
    try:
        with psycopg.connect(url,autocommit=True) as c:
            for kind in ('tables','functions','sequences'):
                c.execute(f'alter default privileges in schema public grant all on {kind} to anon,authenticated,service_role')
        result=run_amem('db','migrate',env=base_env(AMEM_DB_URL=url))
        assert result.returncode==0,result.stderr
        with psycopg.connect(url) as c:
            matrix={
                'amem_audio_enqueue(date,boolean)':{'amem_app'},
                'amem_audio_claim()':{'amem_app'},
                'amem_audio_save(uuid,uuid,text,jsonb)':{'amem_app'},
                'amem_audio_upload_context(uuid,uuid)':{'service_role'},
                'amem_audio_request(date)':{'authenticated','service_role'},
                'amem_audio_status(date)':{'authenticated','service_role'},
                'amem_audio_playback(uuid)':{'authenticated','service_role'},
            }
            for fn, allowed in matrix.items():
                for role in (*roles,'amem_app','rsi_reader','rsi_writer'):
                    assert c.execute("select has_function_privilege(%s,%s,'execute')",(role,'public.'+fn)).fetchone()[0]==(role in allowed),(role,fn)
            for role in roles:
                assert not c.execute("select has_table_privilege(%s,'amem_private.morning_overviews','select')",(role,)).fetchone()[0]
                for operation in ('insert','update','delete'):
                    assert not c.execute('select has_table_privilege(%s,%s,%s)',(role,'public.report_annotations',operation)).fetchone()[0]
    finally:
        with psycopg.connect(pg_url,autocommit=True) as c:
            c.execute(f'drop database {name} with (force)')
            for role in created:
                c.execute(f'drop role {role}')


def draft_for(data, text='The night finished. Check the recorded results and costs.'):
    return {'paragraphs': [{'text': text, 'fact_ids': [f['id'] for f in data['facts']]}]}


def test_direct_sonnet_api_request_and_usage():
    def request(url, **kw):
        assert url == 'https://api.anthropic.com/v1/messages'
        body = json.loads(kw['body'])
        assert body['model'] == 'claude-sonnet-5-5' and 'tools' not in body
        assert kw['headers']['x-api-key'] == 'fixture-only'
        return json.dumps({'model': w.SCRIPT_MODEL, 'stop_reason': 'end_turn',
            'content': [{'type': 'thinking', 'thinking': 'private reasoning'}, {'type': 'text', 'text': '{"approved":true}'}],
            'usage': {'input_tokens': 15, 'output_tokens': 5}}).encode(), {'request-id': 'test-id'}
    value, usage = w.claude_json('Write a briefing', {}, request=request, key='fixture-only')
    assert value == {'approved': True} and usage['input_tokens'] == 15
    assert usage['provider_request_id'] == 'test-id'


def test_complete_natural_prose_requires_review_and_preserves_metadata():
    data = snap()
    calls = []
    def call(system, evidence):
        calls.append(system)
        return (draft_for(data) if len(calls) == 1 else {'approved': True, 'missing_fact_ids': [], 'concerns': []}), {'output_tokens': 10}
    draft, usage = w.write_script(data, call=call)
    assert m.narration(data, draft).startswith('The night finished.')
    assert len(calls) == 2 and usage['review']['approved']
    assert 'No duration target' in calls[0]


@pytest.mark.parametrize('review', [
    {'approved': False, 'missing_fact_ids': [], 'concerns': ['unsupported cause']},
    {'approved': True, 'missing_fact_ids': ['costs'], 'concerns': []},
    {'approved': True},
])
def test_semantic_review_rejects_unsupported_or_missing_content(review):
    data = snap()
    def call(system, evidence):
        return (draft_for(data) if system == w.WRITER_PROMPT else review), {}
    with pytest.raises(w.AudioError, match='script_review_failed') as error:
        w.write_script(data, call=call)
    assert error.value.details['script_draft'] == draft_for(data)
    assert error.value.details['llm_usage']['review'] == review
    assert error.value.details['snapshot']['input_digest'] == data['input_digest']


def test_narration_rejects_missing_sources_and_private_data():
    data = snap()
    draft = draft_for(data)
    draft['paragraphs'][0]['fact_ids'].remove('costs')
    with pytest.raises(ValueError, match='script_coverage'):
        m.narration(data, draft)
    with pytest.raises(ValueError, match='script_private_data'):
        m.narration(data, draft_for(data, 'Read /Users/test/private for user@example.com'))


def test_full_evidence_preserves_long_details_costs_and_all_patterns():
    detail = 'Important technical detail ' * 100
    data = snap([run('a', 'fail', '2026-10-29T02:00:00Z', reason='OAuth session expired')],
        row={'night': '2026-10-28', 'status': 'partial', 'brief': {'patterns': {'items': [
            {'summary': f'Pattern {n}'} for n in range(8)]}, 'costs': {'estimated_usd': 0.06}},
            'report': {'details': detail, 'access_token': 'fixture-sensitive', 'costs': {'subscription': {'tokens_in': 300}}}})
    evidence = data['evidence']
    assert evidence['night_record']['report']['details'] == detail.strip()
    assert evidence['night_record']['report']['access_token'] == '[redacted]'
    assert evidence['night_record']['report']['costs']['subscription']['tokens_in'] == 300
    assert len([f for f in data['facts'] if f['id'].startswith('pattern:')]) == 8
    assert 'sign-in' in data['issues'][0]['next_step']


def test_script_failure_never_uses_canned_fallback_or_calls_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(w, 'collect', lambda *_: snap())
    states = []
    monkeypatch.setattr(w, 'update', lambda c, j, state, payload: states.append((state, payload)))
    def fail(_):
        raise w.AudioError('script_review_failed')
    def forbidden(_):
        pytest.fail('Rejected script must not be narrated')
    job = {'id': str(uuid.uuid4()), 'claim_id': str(uuid.uuid4()), 'night': '2026-10-28'}
    w.work(None, job, tmp_path, now=NOW, make_script=fail, make_speech=forbidden)
    assert states == [('failed', {'error': 'script_review_failed'})]


def test_api_truncation_and_oversized_context_are_not_silently_accepted():
    def truncated(*_, **kw):
        return json.dumps({'model': w.SCRIPT_MODEL, 'stop_reason': 'max_tokens'}).encode(), {}
    with pytest.raises(w.AudioError, match='script_incomplete'):
        w.claude_json('system', {}, request=truncated, key='fixture-only')
    with pytest.raises(w.AudioError, match='script_context_too_large'):
        w.claude_json('system', {'large': 'a' * w.MAX_CONTEXT_BYTES}, request=truncated, key='fixture-only')


def test_unchanged_snapshot_retains_audio_without_model_or_speech_calls(tmp_path,monkeypatch):
    data=snap()
    old=tmp_path/str(uuid.uuid4());old.mkdir()
    w.save_receipt(old/'receipt.json',{'stage':'ready','job':{'id':old.name},'data':{'snapshot':data,'as_of':data['as_of']}})
    monkeypatch.setattr(w,'collect',lambda *_:data)
    states=[]
    monkeypatch.setattr(w,'update',lambda conn,job,state,payload:states.append((state,payload)))
    def forbidden(*_):
        pytest.fail('Unchanged content must not call an LLM or speech provider')
    job={'id':str(uuid.uuid4()),'claim_id':str(uuid.uuid4()),'night':data['night']}
    w.work(None,job,tmp_path,now=NOW,make_script=forbidden,make_speech=forbidden)
    assert states==[('failed',{'error':'no_new_results','as_of':data['as_of'],'existing_overview_id':old.name})]


def test_rejected_draft_and_usage_survive_in_db_payload_and_receipt(tmp_path, monkeypatch):
    data = snap()
    draft = draft_for(data)
    details = {'script_draft': draft, 'llm_usage': {'review': {'approved': False, 'concerns': ['missing cost scope']}}}
    monkeypatch.setattr(w, 'collect', lambda *_: data)
    states = []
    monkeypatch.setattr(w, 'update', lambda c, j, state, payload: states.append((state, payload)))
    def rejected(_):
        raise w.AudioError('script_review_failed', details=details)
    def forbidden(_):
        pytest.fail('Rejected narration must not call speech')
    job = {'id': str(uuid.uuid4()), 'claim_id': str(uuid.uuid4()), 'night': data['night']}
    w.work(None, job, tmp_path, now=NOW, make_script=rejected, make_speech=forbidden)
    assert states == [('failed', {'error': 'script_review_failed', **details})]
    receipt = json.loads((tmp_path / job['id'] / 'receipt.json').read_text())
    assert receipt['failure']['script_draft'] == draft
    assert receipt['failure']['llm_usage']['review']['concerns'] == ['missing cost scope']


def test_provider_name_inside_behavior_test_does_not_prescribe_login_repair():
    issue = snap([run('a', 'fail', '2026-10-29T02:00:00Z', routine='evals-parent',
                      reason='parent asked questions; i-claude-mode-merge-named failed')])['issues'][0]
    assert issue['action_kind'] == 'investigate'
    assert 'sign-in' not in issue['next_step']
    assert 'subscription' not in issue['next_step']


def test_successful_checks_keep_audit_undo_and_internal_skip_findings():
    audit = run('audit', 'pass', '2026-10-29T02:00:00Z', routine='routine-audit')
    audit['meta'] = {'problems': 10, 'failures': ['dep-audit: last run FAIL']}
    outcome = run('outcome', 'pass', '2026-10-29T02:00:00Z', routine='amem-rsi-outcome')
    outcome['meta'] = {'suggest_undo': 1, 'mixed': 2}
    harvest = run('harvest', 'pass', '2026-10-29T02:00:00Z', routine='rsi-harvest')
    harvest['summary'] = 'rc=0 skipped: 187 drafts wait (max 30)'
    data = snap([audit, outcome, harvest])
    facts = {f['id']: f['text'] for f in data['facts']}
    assert 'dep-audit' in facts['routine-note:routine-audit']
    assert 'suggest_undo' in facts['routine-note:amem-rsi-outcome']
    assert '187 drafts' in facts['routine-note:rsi-harvest']
    assert data['issues'] == []  # passing checks are not reclassified as failed runs
