#!/usr/bin/env python3
"""Detach a timing sync using whitelisted hook metadata, never raw prompt/tool data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    try:
        raw = sys.stdin.buffer.read(4_000_001)
        data = json.loads(raw) if len(raw) <= 4_000_000 else {}
        if not isinstance(data, dict):
            return
        event = data.get('hook_event_name')
        if event in {'Stop', 'SubagentStop'}:
            print('{}', flush=True)
        if event not in {'SessionStart', 'Stop', 'SessionEnd', 'SubagentStop'} or os.environ.get('AMEM_TIMING_OFF') == '1':
            return
        transcript = data.get('agent_transcript_path') or data.get('transcript_path')
        if not isinstance(transcript, str) or not transcript.endswith('.jsonl'):
            return
        repo = Path(os.environ.get('AMEM_HOOK_REPO') or Path.home()/'Projects/agent-memory')
        executable = repo/'.venv/bin/amem'
        if not executable.is_file():
            return
        parent = data.get('session_id')
        parent = parent if data.get('agent_id') and isinstance(parent,str) else None
        logs = Path(os.environ.get('AMEM_HOOK_DIR') or Path.home()/'.claude/parent')
        logs.mkdir(parents=True,exist_ok=True,mode=0o700)
        queue=logs/'timing-sync-queue'
        queue.mkdir(exist_ok=True,mode=0o700)
        request=queue/('event-'+str(uuid.uuid4())+'.json')
        fd=os.open(request,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump({'transcript':transcript,'parent_session':parent},out)
            out.flush();os.fsync(out.fileno())
        args = [str(executable),'timing','flush','--queue',str(queue)]
        logfile = logs/'amem-timing-hook.log'
        if logfile.exists() and logfile.stat().st_size > 1_000_000:
            logfile.replace(logs/'amem-timing-hook.log.1')
        # The trusted main checkout loads its own DB configuration. A worker's
        # exported store/credential variables must not redirect this writer.
        env = {k:v for k,v in os.environ.items() if not k.upper().startswith(('AMEM_','PG','SUPABASE_','TYPESAFE_'))}
        fd = os.open(logfile,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'ab') as log:
            subprocess.Popen(args,cwd=repo,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                             start_new_session=True,close_fds=True)
    except (ValueError,TypeError,OSError):
        pass


if __name__=='__main__':
    main()
