# NCM: the research-worker track (the fourth integration)

Status: **contract built and tested (`integrations/ncm.py`); no NCM run has happened.** Mission 004A keeps
NCM as the fourth integration on its own track, separate from the agent-improvement pipeline.

## What NCM is (checked 2026-10-16)

[HazyResearch/ncm](https://github.com/HazyResearch/ncm), "Next-Concept Modeling": a coarse-to-fine parallel
text-generation framework (a multiscale residual-VQ tokenizer plus a transformer that predicts each level in
one pass). MIT license. The repo **evaluates published checkpoints**; it has no training code.

- Downloads: `hf download hazyresearch/ncm-checkpoints --local-dir artifacts` and
  `hf download hazyresearch/ncm-datasets --repo-type dataset --local-dir data` (narrow with `--include`).
- Run: `python evaluate.py --config config/eval/<task>.yaml --checkpoint-dir artifacts/<folder> [--data-dir data/<folder>]`,
  where `<folder>` is the task's folder in the download. For sudoku-easy: `--checkpoint-dir artifacts/sudoku/easy
  --data-dir data/sudoku/easy` (the README's `artifacts/sudoku-easy` does not exist).
- Tasks and the metrics they report (the only metrics `ncm.py` accepts):

| Task | Metrics |
|---|---|
| sudoku-easy, sudoku-medium, sudoku-hard | solved_rate, cell_rate, valid_rate |
| xsum | rouge1, rouge2, rougeL, gen_ppl |
| wmt14 | sacrebleu_cased, sacrebleu_lower, chrf, length_ratio |
| tinystories, owt, lm1b-small/medium/large | gen_ppl, unigram_entropy |

- Hardware: a CUDA PyTorch install is expected; generation runs one shard per visible GPU.

## Why it is isolated

NCM receives **nothing** from the ecosystem: no sessions, transcripts, evidence or credentials. Inputs are
public checkpoints and datasets only. Nothing in the improvement pipeline depends on it. Its value is that a
non-agent workload exercises the same experiment registry, evidence store and brief.

## Track

| Step | What | Where | Needs |
|---|---|---|---|
| N0 | Contract: `results.json` schema, strict validation, `ncm:run/<id>` evidence mapping | this repo | done (20 tests) |
| N1 | CPU attempt: sudoku-easy in a sandbox-style container with a small device patch (the code assumes CUDA and float64) | local Docker, $0 | approval to download the repo, the sudoku-easy checkpoint (~162 MB) and data slice |
| N2 | GPU run: sudoku-easy, then TinyStories, on one rented GPU (Modal L4, inside the free monthly credit; workspace budget $5 as the hard outer cap, per-run worst case at most $0.25) | rented GPU | **decision B2**, an account, the budget and a spend limit |
| N3 | Record: the worker writes `results.json`; `ncm.load_results()` validates it; `evidence.record(ncm.to_evidence(run))` stores one `rsi_evidence` row (system `ncm`) linked to `amem:experiment/<id>` | this repo | an `rsi_experiments` row (kind `research`) |
| N4 | Report (experiment C4): metrics vs the paper, GPU-minutes, dollars, exact commands, checkpoint hashes; or "not run" with the reason | evidence folder + brief | N1 or N2 |

Rules from the blueprint (section G2) that `ncm.py` enforces:
- A run that did not happen is `status: not-run` with a reason and **no metrics**. It is never described as working.
- An `ok` run must record the repo commit, the checkpoint hashes and the exact commands.
- Only the task's own metrics are accepted; unknown fields are errors, not ignored.

Estimates (blueprint, not measured): sudoku-easy 5–10 GPU-minutes (< $0.15); TinyStories 0.25–0.5 GPU-hours.

## N2 caps (deploy/ncm/modal_sudoku.py)

Two levels, one number each:
- **Workspace budget: $5** (Modal console, Usage & Billing), plus a spend limit. Modal's docs call the
  workspace budget the hard outer cap. It bounds the whole workspace, not one run.
- **Per run: at most $0.25** (worst case ~$0.22, below). The script's own limits set this.

Modal bills a container from start to stop. The GPU function caps every phase: `startup_timeout=120`,
`timeout=600`, `scaledown_window=2`, plus `max_containers=1` and `retries=0`. The evaluation itself stops at
`EVAL_TIMEOUT_S=480`, so the container still writes a `failed` results.json before the 600 s cap. If the Modal
call raises anyway (a crash, the 600 s cap, a lost container), the local entrypoint writes a `failed`
results.json with the reason and no metrics.

Longest billed time: 120 + 600 + 2 = **722 s**. The L4 price and the `eu` multiplier (1.15, on everything) were
checked on modal.com on 2026-10-16. The CPU and memory rates are Modal's published per-core and per-GiB rates,
not re-checked in Mission 004C; both are billed on max(request, usage).

| Part | Arithmetic | USD |
|---|---|---|
| L4 GPU | 722 s × $0.000222/s × 1.15 | 0.1843 |
| CPU, at most 2 physical cores (assumed) | 722 s × 2 × $0.0000131/s × 1.15 | 0.0218 |
| Memory, at most 8 GiB (assumed) | 722 s × 8 × $0.00000222/s × 1.15 | 0.0147 |
| **Worst case per run** | | **~0.22** |

The script sets no CPU or memory limit, so the last two rows are an assumption: 4 cores and 12 GiB would
still stay under $0.25. `tests/test_integrations_ncm_worker.py` recomputes this table from the script's
constants. If sudoku-easy needs more than 480 s of evaluation, the run is `failed` ("evaluate.py timed out").

Environment: every pip package is pinned exactly in `ncm_worker.PIP` (torch 2.8.0, transformers 4.57.1,
huggingface_hub 0.35.3, hydra-core 1.3.2, omegaconf 2.3.0, numpy, regex, rouge_score, sacrebleu). The pins were
chosen offline; the image build (no GPU) is their first check. The GPU function also writes `pip-freeze.txt`
next to results.json and puts its sha256 in `limitations` (results.json has no field for it).

Outputs: the Modal Volume `amem-ncm-results` holds `/<run_id>/` (results.json, evaluate.log, pip-freeze.txt);
the local copy is `./ncm-results/<run_id>/results.json` (git-ignored). `run_id` is
`ncm-YYYYMMDD-HHMMSS-sudoku-easy` (UTC), so a second run never overwrites the first folder or evidence row.
Undo one run: `modal volume rm amem-ncm-results /<run_id> -r`. Undo all: `modal volume delete amem-ncm-results`.
