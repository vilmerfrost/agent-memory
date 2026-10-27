"""rsi_replay: cases, deterministic ids, blinding, scoring, Jev, the runner's refusals and its records.

The runner tests use `HostSandbox` (tests/rsi_replay_support.py): the real Sandbox with `run` executing the same
stub-agent and check commands on the host. sandbox/tests/test_replay_docker.py repeats the scenarios in Docker.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from rsi_replay_support import FakeBudget, HostSandbox, eval_problems, rs, setup_root

from agent_memory import rsi_replay as R
from agent_memory import rsi_verdict as V

EXP = "exp-20261017-01"


def replay(root: Path, h: str, cases: list[str], *, reps: int = 3, sandbox=None, **kw):
    sb = sandbox or HostSandbox(root)
    res = R.run_replay(h, R.load_cases(cases), R.ReplayConfig(reps=reps), root=root, experiment_id=kw.pop("exp", EXP),
                       sandbox=sb, **kw)
    return res, sb


def manifest(root: Path, h: str) -> dict:
    return json.loads((root / "artifacts" / h / "manifest.json").read_text())


def record(root: Path, h: str) -> dict:
    return json.loads((root / "artifacts" / h / "evals" / "sandbox-replay.json").read_text())


# ---------------------------------------------------------------- cases, ids, blinding


def test_shipped_cases_load_and_are_stable():
    cases = R.load_cases()
    assert [c.id for c in cases] == ["anatomy-plan", "docs-typo", "fix-add", "meltdown-brief", "parent-authority",
                                     "parent-repo-profile", "parent-safety-guards", "pepe-lock",
                                     "report-todos", "worker-final-report"]
    assert all(c.fixture.is_dir() and c.stub and c.check for c in cases)
    assert R.load_cases(["report-todos"])[0].hidden_check.name == "report-todos"
    assert cases[1].digest() == R.load_cases(["docs-typo"])[0].digest()
    with pytest.raises(R.ReplayError, match="unknown case"):
        R.load_cases(["nope"])
    scen = json.loads((R.BENCH_DIR / "scenarios.json").read_text())["scenarios"]
    assert {s["expect"] for s in scen} == {"IMPROVES", "NO_EFFECT", "WORSE"}
    assert all((R.BENCH_DIR / "candidates" / s["candidate"]).is_dir() for s in scen)


def test_run_ids_are_deterministic_and_fit_the_sandbox():
    assert R.run_id_for(EXP, "fix-add", "B", 2) == "exp-20261017-01-fix-add-B-r2"
    assert R.run_id_for(EXP, "fix-add", "B", 2, "chk") == "exp-20261017-01-fix-add-B-r2-chk"
    long_exp = "exp-20261017-" + "x" * 40
    a = R.run_id_for(long_exp, "report-todos", "A", 0, "chk")
    assert a == R.run_id_for(long_exp, "report-todos", "A", 0, "chk") and len(a) <= 64
    assert rs.ID_RE.match(a) and a != R.run_id_for(long_exp, "report-todos", "B", 0, "chk")
    s = R.session_id_for(EXP, "A", "fix-add", 0)
    assert s == R.session_id_for(EXP, "A", "fix-add", 0) and s != R.session_id_for(EXP, "B", "fix-add", 0)
    assert R.session_id_for("free-form", "A", "fix-add", 0) is None


def test_blind_order_is_seeded_deterministic_and_balanced():
    flips = [R.blind_flip(EXP, f"case-{i}", rep) for i in range(40) for rep in range(3)]
    assert flips == [R.blind_flip(EXP, f"case-{i}", rep) for i in range(40) for rep in range(3)]
    assert 30 < sum(flips) < 90                       # both orders happen
    assert flips != [R.blind_flip("exp-20261017-02", f"case-{i}", rep) for i in range(40) for rep in range(3)]


def test_judge_prompt_is_blind():
    a = {"check_passed": False, "final": "done", "diff": "--- a/x\n+++ b/x\n"}
    b = {"check_passed": True, "final": "done too", "diff": "--- a/y\n+++ b/y\n"}
    p = R.judge_prompt("Fix the bug.", a, b)
    for leak in ("arm", "Arm", "baseline", "candidate", "artifact", EXP, "A:", "B:"):
        assert leak not in p
    assert "Attempt 1" in p and "Attempt 2" in p and "untrusted data" in p
    assert R.stub_judge(a, b) == {"winner": "2", "reason": "only this attempt passed the automated check"}
    assert R.stub_judge(b, a)["winner"] == "1" and R.stub_judge(a, a)["winner"] == "tie"
    assert R.parse_judge_answer('noise {"winner": "1", "reason": "r"} tail') == {"winner": "1", "reason": "r"}
    with pytest.raises(ValueError):
        R.parse_judge_answer('{"winner": "A"}')


def test_tally_counts_only_already_verified_pairs_symmetrically():
    pairs = [{"case": "c", "rep": 0, "winner": "B"}, {"case": "c", "rep": 1, "winner": "B"},
             {"case": "c", "rep": 2, "winner": "A"}, {"case": "c", "rep": 3, "winner": None}]
    t = R.tally(pairs, "stub")
    assert (t.wins_b, t.unfired_b, t.wins_a, t.errors, t.judged) == (2, 0, 1, 1, 3)


def test_replay_jev_fixtures_match_their_states():
    """Every shipped replay fixture answers exactly the state it documents (its file name is that state's key).
    The key is `_replay_state`, not `_state`: tests/test_tasks_state.py reads `_state` as a routine state."""
    from agent_memory import config, jev
    qs = R.Q.load_file(R.QUESTIONS_FILE, "rsi-replay").jev_questions()
    versions: dict[str, int] = {}
    for p in sorted(config.jev_fixtures_dir().glob("*.json")):
        body = json.loads(p.read_text())
        if "_replay_state" in body:
            version = body["_replay_state"].splitlines()[0]
            versions[version] = versions.get(version, 0) + 1
            assert jev.fixture_key(body["_replay_state"], qs) == p.stem and "_state" not in body
            assert set(body["answers"]) == {"effect", "harness_problem", "evidence_strength"}
    # Keep the six historical V1 outcomes and six explicit V2 counterparts.
    assert versions == {"suite: sandbox-replay/1": 6, "suite: sandbox-replay/2": 6}


def test_jev_state_is_numbers_only_and_fixture_mode_never_goes_live(monkeypatch):
    m = V.ArmPair(cases=(V.CaseStats("report-todos", 3, 0, 3),), runs_expected=6, runs_done=6)
    j = V.JudgeResult(mode="stub", judged=3, wins_b=3)
    state = R.jev_state(m, j, R.ReplayConfig(reps=3))
    assert state == R.jev_state(m, j, R.ReplayConfig(reps=3))
    assert "A_pass=0 B_pass=3" in state and "sha" not in state and EXP not in state
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy")
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")          # live is possible, but fixture mode must not use it
    got = R.run_jev(state, "fixture")
    assert (got.status, got.effect, got.mode) == ("ok", "improves", "fixture")
    assert R.run_jev(state.replace("reps: 3", "reps: 9"), "fixture").status == "missing"
    monkeypatch.delenv("AMEM_JEV_LIVE")
    assert R.run_jev(state, "live").status == "unavailable"
    assert R.run_jev(state, "none").status == "unavailable"


# ---------------------------------------------------------------- the runner (host double)


@pytest.mark.parametrize(("candidate", "case", "verdict", "eval_verdict", "a_pass", "b_pass"), [
    ("closing-line", "report-todos", "IMPROVES", "improves", 0, 3),
    ("closing-line", "fix-add", "NO_EFFECT", "no-effect", 3, 3),
    ("no-edit-existing", "docs-typo", "WORSE", "worse", 3, 0),
])
def test_replay_scenarios_and_their_records(tmp_path, monkeypatch, candidate, case, verdict, eval_verdict, a_pass,
                                            b_pass):
    root, bid, h = setup_root(tmp_path, monkeypatch, candidate)
    res, sb = replay(root, h, [case])
    assert res.verdict.verdict == verdict, res.verdict.reasons
    c = res.verdict.inputs["machine"]["cases"][0]
    assert (c["a_pass"], c["b_pass"], c["pairs"]) == (a_pass, b_pass, 3)
    assert res.jev.status == "ok" and res.judge.judged == 3 and res.budget_stop is None
    rec = record(root, h)
    assert (rec["verdict"], rec["artifact_hash"], rec["baseline_id"], rec["suite"]) == (eval_verdict, h, bid, R.SUITE)
    assert rec["results"]["policy"]["version"] == V.POLICY_FORMAT
    probs = eval_problems(root / "artifacts" / h, manifest(root, h), ["sandbox-replay"])
    assert (probs == []) is (verdict == "IMPROVES")
    # Same agent, limits and network for both arms; checks and stub runs have no network and no credentials.
    agent_calls = [x for x in sb.calls if x["arm"] in "AB"]
    assert {(x["network"], x["memory"], x["cpus"], x["pids"]) for x in agent_calls} == {("none", "1g", "1", 256)}
    assert all(x["credentials"] == [] for x in sb.calls)
    assert all("/mnt/check" not in x["mounts"] and "/mnt/hidden" not in x["mounts"] for x in agent_calls)
    assert {x["arm"] for x in sb.calls} == {"A", "B", "K"}
    # Deterministic run ids and evidence.
    assert sorted(r.run_id for r in res.runs) == sorted(R.run_id_for(EXP, case, a, k) for a in "AB" for k in range(3))
    ev = res.evidence_dir
    assert ev == root / "replays" / EXP and (ev / "replay.json").is_file() and (ev / "jev.json").is_file()
    assert len(list((ev / "runs").glob("*.json"))) == 6 and len(list((ev / "judge").glob("*.json"))) == 3
    side = json.loads((sb.experiment_dir(EXP) / "logs" / f"{R.run_id_for(EXP, case, 'B', 0)}.rsi.json").read_text())
    assert side["case"] == case and side["rep"] == 0 and side["eval_verdict"] == eval_verdict
    # Arm B's stub read the artifact's after bytes, arm A the baseline bytes.
    f = manifest(root, h)["files"][0]
    seen = {r.arm: {i["path"]: i["sha256"] for i in r.instructions} for r in res.runs}
    assert seen["B"][f["path"]] == f["after_sha256"] and seen["A"][f["path"]] == f["before_sha256"]
    assert all(r.tokens_in == r.tokens_out == 0 and r.cost_usd == 0 for r in res.runs)
    assert all(r.tool_calls and r.duration_s >= 0 and r.exit_code == 0 for r in res.runs)


def test_mixed_replay_over_all_cases(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    res, _ = replay(root, h, ["docs-typo", "fix-add", "report-todos"], reps=1)
    assert res.verdict.verdict == "IMPROVES" and res.jev.effect == "improves"
    assert res.judge.wins_b == 1 and res.judge.ties == 2


def test_replay_is_reproducible(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    first, _ = replay(root, h, ["report-todos"])
    second, _ = replay(root, h, ["report-todos"])

    def strip(r):
        return [{k: v for k, v in x.to_json().items() if not k.endswith("duration_s")} for x in r.runs]
    assert strip(first) == strip(second)
    assert first.verdict.to_json() == second.verdict.to_json()
    assert [p["order"] for p in first.judge.pairs] == [p["order"] for p in second.judge.pairs]


def test_stale_baseline_is_refused(tmp_path, monkeypatch):
    root, bid, h = setup_root(tmp_path, monkeypatch, "closing-line")
    res, sb = replay(root, h, ["report-todos"], baseline_id="bl-000000000000")
    assert res.verdict.verdict == "INCONCLUSIVE" and res.verdict.reasons[0].startswith("stale-baseline")
    assert sb.calls == [] and res.eval_path is None
    # A baseline copy that changed after the artifact was staged is stale too.
    copy = root / "baselines" / bid / "files" / ".claude" / "CLAUDE.md"
    copy.chmod(0o644)
    copy.write_text("changed\n")
    res, sb = replay(root, h, ["report-todos"])
    assert res.verdict.verdict == "INCONCLUSIVE" and "stale-baseline" in res.verdict.reasons[0] and sb.calls == []


def test_non_exact_artifact_is_never_recorded_as_passing(tmp_path, monkeypatch):
    bench = tmp_path / "bench"
    shutil.copytree(R.BENCH_DIR, bench)
    (bench / "home" / ".claude" / "settings.json").write_text('{"model": "sonnet"}\n')
    hooky = bench / "candidates" / "hooky" / ".claude"
    hooky.mkdir(parents=True)
    (hooky / "CLAUDE.md").write_text((bench / "home" / ".claude" / "CLAUDE.md").read_text())
    (hooky / "settings.json").write_text('{"model": "sonnet", "hooks": {"Stop": []}}\n')
    root, _, h = setup_root(tmp_path, monkeypatch, "hooky", bench=bench)
    evals = root / "artifacts" / h / "evals"
    (evals / "sandbox-replay.json").write_text(json.dumps({"format": "rsi-eval/1", "verdict": "improves"}))
    from dataclasses import replace
    case = R.load_cases(["report-todos"])[0]
    case = replace(case, targets=(*case.targets, {"path": "~/.claude/settings.json", "kind": "settings",
                    "behavior": "Exercise the staged settings during the report task."}))
    sb = HostSandbox(root)
    res = R.run_replay(h, [case], R.ReplayConfig(), root=root, experiment_id=EXP, sandbox=sb)
    assert res.verdict.verdict == "INCONCLUSIVE" and res.verdict.reasons[0].startswith("artifact-not-exact")
    assert sb.calls == [] and res.eval_path is None
    assert not (evals / "sandbox-replay.json").exists()            # the old passing record was moved aside
    assert len(list(evals.glob("sandbox-replay.json.superseded-*"))) == 1


def test_experiment_mismatch_leaves_an_existing_record_alone(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    replay(root, h, ["report-todos"])
    before = record(root, h)
    res, sb = replay(root, h, ["report-todos"], exp="exp-20261017-99")
    assert res.verdict.verdict == "INCONCLUSIVE" and res.verdict.reasons[0].startswith("experiment-mismatch")
    assert sb.calls == [] and record(root, h) == before and res.eval_path is None
    monkeypatch.setattr(rs, "docker_available", lambda: False)          # a host problem, not the artifact's
    res, sb = replay(root, h, ["report-todos"])
    assert res.verdict.reasons == ("sandbox-unavailable: docker or images/lock.json missing",)
    assert sb.calls == [] and record(root, h) == before and res.eval_path is None


def test_run_errors_and_timeouts_are_inconclusive(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    bad = R.run_id_for(EXP, "report-todos", "B", 1)
    sb = HostSandbox(root, fault=lambda rid, arm: {"exit_code": 137, "timed_out": True} if rid == bad else None)
    res, _ = replay(root, h, ["report-todos"], sandbox=sb)
    assert res.verdict.verdict == "INCONCLUSIVE"
    assert res.verdict.reasons[0] == "run-errors:1>0" and f"{bad}: timeout" in res.verdict.reasons
    assert record(root, h)["verdict"] == "inconclusive"
    assert eval_problems(root / "artifacts" / h, manifest(root, h), ["sandbox-replay"]) != []
    # An agent that crashes (stub `fail` op) is an error too, never a WORSE.
    bench = tmp_path / "bench2"
    shutil.copytree(R.BENCH_DIR, bench)
    case = json.loads((bench / "cases" / "fix-add.json").read_text())
    case["stub"] = [{"op": "fail", "message": "boom"}]
    (bench / "cases" / "fix-add.json").write_text(json.dumps(case))
    res = R.run_replay(h, R.load_cases(["fix-add"], bench=bench), R.ReplayConfig(reps=3), root=root,
                       experiment_id=EXP, sandbox=HostSandbox(root))
    assert res.verdict.verdict == "INCONCLUSIVE" and "agent exit 1" in " ".join(res.verdict.reasons)


def test_budget_stop_is_clean(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    budget = FakeBudget(max_runs=5, remaining=42)
    res, sb = replay(root, h, ["report-todos"], budget=budget)
    assert res.budget_stop == "sandbox_runs" and res.verdict.verdict == "INCONCLUSIVE"
    assert res.verdict.reasons == ("budget-exhausted:sandbox_runs",)
    assert len(budget.taken) == 5 and len(sb.calls) == 5         # nothing ran after the refusal
    assert [rid for rid, _ in budget.recorded] == budget.taken    # every run that started was recorded
    assert all(x["timeout_s"] <= 42 for x in sb.calls)           # remaining_s caps the timeout
    assert record(root, h)["verdict"] == "inconclusive"
    assert res.verdict.inputs["machine"]["pairs"] == 1           # the half pair does not count
    # A budget that refuses at once: no run at all, still a clean result.
    res, sb = replay(root, h, ["report-todos"], budget=FakeBudget(max_runs=0))
    assert res.budget_stop == "sandbox_runs" and sb.calls == []


def test_interrupt_records_inconclusive_and_reraises(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    replay(root, h, ["report-todos"])                            # a passing record exists first
    assert record(root, h)["verdict"] == "improves"
    stop = R.run_id_for(EXP, "report-todos", "A", 1)

    def fault(rid, arm):
        if rid == stop:
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        replay(root, h, ["report-todos"], sandbox=HostSandbox(root, fault=fault))
    assert record(root, h)["verdict"] == "inconclusive"
    rep = json.loads((root / "replays" / EXP / "replay.json").read_text())
    assert rep["verdict"]["verdict"] == "INCONCLUSIVE" and "interrupted: KeyboardInterrupt" in rep["errors"]


def _bench_with(tmp_path: Path, case_id: str, **changes) -> Path:
    bench = tmp_path / f"bench-{case_id}"
    shutil.copytree(R.BENCH_DIR, bench)
    p = bench / "cases" / f"{case_id}.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), **changes}))
    return bench


def test_arm_homes_are_prepared_once_and_reset_when_an_agent_changes_them(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    case = json.loads((R.BENCH_DIR / "cases" / "fix-add.json").read_text())
    bench = _bench_with(tmp_path, "fix-add", stub=[{"op": "run", "argv": ["sh", "-c", "echo x > $HOME/planted"]},
                                                   *case["stub"]])
    res = R.run_replay(h, R.load_cases(["fix-add"], bench=bench), R.ReplayConfig(reps=3), root=root,
                       experiment_id=EXP, sandbox=HostSandbox(root))
    assert res.verdict.verdict == "NO_EFFECT", res.verdict.reasons
    events = [json.loads(x) for x in (res.evidence_dir / "log.jsonl").read_text().splitlines()]
    assert [e["arms"] for e in events if e["event"] == "home-reset"] == [["A", "B"], ["A", "B"]]
    assert all(r.check_passed for r in res.runs)        # every pair started from a clean home and still worked
    # The unchanged stub homes of the plain bench are prepared exactly once.
    res, _ = replay(root, h, ["fix-add"])
    events = [json.loads(x) for x in (res.evidence_dir / "log.jsonl").read_text().splitlines()]
    assert not [e for e in events if e["event"] == "home-reset"]


def test_a_check_that_writes_into_its_home_is_not_trusted(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    case = json.loads((R.BENCH_DIR / "cases" / "fix-add.json").read_text())
    bench = _bench_with(tmp_path, "fix-add", check='touch "$HOME/.planted" && ' + case["check"])
    res = R.run_replay(h, R.load_cases(["fix-add"], bench=bench), R.ReplayConfig(reps=3), root=root,
                       experiment_id=EXP, sandbox=HostSandbox(root))
    assert res.verdict.verdict == "INCONCLUSIVE"
    assert all(r.error == "check-home-modified: .planted" for r in res.runs)
    assert list(rs.Sandbox(root).arm_home(EXP, "K").iterdir()) != [] and \
        not (rs.Sandbox(root).arm_home(EXP, "K") / ".planted").exists()


def test_parse_agent_reads_claude_stream_json_and_codex_events():
    claude = "\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                       "input": {"file_path": "calc.py"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "result": "fixed",
         "total_cost_usd": 0.0123, "modelUsage": {"claude-haiku-5-5": {"inputTokens": 900, "outputTokens": 120,
                                                                       "cacheReadInputTokens": 100,
                                                                       "costUSD": 0.0123}}}])
    p = R.parse_agent(claude, "claude", "claude-haiku-5-5")
    assert p["tool_calls"] == [{"name": "Edit", "input": '{"file_path": "calc.py"}',
                                "status": "unknown", "target_reads": []}]
    assert (p["tokens_in"], p["tokens_out"], p["cost_usd"], p["final"], p["is_error"]) == (1000, 120, 0.0123, "fixed",
                                                                                         False)
    codex = "\n".join(json.dumps(e) for e in [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 2000, "cached_input_tokens": 500, "output_tokens": 80}}])
    p = R.parse_agent(codex, "codex", "gpt-6-luna")
    assert p["tool_calls"] == [{"name": "command_execution", "input": "completed",
                                "status": "completed", "target_reads": []}] and p["final"] == "done"
    assert (p["tokens_in"], p["tokens_out"]) == (2000, 80)
    assert R.parse_agent("not json at all", "claude", None)["parsed"] is False


def test_live_agent_and_judge_plumbing_without_any_call(tmp_path, monkeypatch):
    """The live paths' command lines, credentials by name only, network and judge mapping. No CLI runs: the
    host double answers each agent and judge container with canned output (the CLIs stay unverified)."""
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    creds = tmp_path / "dummy-creds.txt"
    creds.write_text("ANTHROPIC_API_KEY=sk-ant-api03-DUMMY-NOT-A-KEY\nCODEX_API_KEY=sk-proj-DUMMY-NOT-A-KEY\n")
    claude_out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
                             "result": "ok", "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 5,
                                                                                 "costUSD": 0.001}}})
    claude_out = "\n".join([json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "read-global", "name": "Read",
         "input": {"file_path": "/home/rsi/.claude/CLAUDE.md"}}]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-global", "is_error": False, "content": "instructions"}]}}),
        claude_out])
    judge_out = "\n".join(json.dumps(e) for e in [
        {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": '{"winner": "2", "reason": "r"}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}}])

    def fault(rid, arm):
        if arm in "AB":
            return {"exit_code": 0, "stdout": claude_out}
        if arm == "J":
            return {"exit_code": 0, "stdout": judge_out}
        return None
    sb = HostSandbox(root, fault=fault)
    monkeypatch.setattr(sb, "net_status", lambda: {"internal": {"internal": True}, "proxy_running": True})
    budget = FakeBudget()
    cfg = R.ReplayConfig(agent="claude", model="claude-haiku-5-5", effort="low", reps=3, judge="codex",
                         credentials=f"env-file:{creds}", judge_credentials=f"env-file:{creds}",
                         judge_credential_name="CODEX_API_KEY")
    res = R.run_replay(h, R.load_cases(["report-todos"]), cfg, root=root, experiment_id=EXP, sandbox=sb, budget=budget)
    agent = [c for c in sb.calls if c["arm"] in "AB"]
    assert all(c["network"] == rs.NET_INTERNAL and c["credentials"] == ["ANTHROPIC_API_KEY", "CODEX_API_KEY"]
               for c in agent)
    cmd = agent[0]["command"]
    assert cmd[:3] == ["python3", "-I", "/mnt/agent/live_entry.py"] and "claude" in cmd
    for flag, value in (("--model", "claude-haiku-5-5"), ("--effort", "low"), ("--max-budget-usd", "0.25"),
                        ("--permission-mode", "dontAsk"), ("-p", "{PROMPT}")):
        assert cmd[cmd.index(flag) + 1] == value
    assert all(c["network"] == "none" and c["credentials"] == [] for c in sb.calls if c["arm"] == "K")
    judge = [c for c in sb.calls if c["arm"] == "J"]
    assert len(judge) == 3 and "codex" in judge[0]["command"] and "gpt-6-luna" in judge[0]["command"]
    logs = sb.experiment_dir(EXP) / "logs"
    for c in agent + judge:
        argv = json.loads((logs / f"{c['run_id']}.json").read_text())["argv"]
        assert "ANTHROPIC_API_KEY" in argv and not any("DUMMY" in a for a in argv)
    # "2" means the second attempt shown: B when the order was AB, A when it was BA.
    for p in res.judge.pairs:
        assert p["winner"] == ("B" if p["order"] == "AB" else "A")
    assert {s for _, s in budget.calls} == {"replay-judge"} and len(budget.calls) == 3
    assert res.arms["A"]["cost_usd"] == pytest.approx(0.003) and res.totals["tokens_out"] == 30


def test_stub_agent_never_gets_credentials():
    with pytest.raises(R.ReplayError, match="never gets credentials"):
        R.ReplayConfig(credentials="keychain:rsi-sandbox/anthropic").validate()
    with pytest.raises(R.ReplayError, match="agent must be"):
        R.ReplayConfig(agent="gpt").validate()


# ---------------------------------------------------------------- staging and CLI


def test_stage_synthetic_refuses_the_production_root(tmp_path):
    with pytest.raises(R.ReplayError, match="production sandbox root"):
        R.stage_synthetic(rs.DEFAULT_ROOT, EXP, "closing-line")
    a = R.stage_synthetic(tmp_path / "r", EXP, "closing-line")
    assert a == R.stage_synthetic(tmp_path / "r", EXP, "closing-line")      # idempotent
    m, h, _ = rs.load_artifact(tmp_path / "r" / "artifacts" / a[1])
    assert h == a[1] and m["baseline_id"] == a[0] and m["files"][0]["kind"] == "instruction"


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    out: list[str] = []
    assert R.main(["run", "--root", str(root), "--artifact", h, "--experiment", EXP, "--dry-run", "--json"],
                  out=out.append) == 0
    plan = json.loads(out[0])
    assert plan["dry_run"] and plan["artifact"]["ok"] and plan["agent_runs"] == 6 and plan["paid_calls"] is False
    assert plan["run_ids"][0] == R.run_id_for(EXP, "docs-typo", "A", 0)
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before
    monkeypatch.delenv("RSI_SANDBOX_DIR")
    assert R.main(["run", "--artifact", h, "--experiment", EXP, "--dry-run"], out=out.append) == 2   # no root
    out.clear()
    assert R.main(["cases"], out=out.append) == 0 and len(out) == len(R.load_cases())


def test_cli_run_with_host_double(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    monkeypatch.setattr(rs, "Sandbox", HostSandbox)
    out: list[str] = []
    code = R.main(["run", "--root", str(root), "--artifact", h, "--experiment", EXP, "--cases", "report-todos",
                   "--reps", "3", "--json"], out=out.append)
    assert code == 0 and json.loads(out[0])["verdict"]["verdict"] == "IMPROVES"
