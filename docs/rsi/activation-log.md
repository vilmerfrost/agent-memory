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

## Phase C preparation (2026-10-16, before approval)

A grant gap was found and fixed in the dev branch, before the migration was applied anywhere. Hosted
Supabase grants every new table and function to `anon`, `authenticated` and `service_role` **by name**
(`pg_default_acl`), so the migration's `revoke ... from public` was not enough:
- `service_role` kept `ALL` on all `rsi_*` tables, including insert on `rsi_decisions` and
  `rsi_decision_tokens`;
- `service_role` kept execute on `amem_rsi_record_cli_decision`. The service key could record a
  "terminal" decision;
- `anon` and `authenticated` kept execute on `rsi_insert_decision` (invoker rights, so the table
  revokes blocked it).

The fix revokes everything from all three API roles. It grants `service_role` only execute on
`amem_rsi_record_decision`. A new test recreates Supabase's default privileges on a scratch
database. It failed before the fix and passes after it. [verify_rsi_core.sql](verify_rsi_core.sql)
is the reviewed post-migration check; it leaves nothing behind (ROLLBACK). Suite: 1432 passed.

Hosted baseline before the migration:
- 22 migrations; `rsi_core` not applied;
- security advisors: INFO only (16 tables with RLS and no policy, the intended lockdown);
- `anon` and `authenticated` can read 0 of 16 tables; `service_role` can read 16 of 16;
- 0 policies;
- the latest physical backup is 2026-10-16 03:44 UTC; PITR is off.

## Phase C: hosted migration (2026-10-16 18:27:40 CEST), approved in chat

- **Applied:** exactly `20261022010000_rsi_core.sql` (sha256 `11668e7cab395a996dd4464b105f6de886c46041cb6ea1f50260e6e0ffeba615`,
  commit `5668057`).
  - Command: the main checkout's `amem db migrate --dir <a folder holding only that file>`.
  - One transaction, together with its `schema_migrations` row. Exit 0, about 1 s.
  - `schema_migrations` now holds 23 files; `applied_at` is 2026-10-16T16:27:40Z.
- **[verify_rsi_core.sql](verify_rsi_core.sql)** (sha256 `1c60f6e0…`, same as the commit) ran with no error.
  Every check raises on failure. It checked:
  - 10 `rsi_*` tables, `sessions.env` and `experiment_id`, RLS on every `rsi_*` table;
  - the API roles have no table rights;
  - only `service_role` can run the phone function;
  - the RSI roles have no decision path;
  - wrong token → `denied`; wrong hash → `hash-mismatch`; valid token → `recorded`; replay →
    `already`; proposal → `approved`; update on `rsi_decisions` → append-only error.

  Afterwards the `rsi_*` tables hold 0 rows (the ROLLBACK worked).
- **Rights after the migration**, compared with the baseline:
  - `anon` / `authenticated`: 0 of 26 tables (before: 0 of 16);
  - `service_role`: 16 of 26, the same 16 as before and none of the `rsi_*` tables;
  - `rsi_reader`: select on 17;
  - `rsi_writer`: select on 17, insert on 9 (not `rsi_decisions`);
  - both RSI roles are NOLOGIN;
  - 32 policies, all for `rsi_reader` / `rsi_writer`.
- **Security advisors:** INFO only ("RLS enabled, no policy"), now 8 tables instead of 16. Nothing new.
- **Data:** the 1330 existing sessions have `env = null`, which counts as prod. The `edits` status
  check now allows `staged`.
- **Ingestion after the migration:** the 18:32:47 amem-ingest run exited 0 (runs 223), with no
  FAIL line. It upserted 4 sessions active after the migration, on the new schema, with the main code.
- **Undo**, if ever needed (never run automatically): revert the code first (Phase D), export `rsi_*`,
  then run [rollback_rsi_core.sql](rollback_rsi_core.sql).

## Phase D preparation (2026-10-16, no production change)

