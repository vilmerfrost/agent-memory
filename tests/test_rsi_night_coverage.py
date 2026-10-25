"""Night triage: a candidate whose file no replay case uses cannot take the experiment slot (it could only end
INCONCLUSIVE). It is listed in report.json with a plain reason and costs no triage call; covered candidates rank
ahead. Disposable Postgres, temp HOME and RSI_SANDBOX_DIR, fakes for evidence, staging and replay (as in
tests/test_rsi_night.py)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agent_memory import rsi_night as N
from rsi_night_support import (RULE_A, FakeReplayer, adapters, candidate, clean_db, make_home, seed_rule,
                               strip_agent_env, write_config)

NIGHT = "2026-10-26"
MELTDOWN = "~/.agents/skills/meltdown/SKILL.md"
HANDOFF = "~/.agents/skills/handoff/SKILL.md"
AGENTS_MD = "~/.codex/AGENTS.md"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    for rel in (MELTDOWN, HANDOFF, AGENTS_MD):
        p = home / rel[2:]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nname: {p.parent.name}\n---\n\n# Rules\n\n- One rule.\n")
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home, tmp=tmp_path)


def insert(url: str, key: str, target: str, sessions: int) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into rsi_candidates (dedupe_key, source, kind, target, metrics) values "
                     "(%s, 'deviations', 'edit_instruction', %s, %s)", (key, target, json.dumps({"sessions": sessions})))


class CountingTriage(N.JevTriage):
    def __init__(self) -> None:
        super().__init__("fixture")
        self.assessed: list[str] = []

    def assess(self, c: Any, *, state: str, budget: Any) -> Any:
        self.assessed.append(c["dedupe_key"])
        return super().assess(c, state=state, budget=budget)


def run(env: SimpleNamespace, **kw: Any) -> dict[str, Any]:
    ad = adapters(**{k: kw.pop(k) for k in ("triage", "replayer") if k in kw})
    rep = N.run_night(night=NIGHT, db_url=env.url, adapters=ad, **kw)
    assert N.validate_report(rep) == [], N.validate_report(rep)
    return rep


def test_uncovered_skill_never_takes_the_experiment_slot(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a", sessions=1)            # a delta, and a covered CLAUDE.md candidate
    insert(env.url, "dev:skill:handoff:closing", HANDOFF, 40)          # most recurrent, but no case uses the file
    insert(env.url, "dev:agents-md:global:computer-use", AGENTS_MD, 30)
    insert(env.url, "dev:skill:meltdown:intro", MELTDOWN, 20)
    triage, replayer = CountingTriage(), FakeReplayer("INCONCLUSIVE")
    rep = run(env, triage=triage, replayer=replayer,
              config_path=write_config(env.tmp, candidates={"max_considered": 2}))
    cands = {c["key"]: c for c in rep["candidates"]}
    # Covered candidates take the two triage slots, even though the uncovered ones recur more.
    assert set(triage.assessed) == {"dev:skill:meltdown:intro", "dev:claude-md:global:" + RULE_A[0]}
    for key, target in (("dev:skill:handoff:closing", HANDOFF), ("dev:agents-md:global:computer-use", AGENTS_MD)):
        c = cands[key]
        assert not c["selected"] and c["reason"] == "deferred: no test case uses this file"
        assert c["skip_reason"] == "no test case uses this file" and c["test_cases"] == [] and c["target"] == target
        row = candidate(env.url, key)
        assert row["status"] == "deferred" and row["decided_reason"] == "deferred: no test case uses this file"
    meltdown = cands["dev:skill:meltdown:intro"]
    assert meltdown["test_cases"] == ["meltdown-brief"] and meltdown["skip_reason"] is None
    (selected,) = [c for c in rep["candidates"] if c["selected"]]
    assert selected["test_cases"]
    assert {e["target"] for e in rep["experiments"]} <= {MELTDOWN, "~/.claude/CLAUDE.md"}
    assert len(replayer.calls) == 1


def test_an_uncovered_candidate_gets_no_triage_call_and_no_experiment(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a", sessions=1)
    insert(env.url, "dev:skill:handoff:closing", HANDOFF, 4)
    triage, replayer = CountingTriage(), FakeReplayer()
    rep = run(env, triage=triage, replayer=replayer)
    keys = {c["key"]: c for c in rep["candidates"]}
    assert keys["dev:skill:handoff:closing"]["skip_reason"] == "no test case uses this file"
    assert not any(c["selected"] for c in rep["candidates"] if c["key"] == "dev:skill:handoff:closing")
    assert "dev:skill:handoff:closing" not in triage.assessed
    assert not [e for e in rep["experiments"] if e["target"] == HANDOFF]


def test_dry_run_reports_the_reason_too(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a", sessions=1)
    insert(env.url, "dev:skill:handoff:closing", HANDOFF, 9)
    rep = run(env, dry_run=True)
    c = {c["key"]: c for c in rep["candidates"]}["dev:skill:handoff:closing"]
    assert c["reason"] == "deferred: no test case uses this file" and not c["selected"]
    # The new candidate fields are additive: a report written before them still validates.
    old = json.loads(json.dumps(rep, default=str))
    for c in old["candidates"]:
        for k in ("target", "test_cases", "skip_reason"):
            c.pop(k)
    assert N.validate_report(old) == []
