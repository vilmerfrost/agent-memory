# Merge plan: `rsi/m004c` into `main` (Mission 004C, G-0)

Prepared 2026-10-17 by the `m004c-mergecheck` worker on branch `worktree-m004c-mergecheck`. Nothing here was
merged into `main`; no ref other than the worker branch moved. **The ownership fix is still landing on
`rsi/m004c`. Re-run `scripts/merge_check.sh` on the final tip and re-read sections 2, 6 and 7 before
merging** (see section 11).

## 0. Verdict

| Check | Result at tip `09413ba` |
|---|---|
| Trial merge (`git merge --no-ff`) | clean, 0 conflicts; merged tree `3aac695` |
| Linear rebase (comparison) | 1 conflict (`.gitignore`); same final tree |
| Full suite on the merged tree | **1956 passed, 1 skipped, 0 failed** (357.7 s) |
| Supabase migrations | none added or changed |
| Dependencies (`pyproject.toml`, `uv.lock`) | unchanged |
| Changes to existing files | `src/agent_memory/cli.py` (+4 lines) and `.gitignore` (+2 lines); everything else is new files; 0 deletions |
| Routines that call new code | none (repo plist, `amem-ingest`, `amem-archive`, `rsi-nightly`, scheduled tasks) |
| Can anything send after the merge without a person typing a command? | no |

Recommendation: merge with a merge commit (`--no-ff`), pinned to the reviewed SHA (section 8).

## 1. Inputs

| Ref | SHA |
|---|---|
| `main` (local) | `5e6a24f96bd2f5f2f3c48e57b1ea5e2c3b743155` |
| `rsi/m004c` (reviewed tip) | `09413ba3669833a02a84586b3363444c18b84715` |
| merge base | `85944053367edcd168755908aebaada997ab62ba` |
| ahead / behind | 47 commits ahead (42 non-merge, 5 merges); 2 behind (`6228567`, `5e6a24f`) |
| trial merge commit (worker branch) | `6585810` |

Local `main` is already 32 commits ahead of `origin/main` (`32ea623`); this plan does not push anything.
`rsi/m004c` is checked out in the m004b worktree (`~/worktrees/agent-memory/m004b`): do not delete or move it.

The two `main` commits change `daily.py`, `deviations.py`, `cli_deviations.py`, `docs/daily.md`,
`docs/deviations.md` and three deviation tests. `rsi/m004c` touches none of these files.

## 2. Commits `main..rsi/m004c` (oldest first)

