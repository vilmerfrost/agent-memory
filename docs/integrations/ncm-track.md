# NCM: the research-worker track (the fourth integration)

Status: **contract built and tested (`integrations/ncm.py`); no NCM run has happened.** Mission 004A keeps
NCM as the fourth integration on its own track, separate from the agent-improvement pipeline.

## What NCM is (checked 2026-10-16)

[HazyResearch/ncm](https://github.com/HazyResearch/ncm), "Next-Concept Modeling": a coarse-to-fine parallel
text-generation framework (a multiscale residual-VQ tokenizer plus a transformer that predicts each level in
one pass). MIT license. The repo **evaluates published checkpoints**; it has no training code.

- Downloads: `hf download hazyresearch/ncm-checkpoints --local-dir artifacts` and
  `hf download hazyresearch/ncm-datasets --repo-type dataset --local-dir data` (narrow with `--include`).
- Run: `python evaluate.py --config config/eval/<task>.yaml --checkpoint-dir artifacts/<task> [--data-dir data/<task>]`.
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
| N2 | GPU run: sudoku-easy, then TinyStories, on one rented GPU (blueprint: Modal L4, inside the free monthly credit, hard cap $10) | rented GPU | **decision B2**, an account, a spend cap |
| N3 | Record: the worker writes `results.json`; `ncm.load_results()` validates it; `evidence.record(ncm.to_evidence(run))` stores one `rsi_evidence` row (system `ncm`) linked to `amem:experiment/<id>` | this repo | an `rsi_experiments` row (kind `research`) |
| N4 | Report (experiment C4): metrics vs the paper, GPU-minutes, dollars, exact commands, checkpoint hashes; or "not run" with the reason | evidence folder + brief | N1 or N2 |

Rules from the blueprint (section G2) that `ncm.py` enforces:
- A run that did not happen is `status: not-run` with a reason and **no metrics**. It is never described as working.
- An `ok` run must record the repo commit, the checkpoint hashes and the exact commands.
- Only the task's own metrics are accepted; unknown fields are errors, not ignored.

Estimates (blueprint, not measured): sudoku-easy 5–10 GPU-minutes (< $0.15); TinyStories 0.25–0.5 GPU-hours.

## N2 per-run worst case (deploy/ncm/modal_sudoku.py)

Modal bills a container from start to stop. The GPU function caps every phase: `startup_timeout=120`,
`timeout=600`, `scaledown_window=2`, plus `max_containers=1` and `retries=0`. The evaluation itself stops at
`EVAL_TIMEOUT_S=480`, so the container still writes a `failed` results.json before the 600 s cap.

Longest billed time: 120 + 600 + 2 = **722 s**. Prices (modal.com/pricing, 2026-10-16; CPU and memory are
billed on max(request, usage)); the `eu` region multiplies everything by 1.15:

| Part | Arithmetic | USD |
|---|---|---|
| L4 GPU | 722 s × $0.000222/s × 1.15 | 0.1843 |
| CPU, at most 2 physical cores (assumed) | 722 s × 2 × $0.0000131/s × 1.15 | 0.0218 |
| Memory, at most 8 GiB (assumed) | 722 s × 8 × $0.00000222/s × 1.15 | 0.0147 |
| **Worst case per run** | | **~0.22** |

The script sets no CPU or memory limit, so the last two rows are an assumption: 4 cores and 12 GiB would
still stay under $0.25. `tests/test_integrations_ncm_worker.py` recomputes this table from the script's
constants. If sudoku-easy needs more than 480 s of evaluation, the run is `failed` ("evaluate.py timed out").
