"""Sandbox replay in real worker containers (Mission 005): the stub agent, baseline vs candidate, in a temp
RSI_SANDBOX_DIR. Never ~/sandbox, never a credential, never a network (the stub and the checks run with
`--network none`), never a paid call (judge stub, Jev fixture mode).

Run:  env AMEM_NO_DOTENV=1 uv run --quiet pytest sandbox/tests/test_replay_docker.py -s -rA -p no:cacheprovider
Needs Docker and `rsi_sandbox.py build` (images/lock.json); skips otherwise.

The eval record is checked with agent-config's own `lib/rsi_artifact.py eval_problems` (loaded read-only from
~/agent-config) when that checkout exists, and always with the same check reimplemented below.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

os.environ["AMEM_NO_DOTENV"] = "1"                      # agent_memory never reads an env file here
for _k in ("AMEM_JEV_LIVE", "TYPESAFE_API_KEY", "AMEM_JEV_FIXTURES_DIR", "AMEM_QUESTIONS_DIR"):
    os.environ.pop(_k, None)

import rsi_sandbox as rs  # noqa: E402

from agent_memory import rsi_replay as R  # noqa: E402

DOCKER_OK = rs.docker_available() and rs.load_lock() is not None
pytestmark = pytest.mark.skipif(not DOCKER_OK, reason="docker or images/lock.json unavailable")
AGENT_CONFIG_LIB = rs.REAL_HOME / "agent-config" / "lib" / "rsi_artifact.py"
SCENARIOS = {s["name"]: s for s in json.loads((R.BENCH_DIR / "scenarios.json").read_text())["scenarios"]}


def local_eval_problems(adir: Path, manifest: dict, required: list[str]) -> list[str]:
    """agent-config lib/rsi_artifact.py eval_problems, reimplemented (same rules, same order)."""
    h = rs._sha(rs.canonical(manifest))
    probs = []
    for s in required:
        try:
            r = json.loads((adir / "evals" / f"{s}.json").read_text())
        except (OSError, ValueError):
            probs.append(f"required evaluation {s} missing")
            continue
        if r.get("format") != "rsi-eval/1" or r.get("suite") != s:
            probs.append(f"evaluation {s}: bad record")
        elif r.get("artifact_hash") != h:
            probs.append(f"evaluation {s} was run on another artifact")
        elif r.get("baseline_id") != manifest["baseline_id"]:
            probs.append(f"evaluation {s} used another baseline")
        elif r.get("verdict") not in ("pass", "improves"):
            probs.append(f"evaluation {s} verdict {r.get('verdict')}")
    return probs


def agent_config_eval_problems():
    """agent-config's real eval_problems, read-only, or None when there is no checkout."""
    if not AGENT_CONFIG_LIB.is_file():
        return None
    spec = importlib.util.spec_from_file_location("agent_config_rsi_artifact", AGENT_CONFIG_LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.eval_problems


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "rsi"
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(root))
    rs.Sandbox(root).init()
    assert not R.sandbox_module().Sandbox().root.is_relative_to(rs.DEFAULT_ROOT)
    yield root


def no_containers_left(experiment: str) -> bool:
    out = rs.docker("ps", "-aq", "--filter", f"label=rsi.experiment={experiment}", check=False).stdout
    return out.strip() == ""


