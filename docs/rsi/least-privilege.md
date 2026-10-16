# RSI V1: least-privilege promotion (Mission 003.5, Phase E)

Status: **Plan A built and tested on branch `rsi/m0035-e`. Not active.** Unattended Apply stays disabled until
the activation steps below run and verify.

## The problem (verified 2026-10-16)

A promotion is safe only if no automated process can create a human decision. Today, four paths give
any process that runs as `vilmerfrost` the power to forge one:

| # | Path | What it allows |
|---|------|----------------|
| T1 | `~/Projects/agent-memory/.env.local` (mode 0600, owner vilmerfrost) holds the full `postgres` connection string. `amem` loads it by itself. | insert into `rsi_decisions`; run every function |
| T2 | The Supabase CLI login token is in the login Keychain (item "Supabase CLI"). | redeploy `amem-decide` with different code; set function secrets |
| T3 | The Supabase connector in Claude sessions runs `execute_sql` as `postgres`, plus `apply_migration` and `deploy_edge_function`. | the same as T1 and T2, from any Claude session that has the connector |
| T4 | Phone tokens are minted on the Mac, and the ntfy topic name is on disk. | with any write path to the tokens: mint a token, call the public edge function, get a `phone` decision |

A fifth path is not forgery but a bypass:
- **T5:** every production config file is writable by any process that runs as `vilmerfrost`.

## Plan A as built

| Step | What it is | Where |
|------|------------|-------|
| A1 | Login role `amem_app`. It is a member of `rsi_writer`, with DML and an RLS policy on every non-RSI table and SELECT on views. It gets no DDL, no insert on `rsi_decisions`, no execute on any decision function, and no write on `rsi_decision_tokens` (tokens are armed only through `amem_rsi_arm_token`, which inserts or re-arms an **unused** token). | migration `20261023010000_amem_app_role.sql` |
| A2 | `amem admin split-credentials` runs in your own terminal. It refuses agent sessions and missing TTYs. It does four things: sets a random `amem_app` password and checks the login; stores the `postgres` connection string in Keychain item `amem-admin-db` with no trusted application (`-T ""`, written through `security -i`, value hex-encoded, never in argv); **proves the item needs a human** (a read must not return without a dialog, otherwise the item is deleted and the step fails); and rewrites only the `AMEM_DB_URL` line of `.env.local` (atomic, mode 0600). Afterwards `amem db migrate` and `amem rsi decide` read the item, which shows a dialog. With no answer they refuse; they never fall back. `restore-credentials` undoes it exactly. | `credentials.py`, `cli_admin.py` |
| A3 | Phone Apply and Undo need a 6-digit TOTP code. `amem-decide` checks the code against the function secret `RSI_TOTP_SECRET`, then calls `amem_rsi_record_phone_decision` with the matched 30-second step. Each step works once (`totp-replayed`). 5 wrong codes lock the proposal. `service_role` lost execute on the token-only function. Reject stays one tap, with no code. **Changed from the design:** Supabase rewrites HTML from edge functions to `text/plain`, so the code is typed into a **static page** (`web/rsi-decide/`, built with `amem rsi decide-page`). It loads nothing external, its CSP allows one endpoint, and the token travels in the URL fragment, which is never sent to a server. Without a hosted page, the phone shows Reject only, and Apply is `amem rsi decide <id> apply` in your terminal. | migration `20261023020000_rsi_totp.sql`, `amem-decide`, `rsi_totp.py`, `amem rsi totp-enroll` |
| A4 | `supabase logout` after each deploy window. | your action |
| A5 | The Supabase connector: `project_ref=uyolylfqjxoplpqmplpq`, `read_only=true`, `features=database,docs,debugging`. | your action (claude.ai connector settings) |

## Tests (automated, disposable Postgres; fake `security`; edge unit tests)

