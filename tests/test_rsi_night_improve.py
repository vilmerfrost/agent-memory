"""`amem rsi night` with the REAL agent-config `improve stage` (Mission 005 DoD): a temp HOME holding a scratch
agent-config (lib/ copied from the agent-config checkout, criteria with apply_mode=stage) and synthetic instruction
targets, a temp RSI_SANDBOX_DIR, a disposable Postgres. The way `improve e2e-promote` sandboxes HOME. The real
~/.claude and ~/agent-config are only read (lib/ and criteria.json are copied); nothing outside tmp_path is written.

The checkout is RSI_TEST_IMPROVE_REPO, else ~/agent-config; the test skips when it has no `improve stage`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_memory import rsi_artifact as RA, rsi_night as N
from rsi_night_support import (CLAUDE_MD, KEY_A, RULE_A, RULE_B, FakeReplayer, adapters, candidate, clean_db, rows,
                               seed_rule, strip_agent_env)

REPO = Path(os.environ.get("RSI_TEST_IMPROVE_REPO") or Path.home() / "agent-config")   # read at import: the real HOME
IMPROVE = REPO / "bin" / "improve"
HAS_STAGE = IMPROVE.is_file() and "def cmd_stage" in IMPROVE.read_text(errors="replace")
pytestmark = pytest.mark.skipif(not HAS_STAGE, reason=f"no agent-config checkout with `improve stage` at {REPO}")


@pytest.fixture
def ienv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    home = (tmp_path / "home").resolve()
    ac, sandbox = home / "agent-config", (tmp_path / "sandbox").resolve()
    for d in (ac / "lib", ac / "improve", ac / "agents", home / ".claude" / "parent", home / ".agents" / "skills" / "demo",
              sandbox):
        d.mkdir(parents=True)
    for f in ("sessions.py", "routine.py", "fence.py", "rsi_artifact.py"):
        shutil.copy(REPO / "lib" / f, ac / "lib" / f)
    crit = json.loads((REPO / "improve" / "criteria.json").read_text())
    crit.update(apply_mode="stage", max_changes_per_week=None, promotion={"required_suites": ["static", "sandbox-replay"]})
    (ac / "improve" / "criteria.json").write_text(json.dumps(crit, indent=1) + "\n")
    shutil.copy(REPO / "improve" / "fence.txt", ac / "improve" / "fence.txt")
    (ac / "improve" / "pins.json").write_text(json.dumps({"files": {}, "rules": {}}) + "\n")
    (home / ".claude" / "CLAUDE.md").write_text(CLAUDE_MD)
    (ac / "agents" / "worker.md").write_text("# Worker\n\n## Rules\n\n- First rule.\n")
    (home / ".agents" / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\n---\n\n# Demo\n\n- Do the demo.\n")
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(ac), *args], check=True, capture_output=True)
    wrapper = tmp_path / "improve"                    # the checkout's improve under this interpreter
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {IMPROVE} \"$@\"\n")
    wrapper.chmod(0o755)
    strip_agent_env(monkeypatch)
    for k, v in (("HOME", str(home)), ("IMPROVE_AC", str(ac)), ("ROUTINE_STATE", str(home / ".claude" / "parent")),
                 ("RSI_SANDBOX_DIR", str(sandbox)), ("AMEM_AGENT_CONFIG_DIR", str(ac)), ("AMEM_IMPROVE_BIN", str(wrapper)),
                 ("PYTHONDONTWRITEBYTECODE", "1")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home, ac=ac, sandbox=sandbox, tmp=tmp_path)


def test_real_improve_stage_produces_the_staged_artifact(ienv: SimpleNamespace) -> None:
    seed_rule(ienv.url, RULE_A, "a")
    seed_rule(ienv.url, RULE_B, "b")
    live = ienv.home / ".claude" / "CLAUDE.md"
    before = live.read_bytes()
    ac_head = subprocess.run(["git", "-C", str(ienv.ac), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    rep = N.run_night(night="2026-10-18", db_url=ienv.url, adapters=adapters(stager=N.ImproveStager(),
                                                                             replayer=FakeReplayer("IMPROVES")))
    assert N.validate_report(rep) == [] and rep["status"] == "ok", rep["notes"]
    e = rep["experiments"][0]
    assert e["status"] == "staged" and e["proposal_id"]
    adir = ienv.sandbox / "artifacts" / e["artifact_hash"]
    m, h, after = RA.load_artifact(adir)                                           # improve's bytes verify
    assert h == e["artifact_hash"] and m["experiment_id"] == e["experiment_id"] and m["baseline_id"] == e["baseline_id"]
    assert [f["path"] for f in m["files"]] == ["~/.claude/CLAUDE.md"]
    assert m["files"][0]["rule_id"].startswith("rsi-night-") and m["activation"] == "none"
    new = next(iter(after.values())).decode()
    assert new == N.stub_change(CLAUDE_MD, N.Hypothesis(**{k: v for k, v in e["hypothesis"].items()},
                                                         target="~/.claude/CLAUDE.md"))
    evals = {r["suite"]: r for r in RA.eval_records(adir)}
    assert evals["static"]["verdict"] == "pass" and evals["static"]["runner"] == "improve stage"
    assert evals["sandbox-replay"]["verdict"] == "improves"
    assert rows(ienv.url, "select artifact_hash, status from rsi_proposals") == [(h, "staged")]
    assert candidate(ienv.url, KEY_A)["status"] == "staged"
    assert live.read_bytes() == before                                             # staging never writes production
    assert subprocess.run(["git", "-C", str(ienv.ac), "rev-parse", "HEAD"], capture_output=True, text=True).stdout == ac_head
    log = [json.loads(x) for x in (ienv.sandbox / "run" / "staging.jsonl").read_text().splitlines()]
    assert [x["artifact_hash"] for x in log] == [h]
    assert not list((ienv.sandbox / "run").glob("rsi-night-*"))                    # the stage input was removed


class CodeBlockImplementer:
    """A change improve's content guard refuses (a pasted code block)."""

    def implement(self, hyp: N.Hypothesis, *, old_text: str, experiment_id: str, baseline_id: str, budget: object) -> str:
        return old_text + "\n```\nrm -rf /tmp/x\n```\n"


def test_real_improve_stage_refusal_fails_the_experiment_only(ienv: SimpleNamespace) -> None:
    seed_rule(ienv.url, RULE_A, "a")
    rep = N.run_night(night="2026-10-18", db_url=ienv.url, adapters=adapters(
        stager=N.ImproveStager(), implementer=CodeBlockImplementer(), replayer=FakeReplayer("IMPROVES")))
    assert N.validate_report(rep) == [] and rep["status"] == "partial" and rep["checkpoint"]["advanced"]  # deferred, no reread
    e = rep["experiments"][0]
    assert e["status"] == "failed" and "improve stage refused" in e["verdict_reasons"][0]
    assert "code block" in e["verdict_reasons"][0]
    assert not (ienv.sandbox / "artifacts").exists() or not any((ienv.sandbox / "artifacts").iterdir())
    assert rows(ienv.url, "select count(*) from rsi_proposals") == [(0,)]
    assert candidate(ienv.url, KEY_A)["status"] == "deferred"
