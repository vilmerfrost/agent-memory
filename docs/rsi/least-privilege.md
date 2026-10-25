# RSI V1: least-privilege promotion (Mission 003.5, Phase E)

Status (2026-10-16): **E1, E2 and E3 done. Phase G option A done: `amem-decide` deployed (Reject only, no TOTP),
A4 done (CLI logged out). A5 not done, by Vilmer's decision: T3 stays open, so the decision-record boundary is
not complete.**
Unattended promotion: the 2026-10-16 decision was replaced on 2026-10-25 ("Within 5 minutes"). The decide job is
prepared, not installed (see "Unattended decide-job" below). R2 is still open.

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

## Safeguards and security boundaries

Two different things protect production. Do not confuse them.

- A **safeguard** stops accidents: a bug, a wrong command, or an agent that leaves its task but still
  follows its instructions. Code that runs as `vilmerfrost` can turn a safeguard off or go around it.
- A **security boundary** holds against a compromised agent that runs as `vilmerfrost`. It needs
  something that agent does not have: your password or code, a secret that is not on the Mac, or a
  different macOS user.

| Control | Kind | Why |
|---------|------|-----|
| Freeze (cap 0, no `AMEM_EDITS_LIVE`, task disabled) | Safeguard | The agent can edit `criteria.json` and the plist (T5). |
| Stage → approve → promote engine, WAL, artifact hashes, `verify_decision` | Safeguard | It controls the normal path only. The agent can write the target files directly, or change the worker's code (T5). |
| Sandbox tests and evals | Safeguard (quality) | They catch bad changes, not hostile ones. |
| Hooks (`secret-read-block`, `parent-guard`, a future `rsi-prod-fence`) | Safeguard | They are config files the agent can edit. They match text, so a different wording gets past them. |
| `_human_only()` in `amem admin` and `totp-enroll` | Safeguard | It checks environment variables and a TTY. An agent can clear the variables and fake a TTY. |
| Phone Reject token, ntfy topic | Safeguard | Both are on the Mac (T4, R3). A Reject can only stop a change. |
| Decide page CSP, token in the URL fragment | Safeguard | They reduce leaks. A compromised page host defeats them (R4). |
| `amem_app` + Keychain-gated admin DSN (A1, A2) | **Boundary for the decision record**, after E3, A4 and A5. **Incomplete: A5 not done (T3 open by decision, 2026-10-16)** | The credential on disk cannot insert, change or delete a decision, or change the schema. It holds only while the Supabase CLI is logged out (T2), the connector is read-only (T3), and you never Allow a dialog you did not start (R1). |
| TOTP for phone Apply and Undo (A3) | **Boundary for phone decisions**, after Phase G, A4 and A5. **Not enabled (option A)**: phone Apply and Undo are refused | The secret is only in the function secrets and your authenticator app. Without A4 and A5, an agent can redeploy the function or reset the secret. |
| claude.ai connector tool setting "Ask always" | **Not a boundary for Claude Code** | Tested 2026-10-16: a `select` from a Claude Code session ran at once, with no prompt, as `postgres`. |
| A separate macOS user that owns the production files (Plan B) | **Boundary for the change itself** | Not built. It is the only control that stops an agent from skipping the approval (R2). |

**What this means.** Plan A makes an approval impossible to *forge* in the database. It does not make the
approval *mandatory*. A compromised agent does not need a forged decision: it can write
`~/.claude/CLAUDE.md`, a skill or a plist directly (T5). It can also change the `amem` code that you later
run and Allow in the Keychain dialog. Only Plan B closes this.

**Checked at E3 (Vilmer, 2026-10-16).** The Keychain dialog asked for the login password before Deny or
Allow. A read therefore needs a secret that is not on the Mac's disk, so a same-user process cannot get past
the dialog by clicking it, even with Accessibility permission. The gate fails only if you type your password
into a dialog you did not start (R1). This was observed once, at the split; if a later dialog shows only
buttons, treat A2 as a safeguard again and say so.

## Decision: no unattended promotion while R2 is open (2026-10-16, Vilmer) — replaced 2026-10-25

**Replaced** by "Unattended decide-job" below (Vilmer, 2026-10-25: "Within 5 minutes"). Kept for the record.

- No launchd job or scheduled task runs `amem rsi decide-poll`.
- A promotion runs only when you start `amem rsi decide-poll` in your own terminal. A phone Apply (after
  Phase G) records the decision. The change lands at your next attended run.
- The loop stays the same: Observe → Stage → Sandbox test → Human approval → Promote → Monitor → Undo.
  Only the trigger for Promote is attended.
- Revisit only after Plan B, or another control that closes R2, is built and verified.

## Unattended decide-job (2026-10-25, prepared, not installed)

Vilmer chose "Within 5 minutes" for Approve / Deny / Undo from Night Desk. `amem rsi decide-job` (launchd
`com.vilmerfrost.amem-rsi-decide`, every 300 s) runs `decide-poll` and the edit-loop `undo-poll` unattended.
Install steps: [activation.md](activation.md), step 10b.

**Which login.** `amem_app` (A1). It is already the login of every routine: E3 put its DSN in `.env.local`, and
the job loads `.env.local` like the other routines. A separate login would add a second password and buy little:
`amem_app` can already do everything the worker needs and nothing that records a decision. No new secret is needed.