- **As `amem_app`, each of these is denied:**
  - inserting a decision;
  - each of the decision functions (token-only, TOTP-gated, terminal, and the bad-code counter);
  - reading `rsi_totp_used`;
  - updating `rsi_artifacts`, truncating `sessions`, creating a table, `set role postgres`;
  - re-opening a used token.
- **As `amem_app`, the routines and the worker work:**
  - staging, the push (arming and re-arming tokens), `decide_poll` promoting a decision, and `verify_decision`;
  - session upsert, edits, the terminal undo function (`amem_request_undo`), runs, the views;
  - the real `improve` cross-repo flow (stage → forged decision refused → apply → exact bytes → undo).
- **Guard for future migrations:** every non-RSI table must have an `amem_app` grant and a policy.
- **TOTP in SQL:**
  - Apply without a step → `totp-required`.
  - A step reused for another proposal → `totp-replayed`.
  - Reject needs no step.
  - 5 bad codes → `locked`.
- **Supabase default privileges, recreated:**
  - the API roles get no RSI tables;
  - only `service_role` runs the two TOTP functions;
  - nobody runs the token-only function.
  - `verify_least_privilege.sql` passes, then `rollback_least_privilege.sql` runs, then
    `verify_rsi_core.sql` passes again (the Phase C state), then the migration applies again.
- **Keychain logic (fake `security`):**
  - the item is gated;
  - an unanswered dialog, a Deny, or a missing item each fail closed;
  - an open-ACL item is deleted and refused;
  - `db migrate` and `rsi decide` never fall back to `AMEM_DB_URL`;
  - split and restore round-trip the env file byte for byte;
  - the commands refuse agent sessions.
- **Push:**
  - without a page, there is no Apply button and the apply token appears nowhere;
  - with a page, the token is in the fragment only;
  - the page refuses `http://` and query strings.
- **Edge function (12 tests):**
  - the RFC 6238 vector;
  - no code → no database call;
  - a wrong code is counted;
  - replay and lock;
  - no secret → Apply refused while Reject still works;
  - CORS only for the page origin.

**Manual checks at activation** (a test cannot click a macOS dialog):
- the split shows a Keychain dialog (click **Deny**);
- `amem admin credentials-status` exits 0;
- a launchd run as `amem_app` exits 0;
- a connector `insert` fails.

## Activation (each step needs its own approval)

| # | Step | Verify | Undo |
|---|------|--------|------|
| E1 | Apply the two migrations to the hosted database (one-file folders, as in Phase C) | `verify_least_privilege.sql`; advisors; the next ingest exits 0 | `rollback_least_privilege.sql` |
| E2 | Merge `rsi/m0035-e` into agent-memory main (local) | live suite; `amem admin credentials-status` responds | `git reset --hard 06322c2` |
| E3 | **You** run `amem admin split-credentials` in your terminal | credentials-status exit 0; the next ingest and the 09:20 amem-daily run exit 0 as `amem_app` | **you** run `amem admin restore-credentials` |
| — | Phase G: deploy `amem-decide`, `amem rsi totp-enroll`, host the page (or choose terminal-only), then `supabase logout` (A4) | a disposable proposal: no code → refused, code → recorded, replay → refused | delete the function; restore the grant via the rollback |
| — | A5: you restrict the connector | a connector `insert` fails | revert the connector setting |

## What Plan A does not close

- **R1:** in an attended session you may click "Allow" on the Keychain dialog when an agent asks.
  Allow only for a command you started yourself. **Never click "Always Allow"**: that trusts the
  `security` tool for every later read.
- **R2 (T5):** production files stay writable by any process that runs as `vilmerfrost`. Closing this
  needs Plan B (a separate macOS identity that owns the config files).
- **R3:** anyone who knows the ntfy topic sees proposal summaries and can tap Reject. A Reject can only
  stop a change.
- **R4:** if the decide page's host account is compromised, a changed page could capture a code for a
  different proposal within its 30 seconds. Check the artifact hash on the page against the morning
  brief.
