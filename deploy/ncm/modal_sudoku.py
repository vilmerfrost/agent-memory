"""NCM sudoku-easy on one Modal L4 GPU (Mission 004B, track step N2). NOT RUN: it needs separate approval.

Before the first run (docs/integrations/cloud-activation.md, NCM steps):
1. A Modal account on the Starter plan ($30/month of included credit; a card on file is required).
2. A Workspace budget set in the Modal console (Usage & Billing). Modal stops workloads at the budget.
3. `pip install modal==1.6.2` in a separate venv (not the amem venv), then `modal setup` by the owner.

Run (after approval), from the repo root:
    modal run deploy/ncm/modal_sudoku.py --experiment-id exp-YYYYMMDD-ncm

What it spends: one L4 in the `eu` region ($0.000222/s x 1.15). The GPU function has timeout=900 s and
retries=0, so one run costs at most ~$0.23 of GPU time (estimate ~$0.05). Downloads happen at image build, which
runs without a GPU. The GPU function runs with block_network=True: it needs nothing from the internet and can
send nothing anywhere. It receives no amem data, credentials or session content: only public files.

What it writes: results.json (the Mission 004A contract, built by integrations/ncm_worker.py) and the raw
evaluation log in the Modal Volume `amem-ncm-results`, and a local copy under ./ncm-results/<run_id>/.
Record it afterwards with `amem evidence record-ncm ./ncm-results/<run_id>/results.json`.

Checked against the modal 1.6.2 source (2026-10-16): Image.debian_slim(python_version=), .apt_install,
.pip_install, .run_commands, .add_local_file(local, remote, copy=False) (must be the last layer),
App.function(image=, gpu=, timeout=, retries=, region=, volumes=, block_network=), Volume.from_name(
create_if_missing=True), Volume.commit(), App.local_entrypoint(), Function.remote(), modal.is_local().
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import time
from datetime import UTC, datetime

import modal

_REMOTE = pathlib.Path("/root/ncm_worker.py")      # where add_local_file puts the worker in the container


def _worker_path(script: str) -> pathlib.Path:
    """The repo's ncm_worker.py when this script runs from a checkout, else the copy mounted in the container.
    `modal run` mounts this one file as /root/modal_sudoku.py, which has only two parents."""
    parents = pathlib.Path(script).resolve().parents
    if len(parents) > 2:
        local = parents[2] / "src" / "agent_memory" / "integrations" / "ncm_worker.py"
        if local.exists():
            return local
    return _REMOTE


_LOCAL = _worker_path(__file__)


def _load_worker():  # one file by path: putting its folder on sys.path would shadow the stdlib `http`
    spec = importlib.util.spec_from_file_location("ncm_worker", _LOCAL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = _load_worker()
GPU, REGION, TIMEOUT_S, EVAL_TIMEOUT_S = "L4", "eu", 900, 780

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install(*W.PIP)
    .run_commands(*W.build_commands()[:1], *W.build_commands()[2:4])   # clone + pinned checkout, both downloads
)
if modal.is_local():                     # the worker file is mounted at container start (the last layer)
    image = image.add_local_file(_LOCAL, str(_REMOTE))
app = modal.App("amem-ncm-sudoku")
volume = modal.Volume.from_name("amem-ncm-results", create_if_missing=True)


def _run(cmd: list[str], cwd: str = "/ncm", timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


@app.function(image=image, gpu=GPU, timeout=TIMEOUT_S, retries=0, region=REGION, volumes={"/results": volume},
              block_network=True)
def evaluate(run_id: str, experiment_id: str | None) -> dict:
    started, t0 = datetime.now(UTC), time.monotonic()
    out = f"/results/{run_id}"
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    gpu_name = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]).stdout.strip().splitlines()[:1]
    commit = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    sha = _run(["sha256sum", *W.CHECKPOINT_FILES], cwd="/ncm/artifacts").stdout
    sha += _run(["sha256sum", *W.DATASET_FILES], cwd="/ncm/data").stdout
    extra: list[str] = []
    try:
        proc = _run(W.EVAL_COMMAND.format(out=out).split(), timeout=EVAL_TIMEOUT_S)
        stdout, code = proc.stdout, proc.returncode
        log = proc.stdout + "\n--- stderr ---\n" + proc.stderr
    except subprocess.TimeoutExpired as exc:
        stdout, code = "", None
        log = f"timed out after {EVAL_TIMEOUT_S}s\n{exc.stdout or ''}"
        extra.append(f"evaluation stopped at the {EVAL_TIMEOUT_S}s cap")
    pathlib.Path(out, "evaluate.log").write_text(log[-200_000:])
    res = W.build_results(run_id=run_id, experiment_id=experiment_id, stdout=stdout, exit_code=code,
                          repo_commit=commit, sha256sum_text=sha, seconds=time.monotonic() - t0, gpu=GPU,
                          gpu_name=gpu_name[0] if gpu_name else None, region=REGION,
                          started_at=started.isoformat(timespec="seconds"),
                          finished_at=datetime.now(UTC).isoformat(timespec="seconds"), out=out,
                          extra_limitations=extra)
    pathlib.Path(out, "results.json").write_text(json.dumps(res, indent=2, sort_keys=True))
    volume.commit()
    return res


@app.local_entrypoint()
def main(experiment_id: str = "") -> None:
    run_id = f"ncm-{datetime.now(UTC):%Y%m%d}-sudoku-easy"
    res = evaluate.remote(run_id, experiment_id or None)
    dest = pathlib.Path("ncm-results") / run_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "results.json").write_text(json.dumps(res, indent=2, sort_keys=True))
    print(json.dumps({k: res[k] for k in ("run_id", "status", "reason", "metrics", "cost")}, indent=2))
    print(f"wrote {dest / 'results.json'}; record it with: amem evidence record-ncm {dest / 'results.json'}")
