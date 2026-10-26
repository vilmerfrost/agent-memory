from __future__ import annotations
from contextlib import contextmanager
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from agent_memory import timing_collector as C, timing_store
from test_execution_timing import otlp, span


@contextmanager
def collector(tmp_path: Path):
    state=C.CollectorState(tmp_path/'events.jsonl')
    server=C.server(state,0)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    thread.start()
    try: yield state,f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown();server.server_close();thread.join()


def test_otlp_receiver_discards_raw_data_and_acknowledges_durable_spool(tmp_path:Path):
    with collector(tmp_path) as (state,url):
        raw=json.dumps(otlp()).encode()
        with urlopen(Request(url+'/v1/logs',raw,{'Content-Type':'application/json'}),timeout=5) as response:
            assert response.status==200 and json.load(response)=={}
        saved=state.spool.read_text()
        assert all(text not in saved for text in ('TOP-SECRET','PRIVATE-OUTPUT','PRIVATE-PROMPT'))
        with urlopen(url+'/health',timeout=5) as response:
            health=json.load(response)
        assert health['accepted_api_spans']==1 and health['raw_payloads_stored'] is False
        assert health['app']=='agent-memory-timing'
        assert state.spool.stat().st_mode & 0o777 == 0o600


def test_receiver_rejects_browser_origins_and_invalid_input(tmp_path:Path):
    with collector(tmp_path) as (state,url):
        for body,headers,code in [(b'{}',{'Content-Type':'application/json','Origin':'https://example.org'},403),
                                  (b'not-json',{'Content-Type':'application/json'},400),
                                  (b'{}',{'Content-Type':'text/plain'},415)]:
            with pytest.raises(HTTPError) as error:
                urlopen(Request(url+'/v1/logs',body,headers),timeout=5)
            assert error.value.code==code
        assert not state.spool.exists()


def test_cursor_only_advances_after_complete_records(db,tmp_path:Path):
    # Reuse the private database fixture from test_execution_timing explicitly below.
    file=tmp_path/'timing-events.jsonl'; cursor=tmp_path/'cursors.json'
    first=span('codex:cursor','cursor-first').record()
    second=span('codex:cursor','cursor-second').record()
    encoded=json.dumps(second)
    file.write_text(json.dumps(first)+'\n'+encoded[:20])
    assert timing_store.ingest_spools([file],db_url=db,cursor_path=cursor).new==1
    assert timing_store.ingest_spools([file],db_url=db,cursor_path=cursor).records==0
    with file.open('a') as out:out.write(encoded[20:]+'\n')
    report=timing_store.ingest_spools([file],db_url=db,cursor_path=cursor)
    assert report.records==1 and report.new==1 and report.invalid==0
    file.rename(tmp_path/'old.jsonl');file.write_text(json.dumps(first)+'\n')
    report=timing_store.ingest_spools([file],db_url=db,cursor_path=cursor)
    assert report.records==1 and report.new==0


from test_execution_timing import db  # noqa: E402,F401