| SHA | Area | Subject |
|---|---|---|
| 579f84a | core | integrations: shared core for Mission 004A (refs, evidence facade, sink loop, HTTP client, test mock) |
| 40408cb | ncm | integrations: NCM research-run contract (results.json) and its evidence mapping; the NCM track |
| e4b0a77 | cli | integrations: amem sync run\|status\|verify and amem evidence query (lazy adapter registry) |
| efcc1cc | langfuse | integrations: Langfuse sandbox-tracing adapter (Mission 004A) |
| a87c954 | clickhouse | integrations: ClickHouse adapter (allowlisted sink, named-query retriever, verify parity) |
| 5cfb5a4 | merge | Merge rsi/m004a-ch: ClickHouse adapter |
| 493d470 | merge | Merge rsi/m004a-lf: Langfuse adapter |
| 1e9a8ff | pastdev | integrations: past.dev decision-memory adapter and record sanitizer (Mission 004A) |
| c0281ac | cli | integrations: CLI factories for the ClickHouse and Langfuse adapters; verify uses ClickHouse's report |
| cfbc969 | merge | Merge rsi/m004a-pd: past.dev adapter and sanitizer |
| dd76d53 | cli | integrations: wire the past.dev adapter into the CLI; CLI tests before activation (7) |
| d8313a1 | docs | docs(integrations): Mission 004A test report (1658 passed) and the backfill/activation plan |
| b20b201 | core | Mission 004B: cloud-first adapters, questions layer, NCM worker (not connected) |
| e7eca35 | langfuse | langfuse: agent final response stays local by default; leak tests for every trace field |
| 32d3d7a | docs | docs(integrations): G-CH1 rebuilt around the existing ClickHouse Cloud org (read-only account check) |
| 01dfcfc | docs | docs(integrations): G-CH1 done: amem-rsi (eu-west-1, 1 x 8 GiB pinned, idle 5 min, IP list 1 location), org spend alert $100 |
| 4898e32 | clickhouse | clickhouse: G-CH2 prep: admin schema and writer user as separate steps, sha256_hash from the env key, grants probe |
| f8468f0 | docs | docs(integrations): G-CH2 done: amem schema, amem_writer (SELECT, INSERT on amem.*), 11/11 grant probes ok |
| 5b8df09 | ncm | ncm: F1 the Modal script imports in the container (/root/modal_sudoku.py has two parents) |
| 2a1f83b | ncm | ncm: F2 cap every billed phase: startup 120 s, run 600 s, idle 2 s, one container |
| 17e3c31 | clickhouse | clickhouse: F1 deviations: team repo-scoped sources ship as \<prefix\>:team with no rule_id, own repos as pseudonyms |
| 3ca2239 | pastdev | pastdev: F2 a lesson from a team repo is a team item (read side) |
| a1aa7fc | ncm | ncm: F3 a raising evaluate.remote() still writes a failed results.json |
| f991376 | ncm | ncm: F4 a failed volume.commit() is a limitation line, not a lost result |
| f5a897e | ncm | ncm: F5 decode TimeoutExpired output before logging it |
| b12fa0b | ncm | ncm: F6 exact pins for every eval dependency, plus the pip freeze hash |
| 49c3e55 | ncm | ncm: F7 run_id carries the time (ncm-YYYYMMDD-HHMMSS-sudoku-easy) |
| 0a52cd1 | ncm | ncm: F8 ignore ncm-results/ (main() writes the local copy inside the repo) |
| d530919 | ncm | ncm: F9 docs: valid volume undo, budget as the outer cap, one cap pair, sudoku paths |
| 4434b48 | egress | integrations: F3 egress deny list (AMEM_EGRESS_DENY plus team repos from Supabase) for every sink |
| ad8dfb8 | pastdev | pastdev: F5 content-based idempotency key, so a replay of unchanged records ingests nothing new |
| 5b26539 | pastdev | pastdev: G-PD0 follow each ingestion by id; sent only when completed; free retry; no cap retries |
| af1e986 | sync | sync: F4 --tables and --since on amem sync plan and amem sync run |
| 191f6b7 | docs | docs(integrations): past.dev PD0 behaviour after G-PD0 (dead letters, free retry by id, sent only when completed) |
| ad60ca5 | egress | sanitize: rewrap the module docstring (F3 follow-up, no code change) |
| 81c3ab7 | synth | synth: synthetic RSI dataset, C1 ClickHouse runner, evidence-ask demo (Mission 004C) |
| b901bdc | docs | docs(integrations): C1 runner and repeat_failures in cloud-readiness (Mission 004C) |
| d81768d | docs | docs(integrations): note the seventh question |
| 695820c | merge | Merge worktree-m004c-synth (W2): synthetic RSI dataset, repeat_failures question, C1 runner, evidence-ask demo |
| 6968d0e | merge | Merge worktree-m004c-egress (W1): egress deny list and team masking (F1-F3), past.dev correctness (G-PD0, F5), sync --tables/--since (F4) |
| 87baa67 | cli | cli: evidence help counts no fixed number of questions; --record reports rows incl. the answer |
| 9cf13aa | docs | docs(integrations): Mission 004C report (ClickHouse G-CH2, shared-intelligence demo, past.dev/backfill/NCM readiness, next approvals) + research appendices |
| b2f0de0 | egress | egress: unknown repository ownership is private on export (Mission 004C, F8, F7, F9) |
| 5033938 | docs | docs(integrations): 004C resume: overnight health, ClickHouse C1 PASS (79 synthetic rows, 8/8 queries, 0 leaks), F8 status, past.dev G-PD1 request |
| 2bad922 | docs | docs(integrations): 004C owner decisions (C1 accepted, past.dev approved); G-PD2 stop: key's project has no caps, C3 not started |
| 5a331ad | scripts | scripts: C3 past.dev runner on the synthetic dataset (Mission 004C, G-PD3 prep) |
| 09413ba | docs | docs(integrations): 004C past.dev C3 PASS (11.6 credits, 0 leaks, all deleted), Phase I deviation-writer investigation, dv13dedee vs K7 |

By area: core 2, cli 4, clickhouse 3, langfuse 2, pastdev 4, egress/sync 4, ncm 10, synth/scripts 2, docs 11, merges 5.