| The worker needs | `amem_app` has it through |
|------------------|---------------------------|
| read decisions, proposals, artifacts | `rsi_writer` select + `rsi_read` policy |
| set proposal status, change id, result, promoted / reverted time | `rsi_writer` column update on `rsi_proposals` |
| write `rsi_decision_results` (with `reason_code`, `updated_at`) | `rsi_writer` insert + update |
| arm the Undo token after a promotion | `amem_rsi_arm_token` (unused tokens only) |
| claim and close edit undo requests, mark the edit reverted | DML + `amem_app_all` policy on `undo_requests`, `edits` |
| `pg_try_advisory_lock`, `verify-decision` (via improve) | any login; read only |

**What it cannot do (tested on the throwaway Postgres, `tests/test_rsi_decide_job.py`):** insert into
`rsi_decisions`; run `amem_rsi_record_decision`, `amem_rsi_record_cli_decision` or `rsi_insert_decision`; write
the two status views; delete result rows.

**The login check (a safeguard, every run).** Before it handles anything, the job asks the database whether its
login is or can become a superuser or BYPASSRLS role, can insert into `rsi_decisions`, or can execute any function
in `public` whose body inserts into `rsi_decisions` or calls `rsi_insert_decision`. Any yes: a `FAIL` line and
nothing handled. So `restore-credentials`, a wrong DSN, or a later grant of a new decision function (the web
path) to `amem_app` stops the job instead of running it with power to forge. It matches function bodies, so a
function added later needs no list update.

**What this does not change.** R2 is still open: a compromised process running as `vilmerfrost` can skip the
approval and write production files directly, or change the job's code. The job makes the *trigger* unattended,
not the approval. A decision still needs the passkey (web Apply / Undo), the TOTP or token (phone) or your TTY.

**Results for Night Desk** (migration `20261025213700_rsi_decision_status.sql`, owner RLS, read only):
`rsi_decision_status` (per decision: `waiting | applied | rejected | undone | refused`, a fixed `reason`,
`state_at`), `edit_undo_status` (per edit undo request) and `rsi_decision_results` itself (owner policy; the API
roles get nothing else). Rollback (manual, reviewed; the job keeps working without the views):

```sql
begin;
drop view if exists public.rsi_decision_status, public.edit_undo_status;
drop policy if exists dashboard_owner_read on public.rsi_decision_results;
do $$ begin
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on public.rsi_decision_results from authenticated';
  end if;
end $$;
alter table public.rsi_decision_results drop constraint if exists rsi_decision_results_reason_code_check;
alter table public.rsi_decision_results drop column if exists reason_code, drop column if exists updated_at;
delete from public.schema_migrations where filename = '20261025213700_rsi_decision_status.sql';
commit;
```
Drop the columns only after the job is stopped or rolled back to code that does not write them.

| # | Step | Verify | Undo |
|---|------|--------|------|
| D1 | Apply `20261025213700_rsi_decision_status.sql` | Night Desk owner reads `rsi_decision_status`; advisors | the SQL above |
| D2 | `amem admin credentials-status` | exit 0, "connects as amem_app" | — |
| D3 | `amem rsi decide-job --no-push` in your terminal | `PASS login=amem_app` | — |
| D4 | Install the plist (activation.md 10b) | an `amem-rsi-decide PASS` line within 5 min | `launchctl bootout`, delete the plist |

## What Plan A does not close

- **R1:** in an attended session you may click "Allow" on the Keychain dialog when an agent asks.
  Allow only for a command you started yourself. **Never click "Always Allow"**: that trusts the
  `security` tool for every later read. This happened once (G6, 2026-10-16). The fix is
  `restore-credentials`, then `split-credentials` (Deny), which re-creates the item with no trusted apps.
- **R2 (T5):** production files stay writable by any process that runs as `vilmerfrost`. Closing this
  needs Plan B (a separate macOS identity that owns the config files).
- **R3:** anyone who knows the ntfy topic sees proposal summaries and can tap Reject. A Reject can only
  stop a change.
- **R4:** if the decide page's host account is compromised, a changed page could capture a code for a
  different proposal within its 30 seconds. Check the artifact hash on the page against the morning
  brief. (Not in use: option A has no page.)
- **T3, open by decision (Vilmer, 2026-10-16):** the claude.ai Supabase connector keeps full access and runs
  SQL as `postgres` from any Claude session, including Claude Code sessions. A session that is tricked can
  insert a decision through it. The routines do not use it. Close it with the read-only connector URL (A5)
  or by disconnecting the connector.
- **R5:** a browser that is signed in to the Supabase dashboard can run SQL as `postgres` (the SQL editor).
  Browser tools ask before they use a new site: never allow supabase.com for an agent.
- **R6 (decision, Vilmer, 2026-10-16): the `postgres` password was not rotated.** It is the same password
  that sat in `.env.local` until E3, readable by any process that ran as `vilmerfrost`, and an agent test
  process read it once at G6. Anything that kept a copy can still use it. Rotating it (dashboard → Database →
  Reset database password, then split again) closes this.
