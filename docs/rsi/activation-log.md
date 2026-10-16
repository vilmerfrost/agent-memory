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

## F4: daily archive schedule (2026-10-16 20:48 CEST), approved in chat (local only, no push, no prune)

- **Built and tested first** on agent-config `rsi/m0035-f4` (`08d6348`):
  - `bin/amem-archive`: `amem archive run`, then a full `amem archive verify` (counts only). Same timeout
    and routines.log rules as `bin/amem-ingest`.
  - `launchd/com.vilmerfrost.amem-archive.plist`: daily 08:45, `RunAtLoad` false.
  - `parent/routines.tsv`: `amem-archive`, 26 h, so routine-audit and config-check watch it.
  - Fake-`uv` checks 13/13: exit codes, verify only after a good run, one PASS line per day, FAIL lines,
    a hung run killed (rc 124). The real CLI through the wrapper on a throwaway archive (file key backend,
    temp sources): two runs, exit 0, the changed file became version 2; the production archive's
    `last_run` did not change. All 10 plists have a registry row; `routine-audit selftest` PASS.
- **Merged:** `git merge --ff-only 08d6348` in `~/agent-config` (20:48:28): `main` `f87355e` → `08d6348`.
- **Installed:** the plist copied to `~/Library/LaunchAgents` (identical to the repo copy) and
  `launchctl bootstrap`ed from the main checkout. Loaded, not running.
- **Kickstart** (20:48:43, one run through launchd): exit 0 in 4 s, no dialog (the launchd context reads the
  key). `files=2093 new_versions=1 unchanged=2092`, 20.7 MB read, 4.0 MB stored (the live session). `verify`: ok,
  2067 objects, 0 bad. routines.log: `amem-archive PASS heartbeat`. Job list: loaded, last exit 0.
- **Next scheduled run:** 2026-10-17 08:45; routine-audit checks it at 09:10. Pending.
- **Denied:** the `parent-guard` hook blocked a Bash command in the agent-memory worktree that only edited this
  log and committed it (the text mentions the launch tools). Not worked around for any job action: this
  doc edit was made with the editor tool instead, and no job command ran from a worktree.
- **Kind: safeguard** (data retention). It writes only to the archive dir and the logs.
- **Undo** (never automatic): `launchctl bootout gui/$(id -u)/com.vilmerfrost.amem-archive`, delete the
  installed plist, `git revert 08d6348` in `~/agent-config` (revert, not reset: routines may commit to main).
  The archive data stays (Phase F undo is separate).

## Phase G: amem-decide, option A (2026-10-16 20:53–21:05 CEST), approved in chat

Option A (Vilmer): terminal-only Apply. The phone gets Reject only. No TOTP secret, no hosted decide page
(so R4 does not apply). Apply is `amem rsi decide <id> apply` in Vilmer's terminal (Keychain password).

- **Preflight:** function secrets were the platform's own only (no `RSI_TOTP_SECRET`, no `RSI_DECIDE_ORIGIN`).
  Deployed functions: only the legacy `amem-undo`.
- **G1, deploy** (20:53:50): `supabase functions deploy amem-decide --no-verify-jwt --project-ref
  uyolylfqjxoplpqmplpq` from `main` `9c4cb71`. Exit 0, 6.8 kB. Version 1, ACTIVE, `verify_jwt` false.
- **G2, source check:** the deployed function, downloaded into a temp folder, is byte-identical to the repo
  (`index.ts`, `handler.ts`, `totp.ts`). The test file is not deployed.
- **G3, live test** (as `amem_app`; one labelled test proposal `ee814446-946b-490a-9437-987b90c85625`,
  "Phase G e2e test (disposable, no files)", artifact `368b1484b373…` with no files). All 5 cases passed:
  - Apply without a code → 503 "Phone approval is not set up — decide in the terminal";
  - Apply with a code → 503 (no secret, so no code is checked and nothing is counted);
  - Reject with a wrong token → 403 "Not allowed";
  - Reject with the right token → 200 "Rejected — nothing will change";
  - Reject again → 200 "Already recorded".

  Database: one `reject` / `phone` decision; the proposal is `rejected`; both tokens are used (one decision
  consumes the apply and reject tokens, by design). **These rows stay**: the audit trail is append-only.
  The test also shows R3 in practice: code running as `vilmerfrost` can arm a Reject token and reject a proposal.