Regenerate: `git log --format='%h %s' --reverse main..<tip>`.

## 3. Diffstat by area (`git diff --shortstat main...rsi/m004c -- <path>`)

| Area | Files | Lines |
|---|---|---|
| `src/agent_memory/integrations/` (new package: clickhouse, langfuse, pastdev, ncm, ncm_worker, egress, sanitize, sync, evidence, refs, http, questions, sb_queries) | 14 | +5372 |
| `src/agent_memory/` CLI and synth (`cli.py` +4, new `cli_integrations.py`, `cli_synth.py`, `synth.py`) | 4 | +1539 |
| `tests/` (all new files) | 19 | +6153 |
| `scripts/` (`c1_clickhouse.py`, `c3_pastdev.py`, `demo_evidence_ask.py`) and `deploy/ncm/modal_sudoku.py` | 4 | +1881 |
| `docs/integrations/` | 12 | +2470 |
| `.gitignore` (`ncm-results/`, `synth-out/`) | 1 | +2 |
| **Total** | **54** | **+17417, -0** |

## 4. Trial merge and rebase

**Merge (recommended).** `git merge --no-ff --no-edit rsi/m004c` on `main` (`5e6a24f`): clean, 0 conflicts. No file
is changed on both sides since the merge base. Merged tree: `3aac695` (deterministic: the same tree came out of
two separate trial merges).

**Linear rebase (comparison only, detached scratch worktree, discarded).** `git rebase main` from `09413ba`
replays 42 commits and drops the 5 merges. One conflict, at `81c3ab7` (synth), in `.gitignore`: the NCM branch
added `ncm-results/` and the synth branch added `synth-out/` on the same line. Merge `695820c` had already
solved this; a linear rebase meets it again. Resolution: keep both lines (`ncm-results/` then `synth-out/`),
which is what `rsi/m004c` has. The rebased tip's tree equals the merged tree (empty `git diff`).
`git rebase --rebase-merges main` stops on the same conflict at the replayed merge `695820c`; aborted.

## 5. Tests

| Tree | Command | Result |
|---|---|---|
| trial merge `6585810` (tree `3aac695`) | `uv run --quiet pytest -q -p no:cacheprovider` (Docker) | 1956 passed, 1 skipped, 0 failed, 357.7 s |
| `scripts/merge_check.sh` (fresh throwaway worktree, same tree `3aac695`) | the same suite | 1956 passed, 1 skipped, 0 failed, 355.5 s |

No failures, so no base runs on `main` or `rsi/m004c` were needed. For scale: the 004C report recorded 1874
passed on `87baa67`.

The suite is isolated from production by `tests/conftest.py:71-80`: it removes every `AMEM_*`, `PG*`,
`TYPESAFE_*` and `SUPABASE_*` variable, sets `AMEM_NO_DOTENV=1`, and points the Keychain at a tripwire. Each
run starts its own throwaway Postgres container.

## 6. Impact on production after the merge

Production = the main checkout's working tree. launchd runs `uv run --directory ~/Projects/agent-memory amem ...`
from it: `amem daily` at 09:20, `amem-ingest` every 15 min, `amem-archive` at 08:45, `rsi-nightly` at 03:30, and
the session hook (`amem sessions ping`).

### 6.1 What a routine runs: unchanged

- **Only `cli.py` wires the new code.** `git grep -n -E 'integrations|synth' -- src/agent_memory` (without the new
  files) finds only `cli.py:24`, `cli.py:30` (imports) and `cli.py:205`, `cli.py:207` (`register`). Other hits
  are the old, unrelated `<synthetic>` model name in `amem_digest.py` and `session_tokens.py`.
- **`amem daily` is unchanged.** `cli_daily.py:15` calls `daily.run_daily`. `daily.py:54-55` imports
  `cli_cloud, config, deviations, distill, jev, scoring, sessions_ingest, store, tasks_ingest` and
  `agent_memory.questions`. That is the old `questions.py`, not the new `integrations/questions.py`. No file on
  the daily path is in the diff.
- **Repo plist** `deploy/launchd/com.vilmerfrost.amem-daily.plist:8-16`: `uv run --directory ~/Projects/agent-memory
  --quiet amem daily`. Not changed by `rsi/m004c`.
