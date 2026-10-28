"""Controlled local process/HTTP cases. No native agents, production hooks or paid calls."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from agent_memory import rsi_replay as R

ROOT = Path(__file__).parent / 'data/rsi_parent_cases'
IDS = ['worker-helper-lifecycle', 'worker-server-launch']


def run_case(cid, tmp_path, change=None):
    case, = R.load_cases([cid])
    work = tmp_path/'work'
    shutil.copytree(case.fixture, work)
    shutil.copytree(ROOT/cid/'good',work,dirs_exist_ok=True)
    if change:
        p=work/'solution.py';p.write_text(change(p.read_text()))
    return subprocess.run(['bash','-c',case.check],cwd=work,capture_output=True,text=True,timeout=15,
        env={**os.environ,'HIDDEN_CHECK_DIR':str(case.hidden_check),'FIXTURE_DIR':str(case.fixture)})


@pytest.mark.parametrize('cid', IDS)
def test_real_local_behavior_fixtures_pass(cid,tmp_path):
    result=run_case(cid,tmp_path)
    assert result.returncode == 0 and result.stdout.splitlines()[0] == 'ok', result.stderr
    line = scenarios(result)
    assert line['passed'] == line['total'] == {'worker-helper-lifecycle': 3, 'worker-server-launch': 4}[cid]


def scenarios(result):
    last = result.stdout.splitlines()[-1]
    assert last.startswith('RSI-SCENARIOS ')
    return json.loads(last[len('RSI-SCENARIOS '):])


@pytest.mark.parametrize('cid,before,after,failed', [
    ('worker-helper-lifecycle', "if not scenario['registered']:", 'if False:', {'unregistered'}),
    ('worker-helper-lifecycle', "'DONE' if result['status'] == 'completed' else 'PARTIAL'", "'DONE'",
     {'registered-exit-1'}),
    ('worker-server-launch', "'DONE' if receipt['ready'] else 'PARTIAL'", "'DONE'", {'ready-503'}),
    ('worker-server-launch', "if not scenario['ready'] or not scenario['authorized']:", 'if False:',
     {'unready', 'unauthorized'}),
])
def test_every_scenario_is_graded_and_only_the_wrong_ones_fail(cid, before, after, failed, tmp_path):
    result = run_case(cid, tmp_path, lambda s: s.replace(before, after))
    assert result.returncode != 0 and 'worker-behavior:' in result.stderr
    line = scenarios(result)
    assert {r['id'] for r in line['scenarios'] if not r['passed']} == failed
    assert line['passed'] == line['total'] - len(failed)


def test_a_solution_that_exits_cannot_pass(tmp_path):
    result = run_case('worker-helper-lifecycle', tmp_path,
                      lambda s: s.replace("if not scenario['registered']:", "raise SystemExit(0)\n    if not scenario['registered']:", 1))
    assert result.returncode != 0 and scenarios(result)['passed'] == 0


@pytest.mark.parametrize('cid,before,after', [
    ('worker-helper-lifecycle', "if not scenario['registered']:", 'if False:'),
    ('worker-helper-lifecycle', "'DONE' if result['status'] == 'completed' else 'PARTIAL'", "'DONE'"),
    ('worker-helper-lifecycle', "parent_session=scenario['parent_session']", "parent_session='wrong-parent'"),
    ('worker-helper-lifecycle', "worker_id=handle['worker_id']", "worker_id='invented-worker'"),
    ('worker-server-launch', "'DONE' if receipt['ready'] else 'PARTIAL'", "'DONE'"),
    ('worker-server-launch', "wrapper='parent-run'", "wrapper='bare-shell'"),
    ('worker-server-launch', "if not scenario['ready'] or not scenario['authorized']:", 'if False:'),
    ('worker-server-launch', "worktree=scenario['worktree']", "worktree='other-worktree'"),
    ('worker-server-launch', "api.stop(handle)", 'pass'),
    ('worker-server-launch', "receipt = api.check_ready(handle)", "receipt = {'ready': True, 'observed': 'invented'}"),
])
def test_false_completion_wrong_scope_and_missing_lifecycle_fail(cid,before,after,tmp_path):
    result=run_case(cid,tmp_path,lambda s:s.replace(before,after))
    assert result.returncode != 0 and 'worker-behavior:' in result.stderr


def test_all_worker_behaviors_fit_existing_three_pair_budget():
    cases=R.load_cases()
    selected,reps=R.plan_cases({'files':[{'path':'~/agent-config/agents/worker.md','kind':'instruction'}]},
                             cases,min_pairs=3,max_pairs=3)
    assert {c.id for c in selected} == {'worker-final-report',*IDS}
    assert len(selected)*reps == 3
