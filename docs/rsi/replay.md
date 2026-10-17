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

## Live replay (unverified in Mission 005)

Live agents and judges exist behind flags and were not run (no paid calls without approval). After approval:

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

Not verified: the live agent and judge command lines inside the worker, Codex `-s read-only` for the judge,
tool-call parsing of live Claude stream-json and Codex events, Jev live answers for a replay state.
