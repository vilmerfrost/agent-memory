from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from agent_memory import client_maintenance as M, rsi_night as N, daily, rsi_schedule as S
from agent_memory.rsi_budget import BudgetConfig

NOW=datetime(2026,10,29,0,0,tzinfo=timezone.utc)


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(M.shutil,'which',lambda name:'/fake/'+name)
    monkeypatch.setattr(N,'load_config',lambda p:SimpleNamespace(budget=BudgetConfig.from_dict({'max_usd':1})))
    return dict(now=NOW,push=False,night_check=lambda *a:[{'status':'ready'}],daily_check=lambda:{'tokens_in':1,'tokens_out':1})


def test_update_failure_does_not_block_compatible_installed_client(tmp_path,ready):
    calls=[]
    def command(argv,timeout):
        calls.append(argv)
        return (0,'v-new') if argv[-1]=='--version' else (1,'download failed')
    result=M.run(log=tmp_path/'routines.log',command=command,**ready)
    assert result['status']=='ready'
    assert all(x['status']=='update_warning' for x in result['updates'])
    assert [a[1:] for a in calls if a[-1]!='--version']==[['update'],['install','latest']]


def test_active_pipeline_prevents_update(tmp_path,ready):
    log=tmp_path/'routines.log'
    with M.lease(log):
        result=M.run(log=log,command=lambda *a:pytest.fail('updated during pipeline'),**ready)
    assert result['reason']=='pipeline_running'


def test_late_wake_never_updates(tmp_path,ready):
    ready['now']=NOW.replace(hour=1)
    result=M.run(log=tmp_path/'routines.log',command=lambda *a:pytest.fail('late update'),**ready)
    assert result['reason']=='outside_midnight_window'


def test_readiness_failure_is_reported_and_persisted(tmp_path,ready):
    def fail(*a):raise RuntimeError('protocol unavailable')
    ready['night_check']=fail
    r=M.run(log=tmp_path/'routines.log',command=lambda *a:(0,'v'),**ready)
    assert r['status']=='failed' and 'protocol unavailable' in r['reason']
    assert Path(r['report']).is_file()


def test_exclusive_update_blocks_new_daily_and_night(tmp_path,monkeypatch):
    log=tmp_path/'routines.log'
    monkeypatch.setattr(daily,'_run_daily_locked',lambda **k:pytest.fail('daily started'))
    monkeypatch.setattr(S,'_run_night_job_locked',lambda **k:pytest.fail('night started'))
    with M.lease(log,exclusive=True):
        assert daily.run_daily(routines_log=log).verdict=='FAIL'
        assert S.run_night_job(routines_log=log,write_log=False).status=='SKIP'


def test_nested_shared_leases_do_not_deadlock(tmp_path):
    with M.lease(tmp_path/'log') as first:
        with M.lease(tmp_path/'log') as second:
            assert first and second


def test_lock_contention_is_real_across_processes(tmp_path):
    import subprocess,sys
    log=tmp_path/'routines.log'
    code="from pathlib import Path; from agent_memory.client_maintenance import lease; import sys\nwith lease(Path(sys.argv[1]),exclusive=True) as got: print(got)"
    with M.lease(log):
        p=subprocess.run([sys.executable,'-c',code,str(log)],capture_output=True,text=True,timeout=10)
    assert p.returncode==0 and p.stdout.strip()=='False'


def test_updater_timeout_reaps_its_process_group(tmp_path):
    import sys,time
    marker=tmp_path/'orphan-wrote'
    child="import time; from pathlib import Path; time.sleep(2); Path("+repr(str(marker))+").write_text('bad')"
    parent="import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"+repr(child)+"]); time.sleep(10)"
    code,_=M._command([sys.executable,'-c',parent],timeout=.2)
    assert code==124
    time.sleep(2.1)
    assert not marker.exists()
