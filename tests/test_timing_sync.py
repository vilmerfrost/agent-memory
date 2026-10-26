from __future__ import annotations
import json
from pathlib import Path

import psycopg
import pytest

from agent_memory import timing_sync


def write(queue:Path,name:str,path:Path):
    file=queue/('event-'+name+'.json')
    file.write_text(json.dumps({'transcript':str(path),'parent_session':None}))
    return file


def test_queue_keeps_requests_when_the_database_is_down(tmp_path:Path,monkeypatch):
    queue=tmp_path/'queue';queue.mkdir()
    file=write(queue,'old',tmp_path/'session.jsonl')
    def fail(*args,**kwargs):raise psycopg.OperationalError('unavailable')
    monkeypatch.setattr(timing_sync,'sync_transcript',fail)
    with pytest.raises(psycopg.OperationalError):timing_sync.flush_queue(queue)
    assert file.exists()


def test_new_request_cannot_be_erased_by_an_older_sync(tmp_path:Path,monkeypatch):
    queue=tmp_path/'queue';queue.mkdir()
    path=tmp_path/'session.jsonl'
    old=write(queue,'old',path)
    newer=None
    def sync(*args,**kwargs):
        nonlocal newer
        newer=write(queue,'new',path)
        return {'new':0,'updated':1}
    monkeypatch.setattr(timing_sync,'sync_transcript',sync)
    result=timing_sync.flush_queue(queue)
    assert result['synced']==1 and result['pending']==1
    assert not old.exists() and newer.exists()


def test_invalid_request_is_quarantined_without_blocking_good_requests(tmp_path:Path,monkeypatch):
    queue=tmp_path/'queue';queue.mkdir()
    (queue/'event-bad.json').write_text('[]')
    write(queue,'good',tmp_path/'session.jsonl')
    seen=[]
    monkeypatch.setattr(timing_sync,'sync_transcript',lambda path,*args,**kw:seen.append(path))
    result=timing_sync.flush_queue(queue)
    assert result['invalid']==1 and result['synced']==1 and len(seen)==1
    assert (queue/'event-bad.invalid').exists()


def test_sync_rejects_non_transcript_and_secret_paths(tmp_path:Path,monkeypatch):
    monkeypatch.setattr(timing_sync.sessions_ingest,'claude_home',lambda:tmp_path/'claude')
    monkeypatch.setattr(timing_sync.sessions_ingest,'codex_home',lambda:tmp_path/'codex')
    for path in (tmp_path/'outside.jsonl',tmp_path/'codex/auth.jsonl',tmp_path/'claude/projects/.env.jsonl'):
        with pytest.raises(ValueError):timing_sync.sync_transcript(path)


def test_empty_or_not_yet_created_transcript_is_not_treated_as_synced(tmp_path:Path,monkeypatch):
    home=tmp_path/'codex'; path=home/'sessions/test.jsonl';path.parent.mkdir(parents=True)
    monkeypatch.setattr(timing_sync.sessions_ingest,'claude_home',lambda:tmp_path/'claude')
    monkeypatch.setattr(timing_sync.sessions_ingest,'codex_home',lambda:home)
    with pytest.raises(OSError):timing_sync.sync_transcript(path)
    path.touch()
    with pytest.raises(OSError):timing_sync.sync_transcript(path)
