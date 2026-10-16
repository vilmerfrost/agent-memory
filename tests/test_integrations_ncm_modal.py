"""Mission 004C: deploy/ncm/modal_sudoku.py loaded with a stand-in `modal` module (modal is not installed here).

Nothing contacts Modal, a GPU or the network: the stand-in records what the script asks of Modal, and the
container's commands (`nvidia-smi`, `sha256sum`, `evaluate.py`, ...) are faked through the script's `_run`.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from agent_memory.integrations import ncm, ncm_worker as W

EXP = "exp-20261020-ncm"
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "ncm" / "modal_sudoku.py"
WORKER = ROOT / "src" / "agent_memory" / "integrations" / "ncm_worker.py"


class _Image:
    def __init__(self) -> None:
        self.layers: list[tuple[str, tuple[Any, ...]]] = []

    @classmethod
    def debian_slim(cls, **kw: Any) -> _Image:
        img = cls()
        img.layers.append(("debian_slim", tuple(kw.items())))
        return img

    def _layer(self, name: str, *args: Any) -> _Image:
        self.layers.append((name, args))
        return self

    def apt_install(self, *a: Any) -> _Image:
        return self._layer("apt_install", *a)

    def pip_install(self, *a: Any) -> _Image:
        return self._layer("pip_install", *a)

    def run_commands(self, *a: Any) -> _Image:
        return self._layer("run_commands", *a)

    def add_local_file(self, *a: Any, **kw: Any) -> _Image:
        return self._layer("add_local_file", *a)


class _Function:
    """What `@app.function(...)` returns: `.local()` runs the body here; `.remote()` must be set by a test."""

    def __init__(self, fn: Any, kw: dict[str, Any]) -> None:
        self.fn, self.kw = fn, kw

    def local(self, *a: Any, **kw: Any) -> Any:
        return self.fn(*a, **kw)

    def remote(self, *a: Any, **kw: Any) -> Any:
        raise AssertionError("a test must stub evaluate.remote")


class _App:
    def __init__(self, name: str) -> None:
        self.name = name

    def function(self, **kw: Any) -> Any:
        return lambda fn: _Function(fn, kw)

    def local_entrypoint(self) -> Any:
        return lambda fn: fn


class _Volume:
    def __init__(self) -> None:
        self.commits = 0

    @classmethod
    def from_name(cls, name: str, **kw: Any) -> _Volume:
        return cls()

    def commit(self) -> None:
        self.commits += 1


def fake_modal(local: bool) -> types.ModuleType:
    mod = types.ModuleType("modal")
    mod.Image, mod.App, mod.Volume = _Image, _App, _Volume          # type: ignore[attr-defined]
    mod.is_local = lambda: local                                    # type: ignore[attr-defined]
    return mod


def load_script(monkeypatch: pytest.MonkeyPatch, *, local: bool = True, as_file: str | None = None) -> Any:
    """Execute the script as Modal would: from the checkout (local) or as /root/modal_sudoku.py (container)."""
    monkeypatch.setitem(sys.modules, "modal", fake_modal(local))
    mod = types.ModuleType("modal_sudoku_under_test")
    mod.__file__ = as_file or str(SCRIPT)
    exec(compile(SCRIPT.read_text(), mod.__file__, "exec"), mod.__dict__)
    return mod


# ---------------------------------------------------------------- F1: the container import


def test_the_script_imports_in_the_container_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`modal run` mounts the script alone at /root/modal_sudoku.py (two parents) and the worker at
    /root/ncm_worker.py. The import must not need the repo layout there."""
    real_spec = importlib.util.spec_from_file_location
    asked: list[str] = []

    def spec_from_file_location(name: str, location: Any, *a: Any, **kw: Any) -> Any:
        asked.append(str(location))
        if str(location) == "/root/ncm_worker.py":                    # the container mount, served from the repo
            location = WORKER
        return real_spec(name, location, *a, **kw)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", spec_from_file_location)
    mod = load_script(monkeypatch, local=False, as_file="/root/modal_sudoku.py")
    assert asked == ["/root/ncm_worker.py"]
    assert mod.W.REPO_COMMIT == W.REPO_COMMIT
    assert not any(name == "add_local_file" for name, _ in mod.image.layers)   # only the local side adds it


