"""NCM worker side: one evaluation's raw outputs -> a results.json that ncm.parse_results accepts (Mission 004B).

Standard library only and no package imports, so deploy/ncm/modal_sudoku.py can load this one file inside the
GPU container. Pure functions over text and numbers: nothing here runs NCM, downloads or sends anything.

Pins (checked 2026-10-16 against GitHub and the Hugging Face API; the README's `artifacts/sudoku-easy` path
is wrong, the files live under `sudoku/easy/`):
- repo HazyResearch/ncm at REPO_COMMIT (main on 2026-10-14);
- checkpoints hazyresearch/ncm-checkpoints at CHECKPOINT_REVISION, datasets hazyresearch/ncm-datasets at
  DATASET_REVISION, with the sha256 of every file the sudoku-easy run reads (PINNED_SHA256).
A run whose commit or file hashes differ from the pins is `failed`, never `ok`.

Metrics: evaluate.py writes no JSON. scripts/eval_merge.py prints a header `solved % solved cell % valid %`
and one row (`{solved%:9.2f} {k:4d}/{n} {cell%:7.2f} {valid%:8.2f}`); SUDOKU_RE reads it. The printed
values are rounded to 0.01 %, which is listed in `limitations`.

Cost: Modal bills per second; L4 is $0.000222/s and the `eu` region multiplies by 1.15 (modal.com/pricing,
region-selection docs, 2026-10-16). `usd` here is that arithmetic over the function's wall time, not a bill.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

REPO = "https://github.com/HazyResearch/ncm"
REPO_COMMIT = "c70274273f489abc73832b2b98584a8483b6b203"
CHECKPOINT_REPO = "hazyresearch/ncm-checkpoints"
CHECKPOINT_REVISION = "d342ce8aaea58830eef3b8ad473664673e41f692"
DATASET_REPO = "hazyresearch/ncm-datasets"
DATASET_REVISION = "6580ba41a4b0a976dde876fbe84d25327617b0e6"
TASK = "sudoku-easy"
CHECKPOINT_FILES = {"sudoku/easy/generator.pt": "1cefebcd1eae0e210b7e991fdce0e86964d0e897e94a3b432d1008ab92d9926e",
                    "sudoku/easy/tokenizer.pt": "3f9c87e99fd49900929cd650181f55bf00530280416992b11ddc4ea869b39d68"}
DATASET_FILES = {"sudoku/easy/puzzles.pickle": "403353dd5d11e49d42627aee84c7b1ceb399fda03d01f6210ce78d0e61f2b29d",
                 "sudoku/easy/solutions.pickle": "b52de75f5e4642adca58b18967bcd4bbcafa699c61b21ac33ee0fd7389e0d576"}
PINNED_SHA256 = {**CHECKPOINT_FILES, **DATASET_FILES}
GPU_USD_PER_S = {"T4": 0.000164, "L4": 0.000222, "A10": 0.000306}
REGION_MULTIPLIER = {"": 1.0, "eu": 1.15}
# Exact pins (Mission 004C), chosen offline: the image build is their first check. The repo pins nothing and asks
# for transformers>=4.51; 4.57.1 is the 4.x line, which needs huggingface_hub<1.0 (0.35.x ships the `hf` CLI and has
# a `cli` extra only for interactive prompts, so none is asked for). pip freeze in the container records the rest.
PIP = ("torch==2.8.0", "numpy==2.2.6", "omegaconf==2.3.0", "hydra-core==1.3.2", "transformers==4.57.1",
       "huggingface_hub==0.35.3", "regex==2024.11.6", "rouge_score==0.1.2", "sacrebleu==2.5.1")
EVAL_COMMAND = ("python evaluate.py --config config/eval/sudoku-easy.yaml --checkpoint-dir artifacts/sudoku/easy "
                "--data-dir data/sudoku/easy --gpus 0 --out {out}")
SUDOKU_RE = re.compile(r"solved %\s+solved\s+cell %\s+valid %\s*\n\s*([\d.]+)\s+(\d+)/(\d+)\s+([\d.]+)\s+([\d.]+)")
_SHA_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(\S+)$")
_GPU_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,40}")
LIMITATIONS = (
    "metrics are read from stdout, rounded to 0.01%",
    "every pip version is our pin (ncm_worker.PIP); the repo pins none",
    "no seeds are set; sudoku decoding is greedy, so only GPU kernel differences can change results",
    "usd is per-second price arithmetic over wall time, not the provider's bill",
)


def build_commands(out: str = "/results/<run_id>") -> list[str]:
    """The exact commands, in order: image build (clone, pins, downloads), then the GPU step."""
    return [
        f"git clone {REPO} /ncm && git -C /ncm checkout {REPO_COMMIT}",
        "pip install " + " ".join(PIP),                                      # exact pins: nothing to quote
        f"hf download {CHECKPOINT_REPO} {' '.join(CHECKPOINT_FILES)} sudoku/easy/config.yaml "
        f"--revision {CHECKPOINT_REVISION} --local-dir /ncm/artifacts",
        f"hf download {DATASET_REPO} {' '.join(DATASET_FILES)} --repo-type dataset "
        f"--revision {DATASET_REVISION} --local-dir /ncm/data",
        "cd /ncm && " + EVAL_COMMAND.format(out=out),
    ]


def freeze_line(freeze_text: str) -> str:
    """One `limitations` line that names the container's whole Python environment by the hash of `pip freeze`.
    (results.json has no field for it; the freeze itself is pip-freeze.txt next to results.json.)"""
    if not (freeze_text or "").strip():
        return "pip freeze was not recorded"
    return f"pip freeze sha256 {hashlib.sha256(freeze_text.encode()).hexdigest()} (pip-freeze.txt in the run folder)"


def parse_sudoku(stdout: str) -> dict[str, float] | None:
    """The three sudoku metrics as rates in [0, 1], or None when the row is missing or out of range."""
    m = SUDOKU_RE.search(stdout or "")
    if not m:
        return None
    k, n = int(m.group(2)), int(m.group(3))
    cell, valid = float(m.group(4)) / 100, float(m.group(5)) / 100
    if n <= 0 or not 0 <= k <= n or not 0 <= cell <= 1 or not 0 <= valid <= 1:
        return None
    return {"solved_rate": round(k / n, 6), "cell_rate": round(cell, 6), "valid_rate": round(valid, 6)}


def parse_sha256sum(text: str) -> dict[str, str]:
    """`sha256sum` output (run from the download root) -> {relative path: hash}."""
    out = {}
    for line in (text or "").splitlines():
        m = _SHA_LINE.match(line.strip())
        if m:
            out[m.group(2).removeprefix("./")] = m.group(1)
    return out


def gpu_cost(seconds: float, gpu: str = "L4", region: str = "eu") -> dict[str, float]:
    rate = GPU_USD_PER_S[gpu] * REGION_MULTIPLIER[region]
    s = max(float(seconds), 0.0)
    return {"gpu_minutes": round(s / 60, 3), "usd": round(s * rate, 4)}


def _pin_problem(repo_commit: str | None, hashes: Mapping[str, str]) -> str | None:
    if repo_commit != REPO_COMMIT:
        return "repo commit is not the pinned one"
    missing = sorted(f for f in PINNED_SHA256 if f not in hashes)
    if missing:
        return f"no sha256 for {missing[0]}"
    bad = sorted(f for f, h in PINNED_SHA256.items() if hashes[f] != h)
    if bad:
        return f"sha256 mismatch for {bad[0]}"
    return None


def build_results(*, run_id: str, experiment_id: str | None, stdout: str, exit_code: int | None,
                  repo_commit: str | None, sha256sum_text: str, seconds: float, gpu: str = "L4",
                  gpu_name: str | None = None, region: str = "eu", started_at: str | None = None,
                  finished_at: str | None = None, out: str | None = None,
                  extra_limitations: Sequence[str] = (), failure: str | None = None) -> dict[str, Any]:
    """One results.json object. `ok` only when the commit and every pinned hash match, the command exited 0
    and the metrics row parsed; otherwise `failed` with a short reason and no metrics. A given `failure` (the
    caller saw the run break, e.g. the Modal call raised) is the reason, as one line of at most 300 chars."""
    hashes = parse_sha256sum(sha256sum_text)
    metrics = parse_sudoku(stdout)
    reason = " ".join(failure.split())[:300] if failure and failure.strip() else _pin_problem(repo_commit, hashes)
    if reason is None and exit_code != 0:
        reason = f"evaluate.py exited {exit_code}" if exit_code is not None else "evaluate.py timed out"
    if reason is None and metrics is None:
        reason = "no metrics row in the evaluation output"
    hex40 = repo_commit if isinstance(repo_commit, str) and re.fullmatch(r"[0-9a-f]{40}", repo_commit) else None
    res: dict[str, Any] = {
        "run_id": run_id, "experiment_id": experiment_id, "task": TASK, "status": "failed" if reason else "ok",
        "reason": reason, "repo_commit": hex40,
        "checkpoint_sha256": {f: h for f, h in hashes.items() if f in CHECKPOINT_FILES},
        "dataset_sha256": {f: h for f, h in hashes.items() if f in DATASET_FILES},
        "checkpoint_revision": CHECKPOINT_REVISION, "dataset_revision": DATASET_REVISION,
        "commands": build_commands(out or f"/results/{run_id}"),
        "hardware": {"device": "cuda", "gpu": gpu_name if gpu_name and _GPU_NAME.fullmatch(gpu_name) else gpu,
                     "gpu_count": 1},
        "metrics": {} if reason else dict(metrics or {}),
        "cost": gpu_cost(seconds, gpu, region), "runtime_s": round(max(float(seconds), 0.0), 1), "provider": "modal",
        "started_at": started_at, "finished_at": finished_at,
        "limitations": [*LIMITATIONS, *extra_limitations][:10],
    }
    return res
