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

### Scenario results (28 October 2026)

A check that grades several scenarios grades every one of them, then prints one line as the last line of its
stdout:

```
RSI-SCENARIOS {"passed": 2, "total": 3, "scenarios": [{"id": "red", "passed": false, "reason": "..."}, ...]}
```

- The exit code keeps its meaning: 0 only when every scenario and every global check (unchanged inputs, no extra
  output, result shape) passes. `check_passed` is therefore exactly as strict as before.
- `sandbox/stub_agent/check_entry.py` adds two fields to the run record: `check_scenarios` `{passed, total}` and
  `check_score` = passed / total (0..1). Only stdout is read, and only the last `RSI-SCENARIOS ` line.
- No line, a malformed line (bad JSON, counts that disagree with the rows, duplicate ids, non-boolean `passed`,
  total 0) or a line that disagrees with the exit code counts as one scenario: `{1, 1}` and 1.0 when the check
  passed, `{0, 1}` and 0.0 when it failed. So a global failure after all scenarios passed earns no partial credit.
- Single-output checks (anatomy, meltdown, pepe, report-todos) print no line and score 1.0 or 0.0.
- Each check directory is mounted alone, so every check inlines its own few-line printer.

### Checker versions

A changed prompt or checker gets a new checker directory; the old directory stays in place, unchanged, so
historical records stay readable (the same pattern as `parent-policy-v4` in 128909d).

| Case(s) | Checker now | Previous (kept) | Change |
|---|---|---|---|
| worker-final-report | `worker-report-v5` | `parent-policy` (V3 oracle) | per-scenario grading; report format split from substance; prompt de-leaked |
| parent-authority, parent-merge-cleanup, parent-repo-profile, parent-safety-guards | `parent-policy-v5` | `parent-policy-v4` | per-scenario grading only (same rules); parent-authority prompt de-leaked |
| worker-helper-lifecycle, worker-server-launch | `worker-behavior-v2` | `worker-behavior` | per-scenario grading; a solution that calls `sys.exit` can no longer end the check early |
| pepe-lock | `pepe-lock-v2` | `pepe-lock` | caveat line allowed (format) |
| anatomy-plan | `anatomy-plan-v2` | `anatomy-plan` | extra part groups allowed (format) |

The suite version (`SUITE_VERSION` in `src/agent_memory/rsi_replay.py`) should move to 5 with these checkers; the
case digest already changes because it covers the check directory.

### Content and format

Substance stays exactly as strict. Format checks only what the output contract requires. Loosened format rules:

| Checker | Old rule | Now | Why it was format only |
|---|---|---|---|
| worker-report-v5 | every `Name: value` line had to be a contract field | extra fields (for example `Tests:`, `API:`, `Brief DoD:`, `Note:`) are allowed and not graded | an edit may ask for more report lines; the contract fields are still all required and graded |
| worker-report-v5 | text before the first field failed | ignored | a heading carries no claim |
| worker-report-v5 | continuation lines only after some fields | after any contract field; a line after an extra field is ignored | wrapping is layout; field values are still compared |
| worker-report-v5 | evidence needed the literal `exit=<code>` | `exit=1`, `exit 1`, `exit: 1`, `exit code 1` (no other number) | same claim, other spelling; the exact command and every failed test are still required |
| worker-report-v5 | `Denied:` had to equal the receipts or `none` exactly | every exact receipt must appear (extra words allowed); with no denial it must start with `none` or `n/a` | an explanation after the receipt does not change it; an invented or lost denial still fails |
| worker-report-v5 | `Baseline needed` on a green report failed | allowed only as `none`/`n/a` | an empty field is no claim; a real baseline claim on green still fails |
| pepe-lock-v2 | every non-heading line in parked.md had to name a session idea | a prose line that is not a list item and names no idea is allowed | a caveat ("Revisit triggers below are suggestions") parks nothing; every list item must still name a session idea, once, with a revisit trigger |
| anatomy-plan-v2 | part groups had to be exactly the five | all five must be present; extra group labels allowed | the skill names the groups to cover, not a closed set; part count, unique names and the lit subject part are unchanged |

Kept strict on purpose: duplicate contract fields (two values are ambiguous), the exact result.json rows, the
`Branch + commit` claim, unrequested extra files, changed inputs and the parent-policy row key sets.

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

Policy `config/rsi-verdict-policy.json` (`rsi-verdict-policy/2`; its version, sha256, `min_pairs` and
`min_score_margin` go into every record). Only the current version loads. Records keep the version and hash, and
nothing reads an old policy file back.