@pytest.mark.parametrize("name", ["improves", "no-effect", "worse"])
def test_docker_replay_scenario(root, name, evidence):
    s = SCENARIOS[name]
    exp = f"exp-20261017-dock-{name}"
    bid, h = R.stage_synthetic(root, exp, s["candidate"])
    res = R.run_replay(h, R.load_cases(s["cases"]), R.ReplayConfig(reps=3), root=root, experiment_id=exp)
    assert res.verdict.verdict == s["expect"], res.verdict.reasons
    assert res.judge.judged == 3 and res.jev.status == "ok" and not res.budget_stop

    adir = root / "artifacts" / h
    manifest = json.loads((adir / "manifest.json").read_text())
    rec = json.loads((adir / "evals" / "sandbox-replay.json").read_text())
    assert (rec["artifact_hash"], rec["baseline_id"], rec["experiment_id"]) == (h, bid, exp)
    assert rec["verdict"] == R.V.EVAL_VERDICT[s["expect"]]
    local = local_eval_problems(adir, manifest, ["sandbox-replay"])
    assert (local == []) is (s["expect"] == "IMPROVES")
    real = agent_config_eval_problems()
    if real is not None:
        got = real(adir, manifest, ["sandbox-replay"])
        assert (got == []) is (s["expect"] == "IMPROVES"), got
        assert (got == []) == (local == [])

    # Real containers: the runner's own logs hold the docker argv of every run.
    lock = rs.load_lock()
    logs = rs.Sandbox(root).experiment_dir(exp) / "logs"
    for run in res.runs:
        for rid in (run.run_id, run.check_run_id):
            meta = json.loads((logs / f"{rid}.json").read_text())
            argv = meta["argv"]
            assert argv[:2] == ["docker", "run"] and lock["worker"]["id"] in argv
            assert argv[argv.index("--network") + 1] == "none" and "--read-only" in argv
            assert not any(a in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY") for a in argv)
    f = manifest["files"][0]
    seen = {r.arm: {i["path"]: i["sha256"] for i in r.instructions} for r in res.runs}
    assert seen["A"][f["path"]] == f["before_sha256"] and seen["B"][f["path"]] == f["after_sha256"]
    assert no_containers_left(exp)
    assert not (rs.DEFAULT_ROOT / "experiments" / exp).exists() and not (rs.DEFAULT_ROOT / "replays" / exp).exists()
    c = res.verdict.inputs["machine"]["cases"][0]
    evidence(f"{name}: {res.verdict.verdict} ({'; '.join(res.verdict.reasons)}); case {c['case']} A {c['a_pass']}/3 "
             f"B {c['b_pass']}/3; judge A{res.judge.wins_a} B{res.judge.wins_b} tie{res.judge.ties}; "
             f"jev {res.jev.effect}; record {rec['verdict']}; eval_problems "
             f"{'agent-config ' if real else ''}{local or 'none'}; runs {res.totals['runs']} agent + "
             f"{res.totals['check_runs']} check, {res.totals['duration_s']}s")


def test_docker_agent_timeout_is_inconclusive(root, tmp_path, evidence):
    bench = tmp_path / "bench"
    shutil.copytree(R.BENCH_DIR, bench)
    case = json.loads((bench / "cases" / "fix-add.json").read_text())
    case["timeout_s"] = 5
    case["stub"] = [{"op": "sleep", "s": 60}]
    (bench / "cases" / "fix-add.json").write_text(json.dumps(case))
    exp = "exp-20261017-dock-timeout"
    _, h = R.stage_synthetic(root, exp, "closing-line")
    res = R.run_replay(h, R.load_cases(["fix-add"], bench=bench), R.ReplayConfig(reps=3), root=root,
                       experiment_id=exp)
    assert res.verdict.verdict == "INCONCLUSIVE" and res.verdict.reasons[0] == "run-errors:6>0"
    assert all(r.timed_out and r.error == "timeout" and r.check_run_id is None for r in res.runs)
    rec = json.loads((root / "artifacts" / h / "evals" / "sandbox-replay.json").read_text())
    assert rec["verdict"] == "inconclusive" and no_containers_left(exp)
    evidence(f"6 agent runs killed at 5 s: {res.verdict.verdict}, record {rec['verdict']}, no container left")


def test_docker_stub_run_has_no_network(root, tmp_path, evidence):
    sb = rs.Sandbox(root)
    exp = "exp-20261017-dock-net"
    home = sb.arm_home(exp, "A")
    home.mkdir(parents=True)
    # Every new netns also gets the kernel's fallback tunnel devices (tunl0, gre0, sit0, ...); they stay down.
    probe = ("import json, os, socket\n"
             "up = sorted(n for n in os.listdir('/sys/class/net') if os.path.isdir(f'/sys/class/net/{n}')\n"
             "            and open(f'/sys/class/net/{n}/operstate').read().strip() != 'down')\n"
             "routes = open('/proc/net/route').read().splitlines()[1:]\n"
             "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=3); net = 'CONNECTED'\n"
             "except OSError as e:\n    net = e.errno\n"
             "print(json.dumps({'not_down': up, 'routes': routes, 'connect_errno': net}))\n")
    r = sb.run(exp, "A", ["python3", "-c", probe], timeout_s=60, network="none", run_id=f"{exp}-probe")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.exit_code == 0 and out["not_down"] == ["lo"] and out["routes"] == []
    assert out["connect_errno"] == 101                     # ENETUNREACH: no route at all
    creds = tmp_path / "dummy-creds.txt"
    creds.write_text("ANTHROPIC_API_KEY=sk-ant-api03-DUMMY-NOT-A-KEY\n")
    with pytest.raises(rs.CredentialError, match="no credentials"):
        sb.run(exp, "A", ["true"], network="none", credentials=f"env-file:{creds}", run_id=f"{exp}-creds")
    assert not (sb.experiment_dir(exp) / "runs" / f"{exp}-creds").exists()
    evidence(f"--network none worker: up {out['not_down']}, routes {out['routes']}, connect 1.1.1.1:443 errno "
             f"{out['connect_errno']}; credentials refused")


def test_docker_rerun_same_experiment_is_reproducible(root, evidence):
    """A second replay of the same experiment reuses its deterministic run ids, homes and paths (the case Docker
    Desktop's file-sharing cache breaks without rsi_replay.SETTLE_S and fresh staging paths)."""
    s = SCENARIOS["improves"]
    exp = "exp-20261017-dock-rerun"
    _, h = R.stage_synthetic(root, exp, s["candidate"])
    cases = R.load_cases(s["cases"])
    first = R.run_replay(h, cases, R.ReplayConfig(reps=3), root=root, experiment_id=exp)
    second = R.run_replay(h, cases, R.ReplayConfig(reps=3), root=root, experiment_id=exp)

    def view(res):
        return [(r.run_id, r.exit_code, r.check_passed, r.error, r.diff, [i["sha256"] for i in r.instructions])
                for r in res.runs]
    assert first.verdict.verdict == second.verdict.verdict == "IMPROVES", (first.verdict.reasons, second.verdict.reasons)
    assert view(first) == view(second)
    assert [p["order"] for p in first.judge.pairs] == [p["order"] for p in second.judge.pairs]
    evidence(f"two replays of {exp}: {first.verdict.verdict} twice, {len(first.runs)} identical runs "
             f"(exit, check, diff, instruction sha)")