- **Live routines (read-only check of `~/agent-config`).** `bin/amem-ingest:60,65,74` runs `amem ingest
  routines-log`, `amem sessions ingest --days 2` and `amem edits undo-poll`. `bin/amem-archive:55,58` runs
  `amem archive run` and `amem archive verify`. `bin/rsi-nightly:89,90,100,115` runs `ingest skills`,
  `skills scores`, `skills cache` and `evals harvest`. The launchd plists `amem-ingest`, `amem-archive`,
  `rsi-nightly` and `amem-daily` only start these. A grep for `amem sync|evidence|synth`, `sync run`,
  `evidence ask`, `synth seed`, `c1_clickhouse`, `c3_pastdev` and `modal run` in `~/agent-config/bin`,
  `~/agent-config/launchd` and `~/.claude/scheduled-tasks` finds nothing.
- **Cloud hook** (`deploy/cloud/setup.sh`) fetches `amem_digest.py` from Supabase `amem_assets`. It does not use
  this code.

### 6.2 What every `amem` call now loads

`cli.py:24,30` imports `cli_integrations` and `cli_synth` at module load. That happens for every `amem` command,
including `amem daily` and the session hook's `amem sessions ping`, which runs before `main()` parses arguments
(`cli.py:223-227`). The result is 10 more modules at startup: `cli_integrations`, `cli_synth`, `synth`,
`integrations`, `integrations.clickhouse` (via `synth.py:70`), `.egress`, `.evidence`, `.http`, `.refs` and
`.sync`. `langfuse`, `pastdev`, `ncm`, `questions` and `sanitize` load only inside a command
(`cli_integrations.py:44-57,95-105,267,274,306`).

Probe (`import agent_memory.cli` with every socket connect refused):

| Tree | agent_memory modules | sockets opened | import time (warm) |
|---|---|---|---|
| `main` 5e6a24f | 55 | none | 0.31 s |
| merged | 65 | none | 0.30 s |

These modules do only stdlib, `psycopg` and `agent_memory.config` work at import time: constants, compiled
regexes, one SQL string (`clickhouse.py:264`) and one lock (`clickhouse.py:596`). Nothing reads env or opens a
connection. The remaining risk: an ImportError in any of the 10 modules would break every `amem` command,
`amem daily` included. The import probe and the suite rule this out for the reviewed tree. `merge_check.sh`
re-checks it on any new tip.

### 6.3 New commands (none run unless a person types them)

| Command | Can send to a provider | Writes Supabase |
|---|---|---|
| `amem sync run --sink clickhouse\|langfuse\|pastdev` | yes, if that sink is configured | yes (`rsi_checkpoints`, `rsi_evidence`) |
| `amem sync verify --sink clickhouse`, `sync grants` | reads/probes ClickHouse, if configured | no |
| `amem sync plan` | no: read-only connection, the provider is never called (`sync.py:298-310`) | no |
| `amem sync schema` | no (prints SQL) | no |
| `amem sync status` | no | no (read) |
| `amem evidence query` / `ask` | reads configured providers | `ask --record` writes `rsi_evidence` |
| `amem evidence record-ncm <file>` | no | yes (`rsi_evidence`), unless `--dry-run` |
| `amem synth seed --db-url` | no | no: refuses any non-local host (`synth.py:114-146`) |
| `scripts/c1_clickhouse.py`, `c3_pastdev.py`, `demo_evidence_ask.py` | the configured provider | local DB only (`--db-url` localhost) |
| `modal run deploy/ncm/modal_sudoku.py` | Modal (needs the separate `modal` package, which is not in `pyproject.toml`) | no |

### 6.4 Flags and settings: default state

A sink sends only when **(a)** a person runs `amem sync run` (or a script above) **and (b)** its settings are
present **and (c)** its kill switch is not `off`. Unset settings give `unavailable` before any request. The
kill switches are opt-out (unset = not off), so the actual gate is (a) plus (b).