def test_the_worker_path_from_the_checkout_and_from_the_container(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_script(monkeypatch)
    assert mod._worker_path(str(SCRIPT)) == WORKER
    assert mod._worker_path("/root/modal_sudoku.py") == Path("/root/ncm_worker.py")
    assert mod._worker_path("/modal_sudoku.py") == Path("/root/ncm_worker.py")
    assert ("add_local_file", (WORKER, "/root/ncm_worker.py")) in mod.image.layers


# ---------------------------------------------------------------- F2: what the GPU function asks of Modal


def test_the_gpu_function_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    kw = load_script(monkeypatch).evaluate.kw
    assert {k: kw[k] for k in ("gpu", "region", "timeout", "startup_timeout", "scaledown_window", "max_containers",
                               "retries", "block_network")} == {
        "gpu": "L4", "region": "eu", "timeout": 600, "startup_timeout": 120, "scaledown_window": 2,
        "max_containers": 1, "retries": 0, "block_network": True}


# ---------------------------------------------------------------- F3: the local entrypoint always leaves a result


def test_a_raising_modal_call_still_writes_a_failed_results_file(monkeypatch: pytest.MonkeyPatch,
                                                                  tmp_path: Path) -> None:
    mod = load_script(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def remote(*a: Any) -> dict[str, Any]:
        raise TimeoutError("Function timed out after 600 seconds\ntraceback lines")

    monkeypatch.setattr(mod.evaluate, "remote", remote)
    mod.main(experiment_id=EXP)
    (path,) = (tmp_path / "ncm-results").glob("*/results.json")
    run = ncm.load_results(path)                                              # passes the contract
    assert run.status == "failed" and run.metrics == {} and run.experiment_id == EXP
    assert run.reason == "the Modal call raised TimeoutError: Function timed out after 600 seconds"
    assert run.provider == "modal" and run.cost["usd"] >= 0 and run.hardware["gpu"] == "L4"
    assert any("local entrypoint" in x for x in run.limitations)


# ---------------------------------------------------------------- F4, F5: the GPU function's own failure paths

RUN_ID = "ncm-20261020-010203-sudoku-easy"
METRICS_ROW = "solved %   solved  cell %  valid %\n    99.50 1990/2000   99.91   100.00\n"
FREEZE = "".join(f"{p}\n" for p in W.PIP)


def fake_run(evaluation: str | BaseException) -> Any:
    """The container's commands, faked: the pinned commit and hashes, and `evaluation` for evaluate.py."""
    def _run(cmd: list[str], cwd: str = "/ncm", timeout: int = 20) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "nvidia-smi":
            out = "NVIDIA L4\n"
        elif cmd[:2] == ["git", "rev-parse"]:
            out = W.REPO_COMMIT + "\n"
        elif cmd[0] == "sha256sum":
            out = "".join(f"{W.PINNED_SHA256[f]}  {f}\n" for f in cmd[1:])
        elif cmd == ["python", "-m", "pip", "freeze"]:
            out = FREEZE
        elif cmd[:2] == ["python", "evaluate.py"]:
            if isinstance(evaluation, BaseException):
                raise evaluation
            out = evaluation
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return subprocess.CompletedProcess(cmd, 0, out, "")
    return _run


class _BrokenVolume:
    def commit(self) -> None:
        raise RuntimeError("volume backend unavailable")


def in_container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, evaluation: str | BaseException) -> Any:
    mod = load_script(monkeypatch)
    monkeypatch.setattr(mod, "RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "_run", fake_run(evaluation))
    return mod


def test_an_ok_evaluation_writes_and_commits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = in_container(monkeypatch, tmp_path, METRICS_ROW)
    res = mod.evaluate.local(RUN_ID, EXP)
    run = ncm.parse_results(res)
    assert run.status == "ok" and run.metrics["solved_rate"] == 0.995 and mod.volume.commits == 1
    assert ncm.load_results(tmp_path / RUN_ID / "results.json") == run
    assert run.commands[-1].endswith(f"--out {tmp_path / RUN_ID}")
    assert (tmp_path / RUN_ID / "pip-freeze.txt").read_text() == FREEZE      # F6: the environment, by hash
    assert W.freeze_line(FREEZE) in run.limitations
    assert ("pip_install", tuple(W.PIP)) in mod.image.layers


def test_a_failed_volume_commit_is_a_limitation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod = in_container(monkeypatch, tmp_path, METRICS_ROW)
    monkeypatch.setattr(mod, "volume", _BrokenVolume())
    run = ncm.parse_results(mod.evaluate.local(RUN_ID, EXP))                  # returned, not raised
    assert run.status == "ok"
    assert "the Modal Volume commit failed (RuntimeError): only the local copy is certain" in run.limitations


def test_a_timed_out_evaluation_logs_text_not_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """subprocess.run(text=True) still puts raw bytes on TimeoutExpired.stdout/stderr (POSIX)."""
    expired = subprocess.TimeoutExpired(["python", "evaluate.py"], 480, output=b"shard 1/4 done\n",
                                        stderr=b"warn: slow \xff\n")
    mod = in_container(monkeypatch, tmp_path, expired)
    run = ncm.parse_results(mod.evaluate.local(RUN_ID, EXP))
    assert run.status == "failed" and run.reason == "evaluate.py timed out" and run.metrics == {}
    assert "evaluation stopped at the 480s cap" in run.limitations
    log = (tmp_path / RUN_ID / "evaluate.log").read_text()
    assert log == "timed out after 480s\nshard 1/4 done\n\n--- stderr ---\nwarn: slow �\n"
