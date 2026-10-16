> Research draft, 2026-10-17, written before the Mission 004C fixes landed. The status of every finding listed here is in ../m004c-report.md, which wins where they differ.

# NCM sudoku-easy on Modal: readiness (Mission 004C, Phase F)

Status: **not ready to run as-is.** One blocker (the Modal script crashes on container start) and one cap
error (the stated worst-case cost is too low). Both are small code fixes. Everything else checks out
against upstream. Nothing was run. No account, GPU or paid workload was used.

- Our code: `/Users/vilmerfrost/worktrees/agent-memory/m004b` (branch now `rsi/m004c`; NCM files last
  changed in `b20b201`). Not on `main`.
- Upstream: shallow clone at `scratchpad/004c/ncm/upstream/ncm` (read only, never executed).
- Fetched: 2026-10-16 23:00–23:12 UTC (2026-10-17 CET). GitHub API, Hugging Face API, PyPI JSON,
  modal.com docs (`/pricing`, `/docs/guide/{budgets,billing,timeouts,region-selection,cuda,gpu}`),
  modal-labs/modal-client `main` source (its `modal_version` says `1.6.2`).

---

## 1. What was checked

| # | Item | Finding | Status |
|---|---|---|---|
| 1 | Repo | `https://github.com/HazyResearch/ncm`, MIT, default branch `main`, HEAD `c70274273f489abc73832b2b98584a8483b6b203` (2026-10-14 "update bibtex"). Equals our `REPO_COMMIT` (ncm_worker.py:28). | VERIFIED |
| 2 | Entrypoint | `evaluate.py` flags: `--config` (req), `--checkpoint-dir` (req), `--data-dir`, `--out`, `--gpus`, `--n` (evaluate.py:11-16). Our command uses `--config --checkpoint-dir --data-dir --gpus 0 --out` (ncm_worker.py:43-44). All exist. | VERIFIED |
| 3 | Paths | README says `artifacts/<task>`; HF stores `sudoku/easy/…`. Our `artifacts/sudoku/easy`, `data/sudoku/easy` are correct. | VERIFIED |
| 4 | Metrics row | `eval_merge.py:75-76` prints `{'solved %':>9} {'solved':>7} {'cell %':>7} {'valid %':>8}` then `{:9.2f} {:4d}/{n} {:7.2f} {:8.2f}`. `SUDOKU_RE` (ncm_worker.py:45) matches. | VERIFIED |
| 5 | Tasks | 10 configs in `config/eval/`: sudoku-easy/medium/hard, xsum, wmt14, tinystories, owt, lm1b-small/medium/large. Equals `TASK_METRICS` (ncm.py:43-49). | VERIFIED |
| 6 | Python / torch / CUDA | README: "Python 3.10 or newer", CUDA PyTorch "recommended". `requirements.txt` now exists, **unversioned** (torch, numpy, omegaconf, hydra-core, transformers>=4.51, huggingface_hub[cli], regex, rouge_score, sacrebleu). Same list as our `PIP` (ncm_worker.py:41). | VERIFIED |
| 7 | Deterministic decode | sudoku-easy.yaml:104 `temperature: 0.0` → `next_concept.py:488-489` argmax. | VERIFIED |
| 8 | torch 2.8 load | Both checkpoint loads pass `weights_only=False` (eval_shard.py:52, vqvae_tokenizer.py:18), so the torch ≥2.6 default does not break them. | VERIFIED |
| 9 | No network at eval | Sudoku path calls no `from_pretrained`: no ctx_encoder in the config; `AutoTokenizer` only when inputs are strings (eval_shard.py:91). Inputs are int lists: puzzles.pickle = 372,063 B ≈ 2000 × 91 ints. flex_attention is off (config has no `use_flex_attention`), so no Triton compile. | VERIFIED (code) / INFERRED (pickle type, not opened) |
| 10 | HF repos | `hazyresearch/ncm-checkpoints` main = `d342ce8aaea58830eef3b8ad473664673e41f692`; `hazyresearch/ncm-datasets` main = `6580ba41a4b0a976dde876fbe84d25327617b0e6`. Both public, not gated. Equal our pins (ncm_worker.py:30,32). | VERIFIED |
| 11 | File hashes | All 4 LFS sha256 equal `PINNED_SHA256` (table §2). HF `sudoku/easy/config.yaml` git oid `adef40fc…` = repo `config/eval/sudoku-easy.yaml` blob: byte-identical. | VERIFIED |
| 12 | GPU memory / runtime | Generator 102.6 MB, tokenizer 59.8 MB fp32 (~26M + ~15M params). 2000 boards, 16 batches of 128, 12 levels. Needs < 2 GB of the L4's 24 GB. Eval compute: seconds. Container total incl. imports/CUDA init: ~1–3 min. | INFERRED |
| 13 | CUDA compat | Modal host driver 580.95.05, CUDA driver API 13.0. torch 2.8.0 cp311 wheel bundles CUDA runtime 12.8 (`nvidia-cuda-runtime-cu12==12.8.90`). L4 = sm_89. Compatible. | VERIFIED |
| 14 | Modal client | PyPI latest `modal 1.6.2` (2026-10-16). Matches our pin. `App.function` has `timeout`, `startup_timeout`, `scaledown_window`, `max_containers`, `retries`, `region`, `block_network`. `--experiment-id` maps from `experiment_id` (cli/run.py:163). | VERIFIED |
| 15 | Price | L4 $0.000222/s; CPU $0.0000131/core/s; memory $0.00000222/GiB/s. Region `eu` (broad) = 1.15× on **GPU + CPU + memory**. Region selection is on Starter. | VERIFIED |
| 16 | Starter | $30/month included compute. "You must have a payment method on file." GPU concurrency 10. | VERIFIED |
| 17 | Budget | Workspace budget = cap on **usage before credits**, "the hard outer cap". Spend limit = cap on out-of-pocket; "Modal stops workloads that would incur additional out-of-pocket charges"; default = usage limit − credits. Docs do not say how fast the budget stops a running container. | VERIFIED / enforcement speed UNVERIFIED |
| 18 | Timeouts | `timeout` = execution time per attempt; "may run a handful of seconds longer". If `startup_timeout` is unset, `timeout` **also** bounds startup separately. Billing covers load time, execution and the idle window (default 60 s). | VERIFIED |
| 19 | Volume under `block_network` | Not documented. Function I/O uses the same client channel, so it likely works. `Volume.commit()` retries up to 90 times internally. | UNVERIFIED |
| 20 | Paper target | Blog chart: sudoku easy 99.5 % solved (mapping read from chart text). | VERIFIED (inferred mapping) |
| 21 | Isolation | Nothing outside `integrations/ncm*.py`, `cli_integrations.py` and the ClickHouse `research_runs` view references NCM. No nightly code imports the worker or Modal script. | VERIFIED |

