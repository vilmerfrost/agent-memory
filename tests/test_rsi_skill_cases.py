"""Skill replay cases (meltdown, pepe, anatomy): they load, grade crafted outputs, are selected for their skill's
changes in both providers, and show exposure in both arms of a stub replay. No model, no network, no real ~/.agents:
the skill texts here are synthetic stand-ins staged in a temp RSI sandbox."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from rsi_replay_support import HostSandbox, setup_root

from agent_memory import config, rsi_night as N, rsi_provider_replay as P, rsi_relevance as REL
from agent_memory import rsi_replay as R, rsi_schedule as S, rsi_verdict as V

DATA = Path(__file__).parent / "data" / "rsi_skill_cases"
SKILLS = {"meltdown-brief": "meltdown", "pepe-lock": "pepe", "anatomy-plan": "anatomy"}
EXP = "exp-20261025-01"


def target(case_id: str) -> str:
    return f"~/.agents/skills/{SKILLS[case_id]}/SKILL.md"


def manifest(*paths: str) -> dict:
    return {"files": [{"path": p, "kind": "skill"} for p in paths]}


# ---------------------------------------------------------------- the cases


@pytest.mark.parametrize("cid", sorted(SKILLS))
def test_skill_case_loads_with_one_exact_skill_target(cid):
    (case,) = R.load_cases([cid])
    sections = {"anatomy-plan": ["workflow"], "meltdown-brief": ["intro", "pick-the-style"],
                "pepe-lock": ["phase-3-lock"]}
    assert case.targets == ({"path": target(cid), "kind": "skill", "behavior": case.targets[0]["behavior"],
                             "sections": sections[cid]},)
    assert case.hidden_check is not None and (case.hidden_check / "check.py").is_file()
    assert case.fixture.is_dir() and any(case.fixture.iterdir())
    assert case.stub[0] == {"op": "skill", "name": SKILLS[cid]} and case.stub[-1]["op"] == "final"
    assert target(cid) in case.prompt and "network" in case.prompt
    # The stub writes exactly the crafted compliant output, so the stub replay grades what the unit test grades.
    writes = {s["path"]: s["text"] for s in case.stub if s["op"] == "write"}
    good = DATA / cid / "good"
    assert writes == {p.relative_to(good).as_posix(): p.read_text(encoding="utf-8")
                      for p in good.rglob("*") if p.is_file()}
    # Hidden answers never ship in the fixture the agent sees.
    assert not set(writes) & {p.relative_to(case.fixture).as_posix() for p in case.fixture.rglob("*")}


def grade(cid: str, outputs: Path | None, tmp_path: Path, edit=None) -> subprocess.CompletedProcess:
    """Run the case's check command the way check_entry does: cwd = the result, the untouched fixture and the hidden
    check dir in the environment."""
    (case,) = R.load_cases([cid])
    work = tmp_path / "work"
    shutil.copytree(case.fixture, work)
    if outputs is not None:
        shutil.copytree(outputs, work, dirs_exist_ok=True)
    if edit is not None:
        edit(work)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
           "HIDDEN_CHECK_DIR": str(case.hidden_check), "FIXTURE_DIR": str(case.fixture)}
    return subprocess.run(["bash", "-c", case.check], cwd=work, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("cid", sorted(SKILLS))
def test_hidden_check_passes_the_compliant_output_and_fails_the_other(cid, tmp_path):
    ok = grade(cid, DATA / cid / "good", tmp_path / "good")
    assert ok.returncode == 0 and ok.stdout.strip() == "ok", ok.stderr
    bad = grade(cid, DATA / cid / "bad", tmp_path / "bad")
    assert bad.returncode != 0 and cid in bad.stderr
    nothing = grade(cid, None, tmp_path / "none")
    assert nothing.returncode != 0 and "missing" in nothing.stderr


def _json_edit(name: str, change):
    def edit(work: Path) -> None:
        p = work / name
        d = json.loads(p.read_text())
        change(d)
        p.write_text(json.dumps(d))
    return edit


def _append(name: str, text: str):
    def edit(work: Path) -> None:
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(text)
    return edit


MUTATIONS = [
    ("meltdown-brief", "explainer", _json_edit("film.json", lambda d: d.update(style="explainer"))),
    ("meltdown-brief", "15 s", _json_edit("film.json", lambda d: d.update(length_s=30))),
    ("meltdown-brief", "eleven_v4", _json_edit("film.json", lambda d: d["voice"].update(model_id="eleven_v3"))),
    ("meltdown-brief", "Vilmer Frost", _json_edit("film.json", lambda d: d["voice"].update(voice_id="Cz0K1kOv9tD8l0b5Qu53"))),
    ("meltdown-brief", "never grades", _json_edit("film.json", lambda d: d.update(reviewer="self"))),
    ("meltdown-brief", "2-4 words", _json_edit("film.json", lambda d: d["lines"].append("This line is far too long for a reel"))),
    ("meltdown-brief", "about 25 words", _json_edit("film.json", lambda d: d.update(lines=d["lines"][:3]))),
    ("meltdown-brief", "script.md lacks", _json_edit("film.json", lambda d: d["lines"].append("Brand new line"))),
    ("meltdown-brief", "", _append("README.md", "\nchanged\n")),
    ("pepe-lock", "user's choice", _json_edit("lock.json", lambda d: d["locked"].append("Flake Hunter"))),
    ("pepe-lock", "user's choice", _json_edit("lock.json", lambda d: d.update(locked=["Ghost Reviewer"]))),
    ("pepe-lock", "locked idea is parked", _append("docs/ideas/parked.md", "- Diff Diary: revisit later\n")),
    ("pepe-lock", "once, one line each", _append("docs/ideas/parked.md", "- PR Weather again. Revisit soon.\n")),
    ("pepe-lock", "revisit trigger", _append("docs/ideas/parked.md", "- Commit Haiku, for real this time\n")),
    ("pepe-lock", "no briefs", _append("docs/ideas/ghost-reviewer.md", "# Ghost Reviewer\n")),
    ("pepe-lock", "", _append("session.md", "\nUser: one more\n")),
    ("anatomy-plan", "Figure", _json_edit("plan.json", lambda d: d.update(size="Hero"))),
    ("anatomy-plan", "30-90", _json_edit("plan.json", lambda d: d.update(estimate_minutes=[120, 240]))),
    ("anatomy-plan", "not a machine", _json_edit("plan.json", lambda d: d.update(object="A flowchart with arrows"))),
    ("anatomy-plan", "40-80 parts", _json_edit("plan.json", lambda d: d.update(parts=d["parts"][:30]))),
    ("anatomy-plan", "want exactly", _json_edit("plan.json", lambda d: [p.update(group="base") for p in d["parts"]
                                                                     if p["group"] == "controls"])),
    ("anatomy-plan", "subject group", _json_edit("plan.json", lambda d: d.update(lit_part="key"))),
    ("anatomy-plan", "true numbers", _json_edit("plan.json", lambda d: d.update(numbers=["90% hit rate"]))),
    ("anatomy-plan", "no geometry", _append("figure.svg", "<svg/>")),
    ("anatomy-plan", "", _append("request.md", "\nchanged\n")),
]


@pytest.mark.parametrize("cid,why,edit", MUTATIONS, ids=[f"{c}:{w or 'fixture-changed'}" for c, w, _ in MUTATIONS])
def test_each_rule_violation_alone_fails_the_check(cid, why, edit, tmp_path):
    res = grade(cid, DATA / cid / "good", tmp_path, edit)
    assert res.returncode != 0
    assert why in res.stderr, res.stderr


# ---------------------------------------------------------------- selection and planning


@pytest.mark.parametrize("cid", sorted(SKILLS))
def test_a_skill_change_selects_its_case_and_plans_enough_pairs(cid):
    cases = R.load_cases()
    rel = REL.select(manifest(target(cid)), cases)
    assert rel["eligible_cases"] == {cid: [target(cid)]} and rel["uncovered_targets"] == []
    chosen, reps = R.plan_cases(manifest(target(cid)), cases, min_pairs=3, max_pairs=3)
    assert [c.id for c in chosen] == [cid] and reps == 3          # 3 pairs per provider: a verdict is possible
    assert P.required_providers(manifest(target(cid))["files"]) == ["claude", "codex"]


def test_planning_keeps_explicit_limits_and_generic_targets():
    cases = R.load_cases()
    generic, reps = R.plan_cases({"files": [{"path": "~/.claude/CLAUDE.md", "kind": "instruction"}]}, cases,
                                 min_pairs=3, max_pairs=3)
    assert [c.id for c in generic] == ["docs-typo", "fix-add", "report-todos"] and reps == 1   # unchanged night
    assert R.plan_cases(manifest(target("pepe-lock")), cases, min_pairs=3, max_pairs=2)[1] == 2
    assert R.plan_cases(manifest(target("pepe-lock")), cases, min_pairs=3, max_pairs=6, reps=4)[1] == 4
    # Uncovered: one case, so run_replay refuses before any container and records the uncovered file.
    one, reps = R.plan_cases(manifest("~/.agents/skills/handoff/SKILL.md"), cases, min_pairs=3, max_pairs=3)
    assert len(one) == 1 and reps == 1
    # Mixed: covered and uncovered targets keep the covered case; run_replay refuses the uncovered one.
    both, _ = R.plan_cases(manifest(target("pepe-lock"), "~/.agents/skills/handoff/SKILL.md"), cases,
                           min_pairs=3, max_pairs=3)
    assert [c.id for c in both] == ["pepe-lock"]


def test_paid_plan_stays_within_the_calibrated_pairs():
    for name, pairs in (("rsi-night.subscription.json", 6), ("rsi-night.subscription-claude.json", 3),
                        ("rsi-night.subscription-codex.json", 3), ("rsi-night.first.json", 3)):
        plan = S.paid_plan(N.load_config(config.REPO_ROOT / "config" / name))
        assert plan["ab_pairs_per_experiment"] == pairs, name
        assert plan["ab_pairs_per_experiment"] <= plan["max_ab_pairs"]


def skill_bench(tmp_path: Path, cid: str, *, before: str | None = None, after: str | None = None) -> Path:
    bench = tmp_path / "bench"
    shutil.copytree(R.BENCH_DIR, bench)
    rel = target(cid)[2:]
    home = bench / "home" / rel
    home.parent.mkdir(parents=True)
    home.write_text(before or f"---\nname: {SKILLS[cid]}\n---\n\n# {SKILLS[cid]}\n\n- The rule as it is.\n")
    cand = bench / "candidates" / f"{SKILLS[cid]}-rule" / rel
    cand.parent.mkdir(parents=True)
    cand.write_text(after or f"---\nname: {SKILLS[cid]}\n---\n\n# {SKILLS[cid]}\n\n- The rule, sharpened.\n")
    return bench


def test_subscription_profile_replays_the_skill_case_for_both_providers(tmp_path, monkeypatch):
    cid = "meltdown-brief"
    root, bid, h = setup_root(tmp_path, monkeypatch, "meltdown-rule", experiment=EXP, bench=skill_bench(tmp_path, cid))
    cfg = N.load_config(config.REPO_ROOT / "config" / "rsi-night.subscription.json")
    seen = []

    class Adapter:
        def __init__(self, stages, settings):
            self.provider, self.settings = stages["replay_agent"], settings

        def replay(self, **kw):
            seen.append((self.provider, self.settings["cases"], self.settings["reps"], self.settings["model"]))
            return N.ReplayOutcome(verdict="NO_EFFECT", reasons=[], arms={}, eval_path="x", runs=6)

    monkeypatch.setattr(N, "SandboxReplayer", Adapter)
    budget = SimpleNamespace(cfg=cfg.budget)
    out = P.replay(cfg.stages, cfg.replay, artifact_hash=h, experiment_id=EXP, baseline_id=bid, budget=budget)
    assert seen == [("claude", [cid], 3, "claude-opus-5-5"), ("codex", [cid], 3, "gpt-6.1-sol")]
    assert len(seen) * 3 <= cfg.budget.max_ab_pairs and out.reasons[:2] == ["claude: NO_EFFECT", "codex: NO_EFFECT"]


# ---------------------------------------------------------------- stub replay: exposure in both arms

# Stub-size policy: this deterministic stub replay runs 3 pairs (max_pairs=3), below the shipped policy/2
# minimum of 5; the shipped score margin (2.0) is kept.
STUB_POLICY = V.VerdictPolicy(require_jev=False, min_pairs=3)


@pytest.mark.parametrize("cid", sorted(SKILLS))
def test_stub_replay_shows_skill_exposure_in_both_arms(cid, tmp_path, monkeypatch):
    bench = skill_bench(tmp_path, cid)
    root, bid, h = setup_root(tmp_path, monkeypatch, f"{SKILLS[cid]}-rule", experiment=EXP, bench=bench)
    cases, reps = R.auto_cases(h, R.load_cases(), root=root, reps=1, max_pairs=3)
    assert [c.id for c in cases] == [cid] and reps == 3
    sb = HostSandbox(root)
    res = R.run_replay(h, cases, R.ReplayConfig(reps=reps, jev="none"), root=root, experiment_id=EXP, sandbox=sb,
                       policy=STUB_POLICY)
    rel = res.relevance
    assert rel["eligible_cases"] == {cid: [target(cid)]} and rel["uncovered_targets"] == []
    assert len(rel["pairs"]) == 3
    for pair in rel["pairs"]:
        for arm in "AB":
            assert pair["arms"][arm] == {"observed": {target(cid): "stub-read-matching-sha256"}, "missing": []}
    assert all(r.check_passed is True and r.error is None and r.triggered for r in res.runs), \
        [(r.run_id, r.error, r.check_log[-300:]) for r in res.runs]
    assert res.verdict.verdict == "NO_EFFECT" and res.judge.judged == 3     # same stub output in both arms
    # The skill is staged where both clients find it, holding the arm's own bytes (A: before, B: after).
    for arm, src in (("A", bench / "home"), ("B", bench / "candidates" / f"{SKILLS[cid]}-rule")):
        home = sb.arm_home(EXP, arm)
        mirror = home / ".claude" / "skills" / SKILLS[cid] / "SKILL.md"
        assert mirror.read_bytes() == (src / target(cid)[2:]).read_bytes() == (home / target(cid)[2:]).read_bytes()
        assert rel["staged_skills"][arm] == {SKILLS[cid]: target(cid)}


# ---------------------------------------------------------------- name-only skill calls


def claude_skill_call(args, *, skills, success=True, complete=True):
    events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "s1", "name": "Skill",
                                                             "input": args}]}}]
    if complete:
        events.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "s1", "is_error": not success, "content": "loaded"}]}})
    return REL.parse_calls(events, "claude", skills)[0]


def test_skill_index_maps_only_unique_staged_names():
    a, c = "~/.agents/skills/meltdown/SKILL.md", "~/.claude/skills/meltdown/SKILL.md"
    assert REL.skill_index({a: "1" * 64}) == {"meltdown": a}
    assert REL.skill_index({a: "1" * 64, c: "1" * 64}, {c: a}) == {"meltdown": a}          # our mirror: one skill
    assert REL.skill_index({a: "1" * 64, c: "2" * 64}, {c: a}) == {}                       # diverged mirror
    assert REL.skill_index({a: "1" * 64, c: "1" * 64}) == {}                               # same bytes, not ours
    assert REL.skill_index({"~/.agents/skills/a b/SKILL.md": "1" * 64, "~/.claude/CLAUDE.md": "1" * 64}) == {}
    assert REL.skill_index({a: "1" * 64, "~/.codex/skills/meltdown/SKILL.md": "3" * 64}) == {}


def test_a_completed_name_only_skill_call_counts_only_through_the_index():
    path = "~/.agents/skills/meltdown/SKILL.md"
    skills = {"meltdown": path}
    call = claude_skill_call({"skill": "meltdown"}, skills=skills)
    assert call["target_reads"] == [path] and call["read_kind"] == REL.SKILL_CALL
    assert claude_skill_call({"skill": "meltdown", "args": "a reel"}, skills=skills)["target_reads"] == [path]
    for args, kw in [({"skill": "meltdown"}, {"skills": None}), ({"skill": "pepe"}, {"skills": skills}),
                     ({"skill": "plugin:meltdown"}, {"skills": skills}),
                     ({"skill": "meltdown", "path": "/etc/passwd"}, {"skills": skills}),
                     ({"skill": "meltdown"}, {"skills": skills, "success": False}),
                     ({"skill": "meltdown"}, {"skills": skills, "complete": False})]:
        assert claude_skill_call(args, **kw)["target_reads"] == [], (args, kw)
    run = R.RunRecord("r", "A", "c", 0, agent="claude", tool_calls=[call])
    files = {path: {"before_sha256": "a" * 64, "after_sha256": "b" * 64}}
    assert REL.evidence(run, [path], files) == {"observed": {path: REL.SKILL_CALL}, "missing": []}
    # Codex has no Skill tool: an event named Skill is not a Claude tool_use and proves nothing.
    ev = [{"type": "item.completed", "item": {"id": "x", "type": "mcp_tool_call", "status": "completed",
                                              "tool": "Skill", "arguments": {"skill": "meltdown"}}}]
    assert REL.parse_calls(ev, "codex", skills)[0]["target_reads"] == []


def test_mirrors_never_replace_or_follow_existing_entries(tmp_path):
    home = tmp_path / "home"
    (home / ".agents" / "skills" / "a").mkdir(parents=True)
    (home / ".agents" / "skills" / "a" / "SKILL.md").write_text("A")
    (home / ".agents" / "skills" / "b").mkdir()
    (home / ".agents" / "skills" / "b" / "SKILL.md").write_text("B")
    (home / ".claude" / "skills" / "b").mkdir(parents=True)
    (home / ".claude" / "skills" / "b" / "SKILL.md").write_text("other B")
    (home / ".agents" / "skills" / "c").mkdir()
    (home / ".agents" / "skills" / "c" / "SKILL.md").symlink_to(home / ".agents" / "skills" / "a" / "SKILL.md")
    mirrors = R.install_skill_mirrors(home)
    assert mirrors == {"~/.claude/skills/a/SKILL.md": "~/.agents/skills/a/SKILL.md"}
    assert (home / ".claude" / "skills" / "b" / "SKILL.md").read_text() == "other B"
    assert not (home / ".claude" / "skills" / "c").exists()
    assert REL.skill_index(R.staged_skills(home), mirrors) == {"a": "~/.agents/skills/a/SKILL.md"}
