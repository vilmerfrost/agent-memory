"""Retryable transcript timing sync. Queue files contain only path/identity metadata."""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agent_memory import codex_sessions, sessions_ingest, config


def sync_transcript(path: Path, parent_session: str | None = None, *, db_url: str | None = None) -> dict:
    if path.name.startswith('.env'):
        raise ValueError('secret-named files are not transcripts')
    path=path.expanduser().resolve()
    roots=(sessions_ingest.claude_home()/'projects',*(r.projects for r in sessions_ingest.transcript_roots()[1:]),
           sessions_ingest.codex_home()/'sessions',sessions_ingest.codex_home()/'archived_sessions')
    if path.name.startswith('.env') or path.suffix!='.jsonl' or not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError('timing sync accepts only session JSONL inside configured agent homes')
    events=sessions_ingest.load_events(path)
    if not path.is_file() or not events:
        raise OSError('transcript is not ready; retain the queued request')
    row=(sessions_ingest.codex_row(path,'timing-hook') if codex_sessions.is_codex(events)
         else sessions_ingest.session_row(path,path.parent.name,parent_session=parent_session))
    if row.copy_only:
        return {'skipped':'copied history'}
    target=db_url or config.db_url()
    options=conninfo_to_dict(target).get('options','')+' -c statement_timeout=15000 -c lock_timeout=5000'
    target=make_conninfo(target,connect_timeout=5,options=options.strip())
    new,updated=sessions_ingest.upsert([row],db_url=target)
    return {'new':new,'updated':updated,'timing_spans':len(row.timing_spans)}


def flush_queue(queue: Path | None = None, *, db_url: str | None = None) -> dict:
    queue=queue or sessions_ingest.claude_home()/'parent/timing-sync-queue'
    result={'synced':0,'invalid':0,'pending':0,'busy':False}
    if not queue.exists():
        return result
    if queue.is_symlink():
        raise ValueError('timing queue cannot be a symlink')
    fd=os.open(queue/'.flush.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            result['busy']=True
            return result
        groups={}
        for file in sorted(queue.glob('event-*.json'))[:1000]:
            try:
                if file.is_symlink() or file.stat().st_size>8192:
                    raise ValueError('invalid queue record')
                item=json.loads(file.read_text())
                if not isinstance(item,dict):
                    raise ValueError('invalid queue record')
                path=item.get('transcript')
                parent=item.get('parent_session')
                if not isinstance(path,str) or (parent is not None and not isinstance(parent,str)):
                    raise ValueError('invalid queue record')
                groups.setdefault((path,parent),[]).append(file)
            except FileNotFoundError:
                continue
            except (ValueError,OSError):
                result['invalid']+=1
                try: file.rename(file.with_suffix('.invalid'))
                except OSError: pass
                continue  # retain quarantined input for diagnosis, without executing it
        for (path,parent),files in groups.items():
            try:
                sync_transcript(Path(path),parent,db_url=db_url)
            except ValueError:
                result['invalid']+=len(files)
                for file in files:
                    try: file.rename(file.with_suffix('.invalid'))
                    except OSError: pass
                continue
            except OSError:
                continue  # session may not exist yet at SessionStart; retry later
            # A newer hook writes another unique file, so it cannot be erased by
            # this completion. A crash before removal only replays idempotent data.
            for file in files:
                file.unlink(missing_ok=True)
            result['synced']+=1
        result['pending']=sum(1 for _ in queue.glob('event-*.json'))
        return result
    finally:
        os.close(fd)