## 2. Pins (keep as-is)

| What | Value | Size |
|---|---|---|
| repo commit | `c70274273f489abc73832b2b98584a8483b6b203` | — |
| checkpoints revision | `d342ce8aaea58830eef3b8ad473664673e41f692` | — |
| datasets revision | `6580ba41a4b0a976dde876fbe84d25327617b0e6` | — |
| `sudoku/easy/generator.pt` | `1cefebcd1eae0e210b7e991fdce0e86964d0e897e94a3b432d1008ab92d9926e` | 102,560,203 B |
| `sudoku/easy/tokenizer.pt` | `3f9c87e99fd49900929cd650181f55bf00530280416992b11ddc4ea869b39d68` | 59,816,209 B |
| `sudoku/easy/puzzles.pickle` | `403353dd5d11e49d42627aee84c7b1ceb399fda03d01f6210ce78d0e61f2b29d` | 372,063 B |
| `sudoku/easy/solutions.pickle` | `b52de75f5e4642adca58b18967bcd4bbcafa699c61b21ac33ee0fd7389e0d576` | 364,063 B |
| `sudoku/easy/config.yaml` (git oid, not hashed by us) | `adef40fc50a296bf598ecf6fee092bc571715c34` | 1,980 B |
| upstream blobs (for the report) | evaluate.py `fb7ae1cc…`, scripts/eval_shard.py `2875b0b2…`, scripts/eval_merge.py `ddee0bc7…` | — |

## 3. Discrepancies and recommended fixes (not applied)

### Must fix before the paid run

**F1. The Modal script crashes when the container imports it.** `deploy/ncm/modal_sudoku.py:37`
computes `Path(__file__).resolve().parents[2]`. `modal run <script>.py` mounts a single-file script at
`/root/modal_sudoku.py` (modal-client `_utils/function_utils.py`: `remote_path = ROOT_DIR / module_file.name`,
`ROOT_DIR = /root`). That path has only 2 parents, so `parents[2]` raises `IndexError` at import. The GPU
container never runs `evaluate`. The current test reads the file statically and cannot catch it.
Fix:
```python
_HERE = pathlib.Path(__file__).resolve()
_LOCAL = _HERE.parents[2] / "src" / "agent_memory" / "integrations" / "ncm_worker.py" if len(_HERE.parents) > 2 else None
...
path = _LOCAL if _LOCAL is not None and _LOCAL.exists() else _REMOTE
```
Add a test that evaluates the path logic with `__file__ = "/root/modal_sudoku.py"`.

