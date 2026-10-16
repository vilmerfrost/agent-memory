# RSI V1: least-privilege promotion (Mission 003.5, Phase E)

Status: **design, not built.** Unattended Apply stays disabled until this is built, activated and
verified.

## The problem (verified 2026-10-16)

A promotion is safe only if no automated process can create a human decision. Today, four paths give
any process that runs as `vilmerfrost` the power to forge one:

| # | Path | What it allows |
|---|------|----------------|
| T1 | `~/Projects/agent-memory/.env.local` (mode 0600, owner vilmerfrost) holds the full `postgres` connection string. `amem` loads it by itself. | insert into `rsi_decisions`; run every function |
| T2 | The Supabase CLI login token is in the login Keychain (item "Supabase CLI"). | redeploy `amem-decide` with different code; set function secrets |
| T3 | The Supabase connector in Claude sessions runs `execute_sql` as `postgres`, plus `apply_migration` and `deploy_edge_function`. | the same as T1 and T2, from any Claude session that has the connector |
| T4 | Phone tokens are minted locally, and the ntfy topic name is on disk. | with any write path to `rsi_decision_tokens`: mint a token, call the public edge function, get a `phone` decision |

A fifth path is not forgery but a bypass:
- **T5:** every production config file is writable by any process that runs as `vilmerfrost`. An
  unattended agent session can edit `~/.claude/CLAUDE.md` directly, without RSI.

A PostgreSQL role or a separate environment variable alone does not fix T1–T4. The same user could
still read the stronger credential.

## Plan A (recommended): one macOS user, human-presence gates

| Step | Change | Closes |
|------|--------|--------|
| A1 | New login role `amem_app` for every routine and the promotion worker. It gets DML on the existing tables (with RLS policies for it) and the `rsi_writer` rights. It gets no DDL, no insert on `rsi_decisions`, and no execute on either decision function. New migration, without a password; a script sets the password. | T1 for automation |
| A2 | The `postgres` connection string moves out of `.env.local` into a Keychain item with an empty trusted-app list (`security add-generic-password ... -T ""`). macOS then shows a dialog on every read. Only `amem db migrate` and `amem rsi decide` (terminal decisions) use it. Unattended processes fail closed. You run the move script in your own terminal, so no agent ever sees either value. | T1 |
| A3 | Phone Apply needs a 6-digit TOTP code. The ntfy button opens a small decide page served by `amem-decide`; Apply needs the token **and** the code. The TOTP secret is a Supabase function secret, never on disk. You enroll once by scanning a QR code shown in your terminal. | T4 |
| A4 | `supabase logout` after each deploy window. Log in only to deploy. | T2 |
| A5 | The Supabase connector in claude.ai: `project_ref=uyolylfqjxoplpqmplpq`, `read_only=true`, `features=database,docs,debugging`. That means no functions and no account tools. You change this in the connector settings. | T3 |

**Negative tests** (automated, against a disposable database and the edge-function unit tests):
- As `amem_app`, each of these is denied:
  - inserting into `rsi_decisions`;
  - executing either decision function;
  - DDL;
  - updating `rsi_artifacts`.
- As `amem_app`, the full worker flow (read the decision, promote, record the result) succeeds.
- Edge function: each of these is refused, and nothing is recorded:
  - Apply without a code;
  - Apply with a wrong code;
  - Apply with a replayed code;
  - a token for one action used for another.
- `improve promote` with the database unreachable is refused (fail closed). A forged decision file
  is refused (already tested).

**Manual checks at activation** (a test cannot click a macOS dialog):
- the Keychain dialog appears;
- a launchd job cannot read the item;
- `execute_sql` with an `insert` fails through the connector.

### What Plan A does not close

- **R1:** in an attended session, you may click "Allow" on the Keychain dialog when an agent asks.
- **R2 (T5):** production files stay writable by any process that runs as `vilmerfrost`. The freeze,
  the fence check and autopin cover only the improve path. Closing R2 needs Plan B.
- **R3:** anyone who knows the ntfy topic sees proposal summaries. This is unchanged from today.

## Plan B: a separate macOS identity

A hidden service user owns the production config files and runs the promotion worker. Your user only
reads them. This closes R2. It needs:
- your admin password;
- new owners or ACLs on about 47 files in two git repos;
- a change to every routine that writes them.

It is stronger and much larger. Plan A does not block it.

## Decision rule for unattended Apply

Enable the promotion worker only when A1–A5 are active and every negative test and manual check
passes. R1 and R2 stay documented residual risks until Plan B.
