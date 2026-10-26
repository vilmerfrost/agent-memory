"""Install the approved local timing collector, Codex exporter and session sync hooks.

No secrets are added. Existing hook groups and unrelated config bytes are preserved.
"""
from __future__ import annotations
import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import tomllib
import time
import shlex
from urllib.error import URLError
from urllib.request import urlopen


def merge_hooks(path: Path, command: str) -> bool:
    data=json.loads(path.read_text()) if path.exists() else {}
    hooks=data.setdefault('hooks',{})
    changed=False
    for event in ('SessionStart','Stop','SessionEnd','SubagentStop'):
        groups=hooks.setdefault(event,[])
        present=any(handler.get('command')==command for group in groups for handler in group.get('hooks',[]))
        if not present:
            groups.append({'hooks':[{'type':'command','command':command,'timeout':10}]})
            changed=True
    if changed:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data,indent=2)+'\n')
    return changed


def configure_exporter(path: Path, port: int) -> bool:
    old=path.read_text() if path.exists() else ''
    data=tomllib.loads(old)
    endpoint=f'http://127.0.0.1:{port}/v1/logs'
    existing=data.get('otel',{})
    exporter=existing.get('exporter')
    if isinstance(exporter,dict) and exporter.get('otlp-http',{}).get('endpoint')==endpoint:
        if existing.get('log_user_prompt') is not False:
            raise ValueError('existing timing exporter must disable raw prompt logging')
        return False
    if existing:
        raise ValueError('existing OTel settings require review; no telemetry route was replaced')
    addition=('\n# Agent-memory timing: local collector filters raw telemetry before saving.\n'
              '[otel]\nenvironment = "local"\nlog_user_prompt = false\n'
              f'exporter = {{ otlp-http = {{ endpoint = "{endpoint}", protocol = "json" }} }}\n')
    tomllib.loads(old+addition)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(old+addition)
    return True


def install(repo: Path, port: int) -> dict:
    repo=repo.resolve()
    home=Path.home()
    codex=Path(os.environ.get('CODEX_HOME') or home/'.codex')
    claude=home/'.claude'
    stamp=datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    paths=[codex/'config.toml',codex/'hooks.json',claude/'settings.json']
    command='python3 '+shlex.quote(str(repo/'deploy/hooks/amem-timing-hook.py'))
    parent=claude/'parent'
    parent.mkdir(parents=True,exist_ok=True)
    uv=shutil.which('uv')
    if not uv:
        raise ValueError('uv is not installed')
    plist=home/'Library/LaunchAgents/com.vilmerfrost.amem-timing.plist'
    plist.parent.mkdir(parents=True,exist_ok=True)
    payload={'Label':'com.vilmerfrost.amem-timing','ProgramArguments':[uv,'run','--directory',str(repo),'--locked',
              '--quiet','amem','timing','collect','--port',str(port)],'WorkingDirectory':str(repo),
             'RunAtLoad':True,'KeepAlive':True,
             'StandardOutPath':str(parent/'amem-timing-collector.log'),
             'StandardErrorPath':str(parent/'amem-timing-collector.log')}
    domain=f'gui/{os.getuid()}'
    status=subprocess.run(['launchctl','print',domain+'/com.vilmerfrost.amem-timing'],capture_output=True)
    if status.returncode!=0:
        # Only check availability when no existing managed instance is present.
        with socket.socket() as check:
            check.bind(('127.0.0.1',port))
        plist.write_bytes(plistlib.dumps(payload))
        subprocess.run(['launchctl','bootstrap',domain,str(plist)],check=True)
    deadline=time.monotonic()+10
    while True:
        try:
            with urlopen(f'http://127.0.0.1:{port}/health',timeout=1) as response:
                health=json.load(response)
            break
        except (URLError,ConnectionError,OSError):
            if time.monotonic()>=deadline:
                raise ValueError('collector did not become ready; exporter and hooks were not changed')
            time.sleep(.1)
    if health.get('app')!='agent-memory-timing':
        raise ValueError('port belongs to a different service')
    # Route data only after the receiving process is confirmed as our collector.
    # Backups stay in the original private directory, not in deliverables.
    for path in paths:
        if path.exists():
            backup=path.with_name(path.name+'.before-timing-'+stamp)
            shutil.copyfile(path,backup)
            backup.chmod(0o600)
    changed=configure_exporter(codex/'config.toml',port)
    hook_changes={str(path):merge_hooks(path,command) for path in paths[1:]}
    return {'exporter_changed':changed,'hook_changes':hook_changes,'collector':health,
            'config_reload':'New Codex processes must load the exporter; existing processes may retain their previous configuration.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path.home()/'Projects/agent-memory')
    parser.add_argument('--port',type=int,default=43927)
    args=parser.parse_args()
    print(json.dumps(install(args.repo,args.port),indent=2))
