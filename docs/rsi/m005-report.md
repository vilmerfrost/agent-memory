# Mission 005 report: sandbox replay and the nightly RSI investigator

Development only. Branch `rsi/m005` (wave 1: w-replay, w-evidence, w-night, w-brief) plus `worktree-m005-e2e`
(wave 2: wiring, the end-to-end demo, the safety checks, this report). Nothing is merged to `main`, nothing is
pushed, nothing is scheduled, no hosted schema or row changed, no provider sink is on, no paid model call ran.

## Summary

- The engine works end to end on synthetic data with the real pieces: the night (S0-S9), Jev triage (fixture
  mode), the evidence pack, the strict hypothesis parser, agent-config's real `improve stage`, the real Docker
  sandbox with the stub agent (A/B), the deterministic verdict, a staged proposal, and `amem rsi brief`.
- One command runs it: `uv run python scripts/m005_demo.py --scenario all` (success, no-effect, regression).
- The eight required scenarios pass as automated Docker tests. The safety requirements of section 10 have
  evidence (section B.4). The real production files did not change (section B.5).
- **Unverified:** every live path. No live model (hypothesizer, implementer, replay agent, judge), no live Jev,
  no real ClickHouse / Langfuse / past.dev, no hosted database. Section F lists them.

## A. Implementation report

| Part | Built | Reused | Changed in wave 2 (w-e2e) |
|---|---|---|---|
| A. Sandbox replay (w-replay) | `rsi_replay.py` (A/B runner, checks in their own container, blind seeded judge, Jev layer, eval record, evidence), `rsi_verdict.py` (deterministic decision table, versioned policy), stub agent, `bench/rsi-replay` cases | `sandbox/rsi_sandbox.py` (prepare-arm, run, record-eval), Nightly Trials' blind pairwise judge and triggered-win rule, `rsi_artifact` hashing | An interrupted replay now reaps its containers (`Sandbox.reap_experiment`, below) |
| B. Evidence (w-evidence) | `rsi_evidence_pack.py`: `build_pack` (bounded, per-source caps and timeouts), `render_for_model` (nonce envelope), `delta_since` (incremental, LIMIT-bounded) | the 004C evidence facade, `sb_queries`, ClickHouse / Langfuse / past.dev retrievers | The night's pack now reads the night's own database (`PackEvidence(db_url)`) |
| C. Night (w-night) | `rsi_night.py` (`amem rsi night`, S0-S9, adapters, strict parser, lock, signals, report `rsi-night/1`) | `deviations.candidates`, `rsi_approval` (candidates, proposals, checkpoints), `rsi_targets` baselines, `improve stage` | db_url wiring; reap after an abort or a wall-clock stop; host model CLI env without store or provider credentials |
| D. Budgets (w-night) | `rsi_budget.py`, `config/rsi-night.json` (lean: 2 hypotheses, 1 experiment, 16 sandbox runs, 5400 s, `max_usd` 0) | | none |
| E. Brief (w-brief) | `rsi_brief.py`, `amem rsi brief` (Apply / Reject / Details, push refused unless `AMEM_RSI_BRIEF_PUSH=1`) | `rsi_approval` commands, `edits_push` | none |
| F. Demo (w-e2e) | `synth.seed_m005` + `amem synth seed --rsi-demo`; `rsi_demo.py` + `scripts/m005_demo.py`; `bench/rsi-demo` (Codex home, per-scenario hypotheses); 5 Jev fixtures; `sandbox/tests/test_m005_e2e_docker.py`, `sandbox/tests/test_m005_safety_docker.py`, `tests/test_rsi_e2e_wiring.py` | the base synthetic story (004C), the throwaway-Postgres pattern of the 004C evidence demo, `bench/rsi-replay` | — |

### Integration gaps found and fixed (wave 2)

1. **The evidence pack read the wrong database.** `run_night(db_url=...)` built its default adapters without the
   URL, so `build_pack` connected to the process default store, not the night's database. Fix:
   `default_adapters(cfg, db_url)` and `PackEvidence(db_url)`. Test: `tests/test_rsi_e2e_wiring.py`.