| Name | Read at | Unset means |
|---|---|---|
| `AMEM_SYNC_CLICKHOUSE`, `AMEM_SYNC_LANGFUSE`, `AMEM_SYNC_PASTDEV` | `sync.py:85-89` | not switched off. `=off` makes `sync run` return `off` before it reads anything (`sync.py:217-218`) |
| `AMEM_PASTDEV` | `sync.py:89` | as above, for past.dev only |
| `AMEM_SYNC_<SINK>_MAX_ROWS_PER_DAY`, `_MAX_REQUESTS_PER_DAY` | `sync.py:122-133` | client-side caps: ClickHouse 100000/2000, Langfuse 500/1000, past.dev 300/200 |
| `AMEM_CLICKHOUSE_URL` (+ `_USER`, `_KEY`, `_DB`, `_CLOUD`, `_LOOKBACK_DAYS`) | `clickhouse.py:148-164` | `not configured (AMEM_CLICKHOUSE_URL)` (`clickhouse.py:129-130`); `health()` returns before any ping (`clickhouse.py:660-662`, `967-969`) |
| `AMEM_LANGFUSE_HOST`, `_PUBLIC_KEY`, `_SECRET_KEY` (+ `_MIN_INTERVAL_S`) | `langfuse.py:718-727` | `not configured` (`langfuse.py:748-750`); `health()` returns before any call (`langfuse.py:774-776`) |
| `AMEM_PASTDEV_KEY` | `pastdev.py:125-129` | no config unless it matches `past_sk_...`; `health()` = `not configured` (`pastdev.py:525-529`, `769-773`) |
| `AMEM_PSEUDONYM_KEY` | `clickhouse.py:271-276` | no pseudonym (ClickHouse mapping only) |
| `AMEM_EGRESS_DENY` | `egress.py:63`, `156-159` | no extra deny terms (team names from Supabase still apply); can only withhold more |
| `AMEM_OWNER_ACCOUNTS` / `AMEM_PERSONAL_OWNERS` | `egress.py:80-88` | `vilmerfrost`; can only narrow what counts as own |
| `AMEM_TLS_CA_FILE` | `http.py:26`, `46-47` | system trust store |
| `RSI_SANDBOX_DIR` | `cli_integrations.py:53` | Langfuse sink has no sandbox dir |
| NCM | none | NCM has no flag. It runs only through `modal run` by hand |

### 6.5 Env keys for the main checkout

`.env.example` is unchanged and lists none of the names above. **The merge needs no new key in
`~/Projects/agent-memory/.env.local`, and none should be added.** The 004C provider secrets were put in the
**m004b worktree's** `.env.local` (`docs/integrations/cloud-activation.md:30,37`;
`m004c/pastdev-research.md:95`), not in the main checkout. This worker did not read any env file. After the merge,
`scripts/sinks_configured.py` (section 10) proves it through the app's own env loading. It prints yes/no only,
never a value, and never opens a socket.

If a later activation step puts keys in the main checkout, the names are: `AMEM_CLICKHOUSE_URL`,
`AMEM_CLICKHOUSE_USER`, `AMEM_CLICKHOUSE_KEY`, `AMEM_PSEUDONYM_KEY`, `AMEM_LANGFUSE_HOST`,
`AMEM_LANGFUSE_PUBLIC_KEY`, `AMEM_LANGFUSE_SECRET_KEY` and `AMEM_PASTDEV_KEY`. Each one needs its own approval
(docs/integrations/activation.md).

### 6.6 Not changed by this merge (noted for completeness)

- `amem rsi freeze-status`, every RSI writer and the freeze code (`cli_rsi.py`, `rsi_targets.py`, `rsi_*`): not
  in the diff. `amem doctor` (`cli.py:63`): unchanged, because the `cli.py` diff is only the 4 wiring lines.
- The repo plist sets `AMEM_EDITS_LIVE=1`, but the live `~/agent-config/launchd/com.vilmerfrost.amem-daily.plist`
  does not. This drift existed before the merge and the merge does not change it.
- `secret-scan`: the 004C report already judged the fake keys in the new leak tests as false positives
  (`m004c-report.md:156-157`). The merge commit adds no new lines of its own.

## 7. Migrations and dependencies

- `git diff --name-status 8594405 09413ba -- supabase` prints nothing. No migration is added, changed or
  removed. The new code uses only existing tables. `rsi_checkpoints`, `rsi_evidence`, `rsi_candidates`,
  `rsi_eval_results` and `rsi_decision_results` come from `supabase/migrations/20261022010000_rsi_core.sql:35-169`.
  The others (`sessions`, `deviations`, `runs`, `lessons`, `lesson_candidates`, `edits`, `skill_calls`,
  `rsi_proposals`, `rsi_decisions`, `rsi_experiments`) are older (`integrations/__init__.py:12-14`).