**F2. The worst-case cost claim is wrong.** Script docstring (modal_sudoku.py:11-13), cloud-readiness.md
F-table and G-NCM4 say one run costs at most ~$0.23. That counts GPU only and one 900 s window. In fact:
startup has its own 900 s bound (no `startup_timeout`), the 60 s idle window is billable, and CPU and memory
are billed and also get the 1.15× multiplier. Worst case today ≈ 1,860 s × ~$0.0003/s ≈ **$0.56**, above the
$0.25 acceptance line. Fix in the decorator (modal_sudoku.py:68):
`timeout=600, startup_timeout=120, scaledown_window=2, max_containers=1` and `EVAL_TIMEOUT_S = 480`.
New worst case ≈ 730 s × $0.0003 ≈ **$0.22**. Update the test constants (test_integrations_ncm_worker.py:100).

### Should fix

- **F3. A crashed or timed-out function leaves no record.** If `evaluate.remote()` raises
  (modal_sudoku.py:102), `main()` writes nothing. The contract says a failed run is recorded as `failed`.
  Fix: wrap it; on exception write a `failed` results.json with reason `modal: <ExceptionType>` and no metrics.
- **F4. `volume.commit()` is unguarded** (modal_sudoku.py:95). With `block_network` unverified, a failing
  commit would discard the result. Fix: `try/except`, add a limitation line; the result still returns.
- **F5. Timeout log is garbled.** `TimeoutExpired.stdout` is always bytes (modal_sudoku.py:85). Decode it.
- **F6. Unpinned image.** Only torch is pinned (ncm_worker.py:41). Today pip resolves transformers 5.20.1,
  huggingface_hub 2.2.0, hydra-core 1.3.7 (forces omegaconf < 2.4), numpy ≤ 2.4 on Python 3.11. Also
  `huggingface_hub[cli]` no longer has a `cli` extra in 2.x (pip warns; `hf download` still exists with the
  same flags). Fix: pin exact versions, or add a build step `pip freeze > /ncm/pip-freeze.txt` and copy it
  plus its sha256 into the run folder.
- **F7. `run_id` is date-only** (modal_sudoku.py:101). A second run that day overwrites the volume folder and
  the evidence row (`add_evidence` upserts on `(system, external_ref)`). Fix: add `%H%M%S`.
- **F8. `ncm-results/` lands in the repo tree and is not gitignored** (modal_sudoku.py:103). Fix: ignore it or
  write outside the repo.
- **F9. Docs.** (a) G-NCM4 undo `modal volume rm amem-ncm-results` is invalid: `rm` needs a path. Use
  `modal volume rm amem-ncm-results /<run_id> -r`, or `modal volume delete amem-ncm-results` (irreversible).
  (b) "Modal stops workloads at the budget" (modal_sudoku.py:5): the docs say the budget is the hard cap;
  the explicit "stops workloads" wording belongs to the spend limit. (c) ncm-track.md:38 says "hard cap $10";
  activation says $5; acceptance says $0.25. Align. (d) ncm-track.md:14 repeats the README path that is
  wrong for sudoku. (e) `LIMITATIONS[1]` "the repo pins no versions": still true, but the repo now ships
  an unversioned `requirements.txt`.
- **F10. G-NCM3 has no amem command.** It is a raw insert on the hosted database. Per CLAUDE.local.md it
  needs explicit approval (hosted change other than a migration). SQL in §4.

### Minor

- `parse_results` does not validate `started_at`/`finished_at`; `evidence.record()` drops
  `observed_at=finished_at` (add_evidence sets `now()`).
- `cost.usd` is GPU × wall time only; the bill also has CPU, memory and load time. The existing limitation
  line covers it; the C4 "within $0.05" check should still pass.

## 4. Command sequence

### Owner (before anything else)
1. **G-NCM1.** Create the Modal workspace (Starter; a card is required). Usage & Billing:
   Workspace budget **$5** (or the lowest value allowed); spend limit **$0** if the field accepts it.
   Accept: both values shown on the page.
2. **G-NCM2.** Separate venv, not the amem one:
   ```bash
   python3 -m venv ~/.venvs/modal && ~/.venvs/modal/bin/pip install modal==1.6.2
   ~/.venvs/modal/bin/modal setup            # browser login; optional: modal token new --expires-in <ttl>
   ~/.venvs/modal/bin/modal profile current  # accept: prints the profile
   ```