2. **An interrupted replay left a container behind.** SIGINT during a replay killed the `docker run` client
   between create and start. The container stayed in `Created` state: `--rm` never fires for it, and the
   `rm -f` in `Sandbox.run`'s `finally` ran before the daemon finished the create. Fix:
   `Sandbox.reap_experiment` (polls until no container of the experiment appeared for 2 s), called by
   `run_replay` on interruption and by the night after an abort or a wall-clock stop. Tests: the interruption
   scenario and `test_reap_removes_created_orphans_even_when_they_appear_late` (a container created 0.8 s
   later is also removed).
3. **The live hypothesizer inherited every env var of the night.** A night started without `AMEM_NO_DOTENV=1`
   loads `.env.local` / `.env`, and `claude -p` got all of it. Fix: `model_cli_env()` drops store, provider,
   cloud and secret-shaped names; the CLI's own login (`CLAUDE_*`, `ANTHROPIC_*`) stays. `distill.run_cli` got
   an optional `env` argument (default unchanged for the daily writers).

No new migration. No change to the verdict policy, the triage questions or the approval code. Wave-1 test files
touched: `tests/rsi_night_support.py` (`FakeSandboxOps.reap`, so the fakes stay Docker-free) and
`tests/test_rsi_replay.py` (the exact count of shipped replay fixtures, 5 → 6). No assertion was weakened.

## B. Test results

All runs: Docker 29.8.1, `rsi-worker:m003` from `sandbox/images/lock.json`, `AMEM_NO_DOTENV=1`, local
Postgres containers only. Branch `worktree-m005-e2e` (code as of `442b26b`; the sandbox suites, the isolation suite
and `--scenario all` ran on the same code minus the demo's step-9 print; the success demo re-ran on `442b26b`: OK,
exit 0, with the Apply / Reject / Details commands printed).

| Suite | Command | Result |
|---|---|---|
| Unit + integration (full) | `uv run --quiet pytest -q -p no:cacheprovider` | **2211 passed, 1 skipped** in 354 s on `442b26b` (the same single skip as in the earlier run on `5087e27`) |
| Wiring (new, part of the full run) | `uv run --quiet pytest -q -p no:cacheprovider tests/test_rsi_e2e_wiring.py` | 29 passed |
| Sandbox suites, one run (Docker) | `env AMEM_NO_DOTENV=1 uv run --quiet pytest sandbox/tests/test_sandbox_units.py sandbox/tests/test_rsi_formats.py sandbox/tests/test_replay_docker.py sandbox/tests/test_m005_e2e_docker.py sandbox/tests/test_m005_safety_docker.py -s -rA -p no:cacheprovider` | **56 passed** in 145 s: units + formats 35, replay 6, E2E scenarios 10 (new), safety 5 (new) |
| Isolation (Docker) | `env AMEM_NO_DOTENV=1 RSI_SANDBOX_DIR=<scratch>/w-e2e-iso/sandbox uv run --quiet pytest sandbox/tests/test_isolation.py -s -rA -p no:cacheprovider` | **76 passed** in 49 s (mount guard, read-only rootfs, no host files, env allowlist, no docker socket, non-root / no caps, cgroup limits, timeout kill, recover / cleanup, no direct internet / host / metadata / DNS, egress proxy allowlist + SNI check). Its dummy-key tests reach api.anthropic.com / api.openai.com through the proxy and get 401 (no cost). |
| Demo (one command) | `uv run python scripts/m005_demo.py --scenario all` | **3/3 OK**, exit 0 (B.1) |
| K7 guard | `python3 ~/agent-config/lib/parent_profile.py selftest` | **PASS** (B.3) |

### B.1 The demo

`uv run python scripts/m005_demo.py --scenario all --workdir <scratch>/w-e2e-demo-all` (exit 0; each scenario gets a
fresh database in one throwaway Postgres container, its own temp HOME and its own RSI sandbox root):