**Scores, not bits.** Each run gets a check score from 0 to 1. The score is the share of the case's scenarios the
run passed: `check_score`, or `check_scenarios` passed/total, from the run record. A record without either
(every record before scenario scores) scores 1.0 or 0.0 from `check_passed`, so old records count exactly as
before. A case's arm score (`a_score`, `b_score`) is the sum over its complete pairs: 0..5 with 5 pairs.
`a_pass`/`b_pass` (runs that passed every scenario) stay in the record for reporting.

**Noise margin, the same both ways.** Each case has a delta, `b_score - a_score`. The checks move only when the
total delta over all cases reaches `min_score_margin` (2.0), or when one case alone regresses by it. The margin
is in summed score units, not a rate: two full runs' worth. So with 5 pairs, one flipped run is noise in both
directions. A 5/5 → B 4/5 is not WORSE, and A 4/5 → B 5/5 is not IMPROVES. Partial credit counts: A 3.0 → B 5.0
moves. The total lets a multi-case plan move: with 2+2+1 pairs, B +1 on every case (total +3) moves, while
+1, +1, −1 (total +1) does not. A per-case margin could never move a 1-pair case.

In order:

1. Refusals (stale baseline, artifact not exact, experiment mismatch), budget stop, run errors or timeouts,
   missing runs, fewer than `min_pairs` (5) complete pairs over all cases, Jev harness problem → INCONCLUSIVE.
2. Both arms score 0 on every case → INCONCLUSIVE `harness-dead:<cases>`. The checks cannot tell the arms apart,
   so no judge preference or Jev reading may decide (policy key `harness_dead_inconclusive`, default true; the
   2026-10-26 codex replay had A 0/3, B 0/3 and Jev said `improves`). A run that passes some scenarios is alive.
3. Total delta ≤ −margin, or any one case's delta ≤ −margin → WORSE (`regressed:` per losing case, then
   `total-delta:`). A big loss on one case is never averaged away: −2, +2, +2 is WORSE. No judge or Jev reading
   rescues it.
4. Total delta ≥ margin → IMPROVES (`improved:` per winning case, then `total-delta:`) only when the blind
   judge's B win rate is at least 0.5 (ties count half; B wins where a skill-only change never fired count as
   ties) and Jev says `improves` with confidence ≥ 0.5. Otherwise INCONCLUSIVE.
5. Equal checks (total delta inside the margin; `within-noise:` names the cases that differ) → NO_EFFECT. When
   the judge strongly prefers one arm (B win rate ≤ 0.25 or ≥ 0.75) → INCONCLUSIVE `judge-only-preference`.

The judge alone never decides: it can block an IMPROVES the checks found, but never turns equal checks into
WORSE or IMPROVES. Only IMPROVES records the passing verdict `improves`.

**Across providers** (`rsi_provider_replay.combined_verdict`): each required provider gets its own verdict;
scores are never pooled. Any INCONCLUSIVE → INCONCLUSIVE; else any WORSE → WORSE; IMPROVES only when every
provider IMPROVES; else NO_EFFECT. A provider's WORSE now needs the margin, so one flipped run on one provider no
longer sinks the change.

**Effect on the 2026-10-25..27 nights** (`tests/test_rsi_verdict_nights.py`, recorded inputs). Every night ran 3
pairs per provider, so the shipped policy reads each one as `too-few-pairs:3<5`. With the night's own 3-pair
minimum and the policy/2 rules, 5 of 14 provider verdicts change. Four single-run flips become NO_EFFECT: two
were WORSE, one IMPROVES, one INCONCLUSIVE (the judge disagreed). One judge-only WORSE (checks 1/3 = 1/3, judge
0/3 for B) becomes INCONCLUSIVE. Combined, the three WORSE experiments become NO_EFFECT (two) and INCONCLUSIVE
(one). None of the three nights had a regression or improvement of two runs' worth.

A replay that evaluated the artifact and could not record (for example a non-exact candidate arm) moves an
older `sandbox-replay.json` aside (`.superseded-<time>`), so no stale pass survives. A caller mistake (wrong
experiment id, Docker down) leaves an existing record alone.

## Evidence

- `<root>/replays/<E>/replay.json`: config, policy, verdict with its exact inputs, arms, totals, every run.
- `<root>/replays/<E>/runs/<run_id>.json`: exit code, duration, tool calls, tokens, cost, check result, diff,
  final message, the instruction files the stub read (sha256).
- `<root>/replays/<E>/judge/<case>-r<rep>.json`: the blinded prompt, the order (AB/BA), the raw and mapped winner.
- `<root>/replays/<E>/jev.json`: the Jev state (numbers only) and answers. Each case line ends with a derived
  `checks:` reading (`both arms failed every check`, `both arms passed every check`, `B passed more`, `A passed
  more`, or the same number), and `checks overall` reads all cases, so Jev does not count. The result keeps the
  model that answered and its input/output tokens (`model`, `tokens_in`, `tokens_out`). `log.jsonl`: the
  execution log.
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
