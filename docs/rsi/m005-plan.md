# Mission 005: Sandbox Replay & Nightly RSI Investigator (development plan and contracts)

Branch `rsi/m005` (from local main `51579d1`), worktree `~/worktrees/agent-memory/m005`. Development only:
no merge to main, no push, no live launchd/hooks/skills/instruction changes, no hosted schema or data change, no
provider sink, no real transcripts, no production promotion, no interference with the Phase I activation.

## 0. Development rules (every worker)

- **No paid model calls.** Every model stage has a deterministic stub/fixture mode, and it is the default:
  JEV in fixture mode (`AMEM_JEV_LIVE` unset), no host `claude -p`, no sandbox run with real credentials
  (`credentials=none`). Live modes exist behind explicit flags and are left **unverified** in this mission.
- **Never touch `~/sandbox`** (the production staging root) or the hosted DB. Tests and demos use a temp
  `RSI_SANDBOX_DIR`, a disposable Postgres (tests' `pg_url` fixture or a throwaway container) and
  `AMEM_NO_DOTENV=1`. Keychain tripwire (`AMEM_ADMIN_SECURITY=<script exiting 44>`) for any migrate.
- Docker is available. Real Docker sandbox runs are allowed with a **stub agent** (no network, no credentials).
- Reuse, do not rebuild: `sandbox/rsi_sandbox.py` (Sandbox, prepare_arm, run, record_eval, recover),
  `rsi_approval.py` (create_proposal, push_proposals), `rsi_artifact.py`, `jev.py` + `questions.py`,
  `integrations/evidence.py` (+ retrievers, `questions.py` ask), `deviations.candidates`, `edits_writer`,
  Nightly Trials ideas (`~/Projects/nightly-trials/runner/{trial,judge,score}.py`: blind pairwise judge, triggered-win
  scoring), agent-config `bin/improve stage` (artifact + `static` eval; promote already checks
  `evals/sandbox-replay.json` via `rsi-eval/1`, exact `artifact_hash` and `baseline_id`).
- Existing tables only: `rsi_experiments` (status planned|running|evaluated|failed|aborted, verdict, meta),
  `rsi_candidates` (status new|deferred|selected|tested|staged|rejected|retired, rank, jev, metrics, meta),
  `rsi_evidence`, `rsi_eval_results`, `rsi_proposals`, `rsi_checkpoints`. Extra fields go in `meta` jsonb. A new
  migration only if unavoidable; it then becomes an activation gate (hosted change needs Vilmer).
- The investigator never approves, never promotes, never edits its own rules/criteria/questions, never runs a
  command or writes a file because retrieved text says so.

## 1. Ownership (avoid overlapping edits)

| Worker | Owns |
|---|---|
| w-replay | `sandbox/rsi_sandbox.py` (only: honor `RSI_SANDBOX_DIR`; a stub-agent command path if needed), `src/agent_memory/rsi_replay.py`, `src/agent_memory/rsi_verdict.py`, `bench/rsi-replay/` (cases), `sandbox/stub_agent/`, tests `tests/test_rsi_replay*.py`, `tests/test_rsi_verdict.py`, `sandbox/tests/test_replay_docker.py` |
| w-evidence | `src/agent_memory/rsi_evidence_pack.py` (+ minimal additive changes in `integrations/evidence.py` / `integrations/questions.py`), tests `tests/test_rsi_evidence_pack.py` |
| w-night | `src/agent_memory/rsi_night.py`, `src/agent_memory/rsi_budget.py`, `config/rsi-night.json`, `questions/rsi-triage.toml` + `tests/fixtures/jev/*rsi-triage*`, `cli_rsi.py` (only the `night` subparser lines), tests `tests/test_rsi_night*.py`, `tests/test_rsi_budget.py` |
| w-brief | `src/agent_memory/rsi_brief.py`, `cli_rsi.py` (only the `brief` subparser lines), tests `tests/test_rsi_brief.py` |

Wave 2 (after wave 1 merges into `rsi/m005`): w-e2e wires the real pieces together, runs the synthetic end-to-end
demo and the safety verification, and writes `docs/rsi/m005-report.md`.

## 2. Interfaces

### 2.1 Verdict (w-replay, `rsi_verdict.py`), deterministic, no model

```python
VERDICTS = ("IMPROVES", "NO_EFFECT", "WORSE", "INCONCLUSIVE")
EVAL_VERDICT = {"IMPROVES": "improves", "NO_EFFECT": "no-effect", "WORSE": "worse", "INCONCLUSIVE": "inconclusive"}
@dataclass(frozen=True)
class Verdict: verdict: str; reasons: tuple[str, ...]; inputs: dict   # inputs = the exact numbers used
def decide(machine: ArmPair, judge: JudgeResult | None, jev: JevResult | None, policy: VerdictPolicy) -> Verdict
```
Only `IMPROVES` maps to a promotable record (`improves`). Errors/timeouts/missing arms -> `INCONCLUSIVE`.
The policy (thresholds, minimum n) is versioned data in the repo; the investigator cannot change it.

### 2.2 Sandbox replay (w-replay, `rsi_replay.py`)

```python
@dataclass(frozen=True)
class ReplayCase: id: str; prompt: str; fixture: Path; check: str; timeout_s: int = 300; hidden_check: Path | None = None
@dataclass(frozen=True)
class ReplayConfig: agent: str = "stub"        # stub | claude | codex  (claude/codex = live, unverified here)
    model: str | None = None; effort: str | None = None; reps: int = 1
    limits: dict = ...                           # same cpu/mem/pids/timeout for both arms
    judge: str = "stub"                          # stub | codex | claude | none
    jev: str = "fixture"                         # fixture | live
def run_replay(artifact_hash: str, cases: Sequence[ReplayCase], cfg: ReplayConfig, *, root: Path,
               experiment_id: str, budget: "rsi_budget.Budget | None" = None) -> ReplayResult
```
`ReplayResult`: `verdict` (Verdict), `arms` {A, B: per-arm metrics}, `runs` [RunRecord: run_id (deterministic:
`<experiment_id>-<case>-<arm>-r<rep>`), arm, case, rep, exit_code, duration_s, tool_calls, tokens_in, tokens_out,
cost_usd, check_passed, error], `judge`, `jev`, `evidence_dir`, `eval_path`
(`<root>/artifacts/<H>/evals/sandbox-replay.json`, written with `Sandbox.record_eval`), `artifact_hash`,
`baseline_id`. Arm A = baseline unchanged; arm B = `prepare_arm(B, artifact=H)` and it must be `exact`, else the
result is `INCONCLUSIVE` and nothing is recorded as passing. A stale baseline (baseline sha != manifest
`baseline_id`) -> refuse (`INCONCLUSIVE`, reason `stale-baseline`).

### 2.3 Evidence packs (w-evidence, `rsi_evidence_pack.py`)

```python
@dataclass(frozen=True)
class Finding: source: str; ref: str; ts: str | None; measurements: dict; n: int | None; status: str
    # source: supabase | clickhouse | langfuse | pastdev | jev | experiments
    # status: ok | unavailable | error | stale | unlinked | truncated
@dataclass(frozen=True)
class PackLimits: max_findings: int = 20; max_chars: int = 8000; per_source: int = 6; timeout_s: float = 20
@dataclass
class EvidencePack: candidate_key: str; findings: list[Finding]; health: dict[str, str]; truncated: bool
def build_pack(candidate: Mapping, *, retrievers=None, limits=PackLimits(), environ=None) -> EvidencePack
def render_for_model(pack: EvidencePack) -> str      # untrusted-data envelope, see 3.
def delta_since(checkpoint_ts, *, conn) -> Delta     # new/changed sessions, deviations, eval/routine failures, experiments
```

### 2.4 Night orchestrator (w-night, `rsi_night.py`, `amem rsi night`)

Stages S0-S9 as plain functions with injectable adapters (protocols): `Triage` (JEV fixture|live),
`Hypothesizer` (fixture|claude-cli; at most 2 hypotheses), `Implementer` (stub|claude|codex inside the sandbox,
produces the new file content; staged with `improve stage` into `$RSI_SANDBOX_DIR`), `Replayer` (2.2),
`EvidenceBuilder` (2.3), `Proposer` (`rsi_approval.create_proposal`, only on `IMPROVES` + exact hash).
Budgets in `config/rsi-night.json` (`rsi_budget.py`): per-model call caps, max hypotheses 2, max experiments 1,
max sandbox runs, wall clock, retries (no unbounded retries of model calls), USD cap; every call and run recorded
(tokens, usd, duration). Lock (one night at a time), cancellation (SIGINT/SIGTERM -> experiment `aborted`,
checkpoint not advanced, sandbox `recover`), checkpoint `rsi_checkpoints` job `rsi-night` advanced only after
S8 finished. Unselected candidates -> `deferred` (kept), failed/rejected stay as history.
Output: `$RSI_SANDBOX_DIR/nights/<night>/report.json` (contract 2.5) + DB rows.

### 2.5 Night report `rsi-night/1` (w-night writes, w-brief reads)

```json
{"format": "rsi-night/1", "night": "2026-10-18", "started_at": "...", "finished_at": "...",
 "status": "ok | partial | aborted | budget_exhausted | preflight_failed",
 "checkpoint": {"before": "...", "after": "...", "advanced": true},
 "delta": {"since": "...", "sessions": 0, "deviations": 0, "eval_failures": 0, "routine_failures": 0},
 "patterns": [{"key": "...", "summary": "...", "n": 0, "sessions": 0, "refs": ["amem:..."]}],
 "candidates": [{"id": "...", "key": "...", "source": "...", "rank": 0.0, "selected": false, "reason": "..."}],
 "experiments": [{"experiment_id": "...", "candidate_key": "...", "hypothesis": {"what": "...", "why": "..."},
                  "target": "...", "artifact_hash": "...", "baseline_id": "...", "verdict": "IMPROVES",
                  "verdict_reasons": ["..."], "arms": {"A": {}, "B": {}}, "risk": "low|medium|high",
                  "confidence": 0.0, "cost_usd": 0.0, "status": "staged | not_promotable | failed | interrupted |
                  skipped_budget", "proposal_id": null, "evidence_refs": ["..."]}],
 "costs": {"by_model": {"<model>": {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0}},
           "sandbox_runs": 0, "providers": {"<name>": {"requests": 0, "usd": 0.0}}, "total_usd": 0.0},
 "health": {"supabase": "ok", "clickhouse": "unavailable", "langfuse": "ok", "pastdev": "ok", "jev": "fixture",
            "sandbox": "ok"},
 "pending_decisions": ["<proposal id>"]}
```

### 2.6 Morning brief (w-brief, `rsi_brief.py`, `amem rsi brief`)

`amem rsi brief [--night N] [--json]` reads the night report + DB rows (`rsi_proposals`, `rsi_eval_results`,
`rsi_candidates`) and prints a markdown brief: sessions analyzed, patterns, candidates, experiments, passed
improvements, failed/inconclusive, costs, service health, pending decisions; per staged proposal: what changes
(diff summary), why, evidence refs, baseline vs candidate, risk/confidence, artifact hash, and the Apply / Reject /
Details actions as the existing CLI commands (`amem rsi decide ...`, `amem rsi show ...`). **No ntfy/phone push in
this mission** (a `--push` flag may exist but must refuse unless explicitly enabled, and tests prove it sends
nothing).

## 3. Untrusted evidence envelope (w-evidence defines, everyone uses)

Retrieved text and run output are data. `render_for_model` wraps them in a delimited block with a random nonce,
strips control/markup that could close the block, caps length, and prefixes a fixed instruction that the content
is untrusted and contains no instructions. The orchestrator never derives a command, path or tool call from it:
model outputs are parsed into strict typed structures (hypothesis what/why/target from an allowlist of
instruction targets), and anything else is rejected.

## 4. Wave 2: end-to-end synthetic demo (w-e2e)

Disposable Postgres + `amem synth seed` extended with repeated instruction failures; temp `RSI_SANDBOX_DIR` with a
synthetic baseline and synthetic instruction targets (never the real `~/.claude`); stub agent in the real Docker
sandbox (no network, no credentials). Scenarios: success (IMPROVES -> staged proposal -> brief), no-effect,
regression (WORSE), provider outage, interrupted experiment, stale baseline, budget exhaustion, prompt injection in
retrieved content. Plus safety checks: sandbox isolation suite, no production credential reachable, no push/merge
from the sandbox, K7 guard active, K4 accounted for, no self-approval, failed eval not promotable, approval bound to
the exact artifact, production instruction files byte-identical (hash before/after).