```
SYNTHETIC DATA: every session, deviation, file and run in this demo is made up. Nothing here is real.

## Scenario success: expect IMPROVES
 1. seed       seeded amem-synth-m005: rule synth-m005-closing-line, 7 sessions (Claude + Codex), 8 deviations;
               base story anchor 2026-09-17 (history)
 2. candidates dev:claude-md:global:synth-m005-closing-line rank 0.7178 SELECTED;
               dev:agents-md:global:synth-m005-closing-line rank 0.5195 deferred: below tonight's experiment slots
 3. evidence   11 findings; health clickhouse ok, experiments ok, jev ok, langfuse ok, pastdev ok, supabase ok
 4. hypothesis 'End every report with the line "Skills used: none" (or name the skills you used instead of none)'
               -> ~/.claude/CLAUDE.md
 5. implement  staged artifact d1f649acc2dd8a7e… (baseline bl-bb7f46141e3e)
 6. A/B        arm A check pass 0.6667 | arm B 1.0 (3+3 stub-agent runs in Docker)
 7. verdict    IMPROVES: improved:report-todos A 0/1 -> B 1/1; judge b_winrate 0.67; jev improves 0.71
 8. staging    staged, proposal 36682590-848d-4c23-8d93-8ae2b7519758; agent-config eval_problems []
 9. brief      brief-success.md: 1 pending decision (Apply / Reject / Details commands bound to the hash)
10. checks     production unchanged True; synthetic production unchanged True; scratch agent-config unchanged
               True; Keychain tripwire hits 0; containers left 0; improve subcommands ['stage']; db host 127.0.0.1
    night 2026-10-17 status ok, checkpoint advanced True, cost $0.00, 6.5 s -> OK

## Scenario no-effect: expect NO_EFFECT
 4. hypothesis 'Keep every bullet in a report short and in plain words'
 6. A/B        arm A check pass 0.6667 | arm B 0.6667
 7. verdict    NO_EFFECT: checks-equal, judge b_winrate 0.50
 8. staging    not_promotable; agent-config eval_problems ['evaluation sandbox-replay verdict no-effect']
    ... checks as above -> OK

## Scenario regression: expect WORSE
 4. hypothesis 'Never edit existing files: put every change in a new file next to the original'
 6. A/B        arm A check pass 0.6667 | arm B 0.0
 7. verdict    WORSE: regressed:docs-typo A 1/1 -> B 0/1; regressed:fix-add A 1/1 -> B 0/1
 8. staging    not_promotable; agent-config eval_problems ['evaluation sandbox-replay verdict worse']
    ... checks as above -> OK
```

The success brief (`brief-success.md`, from the real `amem rsi brief` CLI) has the nine sections, the diff
(`+- End every report with the line "Skills used: none" …`), baseline vs candidate metrics, risk low / confidence
0.70, the full artifact hash, and `amem rsi decide <id> apply|reject --hash <H>` / `amem rsi show <id>`.

### B.2 The eight scenarios (sandbox/tests/test_m005_e2e_docker.py)

| Scenario | What happens | Asserted |
|---|---|---|
| Successful improvement | the closing-line hypothesis; arm B passes report-todos, nothing worse | IMPROVES, proposal staged, Codex candidate deferred, agent-config `eval_problems` = [], brief has Apply / Reject / Details, 12 Docker runs all `--network none`, no credential, production fingerprint unchanged |
| No-effect improvement | a rule the agent cannot act on | NO_EFFECT, candidate `tested`, record `no-effect`, `eval_problems` refuses it, nothing staged |
| Regression | "never edit existing files" | WORSE (docs-typo, fix-add regress), candidate `rejected`, nothing staged |
| Provider outage | ClickHouse and past.dev raise, Langfuse down; and the literal `amem rsi night` CLI with no provider configured | night `partial`, checkpoint advanced (S1-S8 finished), health shows the outage, Supabase evidence still used, IMPROVES staged |
| Interrupted experiment | SIGINT as soon as the first replay container starts | night `aborted`, experiment `aborted`, record `inconclusive`, no container or lock left, checkpoint not advanced; the rerun reuses the pending hypothesis (no new hypothesizer call) and stages it |
| Stale baseline | the live file changes during the experiment; a staged proposal's file changes later | `improve stage` refuses (stale baseline); `housekeeping` marks the proposal `stale`, the brief drops it from pending; a re-replay against the new baseline is refused and the record becomes `inconclusive` (no stale pass survives) |
| Budget exhaustion | `max_sandbox_runs` 5 | night `budget_exhausted` after 5 runs, record `inconclusive`, hypothesis kept pending, next night stages it |
| Prompt injection in retrieved content | hostile text in past.dev's memory, a ClickHouse label and a Supabase edit row; two injected hypotheses in the model reply | one nonce envelope, hostile strings withheld; both injected hypotheses rejected (command-like text; target outside the allowlist); the valid one tested and staged; no canary file, no decision, artifact text clean |

