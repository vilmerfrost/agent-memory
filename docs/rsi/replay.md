# Sandbox replay (`sandbox-replay` suite)

Mission 005, Part A. Code: `src/agent_memory/rsi_replay.py`, `src/agent_memory/rsi_verdict.py`,
`sandbox/stub_agent/`, `bench/rsi-replay/`, policy `config/rsi-verdict-policy.json`, Jev questions
`questions/rsi-replay.toml`. Contract: `docs/rsi/m005-plan.md` 2.1 and 2.2.

A staged artifact (`rsi-artifact/1`) is promotable only with a passing `sandbox-replay` record bound to its exact
hash and baseline. This suite writes that record.

```
artifact H (staged against baseline B, experiment E)
        │
        ├─ arm A: prepare_arm(B)            ─┐   per case and rep, fresh homes, same agent/model/effort/limits
        └─ arm B: prepare_arm(B, artifact=H) ┘   (B must be `exact`, else INCONCLUSIVE, nothing runs)
                │
   agent run (worker container)  ──▶  check run (own container, --network none, empty home,
                                       agent workspace read-only, hidden check never shown to the agent)
                │
   machine (pass per case and arm) + blind judge (seeded order) + Jev (numbers only)
                │
   rsi_verdict.decide (deterministic, versioned policy)  ──▶  artifacts/H/evals/sandbox-replay.json
                                                            ──▶  <root>/replays/E/ (evidence)
```

## Run it (stub agent: no network, no credentials, no paid call)

```
export RSI_SANDBOX_DIR=$(mktemp -d)/rsi            # never ~/sandbox for trials; the CLI never defaults to it
python3 sandbox/rsi_sandbox.py init
uv run python -m agent_memory.rsi_replay stage-synthetic --experiment exp-20261018-demo --candidate closing-line
uv run python -m agent_memory.rsi_replay run --artifact <hash> --experiment exp-20261018-demo --dry-run
uv run python -m agent_memory.rsi_replay run --artifact <hash> --experiment exp-20261018-demo
```

`stage-synthetic` writes a synthetic baseline (`bench/rsi-replay/home/`) and artifact
(`bench/rsi-replay/candidates/<name>/`) and refuses the real `~/sandbox`. Real artifacts come from
`improve stage`. Exit code: 0 for IMPROVES / NO_EFFECT / WORSE, 1 for INCONCLUSIVE, 2 for a bad argument.

Library: `run_replay(artifact_hash, load_cases(ids), ReplayConfig(...), root=..., experiment_id=..., budget=None,
baseline_id=None)` returns a `ReplayResult` (verdict, arms, runs, judge, jev, evidence_dir, eval_path,
budget_stop, totals). `experiment_id` must be the artifact's. `baseline_id` is the baseline the caller expects;
another one in the manifest is a stale baseline. The budget is `rsi_budget.Budget` (`take_sandbox_run` before
every container, `record_run` after, `remaining_s` caps timeouts, `take_call`/`record_call` around live
judge and Jev calls); a `BudgetExhausted` stops the replay cleanly (`budget_stop`, INCONCLUSIVE).

## Costs and the USD cap

The budget's `max_usd` cap sees every model call of a replay, each booked once:

| Call | Booked on | Price |
|---|---|---|
| agent run (Claude Code) | its container run | the CLI's `total_cost_usd`; no cost and no tokens (killed, unreadable) → its `--max-budget-usd` (`cli-cap`) |
| agent run (Codex) | its container run | tokens × the price table |
| live judge (Codex) | its model call (`record_call`) | tokens × the price table; the judge's container run is recorded with `cost_on_call=True` and no cost |
| live Jev | its model call | the table's flat per-call price (the wrapper reports no usage); a failed call is booked too |

The price table is `config/rsi-prices.json` (`rsi-prices/1`, versioned, its version and sha256 in `costs()`
and the dry-run plan). Its prices are **assumptions** from third-party listings, not verified against the
providers' price pages; a human checks and changes them. Rules (`rsi_budget.PriceTable.book`): a cost the
provider reports wins; else the table (Codex: input, cached input, cache writes and output priced apart;
reasoning tokens are part of `output_tokens`); a paid model the table does not name gets the table's high
`default`; a paid call with neither a cost nor tokens books `unknown_usage_usd`. A paid call is never booked at
$0. The pre-call estimate of a paid model without a configured `cost_estimate_usd` comes from the table too.
`result.totals` splits the booked cost: `cost_usd` (agents), `judge_cost_usd`, `jev_cost_usd`, `model_cost_usd`,
`cost_bases`.

## Cases (`bench/rsi-replay/`)

