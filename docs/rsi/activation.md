# RSI V1 activation checklist and rollback plan

Mission 003 built and tested everything below. **None of it is active.** Each step changes
production, so each one needs your explicit approval when you run it. Do them in order. Stop at any
failed check.

Status: step 1 (freeze) done on 2026-10-16. `amem rsi freeze-status` prints `FROZEN`. Every other
step is still open. Progress and evidence: [activation-log.md](activation-log.md).

## Checklist

### 1. Freeze the current writers
Follow [freeze.md](freeze.md): backup, `max_changes_per_week: 0`, remove `AMEM_EDITS_LIVE` from the
amem-daily plist, disable `ab-lessons-monthly`.
- [ ] `amem rsi freeze-status` exits 0 and prints `FROZEN`.

### 2. Hosted Supabase migration (before any agent-memory merge)
The merged agent-memory code writes `sessions.env`. Merged first, the nightly ingest would fail on the
hosted database. The migration is additive: current code ignores the new columns and tables.
- [ ] Apply `supabase/migrations/20261022010000_rsi_core.sql` (`amem db migrate` against the hosted
  `AMEM_DB_URL`).
- [ ] Check the advisors (RLS on every `rsi_*` table, no anon or authenticated grants).
- [ ] The current amem-daily run still passes.

### 3. Merge the code (no deploy yet)
- [ ] agent-config: review and merge `rsi/m003-stage` into `main`.
- [ ] agent-memory: review and merge `rsi/m003-integration` into `main`. It contains
  `rsi/m003-core`, `rsi/m003-archive` and `rsi/m003-sandbox`.
- [ ] `uv sync` in agent-memory (new dependency: `cryptography>=43`).
- [ ] `improve selftest` and `improve e2e-promote` pass. The agent-memory suite passes, with
  `RSI_TEST_IMPROVE_REPO=~/agent-config` set.

### 4. Local transcript archive
See [../archive.md](../archive.md).
- [ ] `amem archive init`. This creates the Keychain key. Approve the Keychain prompt.
- [ ] `amem archive key-export` in your own terminal (it refuses a non-TTY). Store the recovery string
  offline. Without the key, the archive cannot be read.
- [ ] `amem archive run`, then `amem archive verify`. Both exit 0.
- [ ] Optional: a daily launchd job for `amem archive run`. This is a new plist, so it needs its own
  approval.

### 5. Bring the untracked targets under git (optional, recommended)
- [ ] `amem rsi tracking-plan --out ~/sandbox/run/tracking`. Read `apply.sh`.
- [ ] Run `apply.sh`. Keep the backup dir it prints. `inverse.sh <backup>` undoes it exactly.
- [ ] `amem rsi targets` shows 0 files outside git.

### 6. Phone decisions
- [ ] Deploy the edge function `amem-decide` (`verify_jwt = false`: tokens are checked in SQL).
  Smoke test: a random token returns `denied`.

### 7. Least-privilege worker (closes the main residual risk)
- [ ] Create a login role that is a member of `rsi_writer`. It has no insert on `rsi_decisions` and
  no execute on the CLI decision function.
- [ ] Give `decide-poll` that DSN only, not the superuser `AMEM_DB_URL`.

Until this is done, any process that runs as your user and can read `.env.local` could insert a
decision. See [approval.md](approval.md), "Trust boundary".

### 8. Sandbox
See [../sandbox.md](../sandbox.md).
- [ ] `python3 sandbox/rsi_sandbox.py init`, then `build`, then `net-up`. The isolation suite passes
  (110 tests).
- [ ] Create a **dedicated** sandbox API key with a spend limit. Never use a personal OAuth token.
  Store it yourself:
  `security add-generic-password -s rsi-sandbox -a anthropic -w` (it prompts for the value).
- [ ] One real model run with `--credentials keychain:rsi-sandbox/anthropic` exits by itself.

### 9. Switch improve to stage mode
In `~/agent-config/improve/criteria.json`:
- [ ] `"apply_mode": "stage"` and `"promotion": {"required_suites": ["static", "sandbox-replay"]}`.
- [ ] Set `max_changes_per_week` back from 0 to your old value. Under stage mode the cap limits only
  legacy `apply`. Each promotion is gated by its own human decision.
- [ ] `improve apply` without `--human` is refused. ab-lesson stops writing (`stage_only()`).

### 10. Re-enable the writers in stage mode
- [ ] amem-daily plist: `AMEM_EDITS_LIVE=1` **and** `AMEM_EDITS_MODE=stage`. `bootout` and
  `bootstrap` the job.
- [ ] New launchd job for `amem rsi decide-poll` (for example every 5 minutes).
- [ ] New launchd job for `amem rsi push` after the nightly run (the morning brief).
- [ ] Re-enable `ab-lessons-monthly` only if you want it. Under stage mode it cannot write.

### 11. First real cycle (watch it)
- [ ] One proposal arrives on the phone with Apply / Reject.
- [ ] Reject works and changes nothing.
- [ ] Apply on a harmless target promotes the exact bytes. The improve ledger has a `promoted` event with a `prm-` id.
- [ ] Undo restores the exact old bytes.
- [ ] `amem rsi show <proposal>` shows the full audit trail.

## Rollback plan

Undo the steps in reverse. Each one is independent.

| Step | Undo |
|------|------|
| 11 | Undo each promoted change: phone Undo, `amem rsi decide <id> undo`, or `improve revert-change <prm-id>`. A conflict changes nothing and reports the file. |
| 10 | Restore the plists from the step-1 backup. `bootout` the decide-poll and push jobs. |
| 9 | Remove `apply_mode` (legacy behaviour), or keep `stage` and freeze again. |
| 8 | `rsi_sandbox.py net-down`. Delete the Keychain item. `~/sandbox` holds no production data. |
| 7 | Point the worker back to `AMEM_DB_URL`. Drop the login role. |
| 6 | `supabase functions delete amem-decide`. |
| 5 | `inverse.sh <backup dir>`. |
| 4 | Stop the archive job. The archive never deletes or changes the original transcripts. |
| 3 | `git revert` the merge commits. Do this **before** step 2's undo. |
| 2 | Export `rsi_*` (see the header of [rollback_rsi_core.sql](rollback_rsi_core.sql)), then run that file. It is tested: the RSI objects go, other data stays, and the migration applies again. |
| 1 | "Undo the freeze" in [freeze.md](freeze.md). |

An interrupted promotion is not a rollback case. `improve recover` (and every `decide-poll` run)
finishes it, or restores the snapshot, from the WAL.

## Not covered by Mission 003

- **The `sandbox-replay` suite itself.** The sandbox can build the candidate arm from the exact
  artifact and write a bound evaluation (`prepare-arm --artifact`, `record-eval`). The replay
  workload (cases, scoring, model runs) is Mission 004. Until it exists, nothing is promotable,
  by design.
- **ClickHouse, Langfuse, past.dev, NCM.** No integration yet. The schema is ready for them:
  - `rsi_evidence` takes structured findings from any system (`system`, `external_ref` unique,
    `subject_refs`, `metrics`);
  - every artifact, evaluation and decision has a stable key (artifact hash, experiment id,
    baseline id).
- **ClickHouse Cloud.** These keys and append-only tables map 1:1 to MergeTree tables, so ClickHouse
  Cloud needs no schema change on this side. Nothing was deployed.
- **`rsi-prod-fence`.** A hook that blocks unattended writes to production paths. Not built
  (see [freeze.md](freeze.md)).