Evidence lines of that run (`-s`, trimmed):

```
success: IMPROVES (improved:report-todos A 0/1 -> B 1/1; judge b_winrate 0.67; jev improves 0.71); proposal staged;
  12 docker runs --network none; brief has Apply/Reject/Details; production fingerprint f3e116a60036 unchanged; 6.4 s
no-effect: NO_EFFECT (checks-equal, judge b_winrate 0.50); candidate tested; record no-effect;
  agent-config eval_problems ['evaluation sandbox-replay verdict no-effect']; nothing staged
regression: WORSE (regressed:docs-typo A 1/1 -> B 0/1; regressed:fix-add A 1/1 -> B 0/1); candidate rejected;
  record worse; agent-config eval_problems ['evaluation sandbox-replay verdict worse']; nothing staged
outage: night partial, checkpoint advanced, health clickhouse error, langfuse unavailable, pastdev error,
  supabase ok; experiment IMPROVES staged
`amem rsi night` CLI, providers unconfigured: partial, clickhouse/langfuse/pastdev unavailable, IMPROVES staged
interrupted: SIGINT during the replay -> night aborted, experiment aborted, record inconclusive, no container/lock
  left, checkpoint not advanced; rerun -> exp-...-r2 IMPROVES staged with the pending hypothesis (no new call)
reap_experiment removed 2 Created containers (one created 0.8 s later); none left
stale: live file edited mid-experiment -> improve stage refused (live file changed since baseline); staged proposal
  -> stale after a live edit; re-replay vs the new baseline: stale-baseline; record now inconclusive, not promotable
budget: max_sandbox_runs 5 -> budget_exhausted, skipped_budget after 5 runs, record inconclusive, hypothesis kept
  pending; next night -> IMPROVES staged
injection: envelope intact, hostile strings withheld; 2 injected hypotheses rejected (looks like a command or an
  injected instruction; target ~/.ssh/config.md is not in the allowlist); the valid one -> IMPROVES staged;
  no canary, no decision, artifact clean
--network none: push github rc 128; host repos visible: none; TCP github: DNS failure
rsi-internal: push github rc 128 (CONNECT tunnel failed, response 403)
proposal 7a73a3c1-...: CLI decision refused (no TTY / agent marker); rsi_writer cannot record or insert a decision;
  a guessed token -> denied; after a simulated phone approval one byte of the artifact changed -> both loaders
  refuse, promotion worker refused, improve calls ['recover'] (no promote), proposal failed
```

### B.3 K7 and freeze status (read-only)

- `python3 ~/agent-config/lib/parent_profile.py selftest` → `parent_profile selftest: PASS` (agent-config
  `0c2f8f9`, scratch repos in temp). The K7 Git guard fix is active.
- `amem rsi freeze-status --json` → `frozen: true`: `max_changes_per_week` 0, `apply_mode` legacy, no
  `AMEM_EDITS_LIVE` in the installed or repo amem-daily plist, `ab-lessons-monthly` disabled.

### B.4 Safety requirements (section 10)