| Case | Task | Check (hidden from the agent) |
|---|---|---|
| `report-todos` | list the TODOs of notes.txt in REPORT.md | every TODO is a bullet, the last line is `Skills used: ...`, notes.txt unchanged |
| `fix-add` | fix `add()` so the tests pass | `unittest` passes, tests unchanged |
| `docs-typo` | fix "recieve" in README.md | README.md fixed in place, no other file |

A case is `cases/<id>.json` (prompt, check command, timeout, the stub's plan), `fixtures/<id>/` and optional
`checks/<id>/` (mounted as `$HIDDEN_CHECK_DIR`; `$FIXTURE_DIR` is the untouched original).

The stub agent (`sandbox/stub_agent/stub_agent.py`) reads the instruction files a real agent loads
(`~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, agents, used skills), reports each file's sha256, recognizes a small
rule vocabulary (closing line, never edit existing files, max lines) and carries out the case's scripted plan
under those rules. Same home + same case gives the same bytes. Scenarios (`scenarios.json`):

| Candidate | Cases | Verdict |
|---|---|---|
| `closing-line` (exact closing-line rule) | report-todos | IMPROVES |
| `closing-line` | fix-add | NO_EFFECT |
| `no-edit-existing` (forbids in-place edits) | docs-typo | WORSE |
| `closing-line` | all three, reps 1 | IMPROVES |
| `no-edit-existing` | all three, reps 1 | WORSE |

## Verdict (deterministic, `rsi_verdict.decide`)

Policy `config/rsi-verdict-policy.json` (`rsi-verdict-policy/1`; its sha256 goes into every record). In order:
refusals (stale baseline, artifact not exact, experiment mismatch), budget stop, run errors or timeouts, missing
runs, fewer than `min_pairs` (3) complete pairs, Jev harness problem → INCONCLUSIVE. Any case where B passes
less → WORSE. A case where B passes more → IMPROVES only if the blind judge's B win rate is at least 0.5 (ties
count half; B wins where a skill-only change never fired count as ties) and Jev says `improves` with confidence
≥ 0.5; otherwise INCONCLUSIVE. Equal checks → NO_EFFECT, unless the judge strongly prefers A (WORSE) or B
(INCONCLUSIVE: no check sees the difference). Only IMPROVES records the passing verdict `improves`.

A replay that evaluated the artifact and could not record (for example a non-exact candidate arm) moves an
older `sandbox-replay.json` aside (`.superseded-<time>`), so no stale pass survives. A caller mistake (wrong
experiment id, Docker down) leaves an existing record alone.

## Evidence

- `<root>/replays/<E>/replay.json`: config, policy, verdict with its exact inputs, arms, totals, every run.
- `<root>/replays/<E>/runs/<run_id>.json`: exit code, duration, tool calls, tokens, cost, check result, diff,
  final message, the instruction files the stub read (sha256).
- `<root>/replays/<E>/judge/<case>-r<rep>.json`: the blinded prompt, the order (AB/BA), the raw and mapped winner.
- `<root>/replays/<E>/jev.json`: the Jev state (numbers only) and answers. `log.jsonl`: the execution log.
- `<root>/experiments/<E>/logs/`: the sandbox's own stdout, stderr and docker argv per run, plus
  `<run_id>.rsi.json` (case, rep, check, judge winner, eval verdict) for the Langfuse sink.
- Subscription attempts (`auth_mode: subscription`, the night's provider profiles) run the official client on the
  host, not in a container, so the sandbox writes no log for them. The replay writes `<run_id>.json` itself
  (`format: rsi-subscription-run/1`, `rsi_replay.write_attempt_log`), also for a failed, timed-out or crashed
  attempt, and the sidecar follows as for a container run. The log holds only what a container log and its stdout
  already carry: ids, exit code, error code, a host-measured `started_at` and duration, token counts, tool names
  and statuses (never commands or outputs) and the final text, redacted and cut to 4000 characters, with the auth
  home's path removed. Never argv, env, auth settings or the client's runtime evidence; there is no `.stdout`.
  The Langfuse sink reads that log directly (`langfuse.load_run_log`; one generation, no cost: a subscription bills
  no USD). Every trace's root metadata says where its start came from (`timing`: `host-measured`, `run-id` or
  `reconstructed` from the log's mtime) and tool span times are `ordinal`, so only a run's own duration is a
  measured latency. Subscription judge calls still leave no run log (unchanged); checks keep their container log.
  No backfill: nights before this change (up to 2026-10-25) never stored their attempts' agent output, so their
  attempts stay untraced in Langfuse.

Run ids: `<E>-<case>-<arm>-r<rep>`, checks `…-chk`, live judge `<E>-<case>-J-r<rep>`. A re-run of the same
experiment replaces its evidence and its run dirs.

## Container hygiene

- The arms are prepared once per replay. Before each later pair the runner hashes both homes; a home an agent
  changed (a live CLI writes session files) is prepared again. The stub never writes to its home.
- Checks run in `home-K`, made empty at staging. Anything a check leaves there (for example the agent's code,
  run by the check, writing to `$HOME`) makes that run an error (`check-home-modified`) and is removed.
  Entry scripts run with `python3 -I`; checks run with `PYTHONNOUSERSITE=1`.
- An interrupt during a run (SIGINT / SIGTERM, the night's hard timeout) can kill the `docker run` client between
  create and start. The container then stays in `Created` state: `--rm` never fires for it, and `run`'s
  `rm -f` can come before the daemon finished the create. `Sandbox.reap_experiment` removes every container of the
  experiment and polls until none appeared for 2 s; `run_replay` calls it on an interruption, and `amem rsi night`
  after an abort or a wall-clock stop (found by the Mission 005 interruption scenario).
- Docker Desktop's file sharing caches a path's entry for about a second: a container that starts right after
  a host path was deleted and made again gets ENOENT (measured: 7/8 runs failed with no wait, 4/8 after 0.5 s,
  0/8 after 1 s and 2 s). So each replay stages its inputs under a new path, and waits `SETTLE_S` (2 s) after it
  prepared or replaced anything a container mounts.

## Live replay

Live agents and judges run behind flags, only with approval. Gates G1 and G2 (2026-10-17, dev root
`~/sandbox-dev/sandbox`) ran them: live Claude Haiku agents, the Codex blind judge and live Jev worked. The
generic commands:

```
export RSI_SANDBOX_DIR=$HOME/sandbox-dev           # a dev root, not ~/sandbox
python3 sandbox/rsi_sandbox.py init && python3 sandbox/rsi_sandbox.py net-up
uv run python -m agent_memory.rsi_replay stage-synthetic --experiment exp-20261020-live1 --candidate closing-line
uv run python -m agent_memory.rsi_replay run --artifact <hash> --experiment exp-20261020-live1 \
    --agent claude --model claude-haiku-5-5 --credentials keychain:rsi-sandbox/anthropic --max-budget-usd 0.25 \
    --judge codex --judge-credentials keychain:rsi-sandbox/openai --judge-credential-name CODEX_API_KEY \
    --jev live --dry-run                            # then again without --dry-run
# Jev live needs TYPESAFE_API_KEY and AMEM_JEV_LIVE=1 in the env; fixture mode has no answer for a live state,
# so without live Jev a live replay can never be IMPROVES (fail-closed).
```

Expected cost for the default 3 cases × 1 rep: 6 agent runs + 3 judge runs + 1 Jev call. With Haiku as the
agent (Phase H measured $0.0005 for a 2-turn task; these tasks take about 3 to 6 turns) and Codex as judge (about
25k input tokens per call), roughly $0.05 to $0.30. Hard cap on the agent side: `--max-budget-usd 0.25` × 6 =
$1.50; the Codex judge has no per-run flag, so its cap is the spend-capped sandbox key.

G2 measured: 6 Haiku agent runs $0.0080 (CLI-reported), 3 Codex judge calls 38.8k input / 764 output tokens
(about $0.005 at the table's prices; G2 itself booked $0 for them, fixed since), 1 Jev call.

## Gate G2b: re-run after the G2 fixes (commands written, not run)

G2 found two problems, both fixed: (1) the USD cap saw only Claude's cost (Codex and Jev were booked $0, and the
judge's cost went on both its call and its run) → the price table and booking above; (2) live Haiku put the
closing line in its chat reply, not in REPORT.md, in both arms (A 0/3, B 0/3) → the `closing-line` candidate now
says the line is the last line inside the report file, and the `report-todos` prompt says REPORT.md is the report
(the hidden check is unchanged). The new candidate bytes give a new artifact, so G2b stages it again. Same
approval rules as G2 (paid calls, a $0.50 hard cap); same safety steps (isolation suite on the dev root,
production and hosted fingerprints before and after, `net-down`, delete the Jev key file afterwards).

1. Code: the rsi/m005 worktree with this fix merged (`cd ~/worktrees/agent-memory/m005`, `git log --oneline -1`).
2. Stage the new artifact on the dev root (no model, no Docker):

   ```
   uv run python -m agent_memory.rsi_replay stage-synthetic --root ~/sandbox-dev/sandbox \
       --experiment exp-20261017-g2b --candidate closing-line
   # expect {"baseline_id": "bl-63a9878bbf11",
   #         "artifact_hash": "bc4fe3b2c742fe0a84a849626befd7b81088391dca0716f580a67de4cf99a13a"}
   ```

   Same baseline as G1/G2 (the home is unchanged). Another experiment id gives another hash: use what it prints.
3. Runner: copy `~/sandbox-dev/g2_run.py` to `~/sandbox-dev/g2b_run.py` and change:
   - `ARTIFACT = "bc4fe3b2c742fe0a84a849626befd7b81088391dca0716f580a67de4cf99a13a"`,
     `EXPERIMENT = "exp-20261017-g2b"`; `CASES` and `CONFIG` unchanged (Haiku, reps 3, Codex `gpt-6-luna`
     judge, Jev live, `max_budget_usd=0.05`).
   - `BUDGET`: values unchanged (`max_usd` 0.50, `model_calls` gpt-6-luna 3 / jev-1.14.0 1,
     `default_model_calls` 0, `model_retries` 0, the same `cost_estimate_usd`). Only the `max_usd` comment is
     wrong now: each cost is booked once (Claude reported, Codex from tokens, Jev flat $0.01).
   - `simulate()`: book the judge and Jev like the replay does, so the simulation proves the new booking:
     `b.record_call("gpt-6-luna", stage="replay-judge", tokens_in=13000, tokens_out=500, tokens_cache_write=13000)`
     and `b.record_run(f"J{rep}", duration_s=10, model="gpt-6-luna", exit_code=0, cost_on_call=True)` in the judge
     loop; `b.record_call("jev-1.14.0", stage="replay-jev")` for Jev (no `usd=`). Check that the judge calls
     have `price_basis` "table" and usd > 0, Jev (0.01, "call"), the J runs usd 0 ("on-call"); add a Codex token
     overrun (`record_call("gpt-6-luna", ..., tokens_in=5_000_000, tokens_out=100_000)`, about $0.55) that
     `check()` must stop.
   - `run()`: write `replays/g2b-result.json` (keep G2's `g2-result.json`), add `"costs": b.costs()` to the
     budget block, and name the archive dir `<E>.archive-<time>` (a new experiment has nothing to archive).
   The worker that made this fix ran such a copy's `simulate` on the fixed code: planned sequence allowed (15 runs,
   3 judge + 1 Jev calls), booked $0.315625 pessimistic (agents at their $0.05 cap, Codex $0.005625 from tokens,
   Jev $0.01, judge runs $0); the 4th judge call, 16th run, 2nd Jev call and any other paid model refused; a
   Claude or a Codex overrun past $0.50 stopped.
4. Simulate (no model, no Docker): `uv run python ~/sandbox-dev/g2b_run.py simulate` → exit 0, no "unexpected".
5. Dry run (writes nothing; shows the plan and the price table version `2026-10-17`):

   ```
   env RSI_SANDBOX_DIR=$HOME/sandbox-dev/sandbox uv run python -m agent_memory.rsi_replay run \
       --root ~/sandbox-dev/sandbox --artifact bc4fe3b2c742fe0a84a849626befd7b81088391dca0716f580a67de4cf99a13a \
       --experiment exp-20261017-g2b --cases report-todos --reps 3 \
       --agent claude --model claude-haiku-5-5 --credentials keychain:rsi-sandbox/anthropic --max-budget-usd 0.05 \
       --judge codex --judge-model gpt-6-luna --judge-credentials keychain:rsi-sandbox/openai \
       --judge-credential-name CODEX_API_KEY --jev live --dry-run
   ```

6. Live run, only with Vilmer's G2b approval (paid: 6 agent runs, 3 Codex judge calls, 1 Jev call; Jev needs
   `TYPESAFE_API_KEY` from the secret popup and `AMEM_JEV_LIVE=1` for this process):

   ```
   env RSI_SANDBOX_DIR=$HOME/sandbox-dev/sandbox python3 sandbox/rsi_sandbox.py net-up
   env AMEM_JEV_LIVE=1 uv run python ~/sandbox-dev/g2b_run.py run
   env RSI_SANDBOX_DIR=$HOME/sandbox-dev/sandbox python3 sandbox/rsi_sandbox.py net-down
   ```

   Read `replays/g2b-result.json`: `budget.costs.total_usd` now includes the judge and Jev
   (`result.totals.judge_cost_usd`, `jev_cost_usd`, `cost_bases`). Expect about $0.02-0.03 booked.
   IMPROVES needs B passing more often than A in the machine check plus the judge and Jev agreeing; with 3 pairs
   a live model can still give INCONCLUSIVE.
