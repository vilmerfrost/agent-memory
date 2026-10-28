"""Hand-written section-bound decision cases (2026-10-28): hidden grading, exact declarations and night gating.

Each case's prompt gives facts and an output shape but not the rule; the rule lives only in the declared
instruction section. Good outputs pass; wrong decisions, lost evidence and invented success fail. Stubs validate
wiring only, not that a live model follows the rule.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from agent_memory import config, rsi_night as N, rsi_relevance as REL, rsi_replay as R

DATA = Path(__file__).parent / "data/rsi_section_cases"
WORKER = "~/agent-config/agents/worker.md"
PARENT = "~/agent-config/parent/PARENT.md"
STARTING = "4f1c2a9e0b7d4c3a8e6f5d2c1b0a9f8e7d6c5b4a"
CASES = {
    "worker-dod-gaps": (WORKER, ["definition-of-done", "final-report"], 7),
    "worker-helper-routing": (WORKER, ["subworkers"], 5),
    "worker-credential-runner": (WORKER, ["credential-aware-app-execution"], 5),
    "parent-dod-review": (PARENT, ["dod"], 5),
    "parent-work-trigger": (PARENT, ["modes"], 7),
}


def grade(cid, tmp_path, mutate=None):
    case, = R.load_cases([cid])
    work = tmp_path / "work"
    shutil.copytree(case.fixture, work)
    shutil.copytree(DATA / cid / "good", work, dirs_exist_ok=True)
    if mutate:
        mutate(work)
    env = {"PATH": os.environ["PATH"], "HIDDEN_CHECK_DIR": str(case.hidden_check),
           "FIXTURE_DIR": str(case.fixture)}
    return subprocess.run(["bash", "-c", case.check], cwd=work, env=env, capture_output=True, text=True, timeout=30)


def lines(result):
    return [json.loads(x[len("SCENARIO "):]) for x in result.stdout.splitlines() if x.startswith("SCENARIO ")]


def edit(scenario, **changes):
    def mutate(work):
        p = work / "result.json"
        obj = json.loads(p.read_text())
        row = next(r for r in obj["scenarios"] if r["id"] == scenario)
        for key, value in changes.items():
            if value is KeyError:
                row.pop(key)
            else:
                row[key] = value
        p.write_text(json.dumps(obj))
    return mutate


@pytest.mark.parametrize("cid", sorted(CASES))
def test_case_declares_exact_sections_and_loads(cid):
    case, = R.load_cases([cid])
    path, sections, _ = CASES[cid]
    assert case.targets == ({"path": path, "kind": "instruction", "behavior": case.targets[0]["behavior"],
                             "sections": sections},)
    assert case.hidden_check is not None and case.hidden_check.name == "section-decisions"
    assert json.loads((case.fixture / "scenarios.json").read_text())["case"] == cid
    assert case.stub[0] == {"op": "read", "path": path} and case.stub[-1]["op"] == "final"


@pytest.mark.parametrize("cid", sorted(CASES))
def test_prompt_does_not_restate_the_rule(cid):
    """F4: the rule must come from the staged section, not the task text."""
    case, = R.load_cases([cid])
    leaked = ["parent-run", "foreground", "1100", "neighbor", "starting commit", "done when", "follow-up",
              "switch tactic", "endpoint", "production", "talk mode", "wrapper", "not enough"]
    assert [w for w in leaked if w in case.prompt.lower()] == []


@pytest.mark.parametrize("cid", sorted(CASES))
def test_stub_writes_the_known_good_answer(cid):
    case, = R.load_cases([cid])
    text = next(s["text"] for s in case.stub if s["op"] == "write" and s["path"] == "result.json")
    assert json.loads(text) == json.loads((DATA / cid / "good/result.json").read_text())


@pytest.mark.parametrize("cid", sorted(CASES))
def test_known_good_output_passes_with_one_machine_line_per_scenario(cid, tmp_path):
    result = grade(cid, tmp_path)
    assert result.returncode == 0 and result.stdout.strip().splitlines()[-1] == "ok", result.stderr
    rows = lines(result)
    assert len(rows) == CASES[cid][2] and all(r["pass"] and r["case"] == cid for r in rows)
    assert len({r["scenario"] for r in rows}) == len(rows)


def reorder_and_restyle(work):
    p = work / "result.json"
    obj = json.loads(p.read_text())
    for row in obj["scenarios"]:
        row["note"] = "extra keys are allowed"
        for key in ("status", "action", "mode", "next"):
            if isinstance(row.get(key), str):
                row[key] = " " + row[key].upper() + " "
        if isinstance(row.get("command"), str):
            row["command"] = "`" + row["command"].replace(" ", "  ") + "`"
        if isinstance(row.get("gaps"), list):
            row["gaps"] = list(reversed(row["gaps"]))
    obj["scenarios"].reverse()
    p.write_text(json.dumps(obj, indent=2))
    (work / "notes.md").write_text("Reasoning notes are not graded.\n")


@pytest.mark.parametrize("cid", sorted(CASES))
def test_form_alone_never_fails_a_correct_answer(cid, tmp_path):
    result = grade(cid, tmp_path, reorder_and_restyle)
    assert result.returncode == 0, result.stderr


BAD = [
    # worker-dod-gaps: invented success, lost evidence, wrong anchor
    ("worker-dod-gaps", "api-tests-only", "invented-success", edit("api-tests-only", status="DONE", gaps=[])),
    ("worker-dod-gaps", "ui-two-widths", "invented-success", edit("ui-two-widths", status="DONE", gaps=[])),
    ("worker-dod-gaps", "ui-no-neighbors", "missing-evidence", edit("ui-no-neighbors", gaps=[])),
    ("worker-dod-gaps", "api-one-of-two", "wrong-decision", edit("api-one-of-two", status="DONE")),
    ("worker-dod-gaps", "red-preexisting", "invented-success", edit("red-preexisting", status="DONE")),
    ("worker-dod-gaps", "red-preexisting", "wrong-anchor",
     edit("red-preexisting", baseline_commit="9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d")),
    ("worker-dod-gaps", "ui-two-widths", "padded-gaps",
     edit("ui-two-widths", gaps=["browser_width_1100", "browser_width_768"])),
    ("worker-dod-gaps", "api-full", "wrong-decision", edit("api-full", status="PARTIAL", gaps=["load_test"])),
    ("worker-dod-gaps", "red-preexisting", "missing-evidence", edit("red-preexisting", baseline_commit=KeyError)),
    # worker-helper-routing
    ("worker-helper-routing", "long-research-while-coding", "wrong-decision",
     edit("long-research-while-coding", action="launch_reader_background", next="continue_independent_work")),
    ("worker-helper-routing", "parallel-migration", "wrong-decision",
     edit("parallel-migration", action="launch_subworker_background", helper_id_from="self")),
    ("worker-helper-routing", "blocked-on-migration", "wrong-decision",
     edit("blocked-on-migration", next="poll_child_status")),
    ("worker-helper-routing", "blocked-on-migration", "invented-success", edit("blocked-on-migration", next="none")),
    ("worker-helper-routing", "other-app-no-isolation", "wrong-decision",
     edit("other-app-no-isolation", action="start_cli_agent", next="wait_for_result", helper_id_from="self")),
    ("worker-helper-routing", "docs-research", "missing-evidence", edit("docs-research", helper_id_from="none")),
    # worker-credential-runner
    ("worker-credential-runner", "unit-tests", "wrong-decision", edit("unit-tests", command="uv run pytest -q tests/unit")),
    ("worker-credential-runner", "dev-server", "wrong-decision",
     edit("dev-server", command="uv run uvicorn shop.api:app --port 8123")),
    ("worker-credential-runner", "branch-status", "wrong-decision", edit("branch-status", command="parent-run -- git status")),
    ("worker-credential-runner", "plain-repo", "wrong-decision", edit("plain-repo", command="parent-run -- npm test")),
    ("worker-credential-runner", "missing-key", "invented-success",
     edit("missing-key", action="copy_from_main_checkout", command="parent-run -- uv run pytest -q tests/payments",
          missing=None)),
    ("worker-credential-runner", "missing-key", "missing-evidence",
     edit("missing-key", missing={"name": "STRIPE_TEST_KEY", "target": None})),
    ("worker-credential-runner", "missing-key", "wrong-decision", edit("missing-key", action="use_production_key")),
    # parent-dod-review
    ("parent-dod-review", "done-with-red", "invented-success", edit("done-with-red", action="accept")),
    ("parent-dod-review", "partial-preexisting", "invented-success", edit("partial-preexisting", action="accept_with_note")),
    ("parent-dod-review", "partial-preexisting", "wrong-anchor",
     edit("partial-preexisting", baseline_commit="9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d")),
    ("parent-dod-review", "partial-preexisting", "missing-evidence",
     edit("partial-preexisting", tell_vilmer_preexisting=False)),
    ("parent-dod-review", "done-disputed-again", "wrong-decision", edit("done-disputed-again", action="follow_up_to_worker")),
    ("parent-dod-review", "done-disputed-first", "wrong-decision", edit("done-disputed-first", action="fix_it_yourself")),
    ("parent-dod-review", "done-clean", "wrong-decision", edit("done-clean", action="ask_vilmer_to_check")),
    ("parent-dod-review", "done-with-red", "type", edit("done-with-red", tell_vilmer_preexisting=0)),
    # parent-work-trigger
    ("parent-work-trigger", "brief-without-done-when", "invented-success", edit("brief-without-done-when", mode="dispatch")),
    ("parent-work-trigger", "brief-with-done-when", "wrong-decision", edit("brief-with-done-when", mode="talk")),
    ("parent-work-trigger", "recon-then-fix", "wrong-decision", edit("recon-then-fix", mode="dispatch")),
    ("parent-work-trigger", "dont-recon", "wrong-decision", edit("dont-recon", mode="recon")),
    ("parent-work-trigger", "pasted-slack", "invented-success", edit("pasted-slack", mode="dispatch")),
]


@pytest.mark.parametrize("cid,scenario,kind,mutate", BAD, ids=[f"{c}:{s}:{k}" for c, s, k, _ in BAD])
def test_known_bad_output_fails_on_exactly_that_scenario(cid, scenario, kind, mutate, tmp_path):
    result = grade(cid, tmp_path, mutate)
    assert result.returncode != 0 and "section-decisions:" in result.stderr
    failed = [r["scenario"] for r in lines(result) if not r["pass"]]
    assert failed == [scenario], (kind, result.stdout)


def drop_row(work):
    p = work / "result.json"
    obj = json.loads(p.read_text())
    obj["scenarios"] = obj["scenarios"][1:]
    p.write_text(json.dumps(obj))


def invent_row(work):
    p = work / "result.json"
    obj = json.loads(p.read_text())
    obj["scenarios"].append(dict(obj["scenarios"][0], id="bonus"))
    p.write_text(json.dumps(obj))


def duplicate_row(work):
    p = work / "result.json"
    obj = json.loads(p.read_text())
    obj["scenarios"].append(obj["scenarios"][0])
    p.write_text(json.dumps(obj))


def change_input(work):
    p = work / "scenarios.json"
    p.write_text(p.read_text().replace("\"id\"", "\"id\" ", 1))


def delete_input(work):
    (work / "scenarios.json").unlink()


def no_result(work):
    (work / "result.json").unlink()


@pytest.mark.parametrize("cid", sorted(CASES))
@pytest.mark.parametrize("mutate", [drop_row, invent_row, duplicate_row, change_input, delete_input, no_result])
def test_missing_invented_or_tampered_output_fails(cid, mutate, tmp_path):
    result = grade(cid, tmp_path, mutate)
    assert result.returncode != 0 and "section-decisions:" in result.stderr


def test_only_scenarios_that_are_wrong_fail(tmp_path):
    def two_wrong(work):
        edit("api-tests-only", status="DONE", gaps=[])(work)
        edit("ui-full", status="PARTIAL")(work)
    result = grade("worker-dod-gaps", tmp_path, two_wrong)
    verdicts = {r["scenario"]: r["pass"] for r in lines(result)}
    assert result.returncode != 0 and sum(not v for v in verdicts.values()) == 2
    assert not verdicts["api-tests-only"] and not verdicts["ui-full"] and verdicts["api-full"]


def bound(path, section):
    return {"files": [{"path": path, "kind": "instruction", "rule_id": REL.section_rule_id(path, section)}]}


SECTION_CASES = [
    (WORKER, "definition-of-done", ["worker-dod-gaps", "worker-final-report"]),
    (WORKER, "final-report", ["worker-dod-gaps", "worker-final-report"]),
    (WORKER, "subworkers", ["worker-helper-lifecycle", "worker-helper-routing"]),
    (WORKER, "credential-aware-app-execution", ["worker-credential-runner", "worker-server-launch"]),
    (PARENT, "dod", ["parent-dod-review"]),
    (PARENT, "modes", ["parent-work-trigger"]),
]


@pytest.mark.parametrize("path,section,expected", SECTION_CASES)
def test_section_bound_replay_selects_the_new_cases(path, section, expected):
    rel = REL.select(bound(path, section), R.load_cases())
    assert sorted(rel["eligible_cases"]) == expected and rel["uncovered_targets"] == []


@pytest.mark.parametrize("path,section,expected", SECTION_CASES)
def test_night_section_gating_picks_the_new_cases(path, section, expected):
    night = object.__new__(N.Night)
    night.cfg = N.load_config(config.REPO_ROOT / "config/rsi-night.subscription.json")
    night.notes = []
    assert night._test_cases(path, section_id=section) == expected
    assert night.notes == []


def test_night_section_gating_still_skips_sections_without_cases():
    night = object.__new__(N.Night)
    night.cfg = N.load_config(config.REPO_ROOT / "config/rsi-night.subscription.json")
    night.notes = []
    assert night._test_cases(WORKER, section_id="checkpoints") == []
    assert night._test_cases(PARENT, section_id="learning-loop") == []