- `git diff --name-only main...rsi/m004c -- pyproject.toml uv.lock` prints nothing. Dependencies are unchanged.
  `uv run` re-syncs nothing, and the editable install picks up the new modules at once.
- So **nothing hosted needs to change** for the merge: no `amem db migrate`, no Supabase, ClickHouse, Langfuse
  or past.dev step.

## 8. Merge commands (parent, in the main checkout)

Strategy: **a merge commit (`--no-ff`)**, not a rebase. Reasons:

1. It merges cleanly. A rebase hits a `.gitignore` conflict that was already solved once.
2. It keeps every SHA. The 004C docs cite SHAs (`m004c-report.md:108-117`: `4898e32`, `f8468f0`, `81c3ab7`,
   `695820c`, `87baa67`, `b2f0de0`, ...). A rebase would rewrite all 42 and break those references, and
   `rsi/m004c` (checked out in the m004b worktree) would no longer be an ancestor of `main`.
3. It is one unit to undo (`reset --keep` locally, or `revert -m 1` once pushed).
4. The final tree is identical either way (verified). A fast-forward is impossible because `main` has 2 commits
   that `rsi/m004c` lacks.

Timing: avoid 08:40-09:30 (archive 08:45, daily 09:20). `amem-ingest` runs every 15 min. The merge only adds
files and 4 lines in `cli.py`, so a run that starts during the checkout fails at worst once (ImportError) and the
next run succeeds.

```sh
# 0. Re-verify on the final tip (section 11). Note the tip SHA and the "clean: <commit> (tree <T>)" line.
~/worktrees/agent-memory/agent-ab8bab3d37ad6a1b6/scripts/merge_check.sh rsi/m004c main

# 1. Preconditions in the main checkout
git -C ~/Projects/agent-memory status --porcelain            # must print nothing
git -C ~/Projects/agent-memory rev-parse --abbrev-ref HEAD   # main
git -C ~/Projects/agent-memory rev-parse HEAD                # 5e6a24f96bd2f5f2f3c48e57b1ea5e2c3b743155
git -C ~/Projects/agent-memory rev-parse rsi/m004c           # must equal the tip merge_check.sh verified

# 2. Merge the verified SHA (not the moving branch name)
git -C ~/Projects/agent-memory merge --no-ff -m "Merge rsi/m004c: Missions 004B+004C integrations (not wired into any routine)" <VERIFIED_TIP_SHA>

# 3. Same content as the tested tree
git -C ~/Projects/agent-memory rev-parse --short 'HEAD^{tree}'    # must equal <T> from step 0 (3aac695 for tip 09413ba)
```

Do not push. Do not delete `rsi/m004c`. Optional: bring this plan and the two scripts onto `main` with
`git -C ~/Projects/agent-memory cherry-pick <worker docs commit>`. That commit only adds 3 files.

## 9. Rollback

Local only. Nothing hosted was changed, so nothing hosted needs undoing.

```sh
git -C ~/Projects/agent-memory status --porcelain     # must print nothing (commit or move any local edits first)
git -C ~/Projects/agent-memory reset --keep 5e6a24f96bd2f5f2f3c48e57b1ea5e2c3b743155
git -C ~/Projects/agent-memory rev-parse HEAD         # 5e6a24f96bd2f5f2f3c48e57b1ea5e2c3b743155
```

- `reset --keep` moves only the checked-out `main`. It refuses to overwrite uncommitted changes. `rsi/m004c`,
  `integration` and every worktree branch stay where they are. The merge commit stays in `git reflog main`.
- A merge that stopped half-way: `git -C ~/Projects/agent-memory merge --abort`.
- If `main` was pushed after the merge (not part of this plan), do not reset. Use
  `git revert -m 1 <merge sha>` and push through the normal flow. Never force-push.
- Leftovers: if someone ran `amem synth seed` or the NCM script in the main checkout, `synth-out/` or
  `ncm-results/` stop being ignored after the rollback. Delete them by hand.
- If someone ran `amem sync run` from `main` in the meantime, that is an activation, not part of the merge. Undo
  it per `docs/integrations/activation.md:91` (kill switch, then delete the `sync:<sink>` cursor rows).

## 10. Post-merge checks (send nothing new)

