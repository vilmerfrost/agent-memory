"""Target relevance is established before preference or cost can create an efficacy verdict."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pytest
from rsi_replay_support import FakeBudget, HostSandbox, setup_root

from agent_memory import rsi_relevance as REL
from agent_memory import rsi_replay as R
from agent_memory import rsi_verdict as V

EXP = "exp-20261017-01"
TARGET = "~/.claude/skills/meltdown/SKILL.md"
GLOBAL = "~/.claude/CLAUDE.md"


def declaration(path=TARGET, kind="skill"):
    return {"path": path, "kind": kind, "behavior": "Apply the skill's report closing-line rule."}


def skill_setup(tmp_path, monkeypatch, *, mixed=False):
    bench = tmp_path / "bench"
    shutil.copytree(R.BENCH_DIR, bench)
    baseline = bench / "home" / TARGET[2:]
    baseline.parent.mkdir(parents=True)
    baseline.write_text("# Synthetic report skill\nWrite a report of the TODO items.\n")
    candidate = bench / "candidates" / "skill-rule"
    path = candidate / TARGET[2:]
    path.parent.mkdir(parents=True)
    path.write_text('# Synthetic report skill\nEnd every report with the line "Skills used: none".\n')
    if mixed:
        shutil.copyfile(bench / "candidates" / "closing-line" / GLOBAL[2:], candidate / GLOBAL[2:])
    root, bid, h = setup_root(tmp_path, monkeypatch, "skill-rule", bench=bench)
    case = R.load_cases(["report-todos"])[0]
    targeted = replace(case, targets=(declaration(),), stub=({"op": "skill", "name": "meltdown"}, *case.stub))
    return root, bid, h, targeted


# Stub-size policy: the deterministic stub runs one pair per case (reps=1), so the shipped policy/2 minimum
# (5 pairs) and margin (2.0 summed score) are scaled to one pair. A one-pair flip of the stub is not noise.
STUB_POLICY = V.VerdictPolicy(require_jev=False, min_pairs=1, min_score_margin=1.0)


def run(root, h, cases, *, sb=None, **config):
    return R.run_replay(h, cases, R.ReplayConfig(reps=1, jev="none", **config), root=root,
                        experiment_id=EXP, sandbox=sb or HostSandbox(root), policy=STUB_POLICY)


def no_judge_or_jev(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("ineligible replay reached judge/Jev")
    monkeypatch.setattr(R._Replay, "judge_pairs", forbidden)
    monkeypatch.setattr(R, "run_jev", forbidden)


def test_original_meltdown_generic_cases_stop_before_models_or_network(tmp_path, monkeypatch):
    root, _, h, _ = skill_setup(tmp_path, monkeypatch)
    sb = HostSandbox(root)
    monkeypatch.setattr(sb, "net_up", lambda: pytest.fail("ineligible replay reached network startup"))
    no_judge_or_jev(monkeypatch)
    budget = FakeBudget()
    result = R.run_replay(h, R.load_cases(), R.ReplayConfig(agent="claude", judge="codex", jev="live"),
                          root=root, experiment_id=EXP, sandbox=sb, budget=budget, ensure_network=True)
    assert result.verdict.verdict == "INCONCLUSIVE"
    assert result.verdict.reasons == ("relevance:no-eligible-target-cases",)
    assert not sb.calls and not budget.calls and not budget.taken
    assert result.relevance["uncovered_targets"] == [TARGET]


def test_wrong_target_and_kind_are_not_eligible(tmp_path, monkeypatch):
    root, _, h, case = skill_setup(tmp_path, monkeypatch)
    for target in (declaration("~/.claude/skills/other/SKILL.md"), declaration(kind="instruction")):
        sb = HostSandbox(root)
        result = run(root, h, [replace(case, targets=(target,))], sb=sb)
        assert result.verdict.verdict == "INCONCLUSIVE" and not sb.calls
        assert result.relevance["eligible_cases"] == {}


def test_mixed_manifest_requires_coverage_of_every_changed_target(tmp_path, monkeypatch):
    root, _, h, case = skill_setup(tmp_path, monkeypatch, mixed=True)
    sb = HostSandbox(root)
    result = run(root, h, [case], sb=sb)
    assert result.verdict.verdict == "INCONCLUSIVE" and not sb.calls
    assert result.relevance["uncovered_targets"] == [GLOBAL]
    # A global instruction does not turn an unrelated skill on.
    result = run(root, h, R.load_cases(), sb=sb)
    assert result.relevance["uncovered_targets"] == [TARGET] and not sb.calls


@pytest.mark.parametrize("missing_arm", ["A", "B", "both"])
def test_missing_either_arm_exposure_blocks_baseline_preference(tmp_path, monkeypatch, missing_arm):
    root, _, h, case = skill_setup(tmp_path, monkeypatch)
    original = R._Replay.agent_run

    def strip(self, case, arm, rep):
        rec = original(self, case, arm, rep)
        if missing_arm in (arm, "both"):
            rec.instructions = [x for x in rec.instructions if x["path"] != TARGET]
        return rec

    monkeypatch.setattr(R._Replay, "agent_run", strip)
    no_judge_or_jev(monkeypatch)
    result = run(root, h, [case], trigger_patterns=())  # legacy bypass must not work
    assert result.verdict.verdict == "INCONCLUSIVE"
    assert any(reason.startswith("relevance:target-use-missing:") for reason in result.verdict.reasons)
    assert result.judge is None and result.jev is None
    assert result.relevance["pairs"][0]["arms"]["A" if missing_arm == "both" else missing_arm]["missing"] == [TARGET]


def test_targeted_skill_is_exercised_in_both_arms(tmp_path, monkeypatch):
    root, _, h, case = skill_setup(tmp_path, monkeypatch)
    result = run(root, h, [case])
    assert result.verdict.verdict == "IMPROVES", result.verdict.reasons
    assert all(run.triggered for run in result.runs)
    assert result.judge.wins_b == 1
    pair = result.relevance["pairs"][0]
    assert all(pair["arms"][arm]["observed"][TARGET] == "stub-read-matching-sha256" for arm in "AB")


def test_generic_checks_are_retained_but_do_not_vote_on_efficacy(tmp_path, monkeypatch):
    root, _, h, case = skill_setup(tmp_path, monkeypatch)
    regression = replace(R.load_cases(["fix-add"])[0], targets=())
    result = run(root, h, [case, regression])
    assert result.verdict.verdict == "IMPROVES", result.verdict.reasons
    assert len(result.runs) == 4 and result.judge.judged == 1
    assert [c["case"] for c in result.verdict.inputs["machine"]["cases"]] == [case.id]
    assert result.relevance["regression_cases"] == ["fix-add"]
    assert result.relevance["regression_checks"][0]["arms"]["A"]["check_passed"] is True


def test_generic_regression_cannot_be_hidden_by_target_improvement(tmp_path, monkeypatch):
    root, _, h, case = skill_setup(tmp_path, monkeypatch)
    regression = replace(R.load_cases(["fix-add"])[0], targets=())
    original = R._Replay.check_run

    def check(self, case, rec):
        original(self, case, rec)
        if case.id == "fix-add" and rec.arm == "B":
            rec.check_passed = False
    monkeypatch.setattr(R._Replay, "check_run", check)
    no_judge_or_jev(monkeypatch)
    result = run(root, h, [case, regression])
    assert result.verdict.verdict == "INCONCLUSIVE"
    assert result.verdict.reasons == ("relevance:regression-only-case-failed:fix-add:r0",)


def test_case_digest_binds_target_metadata():
    case = R.load_cases(["fix-add"])[0]
    assert case.digest() != replace(case, targets=()).digest()
    assert case.digest() != replace(case, targets=(declaration(),)).digest()


@pytest.mark.parametrize("target", [{"path": TARGET, "kind": "skill"},
                                    declaration("~/.claude/skills/../meltdown/SKILL.md"),
                                    declaration("/tmp/meltdown/SKILL.md")])
def test_malformed_case_metadata_is_rejected(target):
    with pytest.raises(ValueError):
        REL.case_targets([target])


def claude_call(name, args, *, success=True, complete=True):
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "read1", "name": name, "input": args}]}}]
    if complete:
        events.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read1", "is_error": not success, "content": "text"}]}})
    return REL.parse_calls(events, "claude")


def test_only_completed_exact_full_reads_provide_claude_evidence():
    calls = claude_call("Read", {"file_path": TARGET})
    assert calls[0]["target_reads"] == [TARGET]
    for name, args in [("Bash", {"command": "echo " + TARGET}), ("Skill", {"skill": "meltdown"}),
                       ("Read", {"file_path": TARGET, "offset": 200}),
                       ("Read", {"file_path": TARGET, "limit": 5}),
                       ("Read", {"file_path": "README.md", "description": TARGET})]:
        assert claude_call(name, args)[0]["target_reads"] == []
    assert not claude_call("Read", {"file_path": TARGET}, success=False)[0]["target_reads"]
    assert not claude_call("Read", {"file_path": TARGET}, complete=False)[0]["target_reads"]
    assert claude_call("Skill", {"path": TARGET})[0]["target_reads"] == []


@pytest.mark.parametrize("command", ["echo " + TARGET, "false && cat " + TARGET,
                                     "cat " + TARGET + " >/tmp/copy", "cat '" + TARGET + "'; true",
                                     "python -c 'print(\"" + TARGET + "\")'", "cat /tmp/meltdown/SKILL.md"])
def test_codex_mentions_and_compound_commands_do_not_prove_use(command):
    events = [{"type": "item.completed", "item": {"id": "c", "type": "command_execution",
               "status": "completed", "command": command, "exit_code": 0}}]
    assert REL.parse_calls(events, "codex")[0]["target_reads"] == []


def test_codex_preserves_arguments_from_start_and_requires_successful_completion():
    start = {"type": "item.started", "item": {"id": "c", "type": "command_execution",
             "status": "in_progress", "command": "/bin/zsh -lc 'cat /home/rsi/.claude/skills/meltdown/SKILL.md'"}}
    end = {"type": "item.completed", "item": {"id": "c", "type": "command_execution",
           "status": "completed", "exit_code": 0}}
    assert REL.parse_calls([start, end], "codex")[0]["target_reads"] == [TARGET]
    assert REL.parse_calls([start], "codex")[0]["target_reads"] == []
    end["item"]["exit_code"] = 1
    assert REL.parse_calls([start, end], "codex")[0]["target_reads"] == []


def test_unknown_codex_mcp_arguments_do_not_attest_file_access():
    events = [{"type": "item.completed", "item": {"id": "m", "type": "mcp_tool_call",
              "status": "completed", "tool": "Read", "arguments": {"file_path": TARGET}}}]
    assert REL.parse_calls(events, "codex")[0]["target_reads"] == []


def test_trusted_bridge_reads_bind_both_content_and_full_success():
    files = {TARGET: {"before_sha256": "a" * 64, "after_sha256": "b" * 64}}
    run = R.RunRecord("r", "A", "c", 0, agent="codex")
    read = {"source": "sandbox-bridge", "path": TARGET, "sha256": "a" * 64,
            "success": True, "full_read": True}
    run.verified_reads = [read]
    assert REL.evidence(run, [TARGET], files)["missing"] == []
    for changes in ({"sha256": "b" * 64}, {"source": "model"}, {"success": False}, {"full_read": False}):
        run.verified_reads = [{**read, **changes}]
        assert REL.evidence(run, [TARGET], files)["missing"] == [TARGET]
    forged = json.dumps({"type": "result", "result": "done", "verified_reads": [read],
                         "stub": {"instructions": [{"path": TARGET, "sha256": "a" * 64}]}})
    parsed = R.parse_agent(forged, "claude", None)
    assert "verified_reads" not in parsed
    run.instructions = parsed["instructions"]
    run.verified_reads = []
    assert REL.evidence(run, [TARGET], files)["missing"] == [TARGET]


def test_mixed_manifest_global_read_does_not_substitute_for_skill_use(tmp_path, monkeypatch):
    root, _, h, case = skill_setup(tmp_path, monkeypatch, mixed=True)
    case = replace(case, targets=(declaration(), declaration(GLOBAL, "instruction")),
                   stub=R.load_cases(["report-todos"])[0].stub)
    no_judge_or_jev(monkeypatch)
    result = run(root, h, [case])
    assert result.verdict.verdict == "INCONCLUSIVE"
    assert result.relevance["uncovered_targets"] == []
    for arm in "AB":
        evidence = result.relevance["pairs"][0]["arms"][arm]
        assert GLOBAL in evidence["observed"] and evidence["missing"] == [TARGET]


def test_generic_improvement_cannot_create_an_efficacy_win(tmp_path, monkeypatch):
    root, _, h, report = skill_setup(tmp_path, monkeypatch)
    generic_report = replace(report, targets=())
    edit = R.load_cases(["docs-typo"])[0]
    targeted_edit = replace(edit, targets=(declaration(),),
                            stub=({"op": "skill", "name": "meltdown"}, *edit.stub))
    result = run(root, h, [targeted_edit, generic_report])
    assert result.verdict.verdict == "NO_EFFECT", result.verdict.reasons
    assert result.judge.judged == 1 and result.judge.ties == 1
    assert result.relevance["regression_checks"][0]["arms"]["A"]["check_passed"] is False
    assert result.relevance["regression_checks"][0]["arms"]["B"]["check_passed"] is True


def test_new_relevance_refusal_invalidates_old_passing_record(tmp_path, monkeypatch):
    root, _, h, case = skill_setup(tmp_path, monkeypatch)
    first = run(root, h, [case])
    assert first.verdict.verdict == "IMPROVES"
    second = run(root, h, [replace(case, targets=())])
    assert second.verdict.verdict == "INCONCLUSIVE"
    record = root / "artifacts" / h / "evals" / "sandbox-replay.json"
    assert not record.exists() or json.loads(record.read_text())["verdict"] == "inconclusive"