- agent-config: `main` (`319fe0a`) and `rsi/m003-stage` change disjoint files, so the trial merge is clean.
  - The legacy `improve e2e` failed with `IndexError`, on frozen `main` as well. The cause: it copied the
    production `criteria.json` (cap 0) into its sandbox.
  - Fix `d7c6ea8`: the e2e pins its sandbox criteria (no cap, legacy mode). The assertions are unchanged.
  - On the trial merge (`63232f7`): selftest pass, e2e-promote 30/30, e2e 49/49, e2e 49/49 again with
    `apply_mode: "stage"`, ab-lesson selftest 11/11.
  - With the merged code and the frozen criteria, `apply --dry-run` is still refused (weekly cap).
- agent-memory: `main` (`32ea623`) is an ancestor of `rsi/m003-integration`, so the merge is a
  fast-forward. Full suite on `601ca55` with the merged improve: 1432 passed. Edge function: 7/7.

## Phase D: merges (2026-10-16 18:48 CEST), approved in chat (local only, no push)

| # | Repo | Change | Result |
|---|------|--------|--------|
| D1 | `~/agent-config` | `git merge --no-ff rsi/m003-stage` | `f87355e`; tree identical to the tested trial merge `63232f7` |
| D2 | `~/Projects/agent-memory` | `git merge --ff-only rsi/m003-integration`, `uv sync` | `06322c2`; added cryptography 50.0.2, cffi 2.1.1, pycparser 3.11 |

Both ran right after the 18:47:59 ingest run. Nothing was pushed: `origin/main` stays at `907d780` / `32ea623`.

**Verification:**
- Both live trees are clean on `f87355e` / `06322c2`. The only untracked item is the pre-existing `docs/ideas/`.
- Selftests on the live code (temporary state):
  - `improve` selftest pass, e2e-promote 30/30, e2e 49/49;
  - ab-lesson selftest 11/11;
  - agent-memory full suite 1432 passed (with the live merged improve).
- `amem rsi freeze-status` prints `FROZEN`.
- Unattended improvement writes stay off:
  - `improve apply --dry-run` is refused (weekly cap);
  - the loaded amem-daily job has no edits flags;
  - `apply_mode` is unset, so ab-lesson cannot write;
  - its task is disabled.
- The new commands work:
  - `amem rsi --help` lists 11 subcommands;
  - `amem rsi targets` finds 47 files, 9 not in git;
  - `amem archive status` reports initialized=no and key=no. It wrote nothing and made no Keychain call.
- Ingestion on the new code: the 19:03:14 amem-ingest run exited 0 (runs 225), with no FAIL line.
  - Every session active since the merge is tagged.
  - `env = eval` matches `kind = eval` exactly (144).
  - `env = prod` covers chat 20, parent 6, routine 4 and worker 84.
  - No session was tagged sandbox or cloud.
- The promotion worker stays disabled:
  - no launchd job or task runs `decide-poll`;
  - `rsi_decisions`, `rsi_proposals` and `rsi_decision_tokens` are empty.

**Undo:**
- D1: `git -C ~/agent-config revert -m 1 f87355e`, or a reset to `319fe0a` (nothing is pushed).
- D2: `git -C ~/Projects/agent-memory reset --hard 32ea623 && uv sync`.

## Phase E: Plan A built (2026-10-16), approved in chat as the design

Branch `rsi/m0035-e`, commit `c079c04`. Suite 1451 passed; edge function 12/12. See
[least-privilege.md](least-privilege.md).

## E1: Phase E migrations (2026-10-16 19:51:41 CEST), approved in chat

- **Applied**, from a folder holding only these two files (taken from `c079c04`, hashes checked first):
  - `20261023010000_amem_app_role.sql`, sha256 `af72e5d561f6d43220d759ecd6db102d53c95fac0178bdba6ba739d2bb01ea61`;
  - `20261023020000_rsi_totp.sql`, sha256 `2ba9a6612bb449cb48765e4547359d6ad0f2fc4703a5958425977dbca24f93fd`.

  The command was the main checkout's `amem db migrate --dir`. Exit 0, about 1 s. `schema_migrations`
  now holds 25 files.
- **[verify_least_privilege.sql](verify_least_privilege.sql)** (sha256 `c6cde45a…`) ran with no error.
  The `rsi_*` and TOTP tables hold 0 rows afterwards (the ROLLBACK worked).