```sh
git -C ~/Projects/agent-memory diff --shortstat 5e6a24f HEAD            # 54 files changed, 17417 insertions(+) for tip 09413ba
uv run --directory ~/Projects/agent-memory --quiet amem --help          # exit 0; lists sync, evidence, synth
uv run --directory ~/Projects/agent-memory --quiet amem daily --help    # exit 0; sends nothing
uv run --directory ~/Projects/agent-memory --quiet amem doctor          # same jev mode, db host and reachability as before the merge
uv run --directory ~/Projects/agent-memory --quiet amem rsi freeze-status   # exit 0, FROZEN
uv run --directory ~/Projects/agent-memory --quiet python ~/worktrees/agent-memory/agent-ab8bab3d37ad6a1b6/scripts/sinks_configured.py
                                                                        # "sinks that could send from this checkout: 0", exit 0
uv run --directory ~/Projects/agent-memory --quiet pytest -q            # background; Docker; 1956 passed, 1 skipped at tip 09413ba
```

- `amem daily` has no dry run. `--no-push` still writes Supabase and the routines log. Do not run `amem daily`
  by hand. Let the 09:20 run do it, then check its line in `~/.claude/parent/routines.log` and
  `~/.claude/parent/reports/launchd.log`.
- After the next `amem-ingest` tick, check that `launchd.log` shows rc=0.
- If `sinks_configured.py` exits 1, the main checkout's `.env.local` holds provider settings. Nothing sends
  until someone runs `amem sync run`, but decide: remove the keys (through the `request_secret` flow) or set
  `AMEM_SYNC_<SINK>=off`.
- The suite in the main checkout is safe: `conftest.py` drops every `AMEM_*` variable and disables env files.

## 11. Open items carried into `main`

- **Ownership fix in progress** (separate worker, on `rsi/m004c` now). Ingestion stores the remote owner but not
  the host, so `gitlab.example/vilmerfrost/x` classifies as own (`m004c-report.md:88-89`; `egress.py:19-20`).
  **This plan is valid only for tip `09413ba`.** On the final tip:
  1. Run `scripts/merge_check.sh rsi/m004c main`. It must report a clean merge, the import probe with no
     sockets, and a green suite.
  2. Read its "production-relevant paths" and "existing modules modified" lines. A fix in `sessions_ingest.py`
     changes `amem-ingest` (every 15 min) and `amem daily`. It is then a production change and needs its own
     review. A new file in `supabase/migrations/` means the hosted `amem db migrate` is part of the merge
     (standing permission: apply after the merge, then `amem doctor` and a read-back). It also means a rollback
     needs a down-migration decision.
  3. `git log --format='%h %s' 09413ba..rsi/m004c`: add these commits to section 2.
- **F6**: no CLI to dump or delete the past.dev corpus (`m004c-report.md:88`).
- **`amem db migrate` Keychain bug**: it reads the production Keychain admin item even for a local URL
  (`m004c-report.md:122-123`). The suite is protected by its tripwire (`conftest.py:45-50`). Any manual local
  migrate (synth, C1, C3, demo) must keep using `AMEM_ADMIN_SECURITY=<tripwire>`.
- **K7/K4** (push/merge guard area), plus the `dv13dedee-push-and-merge-policy` eval (`m004c-report.md:141,154,265-267`).
  These are not part of this merge. They have their own mission.
- Known limits from the report (`m004c-report.md:119-126`): C1 never ran against a real ClickHouse server, the
  retriever checks only `project`/`rule` against the deny list, and `evidence.record()` drops `finished_at`.
- The kill switches are opt-out. Before any recurring sync is approved, decide whether the routine should also
  set `AMEM_SYNC_<SINK>=off` by default.

## 12. Re-running the check

`scripts/merge_check.sh [TIP] [BASE]` (defaults `rsi/m004c main`) creates a throwaway detached worktree under
`$TMPDIR` and works there:
1. It resolves both SHAs and prints ahead/behind.
2. It lists production-relevant paths and changed existing modules, and the files changed on both sides.
3. It runs `git merge --no-ff` and prints conflicts or the merged tree hash.
4. It runs the import probe and `amem --help` / `amem daily --help`.
5. It runs the full suite. `MERGE_CHECK_SKIP_TESTS=1` skips this step.

It moves no ref, runs with no env file, and removes the worktree on exit. It can run from any checkout of the
repo, because it uses the repo that contains the script.
