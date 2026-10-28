"""Controlled local process/HTTP cases. No native agents, production hooks or paid calls."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from agent_memory import rsi_relevance as REL, rsi_replay as R

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
    assert result.returncode == 0 and result.stdout.strip() == 'ok', result.stderr


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


def test_all_worker_behaviors_fit_the_five_pair_budget():
    cases=R.load_cases()
    whole={'files':[{'path':'~/agent-config/agents/worker.md','kind':'instruction'}]}
    selected,reps,pairs=R.plan_pairs(whole,cases,min_pairs=5,max_pairs=5)
    eligible=set(REL.select(whole,cases)['eligible_cases'])
    assert {'worker-final-report',*IDS} <= eligible
    # At most 5 cases (the first in id order), exactly 5 A/B pairs per provider
    assert {c.id for c in selected} == set(sorted(eligible)[:5]) and pairs == 5 and len(selected)*reps >= 5