- **`amem_app`:**
  - login, **no password yet** (it cannot connect until E3);
  - not superuser, no create-role, no RLS bypass;
  - select on 25 of 28 tables (not the two TOTP tables; only four columns of `rsi_decision_tokens`);
  - insert on 23 tables (not `rsi_decisions`, the tokens, the TOTP tables or `schema_migrations`).
- **API roles:**
  - `anon` / `authenticated` reach 0 of 28 tables;
  - `service_role` reaches the same 16 tables as before;
  - `service_role` runs only `amem_rsi_record_phone_decision` and `amem_rsi_phone_bad_code`. The
    token-only function is gone from it.
  - Policies: 49.
- **Security advisors:** INFO only. `rsi_totp_used` and `rsi_totp_failures` have RLS and no policy
  (owner-only, as intended). The 8 older INFO notes are gone, because those tables now have
  `amem_app` policies.
- **Denied:** the `parent-guard` hook blocked a read-only `launchctl print` started from a worktree.
  It was not worked around: the ingest-idle check used `ps`, and the ingest watch ran from the normal
  working folder.
- **Ingestion after E1:** the 20:04 run exited 0: `amem-ingest` (0 new, 350 present, 0 unparsed),
  `amem-sessions` (266 files, 4 updated, eval=144) and `amem-undo-poll` (0 requests). No FAIL lines since 19:51.
  It still runs as `postgres`; `amem_app` takes over only at E3.
- **Undo** (never automatic): [rollback_least_privilege.sql](rollback_least_privilege.sql) restores the
  Phase C state. It is tested.

## E2: Phase E code merged (2026-10-16 20:05:38 CEST), approved in chat (local only, no push)

- **Merged:** `git merge --ff-only 9c4cb71` in `~/Projects/agent-memory`. `main` moved from `06322c2` to
  `9c4cb71` (`rsi/m0035-e`). No dependency, plist or routine-script change, so no `uv sync`.
  The only untracked item is the existing `docs/ideas/` (Oct 14–15, not touched).
- **Live suite:** 1450 passed, 1 skipped (209 s). The skip is the opt-in cross-repo test. Run alone with
  `RSI_TEST_IMPROVE_REPO=~/agent-config` (`f87355e`): 1 passed, and `~/agent-config` stayed clean. Total 1451.
- **`amem admin credentials-status`:** "connects as postgres; admin Keychain item: absent;
  AMEM_SUPABASE_KEY: none". Exit 1, as expected before E3.
- **`amem rsi freeze-status`:** `FROZEN`.
- **New commands present:** `amem admin split-credentials | restore-credentials | credentials-status`,
  `amem rsi totp-enroll`, `amem rsi decide-page`.
- **Ingestion after E2:** the 20:19 run exited 0: `amem-ingest` (0 new, 350 present, 0 unparsed),
  `amem-sessions` (261 files, 2 updated, eval=144) and `amem-undo-poll` (0 requests). No FAIL lines since
  20:05. The file count has fallen all day (276 at 17:31) because transcripts leave the disk; E2 changed no
  session-ingest code.
- **Undo** (never automatic): `git reset --hard 06322c2` in `~/Projects/agent-memory`.

## E3 preflight (2026-10-16, read-only, no production change)

- **Hosted database, as `amem_app`:** no `nextval` defaults and no sequences in `public`; no materialized
  views; TEMP allowed; insert on every non-RSI table; an `amem_app` policy on every non-RSI table with RLS.
  No select on `rsi_decision_tokens` (four columns only), `rsi_totp_used` or `rsi_totp_failures`, as designed.
- **Two functions without EXECUTE:** `cloud_digests_keep_routine` and `cloud_digests_claim_chat`. Both are
  trigger functions, revoked from PUBLIC on purpose. Postgres does not check EXECUTE when a trigger fires.
  New test `test_amem_app_fires_the_cloud_digest_triggers` proves it: `sessions_ingest` marks digests
  ingested as `amem_app`, and both triggers run.
- **Routine code:** no superuser statements (`session_replication_role`, `vacuum`, `set role`, DDL).
  agent-config, the hooks and Codex never use `AMEM_DB_URL` directly; they call `amem`.

## E3: credential split (2026-10-16 ~20:23 CEST), approved in chat, run by Vilmer

