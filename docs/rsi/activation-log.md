# RSI V1 activation log (Mission 003.5)

One entry per production change. Each change was approved by Vilmer in chat before it ran. The plan
and the rollback for every step are in [activation.md](activation.md).

## Phase A: preflight (2026-10-16, read-only)

- Branches:
  - agent-config `rsi/m003-stage` @ `9655fcd`;
  - agent-memory `rsi/m003-integration` @ `eacfeee`.

  Both are clean. Neither `main` has moved since Mission 003 (local = origin: `907d780`, `32ea623`).
- Tests:
  - agent-memory 1431 passed (integration, with the real improve);
  - agent-memory main 1371 passed;
  - sandbox isolation 110/110;
  - `improve` selftest pass, e2e-promote 30/30, e2e 49/49;
  - edge function 7/7.
- Writers: the inventory in [writers.md](writers.md) holds.
  - `context-governor` (launchd KeepAlive) writes only its own state dir, so it is not a writer.
  - `evals-parent` is a read-only evaluation (P6 restores files).
  - W1 kept 5 changes on Oct 15–16: `imp-20261015-1..3` and `imp-20261016-1..2`.
- Hosted DB: 22 migrations, the same files as the branch; only `20261022010000_rsi_core.sql` is new.
  - The migration is compatible with running code: the two `select *` readers of `sessions` read by key.
  - Daily physical backups exist; point-in-time recovery is off.
- No improvement job was running. The improve write lock was free. Disk had 412 GB free. Docker 29.8.1.
- Already failing before this work: launchd `config-check` and `secret-scan` exit 1.

## Phase B: freeze (2026-10-16 18:10–18:13 CEST), approved in chat

Backup: `~/.claude/parent/freeze-backup/20261016-180523`, which holds criteria.json, both amem-daily
plists, scheduled-tasks.json, the agent-config HEAD, and SHA256SUMS.

| # | Change | Where | Commit |
|---|--------|-------|--------|
| 1 | `max_changes_per_week`: null → 0 | `~/agent-config/improve/criteria.json` | `afcb14a` (local main, not pushed) |
| 2 | removed `AMEM_EDITS_LIVE` from amem-daily; installed and reloaded (`bootout` + `bootstrap`) | `~/agent-config/launchd/com.vilmerfrost.amem-daily.plist` → `~/Library/LaunchAgents/` | `319fe0a` (local main, not pushed) |
| 3 | `ab-lessons-monthly` disabled | Desktop scheduled task | none (app state) |

**Side effect of the verification:** `improve list` caches eval costs in `improve/ledger.jsonl` and
commits them (`bin/improve:953`). That made commit `e125603` (ledger only, 5 lines, no config).

**Verification:**
- `amem rsi freeze-status` prints `FROZEN` (exit 0).
- `improve apply --dry-run` returns `refused: weekly cap reached (0 changes in 2026-W42)`.
- `improve list` shows "Weekly cap: 5 of 0 changes used".
- amem-daily plist:
  - the change in meaning is exactly one key, removed (checked against the backup with plistlib);
  - the installed copy = the repo copy;
  - `launchctl print` shows no `AMEM_EDITS_LIVE`, and still shows `AMEM_JEV_LIVE=1`,
    `AMEM_DEVIATIONS_LIVE=1`, and the 09:20 schedule.
- Desktop tasks: only `ab-lessons-monthly` changed (enabled=False). `workflow-improve-weekly` stays on:
  its applies are refused, and its recheck/revert, autopin, fence-check and report keep running.
- No unrelated production file changed:
  - the 47-file target manifest before and after differs only in the amem-daily plist;
  - in `~/Library/LaunchAgents`, only the amem-daily plist changed;
  - the agent-config tree is clean;
  - `907d780..HEAD` = `afcb14a`, `e125603`, `319fe0a`.
- Protective rollback: `cap_reached()` is consulted only by `apply` (`bin/improve:1081`) and by the
  list/report text. `revert`, `recheck`, `fence-check` and `autopin` never check it. amem-ingest
  (undo-poll, P3) is unchanged.
- Ingestion: the amem-ingest plist and script are unchanged. The job is loaded, with last exit 0 and
  221 runs. The first run after the freeze finished at 18:17:38 with exit 0 (runs 222), with no FAIL
  line in `routines.log`. The hosted `sessions` table holds activity from 18:14 CEST, ingested by
  that run.
- JEV scoring and deviation detection run in amem-daily at 09:20. Their flags and schedule are
  unchanged. The first frozen run is 2026-10-17 09:20. Expect its `routines.log` line to have
  `scored=` and `deviations=`, and no `edits=` field.

**Undo:** "Undo the freeze" in [freeze.md](freeze.md), using the backup above.
