"""Suggest-only and delegation routing on the live shapes (lessons: real-shape-evidence).

A temp HOME gets copies of the live instruction files and improve config (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md,
~/agent-config/parent/DELEGATION.md, agents/worker.md, improve/pins.json and criteria.json), read once, never
written. Deviations use the live rule ids (`global-claude-code-context/shell-habits`, the delegation policy's h2s),
a pinned skill gets a skill_scores-shaped candidate (no rule key) and worker.md (not pinned live) is the editable
contrast. Skipped where the live files are missing. Run with -s to print the triage table."""

from __future__ import annotations

import json
import os
import pwd
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agent_memory import rsi_approval as AP, rsi_brief as B, rsi_night as N
from rsi_night_support import FakeReplayer, adapters, clean_db, make_home, record_refresh, rows, strip_agent_env

REAL = Path(pwd.getpwuid(os.getuid()).pw_dir)
LIVE = {".claude/CLAUDE.md": ".claude/CLAUDE.md", ".codex/AGENTS.md": ".codex/AGENTS.md",
        "agent-config/parent/DELEGATION.md": "agent-config/parent/DELEGATION.md",
        "agent-config/agents/worker.md": "agent-config/agents/worker.md",
        "agent-config/improve/pins.json": "agent-config/improve/pins.json",
        "agent-config/improve/criteria.json": "agent-config/improve/criteria.json"}
# (instruction_source, live rule id, title, sessions)
DEVIATIONS = [("claude-md:global", "global-claude-code-context/shell-habits", "Shell Habits", 3),
              ("claude-md:global", "global-claude-code-context/explaining-completed-work", "Explaining Completed Work", 2),
              ("claude-md:global", "desktop-delegation-policy-v1-1/approval-without-handing-work-back",
               "Approval without handing work back", 2),
              # a chosen input outside the brief's list: the AGENTS.md copy of a delegation rule
              ("agents-md:global", "desktop-delegation-policy-v1-1/one-additional-delegation-layer",
               "One additional delegation layer", 2)]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    missing = [k for k in LIVE if not (REAL / k).is_file()]
    if missing:
        pytest.skip(f"live files missing: {missing}")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    for src, dst in LIVE.items():
        (home / dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REAL / src, home / dst)
    anatomy = home / ".agents" / "skills" / "anatomy" / "SKILL.md"          # pinned live (owner pin)
    anatomy.parent.mkdir(parents=True)
    anatomy.write_text("---\nname: anatomy\n---\n\n# Anatomy\n\n## Steps\n\n- Draw the figure.\n")
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home, sandbox=sandbox, tmp=tmp_path)


def seed(env: SimpleNamespace) -> None:
    with psycopg.connect(env.url) as conn:
        for src, rid, title, n in DEVIATIONS:
            for i in range(n):
                sid = f"sess-{rid[-10:].replace('/', '-')}-{i}"
                conn.execute("insert into sessions (session_id, kind, project_dir, started_at, tool) values "
                             "(%s, 'chat', '/work/demo', now() - interval '1 hour', 'claude')", (sid,))
                conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, turn, "
                             "what, why, how, followed_grade, result_grade, rule_id, rule_title) values (%s, %s, %s, %s, "
                             "1, 'skipped the rule', 'rule unclear', 'state it plainly', 0.3, 0.5, %s, %s)",
                             (f"dev:{sid}:{rid}", sid, src, title, rid, title))
        # the skill_scores shape (no rule key), already open from an earlier night, on a pinned skill
        AP.upsert_candidate(conn, dedupe_key="skill:anatomy", source="skill_scores", kind="edit_skill",
                            target="~/.agents/skills/anatomy/SKILL.md", rule_key="skill:anatomy",
                            evidence_refs=["amem:skill/anatomy"], metrics={"score": 0.41, "n_scored": 6, "sessions": 6})
        # the second chosen input: an open candidate on worker.md (worker sessions' rules; not pinned live): editable
        AP.upsert_candidate(conn, dedupe_key="dev:agent:worker:command-form", source="deviations",
                            kind="edit_instruction", target="~/agent-config/agents/worker.md",
                            rule_key="agent:worker:command-form",
                            evidence_refs=["amem:deviation/w1", "amem:deviation/w2"], metrics={"sessions": 2})
    record_refresh(env.url)          # a fresh skill-score refresh, so skill_scores candidates are not held back