- **Run:** Vilmer ran `.venv/bin/amem admin split-credentials` in `~/Projects/agent-memory`, from the
  desktop app's `!` shell (his own terminal session: a TTY, no agent variables). Output: "done", and
  `AMEM_SUPABASE_KEY: none`. The Keychain probe passed, so a read of `amem-admin-db` did not return
  without a human. No value was printed.
  - The agent's own tool shell sets `CLAUDECODE` and has no TTY, so `_human_only()` refuses it there.
    That check is a safeguard, not a boundary (see least-privilege.md).
- **`amem admin credentials-status`:** "connects as amem_app; admin Keychain item: present;
  AMEM_SUPABASE_KEY: none". Exit 0. `freeze-status`: `FROZEN`.
- **Forged decision as `amem_app`** (hosted database, the routines' own credential, one transaction ending
  in ROLLBACK, each attempt in a savepoint): `current_user` = `amem_app`. All 11 attempts failed with
  42501 (permission denied):
  - insert, update and delete on `rsi_decisions`; insert on `rsi_decision_tokens`;
  - `amem_rsi_record_decision`, `amem_rsi_record_phone_decision`, `amem_rsi_record_cli_decision`,
    `amem_rsi_phone_bad_code`;
  - select on `rsi_totp_used`; `create table`; `set role postgres`.

  0 rows left for the probe id.
- **First routine cycle as `amem_app`:** the 20:34 run exited 0: `amem-ingest` (0 new, 350 present),
  `amem-sessions` (260 files, 2 updated, 2 summaries written) and `amem-undo-poll` (0 requests). No FAIL
  lines since the split. No launchd plist and no agent-config script sets `AMEM_DB_URL`, so the routines read
  the split line, the same path the probe used (`current_user` = `amem_app`).
- **09:20 amem-daily (2026-10-17):** pending. Expected: exit 0, `scored=` and `deviations=`, no `edits=`.
- **Dialog type** (reported by Vilmer): it asked for the login password before Deny or Allow. A same-user
  process cannot pass it by clicking; see least-privilege.md, "Checked at E3".
- **Still open:** T2 (Supabase CLI token) and T3 (the connector runs as `postgres`) until Phase G (A4, A5).
  Until then the decision-record boundary is not complete.
- **Undo** (never automatic): Vilmer runs `.venv/bin/amem admin restore-credentials` and clicks Allow once.

## Phase F: transcript archive (2026-10-16 20:40–20:43 CEST), approved in chat (F1–F3, no schedule, no prune)

- **Preflight** (read-only): `archive run --dry-run` counted 2093 files, 1.90 GB, 0 errors. 407 GB free.
  No archive and no key existed. The session file count had fallen from 276 to 260 that day: Claude Code
  was already deleting old transcripts.
- **F1, `amem archive init`** (20:40:01): Keychain item `amem-archive` / `k1` and
  `~/Library/Application Support/amem-archive` (dir 700, files 600). No value printed.
- **F2, key export:** Vilmer ran `amem archive key-export` in his own terminal and stored the recovery
  string in his password manager. The agent never saw it.
- **F3, first `amem archive run`** (20:41:57, 29 s): 2093 versions, 1376 sessions, 2066 objects,
  1.90 GB read, 636 MB stored (613 MB on disk), 0 skipped, 0 errors.
- **`amem archive verify`:** ok. 2095 manifest lines, 2066 objects checked, 0 missing, 0 bad, 0 chain breaks,
  0 index problems, 0 orphans.
- **Restore test:** one finished Claude session (810 kB, last changed more than 2 days earlier) restored
  into a scratch folder outside every source root. Same sha256, same relative path, same mtime, mode 600.
  The plaintext copy was deleted afterwards. Only hashes and counts were printed.
- **Kind: safeguard.** The encryption protects copies of the archive off this Mac (backups, disk images).
  The key item has the default ACL, so the future daily run can read it without a dialog, and so can any
  process that runs as `vilmerfrost`. Such a process can also read the source transcripts directly.
- **Not done (each needs its own approval):** a daily schedule for `archive run`; `prune`.
  Until a schedule exists, transcripts that Claude Code deletes after today are not archived.
- **Undo** (never automatic, Vilmer runs it): delete `~/Library/Application Support/amem-archive` and the
  Keychain item `amem-archive` / `k1`. The sources are never changed.