- **G4 (A4), `supabase logout`** (20:55:29): token deleted. The "Supabase CLI" Keychain item and the token file
  are gone; `supabase projects list` fails with `AccessTokenRequiredError`. No shell startup file and no launch
  environment sets `SUPABASE_ACCESS_TOKEN`. A later deploy or function rollback needs Vilmer's `supabase login`.
- **G6, live gate test, first attempt (20:55:49): FAILED.** The agent's own shell ran
  `security find-generic-password -s amem-admin-db -w`. The read returned the value after about 9 s: Vilmer
  clicked **Always Allow** by mistake. The test process never printed or stored the value. Stopped there.
  - **Fix (Vilmer, ~20:58):** `amem admin restore-credentials`, then `amem admin split-credentials` (Deny on the
    probe). This re-created the item with no trusted apps and set a new random `amem_app` password.
  - **Decision (Vilmer): no rotation of the `postgres` password.** It is the same password that sat in
    `.env.local` until E3, and the agent's test process read it once at 20:55. Recorded as R6.
  - **After the fix:** `credentials-status` exit 0 (`amem_app`, item present); the forged-decision probe again
    11/11 refused, 0 rows left.
- **G6, second attempt (20:59:50): passed.** The same read: Vilmer clicked Deny; `security` exit 128, no value.
- **Routine cycle after the fix:** the 21:05 run exited 0 as `amem_app` with its new password: `amem-ingest`
  (0 new, 351 present), `amem-sessions` (260 files, 1 updated, 1 summary) and `amem-undo-poll` (0 requests).
  No FAIL lines since 20:56.
- **G5 (A5), the Supabase connector: not done, by decision.** Vilmer set the claude.ai connector's tools to
  "Ask always". A test `select` from this Claude Code session ran at once with **no prompt**, as `postgres`.
  So that setting does not reach Claude Code sessions, and it is not a boundary here. **Decision (Vilmer):
  keep the connector with full access for now.** T3 stays open (see least-privilege.md).
- **Undo** (never automatic): delete `amem-decide` (dashboard, or the CLI after `supabase login`); `supabase login`
  undoes A4. The G3 test rows cannot be removed (append-only by design).

## Phase H: real-model sandbox smoke test (2026-10-16 21:14–21:40 CEST), approved in chat

- **Preparation** (no keys, no paid calls): sandbox suite 110/110 on `main` `9c4cb71`. The worker image has
  Claude Code 2.1.295 (`--max-budget-usd`, `--tools`, no turn limit) and Codex 0.162.0 (no budget flag; its
  bundled model list includes `gpt-6-luna`, "fast and affordable"). **Fix:** the Keychain credential provider
  read any item; now only `rsi-sandbox/anthropic` and `rsi-sandbox/openai` (`e569df9`, a safeguard), 111/111.
- **H1:** `git merge --ff-only e569df9` in `~/Projects/agent-memory` (21:14:36): `main` `9c4cb71` → `e569df9`.
- **Incident during the H1 suite (21:14–21:16): the tests reached the real Keychain.** Older tests run
  `amem db migrate`, which reads `amem-admin-db` once it exists (it has since E3). Vilmer clicked
  **Always Allow** on the dialog, so later reads were silent. Stopped the suite at 21:16:32.
  - Production checked: 25 migrations, the last at 17:51 UTC (E1); nothing applied since; 28 tables; no new
    roles; RSI rows unchanged. The test calls only ran `migrate`, which had nothing to apply. The test log holds
    no connection string.
  - **Root cause fixed** (`a03e85a`, `d7836a5`, dev branch): every test and every CLI subprocess gets a Keychain
    tripwire in `AMEM_ADMIN_SECURITY`, never `/usr/bin/security`, unless a test sets its own fake. Guard test
    `test_tests_never_reach_the_real_keychain`. Re-run: 1451 passed; the 2 env-isolation tests that expected no
    extra `AMEM_*` name were updated and pass (25/25 in both files). No Keychain dialog appeared.
  - **Gate restored:** Vilmer ran `restore-credentials`, then `split-credentials` (Deny); item created 21:23:09.
    `credentials-status` exit 0; the forged-decision probe 11/11 refused; the agent's read was denied (exit 128).
  - **Not on `main` yet:** `a03e85a` and `d7836a5` (tests only), plus the docs commits after `e569df9`.