def table(rep: dict[str, Any]) -> str:
    lines = [f"{'candidate':78} {'target':42} {'lock':15} {'rank':>7} sel  skip"]
    for c in rep["candidates"]:
        lines.append(f"{c['key'][:78]:78} {str(c['target'])[:42]:42} {str(c['suggest_only']):15} "
                     f"{c['rank']:7.3f} {'yes' if c['selected'] else 'no ':3}  {c['skip_reason'] or '-'}")
    return "\n".join(lines)


def test_dry_run_triage_on_live_shapes(env: SimpleNamespace) -> None:
    seed(env)
    rep = N.run_night(night="2026-10-29", db_url=env.url, adapters=adapters(), dry_run=True)
    assert N.validate_report(rep) == [], N.validate_report(rep)
    print("\n" + table(rep))
    print("notes:", *[n for n in rep["notes"] if n.startswith("candidates:")], sep="\n  ")
    by_key = {c["key"]: c for c in rep["candidates"]}
    shell = by_key["dev:claude-md:global:global-claude-code-context/shell-habits"]
    deleg = by_key["dev:claude-md:global:desktop-delegation-policy-v1-1/approval-without-handing-work-back"]
    codex = by_key["dev:agents-md:global:desktop-delegation-policy-v1-1/one-additional-delegation-layer"]
    worker = by_key["dev:agent:worker:command-form"]
    assert shell["target"] == "~/.claude/CLAUDE.md" and shell["suggest_only"] == "hand_edited"   # live auto-pin
    assert deleg["target"] == codex["target"] == "~/agent-config/parent/DELEGATION.md"
    assert deleg["suggest_only"] == codex["suggest_only"] == "outside_allowed"
    assert worker["target"] == "~/agent-config/agents/worker.md" and worker["suggest_only"] is None
    assert by_key["skill:anatomy"]["suggest_only"] == "pinned"
    # nothing here is dropped for its lock: every candidate is ranked, and a reason other than the lock decides
    assert all(c["rank"] is not None for c in rep["candidates"])
    assert not any("not considered" in n and "improve refuses" in n for n in rep["notes"])


def test_a_live_shape_win_is_a_suggestion_in_the_brief(env: SimpleNamespace) -> None:
    seed(env)
    rep = N.run_night(night="2026-10-29", db_url=env.url, adapters=adapters(replayer=FakeReplayer("IMPROVES")))
    assert N.validate_report(rep) == [], N.validate_report(rep)
    done = [e for e in rep["experiments"] if e["experiment_id"]]
    print("\nexperiments:", [(e["candidate_key"], e["target"], e["status"], e["suggest_only"]) for e in done])
    sugg = [e for e in done if e["status"] == "suggested"]
    assert sugg and all(e["proposal_id"] is None for e in sugg)
    assert all(e["status"] in ("suggested", "staged") for e in done)
    assert {e["status"] for e in done if e["suggest_only"]} == {"suggested"}
    staged = rows(env.url, "select count(*) from rsi_proposals")[0][0]
    assert staged == sum(e["status"] == "staged" for e in done)
    print("suggestion record keys:", sorted(sugg[0]["suggestion"]))
    print("rsi_experiments.meta.suggestion:", json.dumps({k: v for k, v in rows(
        env.url, "select meta->'suggestion' from rsi_experiments where id = %s", sugg[0]["experiment_id"])[0][0].items()
        if k != "diff"}, sort_keys=True)[:900])
    md = B.render_markdown(B.make_brief(night="2026-10-29", db_url=env.url))
    part = md.split(f"## {B.SUGGESTED_HEADING}")[1].split("## Failed or inconclusive")[0]
    print(part[:1500])
    assert "Suggested edit for a locked file: apply by hand." in part and "amem rsi decide" not in part
