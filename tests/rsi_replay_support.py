"""Test doubles for rsi_replay: a Sandbox that runs the worker command on the host instead of in Docker.

`HostSandbox.run` builds the real docker argv first (so the mount guard and every argument check still run),
then executes the same command on the host with each container path (/mnt/<x>, /work) mapped to the host dir
it would be mounted from, HOME = the arm's home. Only this repo's own stub agent and check entry run this way:
trusted code, no model, no network. The Docker suite (sandbox/tests/test_replay_docker.py) runs the same replays
in real worker containers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_memory import rsi_replay as R

rs = R.sandbox_module()

# agent-config lib/rsi_artifact.py PASSING and eval_problems, reimplemented here so the unit tests do not need a
# checkout of agent-config. sandbox/tests/test_replay_docker.py calls the real one when it is present.
PASSING = ("pass", "improves")


def eval_problems(adir: Path, manifest: dict, required: list[str]) -> list[str]:
    h = rs._sha(rs.canonical(manifest))
    probs = []
    for s in required:
        try:
            r = json.loads((Path(adir) / "evals" / f"{s}.json").read_text())
        except (OSError, ValueError):
            probs.append(f"required evaluation {s} missing")
            continue
        if r.get("format") != "rsi-eval/1" or r.get("suite") != s:
            probs.append(f"evaluation {s}: bad record")
        elif r.get("artifact_hash") != h:
            probs.append(f"evaluation {s} was run on another artifact")
        elif r.get("baseline_id") != manifest["baseline_id"]:
            probs.append(f"evaluation {s} used another baseline ({r.get('baseline_id')})")
        elif r.get("verdict") not in PASSING:
            probs.append(f"evaluation {s} verdict {r.get('verdict')}")
    return probs


class HostSandbox(rs.Sandbox):
    """rs.Sandbox whose `run` executes on the host (see module doc). `fault(run_id, arm)` may return a RunResult
    (a simulated timeout or crash) or raise (an interrupt) before the command runs."""

    def __init__(self, root: Path, fault: Callable[[str, str], Any] | None = None):
        super().__init__(root)
        self.fault = fault
        self.calls: list[dict] = []

    def run(self, experiment, arm, command, *, timeout_s=rs.DEFAULT_TIMEOUT_S, memory="2g", cpus="2", pids=512,
            credentials="none", credential_name=None, ro_mounts=(), run_id=None, max_output=1 << 20,
            network=rs.NET_INTERNAL):
        exp_dir = self.experiment_dir(experiment)
        run_dir, log_dir = exp_dir / "runs" / run_id, exp_dir / "logs"
        for d in (exp_dir / "runs", run_dir, log_dir):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        creds = rs.load_credentials(credentials, credential_name)
        argv, name, _ = self.build_run_argv(experiment=experiment, arm=arm, run_id=run_id, command=list(command),
                                            image="sha256:host-double", timeout_s=timeout_s, memory=memory, cpus=cpus,
                                            pids=pids, credential_names=sorted(creds), ro_mounts=list(ro_mounts),
                                            network=network)
        self.calls.append({"run_id": run_id, "arm": arm, "network": network, "timeout_s": timeout_s,
                           "memory": memory, "cpus": cpus, "pids": pids, "credentials": sorted(creds),
                           "mounts": sorted(t for _, t in ro_mounts), "command": list(command)})
        if self.fault is not None:
            res = self.fault(run_id, arm)
            if res is not None:
                return self._result(experiment, arm, run_id, name, argv, run_dir, log_dir, **res)
        mapping = {t: Path(s) for s, t in ro_mounts}
        mapping["/work"] = run_dir

        def host(arg: str) -> str:
            for t in sorted(mapping, key=len, reverse=True):
                if arg == t or arg.startswith(t + "/"):
                    return str(mapping[t]) + arg[len(t):]
            return arg

        cmd = [sys.executable if c == "python3" else host(c) for c in command]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.arm_home(experiment, arm)),
               "LANG": "C.UTF-8", "EXPERIMENT_ID": experiment, "RSI_ARM": arm}
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, cwd=run_dir, env=env, capture_output=True, text=True, timeout=timeout_s,
                               stdin=subprocess.DEVNULL)
            code, out, err, timed_out = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as e:
            code, out, err, timed_out = 137, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), "", True
        return self._result(experiment, arm, run_id, name, argv, run_dir, log_dir, exit_code=code, stdout=out,
                            stderr=err, timed_out=timed_out, duration_s=round(time.monotonic() - t0, 2))

    def _result(self, experiment, arm, run_id, name, argv, run_dir, log_dir, *, exit_code, stdout="", stderr="",
                timed_out=False, duration_s=0.01):
        (log_dir / f"{run_id}.stdout").write_text(stdout)
        (log_dir / f"{run_id}.stderr").write_text(stderr)
        res = rs.RunResult(experiment=experiment, arm=arm, run_id=run_id, container=name, exit_code=exit_code,
                           timed_out=timed_out, duration_s=duration_s, stdout=stdout, stderr=stderr,
                           run_dir=str(run_dir), log_dir=str(log_dir), argv=argv)
        (log_dir / f"{run_id}.json").write_text(json.dumps(res.to_json(max_output=0), indent=2) + "\n")
        return res


class FakeBudget:
    """The rsi_budget.Budget surface run_replay uses. Refuses the run after `max_runs` runs."""

    class BudgetExhausted(RuntimeError):
        def __init__(self, kind: str):
            super().__init__(kind)
            self.kind = kind

    def __init__(self, max_runs: int = 1000, remaining: float = 10_000.0):
        self.max_runs, self.remaining = max_runs, remaining
        self.taken: list[str] = []
        self.recorded: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, str]] = []

    def take_sandbox_run(self, run_id: str) -> None:
        if len(self.taken) >= self.max_runs:
            raise FakeBudget.BudgetExhausted("sandbox_runs")
        self.taken.append(run_id)

    def record_run(self, run_id: str, **kw: Any) -> None:
        self.recorded.append((run_id, kw))

    def remaining_s(self) -> float:
        return self.remaining

    def take_call(self, model: str, stage: str) -> None:
        self.calls.append((model, stage))

    def record_call(self, model: str, **kw: Any) -> None:
        pass


def setup_root(tmp_path: Path, monkeypatch, candidate: str = "closing-line", experiment: str = "exp-20261017-01",
               bench: Path | None = None) -> tuple[Path, str, str]:
    """A temp RSI sandbox root (also $RSI_SANDBOX_DIR) with a synthetic baseline + artifact. Docker is faked."""
    root = tmp_path / "rsi"
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(root))
    monkeypatch.setattr(rs, "docker_available", lambda: True)
    monkeypatch.setattr(rs, "load_lock", lambda: {"worker": {"id": "sha256:host-double"}})
    rs.Sandbox(root).init()
    bid, h = R.stage_synthetic(root, experiment, candidate, bench=bench)
    return root, bid, h
