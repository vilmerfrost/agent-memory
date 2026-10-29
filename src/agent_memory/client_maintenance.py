"""Bounded midnight updates, mutually exclusive with daily/night pipelines."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

from agent_memory import config


@contextmanager
def lease(log: Path, *, exclusive=False):
    path = log.parent / 'client-maintenance.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as fh:
        try:
            fcntl.flock(fh, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _command(argv, timeout=180):
    # Updaters need no application secrets or subscription token.
    env={k:os.environ[k] for k in ('HOME','PATH','LANG','TMPDIR') if k in os.environ}
    try:
        process=subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True, env=env)
        try:
            stdout,_=process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL)
                process.communicate()
            return 124, 'timeout'
        return process.returncode, stdout.strip()[:120]
    except OSError:
        return 127, 'unavailable'


def _daily_probe():
    from agent_memory import distill
    from agent_memory.rsi_subscription_config import usage_fields
    raw=distill.run_cli(distill.writer_argv(), 'Reply with exactly OK.', timeout=60)
    envelope=json.loads(raw)
    if distill.parse_envelope(raw).strip() != 'OK':
        raise ValueError('daily_writer_probe_failed')
    return usage_fields(envelope.get('usage') or {})


def _say(message):
    print(f"[{datetime.now().astimezone():%H:%M:%S}] client maintenance: {message}", flush=True)


def run(*, log: Path, config_path: Path | None = None, now=None, push=True,
        command=None, night_check=None, daily_check=None):
    from agent_memory import client_readiness, daily, rsi_night
    from agent_memory.rsi_budget import Budget
    now=now or datetime.now().astimezone()
    result={'started_at':now.isoformat(),'status':'skipped','updates':[]}
    # launchd can catch up after sleep: never update on waking after the maintenance window.
    if now.hour != 0 or now.minute >= 20:
        result['reason']='outside_midnight_window'
        return result
    with lease(log,exclusive=True) as acquired:
        if not acquired:
            result['reason']='pipeline_running'
            return result
        command=command or _command
        for name,args in (('codex',['update']),('claude',['install','latest'])):
            client=shutil.which(name)
            if not client:
                result['updates'].append({'client':name,'status':'unavailable'})
                continue
            _say(f'{name}: checking current version')
            _,before=command([client,'--version'],10)
            _say(f'{name}: update started, timeout 180s; installer output may be silent until completion')
            code,_=command([client,*args],180)
            _say(f'{name}: updater finished with exit {code}')
            # The installer may replace a launcher. Resolve it again after the command.
            current=shutil.which(name)
            vcode,after=command([current,'--version'],10) if current else (127,'unavailable')
            result['updates'].append({'client':name,'before':before,'after':after,
                                      'exit_code':code,'status':'updated' if code==0 and vcode==0 else 'update_warning'})
        budget=None
        try:
            cfg=rsi_night.load_config(config_path)
            budget=Budget(cfg.budget)
            _say('checking configured night roles')
            result['roles']=night_check(cfg,budget) if night_check else client_readiness.check(cfg,budget,progress=_say)
            _say('checking daily Sonnet writer')
            result['daily_writer_usage']=(daily_check or _daily_probe)()
            _say('all configured model checks passed')
            result['status']='ready'
        except Exception as exc:
            result['status']='failed'
            result['reason']=f'{type(exc).__name__}: {str(exc)[:200]}'
        result['costs']=budget.costs() if budget else {}
        result['finished_at']=datetime.now().astimezone().isoformat()
        report=log.parent/'reports'/('client-maintenance-'+now.strftime('%Y-%m-%d')+'.json')
        report.parent.mkdir(parents=True,exist_ok=True)
        temporary=report.with_suffix('.tmp')
        temporary.write_text(json.dumps(result,indent=2,default=str)+'\n')
        temporary.replace(report)
        result['report']=str(report)
        warnings=any(x['status']!='updated' for x in result['updates'])
        if push and (result['status']=='failed' or warnings):
            daily.send('CLI maintenance needs attention',
                       ('Client readiness failed: '+result.get('reason','unknown')+'. See the maintenance report.') if result['status']=='failed' else
                       'An update failed; configured model checks passed. See the maintenance report.')
        return result