| Requirement | Evidence |
|---|---|
| Sandbox filesystem and network isolation | `test_isolation.py` 76 passed (temp root ending in `/sandbox`); in the success and injection scenarios every container run's docker argv is checked: the locked worker image, `--network none`, `--read-only`, no credential name (`--cap-drop ALL`, non-root and the limits come from the same argv builder that `test_isolation.py` proves) |
| Production credentials inaccessible | worker env = `ENV_ALLOWLIST` + the image's own ENV (5 telemetry switches) and nothing secret-shaped; no `/Users`, no `~/.ssh`, `.git-credentials`, `.netrc`, `gh` or Codex auth file in the container; the stub runs with `credentials=none` (a credential with `--network none` is refused). Host side: the demo env is an allowlist (no `AMEM_SUPABASE_*`, provider keys, agent markers), `AMEM_NO_DOTENV=1`, local `AMEM_DB_URL`, Keychain admin item behind a tripwire (0 hits in every run). The live hypothesizer's env is scrubbed (`model_cli_env`). |
| No sandbox worker can push or merge | `test_sandbox_worker_cannot_push_or_merge_without_network`: `git push https://github.com/...` rc 128 (no route, no DNS), push / fetch / merge against the host repos fail (not visible in the container), TCP to github.com fails. `..._through_the_egress_proxy` (the live-agent network): push rc 128, `CONNECT tunnel failed, response 403` |
| K7 Git guard fix active | B.3 |
| K4 env-file gap accounted for | below |
| The investigator cannot approve its own proposals | AST check over `rsi_night`, `rsi_replay`, `rsi_verdict`, `rsi_evidence_pack`, `rsi_budget`, `rsi_brief`, `rsi_demo`: no call of a decision or token function, no SQL that writes `rsi_decisions` or arms a token. On a night-staged proposal: a CLI decision from a non-TTY or agent process is refused (`NotHuman`); role `rsi_writer` cannot call `amem_rsi_record_cli_decision` or insert a decision; a guessed phone token is `denied`. The success and injection scenarios assert zero decisions for their staged proposals. |
| A failed evaluation cannot become promotable | agent-config's own `lib/rsi_artifact.py eval_problems` (loaded read-only) refuses the NO_EFFECT, WORSE and INCONCLUSIVE (interrupted, budget, stale) records; only IMPROVES writes `improves` |
| Approval bound to the exact tested artifact | the night-staged proposal gets a (simulated, throwaway DB) phone approval; one byte of the artifact changes; both loaders refuse (`after bytes changed since staging`); the promotion worker answers `refused`, marks the proposal `failed`, and never calls `improve promote` |
| Production writes disabled | lean config: `max_usd` 0, every stage in fixture / stub mode, every paid model refused by the budget (tested); the night only ever runs `improve stage` (each run logs improve's subcommands: only `stage`); `freeze-status` FROZEN |

**K4 (environment files).** K4 is the `.env*` deny gap of agent sessions (agent-config eval `o-env-deny`): a
permission rule denies `Read(.env*)` and `cat`/`head`/`tail`/`grep`, but other commands are not covered. M005 paths
that could read an env file:

- **Sandbox runs (stub, live agent, judge, implementer, checks): none.** No host path holding `.env` can be
  mounted (the mount guard refuses any path containing `.env` and every protected home path); baselines refuse
  secret-shaped content; the worker env is an allowlist.
- **Hypothesizer (`claude -p` on the host): cannot read files.** It runs with `--tools ""`,
  `--disable-slash-commands`, `--strict-mcp-config` (no MCP), no session file, and now a scrubbed env.
- **Host orchestration (`amem rsi night`, `rsi brief`, replay runner, evidence pack, `improve stage`): yes, by
  design.** `config.load_env()` loads `.env.local` and `.env` from the repo root unless `AMEM_NO_DOTENV=1`; that is
  how a routine gets its `AMEM_DB_URL`. This is code, not a model: nothing reads a file because retrieved text says
  so. In the main checkout, `.env.local` still holds the full `postgres` DSN until `amem admin split-credentials`
  (least-privilege E3) runs.
- **Open:** K4 itself (agent sessions can still read `.env` through commands the deny rule does not list) is not
  fixed by M005. `improve stage` (subprocess) still inherits the night's full env; it reads no model output and
  needs none of it, but a scrub there is a small follow-up.

### B.5 Production unchanged

`rsi_demo.production_fingerprint()` hashes `~/.claude/CLAUDE.md`, `~/.claude/settings.json`,
`~/.codex/AGENTS.md`, agent-config's `lessons.md`, `PARENT.md`, `criteria.json`, `pins.json`, `settings.json`, the
trees `~/.agents/skills`, `~/.claude/scheduled-tasks`, `~/agent-config/agents`, `~/agent-config/hooks`,
agent-config `HEAD` and `git status`, the full `~/sandbox` listing (path, size, mtime) and every LaunchAgent plist.

| When | Overall sha256 | Diff |
|---|---|---|
| Before any wave-2 work (2026-10-17 ~17:30 UTC) | `f3e116a600369b7db6468c87e8669e2331f6dcbb0724a6edf8e91507e683da27` | – |
| After all demo, scenario, safety, isolation and replay runs (~18:12 UTC) | `f3e116a600369b7db6468c87e8669e2331f6dcbb0724a6edf8e91507e683da27` | none |
| After the final demo re-run (~18:15 UTC) | `f3e116a600369b7db6468c87e8669e2331f6dcbb0724a6edf8e91507e683da27` | none |

Selected items (unchanged): `~/.claude/CLAUDE.md` `e8f31039…`, `~/.codex/AGENTS.md` `da6832b1…`, agent-config
HEAD `0c2f8f9` with a clean status, `~/.agents/skills` tree `d3cfb211…`, `~/sandbox` listing `276427dc…`,
LaunchAgents `1daec8f2…`. The demo and the success scenario also compare the fingerprint in-run
(`production_unchanged`).

No hosted DB connection: every amem process ran with `AMEM_NO_DOTENV=1` and `AMEM_DB_URL` =
`postgresql://postgres:postgres@127.0.0.1:<port>/...` (a throwaway container). The demo asserts
`db_host == 127.0.0.1`; the demo and every scenario assert 0 Keychain tripwire hits; the worktree holds no env file.

## C. Architecture

```mermaid
flowchart TD
  subgraph SRC["Evidence sources (read-only, untrusted text)"]
    SB[("Supabase / Postgres<br/>sessions, deviations, edits, rsi_*")]
    CH[("ClickHouse")]
    LF[("Langfuse")]
    PD[("past.dev")]
  end
  S0["S0 preflight<br/>lock, DB, sandbox, budget"] --> S1["S1 delta since checkpoint"]
  SB --> S1
  S1 --> S2["S2 candidates<br/>rsi_candidates"]
  S2 --> S3["S3 triage<br/>JEV answers + fixed rank formula"]
  JEV{{"JEV typed questions"}} --> S3
  S3 -->|"not selected"| DEF[("deferred: next night")]
  S3 -->|"selected"| S4["S4 evidence pack<br/>bounded, nonce envelope"]
  SB & CH & LF & PD --> S4
  S4 --> S5["S5 hypotheses<br/>max 2, strict parser, target allowlist"]
  S5 --> S6["S6 implement<br/>stub / Claude / Codex in the sandbox"]
  S6 --> ST["improve stage<br/>immutable artifact + static eval"]
  ST --> S7["S7 sandbox A/B replay<br/>A = baseline, B = exact artifact"]
  S7 --> V["deterministic verdict<br/>checks + blind judge + JEV"]
  JEV --> V
  V -->|"NO_EFFECT / WORSE / INCONCLUSIVE"| HIST[("learning history")]
  V -->|"IMPROVES + exact hash"| S8["S8 staged proposal"]
  S8 --> S9["S9 checkpoint"]
  S9 --> BR["amem rsi brief<br/>Apply / Reject / Details"]
  BR --> VIL(("Vilmer"))
  subgraph HUMAN["Outside the investigator"]
    VIL -->|"terminal or phone"| DEC["rsi_decisions"]
    DEC --> PW["promotion worker<br/>exact bytes or refuse"]
  end
```

The investigator writes candidates, experiments, eval records, artifacts (via `improve stage`), proposals and its
report. It never writes `rsi_decisions`, never runs `improve apply` or `promote`, and never edits its own config,
questions or verdict policy.

## D. Cost estimate for a lean night

Today the lean config costs $0: every stage runs in fixture or stub mode and `max_usd` is 0. The table is for the
same lean shape (2 hypotheses, 1 experiment, 3 cases × 1 rep) with live stages. USD values use the per-call
estimates in `config/rsi-night.json` (the budget's own accounting basis) and w-replay's replay estimate. They are
estimates, not measured prices.

| Stage | Configured model | Calls per night | Tokens per call (estimate) | USD per night (estimate) |
|---|---|---|---|---|
| S1-S2 delta, candidates | none (database) | 0 | – | 0 |
| S3 triage | Jev `jev-1.14.0` | 2-20 (one per candidate, cap 25) | small state + 4 questions | $0.02-0.20 ($0.01 per call) |
| S4 evidence | ClickHouse, Langfuse, past.dev | ~2 CH + 0-3 LF + 1 PD requests | – | ~$0 on the current plans (assumption); past.dev uses credits |
| S5 hypotheses | `opus` via `claude -p` (`AMEM_EDITS_MODEL`) | 1 (+1 retry max) | ~6-8k in, ~0.5k out | $0 on the subscription login, $1.50 per call as API estimate |
| S6 implementation | Claude `opus` or Codex in the sandbox | 1 | ~10-20k in, ~1-2k out | $1.00 (Codex) - $1.50 (opus) estimate |
| S7 replay agents | e.g. `claude-haiku-5-5` (w-replay's live command) | 6 runs | 3-6 turns each | $0.05-0.30 with the 3 judge runs (w-replay estimate; Haiku measured $0.0005 for a 2-turn task); hard cap 6 × $0.25 |
| S7 judge | Codex `gpt-6-luna` (default) | 3 | ~25k in | included above |
| S7 replay Jev | `jev-1.14.0` | 1 | – | $0.01 |
| Sandbox compute | local Docker | 12 runs | – | 0 |

Total: about **$0.1-0.5 per night** with a live replay and live Jev only (G1 + G3), and about **$1.1-3.5 per night**
with the live hypothesizer and implementer too (G2), or about $35-105 for 30 nights at the upper end. Assumptions:
one candidate selected, no retries beyond the configured one, the config's per-call estimates hold (they are
conservative placeholders, not measured), the subscription covers `claude -p`. Each gate raises `max_usd` only as far
as that gate needs.

## E. Activation plan

Each gate needs Vilmer's explicit approval. Each stays revertible. No gate enables promotion: Apply stays a human
decision handled by the existing promotion worker, which has its own approval history (least-privilege doc).

| Gate | What | Steps | Proves | Rollback |
|---|---|---|---|---|
| G1 | Live smoke replay | Vilmer creates spend-capped Keychain items `rsi-sandbox/anthropic` (and `rsi-sandbox/openai`). On a dev root (`RSI_SANDBOX_DIR=$HOME/sandbox-dev`, never `~/sandbox`): `rsi_sandbox.py init && net-up`, `stage-synthetic`, then the live `rsi_replay run` of `docs/rsi/replay.md` with `--dry-run`, then without. | live agent and judge command lines inside the worker, stream-json parsing, real cost per replay | `net-down`; delete the dev root; revoke the sandbox keys |
| G2 | Live hypothesizer and implementer | A dev copy of `config/rsi-night.json` with `stages.hypothesizer=claude-cli`, `implementer=claude` (or `codex`), `implementer.credentials=keychain:rsi-sandbox/anthropic`, `max_usd` ~5, then `uv run python scripts/m005_demo.py --config <dev config>` (the disposable world). Check first that `claude -p` finds its login with the demo's temp HOME (unverified). | typed hypotheses from the real model, the sandbox implementer, cost accounting | delete the dev config; nothing persists outside the disposable world |
| G3 | Live Jev triage | `TYPESAFE_API_KEY` + `AMEM_JEV_LIVE=1` for that one run, `stages.triage=live`, `replay_jev=live`. | live answers for triage and replay states (fixture mode has none for live states, so a live replay can only be IMPROVES with live Jev) | unset the flag |
| G4 | Hosted read-only dry run | After least-privilege E3 (`amem_app`), optionally apply `rsi_evidence_pack.PROPOSED_INDEXES` (a hosted schema change). Then `amem rsi night --dry-run --json` against the hosted DB (no lock, no rows, no report). | delta size, candidates and ranks on real data, query cost | drop the indexes (if applied); nothing else was written |
| G5 | Merge `rsi/m005` to `main` | Only after Phase I (I1-I3) is verified and green. Merge `worktree-m005-e2e` into `rsi/m005`, rerun the full suite and the sandbox suites, then merge to `main`. | the merged tree is green; Phase I unaffected | `git reset --keep` to the pre-merge SHA |
| G6 | Schedule | A new LaunchAgent for `amem rsi night` (not the existing `rsi-nightly`, which runs skills ingest and harvest), stage-only, outside the Phase I window, with the G1-G3 budget. | a night runs unattended and only stages | `launchctl bootout` and remove the plist |

Never part of any gate: `improve apply`, automated promotion, writing `~/sandbox` from tests, changing the verdict
policy or the triage questions from the investigator.

## F. Remaining risks and unverified items

- **Every live path is unverified:** `ClaudeCliHypothesizer`, `SandboxImplementer` (Claude and Codex), live replay
  agents, the live judge (Claude and Codex), live Jev triage and replay, real ClickHouse / Langfuse / past.dev
  answers. Their wiring is tested with fakes and stand-ins only.
- **Provider access:** no hosted provider was called. The demo's ClickHouse / Langfuse / past.dev answers are
  canned stand-ins that use the real named-query specs (`rsi_demo.stand_in_providers`); real answers are
  unverified. The night treats an unconfigured provider as `unavailable`, so a night without all three configured
  reports `partial` (the checkpoint still advances).
- **Index DDL:** `PROPOSED_INDEXES` is not applied (a hosted schema change, G4). Without it the delta queries are
  bounded sequential scans.
- **Phase I dependency:** Phase I (I1-I3, stage-only amem-daily) was approved on 2026-10-17 to run after the
  2026-10-18 gate check; this worker did not check whether it ran, and `freeze-status` still showed
  `apply_mode` legacy. `rsi/m005` must not merge before Phase I is verified (G5). The existing `rsi-nightly`
  LaunchAgent is unrelated to `amem rsi night`.
- **Self-approval boundary:** the investigator's code never writes a decision (tested), and the role `rsi_writer`
  cannot (tested). In production that role boundary holds only once routines run as `amem_app` (least-privilege
  E3); until then a process that loads the main checkout's `.env.local` holds the full `postgres` DSN.
- **K4:** open for agent sessions (see B.4). `improve stage` still inherits the night's env.
- **K7:** active (selftest PASS); it guards agent sessions on the host, not the sandbox (the sandbox has no route
  and no credential, so no push is possible there anyway).
- **Docker Desktop file sharing:** the ~1 s path cache (w-replay's `SETTLE_S` 2 s) and the interrupted-run race
  (fixed here with `reap_experiment`) are both timing-based on Docker Desktop; a slower machine may need a longer
  settle.
- **The stub agent is a stand-in.** It proves the A/B harness, isolation and determinism, not that a real model
  follows the changed instruction. With reps 1 and 3 cases the verdict rests on 3 pairs (the policy minimum).
- **Synthetic data only.** The `--rsi-demo` rows are outside gold.json; do not combine them with the egress checks.
- **G2 follow-ups (fixed after G2; the live re-run G2b is written, not run):** the budget now books Codex judge
  calls from tokens and Jev calls at a flat price from a versioned price table of assumed, unverified prices
  (`config/rsi-prices.json`), never $0 for a paid model, each cost once; the `closing-line` candidate and the
  `report-todos` prompt say the closing line goes inside REPORT.md. Commands: `docs/rsi/replay.md`, "Gate G2b".
  The demo's own hypotheses (`bench/rsi-demo/hypotheses/`) keep the short closing-line wording: they only run
  with the stub agent.

### Mission 005.5 updates (2026-10-17, on `integration`)

- **Provider access:** the night's S1 and S4 now use the real, policy-gated, read-only clients
  (`rsi_evidence_providers.py`, `config/rsi-evidence-providers.json`, docs/rsi/evidence-providers.md). The stand-ins
  remain only in the demo and the mock rehearsal. past.dev stays closed (`read: false`) until the owner approves
  investigator recall. Live probe 2026-10-17: Supabase ok; ClickHouse and Langfuse unconfigured in the main env.
- **Live replay wiring:** the night's SandboxReplayer passes the judge model, credential references and
  `ensure_network`; live Jev triage and the Codex judge/implementer are booked as estimates, never provider-reported.
- **Apply rule (B table, E2E success row):** the brief shows Reject / Details and withholds Apply for stand-in
  evaluations; Apply needs an independent judge and live Jev on the bound sandbox-replay record.
- **Scheduling and first-night limits:** docs/rsi/night-ops.md (jobs not installed).
