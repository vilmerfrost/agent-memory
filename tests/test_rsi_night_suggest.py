"""Suggest-only targets and delegation routing (docs/rsi/hand-edits.md, docs/rsi/night-ops.md).

A file improve refuses (a pins.json file pin, or outside criteria.json allowed_targets) stays a night target: its
ideas are ranked with the same rank(), weights, slots and limits as every other idea, and still need a replay case.
A winning experiment on it becomes a suggestion (report status `suggested`, rsi_experiments.meta.suggestion), never a
proposal, and the brief shows it without an Apply command. Delegation-policy rules of the generated copies
(~/.claude/CLAUDE.md, ~/.codex/AGENTS.md) target their source ~/agent-config/parent/DELEGATION.md.
Same disposable Postgres, temp HOME and fakes as test_rsi_night.py."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agent_memory import cli, edits_targets as ET, rsi_approval as AP, rsi_brief as B, rsi_night as N, rsi_targets as T
from rsi_night_support import (KEY_A, KEY_B, RULE_A, RULE_B, FakeReplayer, FakeStager, adapters, candidate, clean_db,
                               make_home, read_report, rows, seed_rule, strip_agent_env)

N1 = "2026-10-18"
CLAUDE_PIN = {"~/.claude/CLAUDE.md": "auto-pin: edited by hand after imp-20261015-3"}
DELEGATION_MD = ("# Desktop delegation policy v1.1\n\nApply this policy in delegated work.\n\n"
                 "## Parent and capacity\n\n- The root parent owns the task.\n\n"
                 "## Approval without handing work back\n\n- Carry approval forward; do not ask again.\n")
DELEG_RULE = "claude-md:global:desktop-delegation-policy-v1-1/approval-without-handing-work-back"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home, sandbox=sandbox, tmp=tmp_path)


def run(env: SimpleNamespace, night: str = N1, **kw: Any) -> dict[str, Any]:
    rep = N.run_night(night=night, db_url=env.url, adapters=kw.pop("ad", None) or adapters(**kw))
    assert N.validate_report(rep) == [], N.validate_report(rep)
    assert B.validate_report(json.loads(json.dumps(rep, default=str))) == []
    return rep


def pin(env: SimpleNamespace, files: dict[str, str]) -> None:
    (env.home / "agent-config" / "improve" / "pins.json").write_text(json.dumps(
        {"files": {k: {"reason": v} for k, v in files.items()}, "rules": {}}))


def seed_ab(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    seed_rule(env.url, RULE_B, "b")


def ranks(rep: dict[str, Any]) -> dict[str, float]:
    return {c["key"]: c["rank"] for c in rep["candidates"]}


# --------------------------------------------------------------------------- ranking: no bias either way


def test_rank_is_the_same_formula_for_any_target() -> None:
    a = N.TriageAnswers("experiment", 1.0, 1.0, 0.0, 1.0, "fixture")
    w = {"impact": 1.0, "recurrence": 1.0, "testability": 1.0, "risk": 1.0, "history": 1.0}
    assert N.rank(a, 3, [], w) == N.rank(a, 3, [], w)   # rank() has no target input at all


def test_a_pinned_target_ranks_exactly_like_the_unpinned_one_and_can_be_selected(env: SimpleNamespace) -> None:
    seed_ab(env)
    unpinned = run(env, replayer=FakeReplayer("NO_EFFECT"))
    clean_db(env.url)
    seed_ab(env)
    pin(env, CLAUDE_PIN)
    pinned = run(env, night="2026-10-19", replayer=FakeReplayer("NO_EFFECT"))
    assert ranks(pinned) == ranks(unpinned) and set(ranks(pinned)) == {KEY_A, KEY_B}
    assert [c["key"] for c in pinned["candidates"] if c["selected"]] == \
        [c["key"] for c in unpinned["candidates"] if c["selected"]] == [KEY_A]
    by_key = {c["key"]: c for c in pinned["candidates"]}
    assert by_key[KEY_A]["suggest_only"] == "hand_edited" and by_key[KEY_A]["skip_reason"] is None
    assert all(c["suggest_only"] is None for c in unpinned["candidates"])
    assert [(e["candidate_key"], e["status"], e["suggest_only"]) for e in pinned["experiments"] if e["experiment_id"]] == \
        [(KEY_A, "not_promotable", "hand_edited")]       # NO_EFFECT: tested, nothing suggested
    assert not any("not considered" in n and "improve refuses" in n for n in pinned["notes"])
    assert any("kept as suggest-only" in n and "CLAUDE.md" in n for n in pinned["notes"])


def test_a_locked_target_without_a_replay_case_is_still_skipped_no_test_case(env: SimpleNamespace) -> None:
    # ~/.agents/skills/demo/SKILL.md: allowlisted, auto-pinned after a hand edit, and no replay case declares it
    pin(env, {"~/.agents/skills/demo/SKILL.md": "auto-pin: edited by hand after imp-20261020-2"})
    with psycopg.connect(env.url) as conn:
        AP.upsert_candidate(conn, dedupe_key="dev:skill:demo:demo", source="deviations", kind="edit_instruction",
                            target="~/.agents/skills/demo/SKILL.md", rule_key="skill:demo:demo",
                            evidence_refs=["amem:deviation/d1", "amem:deviation/d2"], metrics={"sessions": 6})
    rep = run(env)
    c = next(c for c in rep["candidates"] if c["key"] == "dev:skill:demo:demo")
    assert c["selected"] is False and c["skip_reason"] == N.NO_TEST_CASE and c["test_cases"] == []
    assert c["suggest_only"] == "hand_edited" and "deferred: no test case" in c["reason"]
    assert rep["experiments"] == []


def test_owner_pins_and_judge_prompts_stay_excluded_before_triage(env: SimpleNamespace) -> None:
    """Only a hand-edit auto-pin and a file outside allowed_targets are suggest-only. A file pinned on purpose (an
    owner pin, a generated copy) and a judge / eval prompt (the measuring stick) leave the pool as before."""
    judge = env.home / ".claude" / "scheduled-tasks" / "evals-parent" / "SKILL.md"
    judge.parent.mkdir(parents=True)
    judge.write_text("# Evals parent\n\n## Grade\n\n- Grade the run.\n")
    pin(env, {"~/.agents/skills/demo/SKILL.md": "Vilmer 2026-10-27: owner pin, the night and improve never touch this skill",
              "~/.claude/scheduled-tasks/evals-parent/SKILL.md":
                  "judge prompt: the measuring stick is never changed by the loop (Vilmer, 2026-10-13)",
              **CLAUDE_PIN})
    seed_ab(env)                                       # KEY_A / KEY_B on the hand-edited CLAUDE.md: still considered
    with psycopg.connect(env.url) as conn:
        for key, target in (("dev:skill:demo:demo", "~/.agents/skills/demo/SKILL.md"),
                            ("routine:evals-parent", "~/.claude/scheduled-tasks/evals-parent/SKILL.md")):
            AP.upsert_candidate(conn, dedupe_key=key, source="deviations", kind="edit_instruction", target=target,
                                rule_key=key.removeprefix("dev:"), evidence_refs=[f"amem:deviation/{key}-1",
                                f"amem:deviation/{key}-2", f"amem:deviation/{key}-3"], metrics={"sessions": 9})
    rep = run(env, replayer=FakeReplayer("NO_EFFECT"))
    assert {c["key"] for c in rep["candidates"]} == {KEY_A, KEY_B}
    assert {c["suggest_only"] for c in rep["candidates"]} == {"hand_edited"}
    b = rep["blocked"]
    assert b["groups"]["pinned"] == 1 and b["groups"]["grades_loop"] == 1 and b["groups"]["hand_edited"] == 0
    assert {(t["target"], t["reason"]) for t in b["refused_files"]} == {
        ("~/.agents/skills/demo/SKILL.md", "pinned"), ("~/.claude/scheduled-tasks/evals-parent/SKILL.md", "grades_loop"),
        ("~/.claude/CLAUDE.md", "hand_edited")}
    excluded = next(n for n in rep["notes"] if "on a target improve refuses" in n)
    assert excluded.startswith("candidates: 2 ") and excluded.endswith("), not considered")
    assert "demo/SKILL.md" in excluded and "evals-parent" in excluded and "CLAUDE.md" not in excluded
    assert not any("without an in-scope target" in n for n in rep["notes"])
    assert all(e["target"] == "~/.claude/CLAUDE.md" for e in rep["experiments"])


# --------------------------------------------------------------------------- a win becomes a suggestion


def test_a_winning_experiment_on_a_locked_file_is_a_suggestion_not_a_proposal(env: SimpleNamespace,
                                                                               monkeypatch: pytest.MonkeyPatch,
                                                                               capsys: pytest.CaptureFixture[str]) -> None:
    seed_ab(env)
    pin(env, CLAUDE_PIN)
    improve = FakeStager()                     # the `improve stage` stand-in: must never see a locked file
    rep = run(env, stager=improve, replayer=FakeReplayer("IMPROVES"))
    assert improve.calls == []
    [e] = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["status"] == "suggested" and e["verdict"] == "IMPROVES" and e["proposal_id"] is None
    s = e["suggestion"]
    assert s["format"] == N.SUGGESTION_FORMAT and s["suggest_only"] is True and s["applied_by"] == "hand"
    assert s["target"] == "~/.claude/CLAUDE.md" and s["lock"] == "hand_edited" and s["artifact_hash"] == e["artifact_hash"]
    assert s["diff_summary"]["added"] >= 1 and s["diff"].startswith("--- a/.claude/CLAUDE.md")
    assert s["evidence_refs"] and s["verdict_reasons"] == ["fake replay: IMPROVES"]
    # nothing appliable: no proposal, no decision token, no pending decision
    assert rows(env.url, "select count(*) from rsi_proposals") == [(0,)]
    assert rows(env.url, "select count(*) from rsi_decision_tokens") == [(0,)]
    assert rep["pending_decisions"] == []
    # the database keeps the suggestion on the experiment row and the tested bytes as a suggest_only artifact
    [(status, verdict, meta)] = rows(env.url, "select status, verdict, meta from rsi_experiments where id = %s",
                                     e["experiment_id"])
    assert (status, verdict, meta["report_status"]) == ("evaluated", "IMPROVES", "suggested")
    assert meta["suggestion"] == json.loads(json.dumps(s)) and "proposal_id" not in meta
    [(kind,)] = rows(env.url, "select manifest->>'proposal_kind' from rsi_artifacts where hash = %s", e["artifact_hash"])
    assert kind == N.SUGGEST_KIND
    cand = candidate(env.url, KEY_A)
    assert cand["status"] == "tested" and cand["decided_reason"].startswith("suggested edit for a locked file")
    # the live file is untouched
    assert (env.home / ".claude" / "CLAUDE.md").read_text().startswith("# Global")
    # the brief: its own section, no Apply / decide command anywhere for it
    brief = B.make_brief(night=N1, db_url=env.url)
    assert brief["passed"] == [] and [x["experiment_id"] for x in brief["suggested"]] == [e["experiment_id"]]
    assert "actions" not in brief["suggested"][0] and brief["suggested"][0]["applied_by"] == "hand"
    assert all(f["experiment_id"] != e["experiment_id"] for f in brief["failed"])
    md = B.render_markdown(brief)
    assert f"## {B.SUGGESTED_HEADING}" in md
    assert "Suggested edit for a locked file: apply by hand." in md
    assert "~/.claude/CLAUDE.md is pinned after a hand edit" in md and "Diff summary: ~/.claude/CLAUDE.md: +" in md
    assert "amem rsi decide" not in md and "**Apply:**" not in md and "**Reject:**" not in md
    assert "1 suggested edit for a locked file to apply by hand" in md
    # `amem rsi brief` renders the same text
    monkeypatch.setenv("AMEM_DB_URL", env.url)
    assert cli.main(["rsi", "brief", "--night", N1]) == 0
    out = capsys.readouterr().out
    assert "Suggested edit for a locked file: apply by hand." in out and "amem rsi decide" not in out


def test_an_unpinned_win_is_still_a_staged_proposal(env: SimpleNamespace) -> None:
    seed_ab(env)
    rep = run(env, replayer=FakeReplayer("IMPROVES"))
    [e] = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["status"] == "staged" and e["proposal_id"] and e["suggest_only"] is None and "suggestion" not in e


def test_out_of_allowed_targets_is_suggest_only_too(env: SimpleNamespace) -> None:
    # a chosen input: criteria.json drops CLAUDE.md (no pin at all): the lock is `outside_allowed`
    seed_ab(env)
    crit = env.home / "agent-config" / "improve" / "criteria.json"
    crit.write_text(json.dumps({"allowed_targets": ["@/agents/worker.md", "~/.agents/skills/"]}))
    rep = run(env, replayer=FakeReplayer("IMPROVES"))
    [e] = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["status"] == "suggested" and e["suggestion"]["lock"] == "outside_allowed"
    assert rows(env.url, "select count(*) from rsi_proposals") == [(0,)]


def test_suggest_stager_refuses_a_stale_baseline_and_writes_only_the_sandbox(env: SimpleNamespace) -> None:
    key = "~/.claude/CLAUDE.md"
    bid, bdir = T.create_baseline([T.untilde(key)])
    st = N.SuggestStager().stage(experiment_id="exp-20261018-x1", baseline_dir=bdir, key=key,
                                 new_text=(bdir / "files" / ".claude/CLAUDE.md").read_text() + "- One more rule.\n",
                                 rule_id="rsi-night-x", timeout=5)
    assert st.status == "staged" and Path(st.artifact_dir).parent == env.sandbox / "artifacts"
    from agent_memory import rsi_artifact as RA
    m, h, _ = RA.load_artifact(Path(st.artifact_dir))
    assert h == st.artifact_hash and m["proposal_kind"] == "suggest_only" and m["baseline_id"] == bid
    T.untilde(key).write_text("# Global\n\n- A hand edit after the baseline.\n")
    st2 = N.SuggestStager().stage(experiment_id="exp-20261018-x2", baseline_dir=bdir, key=key, new_text="x\n",
                                  rule_id="rsi-night-x", timeout=5)
    assert st2.status == "refused" and "stale baseline" in (st2.reason or "")


def test_report_validation_refuses_a_suggestion_with_a_proposal() -> None:
    e = {"status": "suggested", "verdict": "IMPROVES", "experiment_id": "exp-20261018-ab", "artifact_hash": "a" * 64,
         "proposal_id": "00000000-0000-0000-0000-000000000001", "target": "~/.claude/CLAUDE.md",
         "suggestion": {"format": N.SUGGESTION_FORMAT, "suggest_only": True, "applied_by": "hand",
                        "target": "~/.claude/CLAUDE.md", "artifact_hash": "a" * 64, "lock": "hand_edited",
                        "diff": "", "diff_summary": {}}}
    assert any("never has a proposal" in p for p in N.suggestion_problems(e, "x"))
    assert N.suggestion_problems({**e, "proposal_id": None}, "x") == []


# --------------------------------------------------------------------------- delegation policy -> DELEGATION.md


def test_delegation_rule_ids() -> None:
    assert ET.delegation_rule_id(DELEG_RULE) == "approval-without-handing-work-back"
    assert ET.delegation_rule_id("dev:" + DELEG_RULE) == "approval-without-handing-work-back"
    assert ET.delegation_rule_id("agents-md:global:desktop-delegation-policy-v1-1/parent-and-capacity") == \
        "parent-and-capacity"
    assert ET.delegation_rule_id("claude-md:global:desktop-delegation-policy-v1-1") == "intro"
    # chosen inputs outside the list: other rules, a repo copy, an empty tail
    assert ET.delegation_rule_id("claude-md:global:global-claude-code-context/shell-habits") is None
    assert ET.delegation_rule_id("claude-md:demo:desktop-delegation-policy-v1-1/parent-and-capacity") is None
    assert ET.delegation_rule_id("claude-md:global:desktop-delegation-policy-v1-1/") is None
    assert ET.delegation_rule_id(None) is None


def test_section_for_a_delegation_rule_in_the_source_and_the_copy() -> None:
    copy = "# Desktop delegation policy v1.1\n\nIntro.\n\n## Approval without handing work back\n\n- Rule.\n" \
           "\n# Global Claude Code Context\n\n## Shell Habits\n\n- Quote globs.\n"
    assert N._section_for(DELEGATION_MD, DELEG_RULE).id == "approval-without-handing-work-back"
    assert N._section_for(DELEGATION_MD, "claude-md:global:desktop-delegation-policy-v1-1").id == "intro"
    assert N._section_for(copy, DELEG_RULE).id == "desktop-delegation-policy-v1-1/approval-without-handing-work-back"
    assert N._section_for(copy, "claude-md:global:global-claude-code-context/shell-habits").id == \
        "global-claude-code-context/shell-habits"


def test_the_real_delegation_md_resolves_the_retargeted_key() -> None:
    """The seam with the replay cases: on the live source file the CLAUDE.md-style key finds the bare h2 slug."""
    real = Path.home() / "agent-config" / "parent" / "DELEGATION.md"
    if not real.is_file():
        pytest.skip("no live ~/agent-config/parent/DELEGATION.md on this machine")
    assert N._section_for(real.read_text(), DELEG_RULE).id == "approval-without-handing-work-back"
    assert N._section_for(real.read_text(), "dev:" + DELEG_RULE).id == "approval-without-handing-work-back"


def test_test_cases_finds_a_section_case_for_a_retargeted_delegation_candidate(env: SimpleNamespace) -> None:
    deleg = "~/agent-config/parent/DELEGATION.md"
    write_delegation(env)
    night = N.Night.__new__(N.Night)
    night.allowlist, night.notes = [deleg, "~/.claude/CLAUDE.md"], []
    night._case_coverage = {deleg: ["delegation-approval"], "~/.claude/CLAUDE.md": ["claude-closing"]}
    night._case_sections = {(deleg, "delegation-approval"): ["approval-without-handing-work-back"],
                            ("~/.claude/CLAUDE.md", "claude-closing"): ["global/closing-line"]}
    night._case_digests = {}
    assert night._test_cases(deleg, DELEG_RULE) == ["delegation-approval"]
    assert night._test_cases(deleg, "dev:" + DELEG_RULE) == ["delegation-approval"]
    # another delegation section has no case: NO_TEST_CASE, as for any file
    assert night._test_cases(deleg, "claude-md:global:desktop-delegation-policy-v1-1/parent-and-capacity") == []


def write_delegation(env: SimpleNamespace) -> Path:
    p = env.home / "agent-config" / "parent" / "DELEGATION.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DELEGATION_MD)
    return p


def test_rsi_targets_lists_delegation_md(env: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    write_delegation(env)
    assert cli.main(["rsi", "targets", "--json", "--no-schedules"]) == 0
    files = {f["path"]: f for f in json.loads(capsys.readouterr().out)["files"]}
    assert files["~/agent-config/parent/DELEGATION.md"]["kind"] == "instruction"
    assert "~/agent-config/parent/DELEGATION.md" in N.instruction_targets()
    assert N._refused_by_improve("~/agent-config/parent/DELEGATION.md")          # not in allowed_targets
    assert ET.lock_action(T.untilde("~/agent-config/parent/DELEGATION.md")).action == ET.SUGGEST


def test_morning_edits_resolve_sends_delegation_rules_to_delegation_md(env: SimpleNamespace) -> None:
    """The edits step (edits.plan_candidate -> edits_targets.resolve) agrees with the night: a delegation rule of a
    generated copy is a suggestion on the source DELEGATION.md (outside improve's allowed targets)."""
    src = write_delegation(env).resolve()
    rule = "desktop-delegation-policy-v1-1/approval-without-handing-work-back"
    for source in ("claude-md:global", "agents-md:global"):
        t = ET.resolve(source, None, rule)
        assert (t.action, t.path) == (ET.SUGGEST, src) and "generated copy" in t.reason
    # other rules keep their file; without a rule id (older callers) nothing changes
    assert ET.resolve("claude-md:global", None, "global-claude-code-context/shell-habits").path == \
        (env.home / ".claude" / "CLAUDE.md").resolve()
    assert ET.resolve("claude-md:global").path == (env.home / ".claude" / "CLAUDE.md").resolve()
    assert ET.resolve("claude-md:demo", ["~/Projects/demo/CLAUDE.md"], rule).reason.startswith("repo file")
    src.unlink()                  # no source file: the copy stays the target, as before
    assert ET.resolve("claude-md:global", None, rule).path == (env.home / ".claude" / "CLAUDE.md").resolve()


def test_open_and_new_delegation_candidates_target_delegation_md(env: SimpleNamespace) -> None:
    write_delegation(env)
    (env.home / ".claude" / "CLAUDE.md").write_text(
        "# Desktop delegation policy v1.1\n\nIntro.\n\n## Approval without handing work back\n\n- Rule.\n"
        + "\n# Global\n\n## Closing line\n\n- End with a short summary.\n")
    with psycopg.connect(env.url) as conn:       # an earlier night's open candidate, sighted on the copy
        AP.upsert_candidate(conn, dedupe_key="dev:" + DELEG_RULE, source="deviations", kind="edit_instruction",
                            target="~/.claude/CLAUDE.md", rule_key=DELEG_RULE,
                            evidence_refs=["amem:deviation/1", "amem:deviation/2"], metrics={"sessions": 2})
        # tonight's sighting of the AGENTS.md copy's rule (a deviation in the delta)
        conn.execute("insert into sessions (session_id, kind, project_dir, started_at, tool) "
                     "values ('sess-deleg-1', 'chat', '/work/demo', now() - interval '1 hour', 'codex')")
        conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, turn, what, "
                     "why, how, followed_grade, result_grade, rule_id, rule_title) values ('dev:sess-deleg-1:x', "
                     "'sess-deleg-1', 'claude-md:global', 'Approval without handing work back', 1, 'asked again', "
                     "'unclear', 'carry it', 0.2, 0.5, 'desktop-delegation-policy-v1-1/parent-and-capacity', "
                     "'Parent and capacity')")
    rep = run(env)
    deleg = "~/agent-config/parent/DELEGATION.md"
    assert candidate(env.url, "dev:" + DELEG_RULE)["target"] == deleg
    by_key = {c["key"]: c for c in rep["candidates"]}
    assert by_key["dev:" + DELEG_RULE]["target"] == deleg and by_key["dev:" + DELEG_RULE]["suggest_only"] == "outside_allowed"
    new_key = "dev:claude-md:global:desktop-delegation-policy-v1-1/parent-and-capacity"
    assert candidate(env.url, new_key)["target"] == deleg
    assert {p["key"]: p["target"] for p in rep["patterns"]}[new_key] == deleg
    assert any("delegation-policy idea(s) target the source" in n for n in rep["notes"]), rep["notes"]
    assert read_report(N1)["candidates"] == rep["candidates"]
