"""NCM research worker: the results contract and its evidence mapping (blueprint C4 "NCM", experiment C4).

NCM (HazyResearch, "Next-Concept Modeling", MIT; https://github.com/HazyResearch/ncm, checked 2026-10-16)
is the fourth integration and runs on its own research-worker track (docs/integrations/ncm-track.md). It
receives NOTHING from the ecosystem: only public checkpoints and datasets. The repo evaluates; it has no
training code. Its evaluation wants a CUDA GPU (one generation shard per GPU).

This module never runs NCM and never touches the network. It defines what a finished run must hand back
(`results.json`), checks it, and maps it to one `ncm:run/<id>` evidence row in rsi_evidence (system `ncm`),
so the experiment registry, the evidence facade and the brief treat a research run like any other evidence.
A run that did not happen is recorded as `status: not-run` with its reason, never as a result.

results.json (written by the worker, one per run):

    {"run_id": "ncm-20261020-sudoku-easy", "experiment_id": "exp-20261020-ncm", "task": "sudoku-easy",
     "status": "ok" | "failed" | "not-run", "reason": null,
     "repo_commit": "<40 hex>", "checkpoint_sha256": {"<file>": "<64 hex>"},
     "commands": ["python evaluate.py --config config/eval/sudoku-easy.yaml ..."],
     "hardware": {"device": "cuda" | "cpu", "gpu": "L4", "gpu_count": 1},
     "metrics": {"solved_rate": 0.91, "cell_rate": 0.98, "valid_rate": 0.99},
     "cost": {"gpu_minutes": 7.5, "usd": 0.10}, "started_at": "...", "finished_at": "..."}

Optional (Mission 004B, the brief's "dataset version", "runtime", "errors and limitations"):
    "checkpoint_revision" / "dataset_revision": the 40-hex Hugging Face revisions downloaded,
    "dataset_sha256": {"<file>": "<64 hex>"}, "runtime_s": 212.4, "provider": "modal",
    "limitations": ["stdout metrics are rounded to 0.01%", ...]   (at most 10 one-line strings)
The worker side that writes this file is integrations/ncm_worker.py.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_memory.integrations import refs
from agent_memory.integrations.evidence import EvidenceRef

# task -> the metric names its evaluation reports (the repo README, 2026-10-16). Nothing else is accepted.
TASK_METRICS: dict[str, frozenset[str]] = {
    **{t: frozenset({"solved_rate", "cell_rate", "valid_rate"}) for t in ("sudoku-easy", "sudoku-medium", "sudoku-hard")},
    "xsum": frozenset({"rouge1", "rouge2", "rougeL", "gen_ppl"}),
    "wmt14": frozenset({"sacrebleu_cased", "sacrebleu_lower", "chrf", "length_ratio"}),
    **{t: frozenset({"gen_ppl", "unigram_entropy"})
       for t in ("tinystories", "owt", "lm1b-small", "lm1b-medium", "lm1b-large")},
}
STATUSES = ("ok", "failed", "not-run")
DEVICES = ("cuda", "cpu", "mps")
_RUN_ID = re.compile(r"^ncm-\d{8}-[a-z0-9][a-z0-9-]{0,60}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_GPU = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,40}$")
MAX_COMMANDS, MAX_COMMAND_LEN = 10, 400


class ResultsError(ValueError):
    pass


@dataclass
class ResearchRun:
    run_id: str
    task: str
    status: str
    experiment_id: str | None = None
    reason: str | None = None
    repo_commit: str | None = None
    checkpoint_sha256: dict[str, str] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    hardware: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    cost: dict[str, float] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    checkpoint_revision: str | None = None
    dataset_revision: str | None = None
    dataset_sha256: dict[str, str] = field(default_factory=dict)
    runtime_s: float | None = None
    provider: str | None = None
    limitations: list[str] = field(default_factory=list)


def _num(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        raise ResultsError(f"{what} must be a finite number")
    return float(v)


def parse_results(data: Mapping[str, Any]) -> ResearchRun:
    """Validate one results.json. Strict: unknown tasks, metrics or fields are errors, not ignored."""
    allowed = {"run_id", "experiment_id", "task", "status", "reason", "repo_commit", "checkpoint_sha256", "commands",
               "hardware", "metrics", "cost", "started_at", "finished_at", "checkpoint_revision", "dataset_revision",
               "dataset_sha256", "runtime_s", "provider", "limitations"}
    extra = set(data) - allowed
    if extra:
        raise ResultsError(f"unknown field(s): {', '.join(sorted(extra))}")
    run_id, task, status = data.get("run_id"), data.get("task"), data.get("status")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ResultsError("run_id is ncm-YYYYMMDD-<slug>")
    if task not in TASK_METRICS:
        raise ResultsError(f"unknown task {task!r}")
    if status not in STATUSES:
        raise ResultsError(f"status is one of {STATUSES}")
    exp = data.get("experiment_id")
    if exp is not None and (not isinstance(exp, str) or not refs.EXPERIMENT_RE.fullmatch(exp)):
        raise ResultsError("experiment_id is exp-YYYYMMDD-<slug>")
    reason = data.get("reason")
    if status != "ok" and (not isinstance(reason, str) or not reason.strip()):
        raise ResultsError(f"a {status} run needs a reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise ResultsError("reason is text of at most 500 characters")
    metrics = {str(k): _num(v, f"metric {k}") for k, v in (data.get("metrics") or {}).items()}
    unknown = set(metrics) - TASK_METRICS[task]
    if unknown:
        raise ResultsError(f"metrics not reported by {task}: {', '.join(sorted(unknown))}")
    if status == "ok" and not metrics:
        raise ResultsError("an ok run reports at least one metric")
    if status != "ok" and metrics:
        raise ResultsError(f"a {status} run reports no metrics (never described as working)")
    commit = data.get("repo_commit")
    if status == "ok" and (not isinstance(commit, str) or not _HEX40.fullmatch(commit)):
        raise ResultsError("an ok run records the 40-hex repo commit")
    ckpt = data.get("checkpoint_sha256") or {}
    if not isinstance(ckpt, dict) or any(not (isinstance(k, str) and _FILE.fullmatch(k) and isinstance(v, str)
                                              and _HEX64.fullmatch(v)) for k, v in ckpt.items()):
        raise ResultsError("checkpoint_sha256 maps relative file names to 64-hex sha256")
    if status == "ok" and not ckpt:
        raise ResultsError("an ok run records the checkpoint hashes")
    cmds = data.get("commands") or []
    if (not isinstance(cmds, list) or len(cmds) > MAX_COMMANDS
            or any(not isinstance(c, str) or not c or len(c) > MAX_COMMAND_LEN or "\n" in c for c in cmds)):
        raise ResultsError(f"commands: at most {MAX_COMMANDS} one-line strings")
    if status == "ok" and not cmds:
        raise ResultsError("an ok run records the exact commands")
    hw = data.get("hardware") or {}
    if hw:
        if hw.get("device") not in DEVICES:
            raise ResultsError(f"hardware.device is one of {DEVICES}")
        if "gpu" in hw and (not isinstance(hw["gpu"], str) or not _GPU.fullmatch(hw["gpu"])):
            raise ResultsError("hardware.gpu is a short model name")
        if "gpu_count" in hw and (not isinstance(hw["gpu_count"], int) or not 0 <= hw["gpu_count"] <= 8):
            raise ResultsError("hardware.gpu_count is 0-8")
        if set(hw) - {"device", "gpu", "gpu_count"}:
            raise ResultsError("hardware has device, gpu, gpu_count only")
    cost = {str(k): _num(v, f"cost {k}") for k, v in (data.get("cost") or {}).items()}
    if set(cost) - {"gpu_minutes", "usd"} or any(v < 0 for v in cost.values()):
        raise ResultsError("cost has non-negative gpu_minutes and usd only")
    revs = {}
    for k in ("checkpoint_revision", "dataset_revision"):
        v = data.get(k)
        if v is not None and (not isinstance(v, str) or not _HEX40.fullmatch(v)):
            raise ResultsError(f"{k} is a 40-hex revision")
        revs[k] = v
    dsha = data.get("dataset_sha256") or {}
    if not isinstance(dsha, dict) or any(not (isinstance(k, str) and _FILE.fullmatch(k) and isinstance(v, str)
                                              and _HEX64.fullmatch(v)) for k, v in dsha.items()):
        raise ResultsError("dataset_sha256 maps relative file names to 64-hex sha256")
    runtime = data.get("runtime_s")
    if runtime is not None and _num(runtime, "runtime_s") < 0:
        raise ResultsError("runtime_s is non-negative")
    provider = data.get("provider")
    if provider is not None and (not isinstance(provider, str) or not _GPU.fullmatch(provider)):
        raise ResultsError("provider is a short name")
    lims = data.get("limitations") or []
    if (not isinstance(lims, list) or len(lims) > MAX_COMMANDS
            or any(not isinstance(x, str) or not x or len(x) > 200 or "\n" in x for x in lims)):
        raise ResultsError(f"limitations: at most {MAX_COMMANDS} one-line strings of <= 200 characters")
    return ResearchRun(run_id, task, status, exp, reason, commit, dict(ckpt), list(cmds), dict(hw), metrics, cost,
                       data.get("started_at"), data.get("finished_at"), revs["checkpoint_revision"],
                       revs["dataset_revision"], dict(dsha), float(runtime) if runtime is not None else None, provider,
                       list(lims))


def load_results(path: Path) -> ResearchRun:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResultsError(f"unreadable results.json: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise ResultsError("results.json holds one object")
    return parse_results(data)


def to_evidence(run: ResearchRun) -> EvidenceRef:
    """One evidence row per run. Metrics, cost and hardware only; the reason is the only text."""
    metrics: dict[str, Any] = {"task": run.task, "status": run.status, **run.metrics}
    metrics.update({f"cost_{k}": v for k, v in run.cost.items()})
    metrics.update({f"hw_{k}": v for k, v in run.hardware.items()})
    if run.repo_commit:
        metrics["repo_commit"] = run.repo_commit
    for k in ("checkpoint_revision", "dataset_revision", "runtime_s", "provider"):
        if getattr(run, k) is not None:
            metrics[k] = getattr(run, k)
    subjects = [refs.amem("experiment", run.experiment_id)] if run.experiment_id else []
    return EvidenceRef(refs.ncm_run(run.run_id), "ncm", "research-run", subjects, metrics,
                       summary=run.reason if run.status != "ok" else None, observed_at=run.finished_at)