- **H2 (Vilmer):** spend-capped keys in a new Anthropic workspace and a new OpenAI project ($5/month each),
  stored as Keychain items `rsi-sandbox/anthropic` and `rsi-sandbox/openai`. The secrets popup was not used:
  it writes env files, and the sandbox refuses env files for real keys.
- **H3:** synthetic baseline `h-smoke-base` (only `.claude/CLAUDE.md` with `sandbox-ok-claude` and
  `.codex/AGENTS.md` with `sandbox-ok-codex`), arms A and B prepared (`disableAllHooks` added), `net-up`.
- **H4, each run 300 s / 1 GB / 1 CPU / 256 pids, task "write your status word to status.txt, reply done":**
  - **Claude** (arm A, `claude-haiku-5-5`, `--max-budget-usd 0.25`, tools Read and Write): exit 0 in 2.8 s,
    `is_error` false, 2 turns, **$0.00048**. `status.txt` = `sandbox-ok-claude`.
  - **Codex, run 1** (`-s workspace-write`): 401 "Incorrect API key". OpenAI saw exactly 128 characters: the
    `security … -w` prompt had cut the key. No charge. Vilmer re-stored it through a hex pipe (no length limit).
  - **Codex, run 2** (`-s workspace-write`): exit 0 in 11.5 s, about $0.003. The model ran and named
    `sandbox-ok-codex`, but every tool write failed ("Failed to write file"); Codex's own `-o` file was written.
    So Codex's inner sandbox, not the container, blocked it.
  - **Codex, run 3** (`-s danger-full-access`, approved): exit 0 in 6.6 s, about $0.002 (25,181 input tokens,
    12,542 cached; 73 output). `status.txt` = `sandbox-ok-codex`, last message "done".
  - Total spend: about **$0.006**.
- **Proxy, all runs:** ALLOW only `api.anthropic.com:443` and `api.openai.com:443`. Denied: `chatgpt.com`,
  `ab.chatgpt.com`, `github.com`, `api.github.com` (Codex at startup; it worked anyway).
- **Production after H:** 25 migrations; RSI rows unchanged (1 proposal, 1 decision, 1 artifact, 0 eval
  results, 0 decision results); both repos unchanged; `freeze-status` FROZEN; no sandbox artifact; no
  promotion job. No A/B capability is claimed: `sandbox-replay` does not exist.
- **Cleanup:** `cleanup --max-age-hours 0`, `recover`, `net-down`; experiment `h-smoke` and baseline
  `h-smoke-base` deleted. 0 containers, 0 networks, 0 locks left. The two sandbox keys stay in the Keychain
  (spend-capped) until Vilmer decides to keep or revoke them.
- **Docs fixed:** sandbox.md: store long keys through the hex pipe, not the `-w` prompt; Codex needs
  `-s danger-full-access` in the worker; real-key runs are now verified.
- **Merged after approval:** `git merge --ff-only 8594405` in `~/Projects/agent-memory` (21:42:35): `main`
  `e569df9` → `8594405`, tests and docs only (0 other files). Live suite with the tripwire: **1453 passed**,
  0 failed (198 s); no Keychain dialog; the admin item is still the one created at 21:23:09; agent-config
  unchanged. The two sandbox keys are kept (Vilmer). Phase I not started.
