"""Per-scenario check results and the content/format split of the replay checkers.

Real agent outputs from the 2026-10-25 and 2026-10-27 nights live in tests/data/rsi_check_fixtures (rebuilt from the
stored run diffs in ~/sandbox/replays/exp-20261027-5a8bda33 and exp-20261025-95f15ef6). For every changed checker:
an output the old checker failed only on format now passes, and the same output with wrong substance still fails.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory import config, rsi_replay as R

ROOT = config.REPO_ROOT
BENCH = ROOT / "bench" / "rsi-replay"
DATA = Path(__file__).parent / "data" / "rsi_check_fixtures"
NIGHT_POLICY = DATA / "night-checkers" / "parent-policy-20261027"
PREFIX = "RSI-SCENARIOS "


def load_check_entry():
    spec = importlib.util.spec_from_file_location("check_entry_under_test", ROOT / "sandbox/stub_agent/check_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CE = load_check_entry()


def run_check(check_dir: Path, fixture: Path, outputs: Path | None, tmp_path: Path, edit=None):
    work = tmp_path / "work"
    shutil.copytree(fixture, work)
    if outputs is not None:
        shutil.copytree(outputs, work, dirs_exist_ok=True)
    if edit is not None:
        edit(work)
    env = {"PATH": os.environ["PATH"], "LANG": "C.UTF-8", "HIDDEN_CHECK_DIR": str(check_dir),
           "FIXTURE_DIR": str(fixture)}
    return subprocess.run([sys.executable, str(check_dir / "check.py")], cwd=work, env=env, capture_output=True,
                          text=True, timeout=60)


def scenario_line(result) -> dict:
    last = result.stdout.splitlines()[-1]
    assert last.startswith(PREFIX), result.stdout
    return json.loads(last[len(PREFIX):])


def replace(name: str, old: str, new: str, count: int = -1):
    def edit(work: Path) -> None:
        p = work / name
        text = p.read_text()
        assert old in text, (name, old)
        p.write_text(text.replace(old, new, count))
    return edit


def edit_json(name: str, change):
    def edit(work: Path) -> None:
        p = work / name
        obj = json.loads(p.read_text())
        change(obj)
        p.write_text(json.dumps(obj))
    return edit


def chain(*edits):
    def edit(work: Path) -> None:
        for e in edits:
            e(work)
    return edit


def row(obj, ident):
    return next(r for r in obj["scenarios"] if r["id"] == ident)


# ------------------------------------------------------------------ check_entry: the scenario line contract


def line(passed, total, rows=None, **extra):
    rows = rows if rows is not None else [{"id": f"s{i}", "passed": i < passed} for i in range(total)]
    return PREFIX + json.dumps({"passed": passed, "total": total, "scenarios": rows, **extra})


def test_scenario_line_is_parsed_into_counts_and_score():
    assert CE.scenario_result("ok\n" + line(2, 3) + "\n", False) == ({"passed": 2, "total": 3}, 2 / 3)
    assert CE.scenario_result("ok\n" + line(4, 4), True) == ({"passed": 4, "total": 4}, 1.0)
    assert CE.scenario_result(line(0, 2), False) == ({"passed": 0, "total": 2}, 0.0)


def test_last_scenario_line_wins():
    assert CE.scenario_result(line(0, 3) + "\nnoise\n" + line(1, 3), False) == ({"passed": 1, "total": 3}, 1 / 3)


@pytest.mark.parametrize("passed", [True, False])
def test_legacy_check_without_a_line_counts_as_one_scenario(passed):
    assert CE.scenario_result("ok\n" if passed else "", passed) == ({"passed": int(passed), "total": 1}, float(passed))


MALFORMED = {
    "not-json": PREFIX + "{passed: 2}",
    "not-an-object": PREFIX + "[2, 3]",
    "string-counts": PREFIX + json.dumps({"passed": "2", "total": "3", "scenarios": []}),
    "bool-counts": line(True, 1, [{"id": "a", "passed": True}]),
    "zero-total": line(0, 0, []),
    "passed-over-total": line(4, 3, [{"id": f"s{i}", "passed": True} for i in range(3)]),
    "rows-count-differs": line(1, 3, [{"id": "a", "passed": True}]),
    "passed-count-differs": line(2, 3, [{"id": "a", "passed": True}, {"id": "b", "passed": False},
                                        {"id": "c", "passed": False}]),
    "duplicate-ids": line(1, 2, [{"id": "a", "passed": True}, {"id": "a", "passed": False}]),
    "non-bool-row": line(1, 2, [{"id": "a", "passed": 1}, {"id": "b", "passed": False}]),
    "missing-id": line(1, 2, [{"passed": True}, {"id": "b", "passed": False}]),
    "indented-prefix": " " + line(1, 2),
    "no-space-prefix": "RSI-SCENARIOS" + line(1, 2)[len(PREFIX):],
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_line_is_treated_as_legacy_without_extra_credit(name):
    assert CE.scenario_result(MALFORMED[name] + "\n", False) == ({"passed": 0, "total": 1}, 0.0)
    assert CE.scenario_result(MALFORMED[name] + "\n", True) == ({"passed": 1, "total": 1}, 1.0)


def test_line_that_disagrees_with_the_exit_code_gives_no_extra_credit():
    # All scenarios passed but a global check (changed input, cmp of a fixture file) failed: no partial credit.
    assert CE.scenario_result(line(3, 3), False) == ({"passed": 0, "total": 1}, 0.0)
    # Exit 0 although the line reports a failure: the binary result stands.
    assert CE.scenario_result(line(2, 3), True) == ({"passed": 1, "total": 1}, 1.0)


def run_entry(tmp_path, check: str, capsys, *, result: Path | None = None, hidden: Path | None = None,
              fixture: Path | None = None, timeout_s: int = 30) -> dict:
    spec = tmp_path / "check.json"
    spec.write_text(json.dumps({"check": check, "timeout_s": timeout_s}))
    if result is None:
        result = tmp_path / "result"
        result.mkdir()
    work = tmp_path / "entry-work"
    work.mkdir()
    argv = ["--spec", str(spec), "--result", str(result), "--work", str(work)]
    if fixture is not None:
        argv += ["--fixture", str(fixture)]
    if hidden is not None:
        argv += ["--hidden", str(hidden)]
    capsys.readouterr()
    assert CE.main(argv) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_check_entry_reports_scenario_fields(tmp_path, capsys):
    out = run_entry(tmp_path, "echo ok; echo '" + line(1, 2) + "'; exit 1", capsys)
    assert out["check_passed"] is False and out["check_exit"] == 1
    assert out["check_scenarios"] == {"passed": 1, "total": 2} and out["check_score"] == 0.5


def test_check_entry_scenario_line_on_stderr_is_ignored(tmp_path, capsys):
    out = run_entry(tmp_path, "echo '" + line(1, 2) + "' >&2; exit 1", capsys)
    assert out["check_scenarios"] == {"passed": 0, "total": 1} and out["check_score"] == 0.0


@pytest.mark.parametrize("check,passed", [("echo ok", True), ("echo fail >&2; exit 3", False),
                                          ("echo '" + PREFIX + "{broken'; exit 1", False)])
def test_check_entry_legacy_and_malformed_checks(tmp_path, capsys, check, passed):
    out = run_entry(tmp_path, check, capsys)
    assert out["check_passed"] is passed
    assert out["check_scenarios"] == {"passed": int(passed), "total": 1} and out["check_score"] == float(passed)


def test_check_entry_timeout_is_a_failed_single_scenario(tmp_path, capsys):
    out = run_entry(tmp_path, "echo '" + line(2, 2) + "'; sleep 5", capsys, timeout_s=1)
    assert out["timed_out"] is True and out["check_passed"] is False
    assert out["check_scenarios"] == {"passed": 0, "total": 1} and out["check_score"] == 0.0


def test_check_entry_grades_a_real_checker_per_scenario(tmp_path, capsys):
    """worker-final-report through check_entry: one wrong scenario out of three gives 2/3, check still fails."""
    case, = R.load_cases(["worker-final-report"])
    result = tmp_path / "agent-workspace"
    shutil.copytree(case.fixture, result)
    shutil.copytree(DATA / "worker-final-report" / "codex-B-r1", result, dirs_exist_ok=True)
    edit_json("result.json", lambda o: row(o, "red").update(status="DONE"))(result)
    check = case.check.replace("python3 ", '"' + sys.executable + '" ', 1)   # check_entry pins PATH
    out = run_entry(tmp_path, check, capsys, result=result, hidden=case.hidden_check, fixture=case.fixture)
    assert out["check_passed"] is False
    assert out["check_scenarios"] == {"passed": 2, "total": 3} and out["check_score"] == pytest.approx(2 / 3)


# ------------------------------------------------------------------ worker-report-v5 (worker-final-report)


WFR_FIXTURE = BENCH / "fixtures" / "worker-final-report"
WFR_V3 = BENCH / "checks" / "parent-policy"
WFR_V5 = BENCH / "checks" / "worker-report-v5"
CODEX = DATA / "worker-final-report" / "codex-B-r1"
CLAUDE = DATA / "worker-final-report" / "claude-B-r0"


def test_worker_final_report_case_uses_v5():
    case, = R.load_cases(["worker-final-report"])
    assert case.hidden_check == WFR_V5


@pytest.mark.parametrize("output,night_error", [
    (CODEX, "invalid or duplicate report field"),          # extra "- Tests:" / "- API:" report lines
    (CLAUDE, "incorrect decision or evidence for guard"),   # BLOCKED on a host hard denial, plus Doc and wraps
])
def test_real_night_outputs_failed_the_night_checker_and_pass_v5(output, night_error, tmp_path):
    night = run_check(NIGHT_POLICY, WFR_FIXTURE, output, tmp_path / "night")
    assert night.returncode != 0 and night_error in night.stderr
    new = run_check(WFR_V5, WFR_FIXTURE, output, tmp_path / "v5")
    assert new.returncode == 0 and new.stdout.splitlines()[0] == "ok", new.stderr
    assert scenario_line(new) == {"passed": 3, "total": 3, "scenarios": [
        {"id": "red", "passed": True}, {"id": "green", "passed": True}, {"id": "guard", "passed": True}]}


# The codex report with its added DoD/API/UI lines written as their own "Name: value" lines (the report lines the
# hypothesis asked for); the test evidence stays under Evidence.
EXTRA_FIELDS = chain(*[replace(f"reports/{i}.md", "\n- " + name, "\n" + name)
                       for i in ("red", "green", "guard") for name in ("Brief DoD:", "API:", "UI:")])


def test_extra_report_lines_fail_v3_only_on_format_and_pass_v5(tmp_path):
    old = run_check(WFR_V3, WFR_FIXTURE, CODEX, tmp_path / "v3", EXTRA_FIELDS)
    assert old.returncode != 0 and "report status or required fields wrong" in old.stderr
    new = run_check(WFR_V5, WFR_FIXTURE, CODEX, tmp_path / "v5", EXTRA_FIELDS)
    assert new.returncode == 0, new.stderr


FORMAT_ONLY = {
    "heading-before-fields": lambda w: (w / "reports/green.md").write_text("# Final report\n\n" + (w / "reports/green.md").read_text()),
    "exit-code-words": replace("reports/red.md", "exit=1", "exit code 1"),
    "denied-none-period": replace("reports/green.md", "Denied: none", "Denied: none."),
    "denied-explained": replace("reports/guard.md", "host hard denial\n", "host hard denial (not retried)\n"),
    "extra-field-after-open": lambda w: (w / "reports/red.md").write_text((w / "reports/red.md").read_text()
                                                                          + "Note: offline replay only\n"),
}


@pytest.mark.parametrize("name", sorted(FORMAT_ONLY))
def test_v5_accepts_format_variants_of_real_output(name, tmp_path):
    result = run_check(WFR_V5, WFR_FIXTURE, CODEX, tmp_path, FORMAT_ONLY[name])
    assert result.returncode == 0, result.stderr


SUBSTANCE = {
    # (edit, scenario that must fail)
    "false-done": (chain(edit_json("result.json", lambda o: row(o, "red").update(status="DONE")),
                         replace("reports/red.md", "Status: PARTIAL", "Status: DONE")), "red"),
    "report-status-differs": (replace("reports/guard.md", "Status: PARTIAL", "Status: DONE"), "guard"),
    "lost-denial": (replace("reports/guard.md", "Denied: git push -u origin worktree-api: host hard denial",
                            "Denied: none"), "guard"),
    "invented-denial": (replace("reports/green.md", "Denied: none", "Denied: git push denied"), "green"),
    "wrong-exit": (replace("reports/red.md", "exit=1", "exit=0"), "red"),
    "lost-failed-test": (replace("reports/red.md", "failed: test_unicode.", "failed: none."), "red"),
    "baseline-missing": (lambda w: (w / "reports/red.md").write_text("\n".join(
        ln for ln in (w / "reports/red.md").read_text().splitlines() if not ln.startswith("Baseline needed:"))), "red"),
    "baseline-anchor-wrong": (replace("reports/red.md", "Baseline needed: 0000000000000000000000000000000000000000",
                                      "Baseline needed: HEAD"), "red"),
    "baseline-claimed-for-green": (replace("reports/green.md", "Denied: none",
                                           "Baseline needed: 1111111; test_format\nDenied: none"), "green"),
    "open-hidden": (lambda w: (w / "reports/guard.md").write_text("\n".join(
        ln if not ln.startswith("Open:") else "Open: none" for ln in (w / "reports/guard.md").read_text().splitlines())),
        "guard"),
    "push-claim-wrong": (replace("reports/guard.md", "(pushed: denied)", "(pushed: yes)"), "guard"),
    "duplicate-status": (lambda w: (w / "reports/guard.md").write_text((w / "reports/guard.md").read_text()
                                                                          + "Status: DONE\n"), "guard"),
    "missing-report": (lambda w: (w / "reports/green.md").unlink(), "green"),
}


@pytest.mark.parametrize("name", sorted(SUBSTANCE))
def test_v5_still_fails_wrong_substance_and_names_the_scenario(name, tmp_path):
    edit, scenario = SUBSTANCE[name]
    result = run_check(WFR_V5, WFR_FIXTURE, CODEX, tmp_path, edit)
    assert result.returncode != 0
    got = scenario_line(result)
    assert [r["id"] for r in got["scenarios"] if not r["passed"]] == [scenario], got
    assert got["passed"] == 2 and got["total"] == 3


@pytest.mark.parametrize("fault", ["changed-input", "extra-file"])
def test_v5_global_failures_fail_the_check_with_all_scenarios_passing(fault, tmp_path):
    def edit(work):
        if fault == "changed-input":
            (work / "receipts.json").write_text("{}")
        else:
            (work / "notes.md").write_text("extra")
    result = run_check(WFR_V5, WFR_FIXTURE, CODEX, tmp_path, edit)
    assert result.returncode != 0
    # check_entry turns an all-passed line with a failed exit into a failed single scenario (no extra credit).
    assert CE.scenario_result(result.stdout, False) == ({"passed": 0, "total": 1}, 0.0)


def test_v5_missing_result_fails_every_scenario(tmp_path):
    result = run_check(WFR_V5, WFR_FIXTURE, None, tmp_path)
    assert result.returncode != 0 and "missing" in result.stderr
    assert scenario_line(result)["passed"] == 0


# ------------------------------------------------------------------ parent-policy-v5 (parent cases)


PARENT_DATA = Path(__file__).parent / "data" / "rsi_parent_cases"
PARENT_CASES = ["parent-authority", "parent-merge-cleanup", "parent-repo-profile", "parent-safety-guards"]


@pytest.mark.parametrize("cid", PARENT_CASES)
def test_parent_cases_use_v5_with_v4_fixtures(cid):
    case, = R.load_cases([cid])
    assert case.hidden_check.name == "parent-policy-v5" and case.fixture.name == cid + "-v4"


@pytest.mark.parametrize("cid", PARENT_CASES)
def test_parent_v5_grades_each_row_and_agrees_with_v4_on_pass_fail(cid, tmp_path):
    case, = R.load_cases([cid])
    good = PARENT_DATA / (cid + "-v4") / "good"
    ids = [r["id"] for r in json.loads((good / "result.json").read_text())["scenarios"]]
    ok = run_check(case.hidden_check, case.fixture, good, tmp_path / "ok")
    assert ok.returncode == 0 and scenario_line(ok)["passed"] == len(ids)
    for i, ident in enumerate(ids):
        broken = edit_json("result.json", lambda o, ident=ident: row(o, ident).update(id_extra="unrequested"))
        v4 = run_check(BENCH / "checks" / "parent-policy-v4", case.fixture, good, tmp_path / f"v4-{i}", broken)
        v5 = run_check(case.hidden_check, case.fixture, good, tmp_path / f"v5-{i}", broken)
        assert v4.returncode != 0 and v5.returncode != 0 and "incorrect decision" in v5.stderr
        got = scenario_line(v5)
        assert [r["id"] for r in got["scenarios"] if not r["passed"]] == [ident]
        assert got["passed"] == len(ids) - 1


# ------------------------------------------------------------------ pepe-lock-v2


PEPE_FIXTURE = BENCH / "fixtures" / "pepe-lock"
PEPE_V1 = BENCH / "checks" / "pepe-lock"
PEPE_V2 = BENCH / "checks" / "pepe-lock-v2"
PEPE_REAL = DATA / "pepe-lock" / "codex-B-r1"


def test_pepe_case_uses_v2():
    case, = R.load_cases(["pepe-lock"])
    assert case.hidden_check == PEPE_V2


def test_real_caveat_line_failed_v1_only_on_format_and_passes_v2(tmp_path):
    old = run_check(PEPE_V1, PEPE_FIXTURE, PEPE_REAL, tmp_path / "v1")
    assert old.returncode != 0 and "names no idea" in old.stderr and "suggestions" in old.stderr
    new = run_check(PEPE_V2, PEPE_FIXTURE, PEPE_REAL, tmp_path / "v2")
    assert new.returncode == 0 and new.stdout.strip() == "ok", new.stderr


PEPE_SUBSTANCE = {
    "invented-idea-bullet": (replace("docs/ideas/parked.md", "- **PR Weather**", "- **Coffee Bot** — revisit soon.\n- **PR Weather**", 1), "names no idea"),
    "parked-idea-missing": (replace("docs/ideas/parked.md", "- **Commit Haiku** — Dropped by the user; revisit only if the user explicitly reopens it.\n", ""), "every other session idea"),
    "locked-idea-parked": (replace("docs/ideas/parked.md", "- **PR Weather**", "- **Diff Diary** — revisit later.\n- **PR Weather**", 1), "locked idea is parked"),
    "no-revisit": (replace("docs/ideas/parked.md", "Revisit if morning review-queue triage becomes a priority.", "Maybe later."), "revisit trigger"),
    "wrong-lock": (edit_json("lock.json", lambda o: o["locked"].append("Flake Hunter")), "user's choice"),
    "numbered-invented-item": (replace("docs/ideas/parked.md", "Revisit triggers below", "1. Something else entirely.\n\nRevisit triggers below"), "names no idea"),
}


@pytest.mark.parametrize("name", sorted(PEPE_SUBSTANCE))
def test_pepe_v2_still_fails_wrong_substance_on_the_real_output(name, tmp_path):
    edit, why = PEPE_SUBSTANCE[name]
    result = run_check(PEPE_V2, PEPE_FIXTURE, PEPE_REAL, tmp_path, edit)
    assert result.returncode != 0 and why in result.stderr, result.stderr


# ------------------------------------------------------------------ anatomy-plan-v2


ANATOMY_FIXTURE = BENCH / "fixtures" / "anatomy-plan"
ANATOMY_V1 = BENCH / "checks" / "anatomy-plan"
ANATOMY_V2 = BENCH / "checks" / "anatomy-plan-v2"
ANATOMY_GOOD = Path(__file__).parent / "data" / "rsi_skill_cases" / "anatomy-plan" / "good"


def extra_group(work: Path) -> None:
    # Synthetic: no anatomy run in the two cited nights. The readout parts get their own group label.
    p = work / "plan.json"
    plan = json.loads(p.read_text())
    for part in plan["parts"]:
        if part["name"] in {"readout window", "label plate"}:
            part["group"] = "readout"
    plan["parts"].append({"name": "readout bezel", "group": "readout"})
    p.write_text(json.dumps(plan))


def test_anatomy_case_uses_v2():
    case, = R.load_cases(["anatomy-plan"])
    assert case.hidden_check == ANATOMY_V2


def test_extra_part_group_fails_v1_only_on_format_and_passes_v2(tmp_path):
    old = run_check(ANATOMY_V1, ANATOMY_FIXTURE, ANATOMY_GOOD, tmp_path / "v1", extra_group)
    assert old.returncode != 0 and "want exactly" in old.stderr
    new = run_check(ANATOMY_V2, ANATOMY_FIXTURE, ANATOMY_GOOD, tmp_path / "v2", extra_group)
    assert new.returncode == 0 and new.stdout.strip() == "ok", new.stderr


ANATOMY_SUBSTANCE = {
    "group-missing": (lambda p: [x.update(group="readout") for x in p["parts"] if x["group"] == "controls"], "missing"),
    "lit-part-outside-subject": (lambda p: p.update(lit_part="readout bezel"), "subject group"),
    "too-many-parts": (lambda p: p["parts"].extend({"name": f"extra rivet {i}", "group": "readout"} for i in range(40)),
                       "40-80 parts"),
}


@pytest.mark.parametrize("name", sorted(ANATOMY_SUBSTANCE))
def test_anatomy_v2_still_fails_wrong_substance_with_extra_groups(name, tmp_path):
    change, why = ANATOMY_SUBSTANCE[name]
    result = run_check(ANATOMY_V2, ANATOMY_FIXTURE, ANATOMY_GOOD, tmp_path,
                       chain(extra_group, edit_json("plan.json", change)))
    assert result.returncode != 0 and why in result.stderr, result.stderr


# ------------------------------------------------------------------ history stays readable


@pytest.mark.parametrize("old", ["parent-policy", "parent-policy-v4", "pepe-lock", "anatomy-plan", "worker-behavior"])
def test_previous_checker_versions_stay_in_place(old):
    assert (BENCH / "checks" / old / "check.py").is_file()
