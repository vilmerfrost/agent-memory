from __future__ import annotations
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import tomllib

import pytest

from conftest import base_env

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('install_timing',ROOT/'scripts/install_timing.py')
install=importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


def test_configure_preserves_other_settings_and_disables_prompt_export(tmp_path:Path):
    path=tmp_path/'config.toml'
    before='# keep this comment\nmodel = "unchanged"\n[mcp_servers.example]\ncommand = "preserve"\n'
    path.write_text(before)
    assert install.configure_exporter(path,43927)
    assert path.read_text().startswith(before)
    parsed=tomllib.loads(path.read_text())
    assert parsed['model']=='unchanged' and parsed['mcp_servers']['example']['command']=='preserve'
    assert parsed['otel']['log_user_prompt'] is False
    assert parsed['otel']['exporter']['otlp-http']=={'endpoint':'http://127.0.0.1:43927/v1/logs','protocol':'json'}
    assert not install.configure_exporter(path,43927)


def test_configure_never_replaces_an_existing_telemetry_route(tmp_path:Path):
    path=tmp_path/'config.toml'; before='[otel]\nexporter="none"\n'
    path.write_text(before)
    with pytest.raises(ValueError,match='review'):
        install.configure_exporter(path,43927)
    assert path.read_text()==before


def test_hook_merge_is_idempotent_and_preserves_guards(tmp_path:Path):
    path=tmp_path/'hooks.json'
    old={'other':'keep','hooks':{'PreToolUse':[{'hooks':[{'command':'guard'}]}],
                               'Stop':[{'hooks':[{'command':'other-stop'}]}]}}
    path.write_text(json.dumps(old))
    assert install.merge_hooks(path,'timing-hook')
    assert not install.merge_hooks(path,'timing-hook')
    result=json.loads(path.read_text())
    assert result['other']=='keep' and result['hooks']['PreToolUse']==old['hooks']['PreToolUse']
    assert result['hooks']['Stop'][0]==old['hooks']['Stop'][0]


def test_detached_hook_only_forwards_metadata_and_drops_caller_store(tmp_path:Path):
    repo=tmp_path/'repo'; binary=repo/'.venv/bin/amem';binary.parent.mkdir(parents=True)
    marker=tmp_path/'received.json'
    binary.write_text(f'#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n'
                      f'Path({str(marker)!r}).write_text(json.dumps({{"args":sys.argv[1:],"backend":os.environ.get("AMEM_BACKEND")}}))\n')
    binary.chmod(0o755)
    payload={'hook_event_name':'Stop','session_id':'safe-session','transcript_path':str(tmp_path/'session.jsonl'),
             'last_assistant_message':'PRIVATE-PROMPT'}
    result=subprocess.run([sys.executable,str(ROOT/'deploy/hooks/amem-timing-hook.py')],input=json.dumps(payload),
                          env=base_env(AMEM_HOOK_REPO=str(repo),AMEM_HOOK_DIR=str(tmp_path/'logs'),AMEM_BACKEND='caller-store'),
                          capture_output=True,text=True,timeout=5)
    assert result.returncode==0 and json.loads(result.stdout)=={}
    deadline=time.monotonic()+5
    while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
    record=json.loads(marker.read_text())
    assert record['backend'] is None
    assert record['args']==['timing','flush','--queue',str(tmp_path/'logs/timing-sync-queue')]
    queued,=list((tmp_path/'logs/timing-sync-queue').glob('event-*.json'))
    assert json.loads(queued.read_text())=={'transcript':payload['transcript_path'],'parent_session':None}
    assert 'PRIVATE-PROMPT' not in (tmp_path/'logs/amem-timing-hook.log').read_text()
