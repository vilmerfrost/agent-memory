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
    data['facts'][0]['text'] = 'a' * 9001
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
    def bad_outline(_):
        raise ValueError('malformed')
    w.work(None, job, tmp_path, now=NOW, make_outline=bad_outline, make_speech=speech, do_upload=broken_upload)
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
    w.work(None, job, tmp_path, now=NOW, make_outline=lambda d: ({'fact_ids':[f['id'] for f in d['facts']]}, {}), make_speech=speech)
    w.resume_uploads(None, tmp_path)
    assert calls == ['speech'] and states[-1] == 'outcome_unknown'