### Agent ($0, after approval of the code fixes)
3. Apply F1–F2 (and F3–F8) on the NCM branch. Run
   `uv run pytest tests/test_integrations_ncm.py tests/test_integrations_ncm_worker.py`.
4. Re-check pins on run day (metadata only):
   ```bash
   gh api repos/HazyResearch/ncm/commits/c70274273f489abc73832b2b98584a8483b6b203 --jq .sha
   curl -s 'https://huggingface.co/api/models/hazyresearch/ncm-checkpoints/tree/d342ce8aaea58830eef3b8ad473664673e41f692/sudoku/easy?expand=true'
   curl -s 'https://huggingface.co/api/datasets/hazyresearch/ncm-datasets/tree/6580ba41a4b0a976dde876fbe84d25327617b0e6/sudoku/easy?expand=true'
   ```
5. **G-NCM3** (separate approval: hosted DB write):
   ```sql
   insert into rsi_experiments (id, kind, hypothesis, meta)
   values ('exp-YYYYMMDD-ncm', 'research',
           '{"task":"sudoku-easy","accept":"solved_rate >= 0.98"}'::jsonb,
           '{"track":"ncm","provider":"modal","gpu":"L4","region":"eu"}'::jsonb)
   on conflict (id) do nothing;
   ```

### The single capped run (G-NCM4, owner approval)
6. From the fixed checkout (repo root):
   ```bash
   ~/.venvs/modal/bin/modal run deploy/ncm/modal_sudoku.py --experiment-id exp-YYYYMMDD-ncm
   ```
   The image build (git clone, pip, two `hf download`s) runs on CPU first. Then one L4 in `eu` runs
   `evaluate.py` with no network. Do not re-run on failure; a failed run is recorded as `failed`.

### After the run
7. Check the Modal Usage page: the run's cost ≤ $0.25 and within $0.05 of `cost.usd`.
8. `~/.venvs/modal/bin/modal volume ls amem-ncm-results /<run_id>` (expect `results.json`, `evaluate.log`,
   `shard_0.log`, `shard_0.pkl`).
9. **G-NCM5:** `uv run amem evidence record-ncm ncm-results/<run_id>/results.json --dry-run --json`, then
   without `--dry-run`. Accept: one `ncm:run/<run_id>` row linked to `amem:experiment/exp-YYYYMMDD-ncm`.

## 5. Cost and hard limits

| | Value |
|---|---|
| Rate (eu) | GPU $0.000255/s + CPU ~$0.000015/core/s + memory ~$0.0000026/GiB/s ≈ **$0.0003/s** |
| Expected | 60–180 s billed → **$0.02–0.06** |
| Worst case, current code | ≈ 1,860 s → **≈ $0.56** |
| Worst case, after F2 | 120 s startup + 600 s run + 2 s idle + a few s → **≈ $0.22** |
| Image build | CPU only; whether builds are billed is not documented; a few minutes ≈ < $0.01 if billed |
| Hard stop | Workspace budget $5 (usage before credits) + spend limit $0. The $30 Starter credit covers $5, so out-of-pocket is $0. |

## 6. Risks

1. F1 not fixed → the run fails at import (small charge, no result).
2. Budget enforcement speed is undocumented. The script caps (F2) are the per-run limit; the budget is the
   backstop.
3. `block_network` + Volume commit is undocumented (F4 makes it non-fatal).
4. Unpinned packages (F6) can change between the image build and a later rebuild.
5. EU L4 capacity is not documented. A queue costs nothing; stop it with Ctrl-C.
6. Results may differ in the last digits from the paper's H100 run (different GPU and kernels; greedy decode).
7. G-NCM3 and G-NCM5 write to the hosted database; each needs its own approval.

## 7. Approval request (paste as-is)

> Approve **one** paid Modal run (G-NCM4): from `<checkout>` at commit `<sha with F1–F2>`, run
> `modal run deploy/ncm/modal_sudoku.py --experiment-id exp-YYYYMMDD-ncm` once.
> Limits: Workspace budget $5 and spend limit $0 (out-of-pocket $0; the $30 Starter credit covers it);
> 1 × L4 in `eu`, `timeout=600`, `startup_timeout=120`, `retries=0`, `scaledown_window=2`, `max_containers=1`;
> worst case ≈ $0.22, expected ≈ $0.04. No retry without a new approval.
> Inputs: only public files: GitHub `HazyResearch/ncm@c7027427` and the four pinned Hugging Face files.
> No amem data, credentials or secrets are sent.
> Output: `results.json` and logs in the Modal volume `amem-ncm-results` and `./ncm-results/<run_id>/`.
> Separately approved: G-NCM3 (one `rsi_experiments` row) and G-NCM5 (one `rsi_evidence` row).
