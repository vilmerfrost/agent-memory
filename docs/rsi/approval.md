# RSI V1 approval-first: proposals, decisions, promotion, undo

Mission 003. Policy: **every automated RSI production change needs a human decision before it is
applied.** Nothing in this flow writes a production file without one.

```
observe → evaluate → stage (sandbox) → proposal + morning push → human Apply / Reject
        → promotion worker → improve promote → monitor → Undo (phone or CLI)
```

Formats (artifact, evaluation, decision, WAL): [spec.md](spec.md). Baselines and targets:
[targets.md](targets.md). Activation and rollback: [activation.md](activation.md).

## Who writes what

| Step | Command | Writes | Production files |
|------|---------|--------|------------------|
| Stage | `amem edits run` with `AMEM_EDITS_MODE=stage` → `improve stage` | baseline, artifact, `static` eval, proposal row | read only |
| Sandbox eval | `rsi_sandbox.py prepare-arm --artifact H` + `record-eval` | `artifacts/H/evals/<suite>.json` | none |
| Push | `amem rsi push` | decision tokens, one ntfy message per proposal | none |
| Decide (phone) | ntfy button → edge function `amem-decide` → SQL `amem_rsi_record_decision` | one decision row | none |
| Decide (web) | Night Desk → `amem-decide` `/web/*` (login, plus Face ID for Apply / Undo) → SQL `amem_rsi_web_decide` | one decision row, or one edit undo request | none |
| Decide (CLI) | `amem rsi decide <proposal> apply\|reject\|undo` (TTY, types 8 hash chars) | one decision row | none |
| Promote / undo | `amem rsi decide-poll` → `improve promote` / `improve revert-change` | the approved bytes, ledger, git commit | **yes, only here** |

The edge function records a decision. It never promotes. Only the promotion worker
(`decide-poll`) runs `improve promote`, and `improve promote` asks the database again
(`amem rsi verify-decision`) before it writes.

## Proposal states

```
staged ──Apply──▶ approved ──worker──▶ promoting ──▶ promoted ──Undo──▶ reverted
  │                  │                     │
  ├─Reject─▶ rejected │                     ├─▶ stale   (live file moved since staging)
  ├─TTL───▶ expired   └─decision TTL─▶ expired└─▶ failed (artifact bytes, evals, scope, …)
  └─live file changed─▶ stale (housekeeping)
```

- A proposal expires 72 h after staging. Decision tokens expire with it.
- One live proposal per artifact hash (unique index).

## Decision tokens (phone)

- `amem rsi push` issues one random token per action (`apply`, `reject`). The database stores only
  `sha256(token:proposal_id:artifact_hash:action)`. A token is useless for another proposal, another
  hash or another action.
- `amem_rsi_record_decision` checks the hash, expiry and proposal state in SQL, then inserts the
  decision and marks the token used. Apply and reject are consumed together, so a second tap or the
  other button returns `already`.
- A used token is never re-armed. Undo gets its own token after promotion.
- Results: `recorded`, `already`, `denied`, `expired`, `missing`, `hash-mismatch`, `not-staged`,
  `not-promoted`, `invalid`. The edge function returns only that word.

## Web decisions (Night Desk, Face ID)

**Decision (Vilmer, 2026-10-25):** Apply and Undo from the website need "login plus face id". This replaces
Option A of 2026-10-16 (Apply only in the terminal). Reject needs the login only: it can only stop a change.
Undo covers both kinds: night proposals and the older edit loop's changes.

```
Night Desk (signed in) ── tap Apply / Reject / Undo ── confirm sheet
  → POST /web/challenge   owner login + Origin; SQL binds a 2-minute, one-use challenge to
                          (kind, id, action, artifact hash); refuses early (not-staged, not-promoted, …)
  → Face ID               navigator.credentials.get, userVerification "required"      [Apply / Undo only]
  → POST /web/decide      verify the assertion (challenge, origin, RP ID hash, UV flag, signature,
                          sign count) → SQL amem_rsi_web_decide consumes the challenge and records
  → decide-poll           unchanged: verify-decision, improve promote / revert-change
```

**The function still only records.** A proposal decision is one append-only `rsi_decisions` row with
`source = channel = 'web'`, `verified = 'edge-token'` and `passkey_id` (the credential, for Apply / Undo).
`verified` stays `edge-token` because agent-config's `improve promote` accepts only `edge-token` or
`cli-tty`. An edit undo is the existing `undo_requests` row (`source = 'web'`); `amem-undo-poll` stays the
executor.

**Checks in the edge function** (`supabase/functions/amem-decide/web.ts`, library
`@simplewebauthn/server` 14.0.3, pinned with `deno.lock`):
- `Origin` must equal `RSI_DECIDE_ORIGIN`, else `denied` before anything else.
- The `Authorization` JWT must name `RSI_OWNER_USER_ID` (`sub`, `role = authenticated`, not expired), and
  Supabase Auth (`/auth/v1/user`) must confirm it. A decoded-but-forged token, another user, a signed-out
  session or a missing owner secret is `denied`.
- WebAuthn: RP ID `night-desk-vilmer-frosts-projects.vercel.app`, user verification required. The challenge
  is 32 random bytes; the database stores only its sha256. A failed assertion burns the challenge.

**Checks in SQL** (migration `20261025210000_rsi_web_decide.sql`): one use and 2-minute expiry, the
signed challenge's hash, an enrolled and unrevoked credential, a sign count that moves forward (0 stays
allowed for passkeys that never count), then the phone path's state, expiry and artifact-hash rules. A
second tap returns `already` and records nothing.

**Results.** Every `/web/*` reply is JSON with `result`; the HTTP status is informational.
- `/web/challenge`: `ok` (plus `challenge_id`, `expires_at`, `needs_passkey`, `request_options`, `summary`),
  or `denied`, `invalid`, `missing`, `expired`, `not-staged`, `not-promoted`, `not-enrolled`.
- `/web/decide`: `recorded`, `already`, `denied`, `expired`, `missing`, `hash-mismatch`, `not-staged`,
  `not-promoted`, `invalid`, `passkey-required`, `passkey-invalid`, `not-enrolled`.
- `/web/enroll/options`: `ok` (plus `challenge_id`, `creation_options`), `invalid`, `expired`.
- `/web/enroll/verify`: `enrolled`, `invalid`, `expired`.
- Any route: `error` when Supabase is unreachable (nothing recorded).

**Passkeys come only from your terminal.**
- `amem rsi passkey-enroll` (TTY, refuses agent sessions) prints a one-use code (24 base32 characters,
  10 minutes; only its sha256 is stored) and the Night Desk enroll link (code in the `#fragment`). A new
  code ends earlier unused codes. Night Desk registers the Face ID passkey with that code *and* the owner
  login. Without the code, a stolen Night Desk session cannot add its own passkey.
- `amem rsi passkey-list` shows the passkeys (no keys). `amem rsi passkey-revoke <id>` (TTY) revokes one;
  rows are kept for the audit trail.
- The passkey, challenge and code tables are owner-only: no grant to the API roles, `amem_app` or the
  automation roles. These commands use the admin connection string, like `amem rsi decide`.
- Changing the Night Desk domain invalidates the passkey (RP ID). Re-enroll after a domain change.

**Where the boundary is.** SQL cannot check an ECDSA signature, so it trusts the edge function for the
login and the assertion, exactly as the phone path trusts it for the TOTP code. Whoever holds the
`service_role` key or can redeploy `amem-decide` can record a web Apply (T2, T3 in
[least-privilege.md](least-privilege.md)). A stolen Night Desk session alone can Reject, but cannot Apply,
Undo or enroll a passkey. `passkey-enroll` / `passkey-revoke` refuse agent sessions and need the admin
connection string: a safeguard plus the Keychain gate, not a separate boundary.

**Function secrets** (set at deploy, not here): `RSI_OWNER_USER_ID` (your Supabase Auth user id),
`RSI_DECIDE_ORIGIN = https://night-desk-vilmer-frosts-projects.vercel.app`. Optional `RSI_WEBAUTHN_RP_ID`.

**Go-live.** `amem_request_undo_owner` (Night Desk's older Undo RPC) lets a signed-in session undo an edit
without Face ID. Its revoke is a separate file that `amem db migrate` does not read:
`supabase/migrations-golive/20261025220000_night_desk_web_golive.sql`. Apply it together with the Night Desk
deploy that uses `/web/*`, not before (the current Night Desk still calls the RPC):

```
git mv supabase/migrations-golive/20261025220000_night_desk_web_golive.sql supabase/migrations/
uv run amem db migrate
```

**Tests.** `parent-run -- python3 scripts/edge_test.py amem-decide` runs the Deno tests in the pinned image
`denoland/deno:2.9.7@sha256:fa335acd…` (a software authenticator; generated keys only). `tests/test_web_decide.py`
covers the SQL, the grants, the go-live revoke and an end-to-end run (real handler, real SQL, software
passkey); its Deno parts skip when Docker or the image is missing.

## The promotion worker (`amem rsi decide-poll`)

One worker at a time: a file lock (`~/sandbox/run/rsi-decide.lock`) and a Postgres advisory lock.
Each run:

1. `improve recover` finishes or rolls back any interrupted promotion (WAL).
2. Housekeeping: expire old proposals, mark stale ones.
3. For each unhandled decision, in order:
   - **reject**: record it. Nothing else.
   - **apply**: the local artifact bytes must equal the stored artifact (hash and every file). The
     worker writes a 0600 `rsi-decision/1` file and runs
     `improve promote --artifact H --expect-hash H --decision FILE`. `improve` then checks the
     decision (format, action, hash, expiry, not used for another artifact), confirms it with
     `amem rsi verify-decision`, checks every required evaluation, scope, pins and blocked rules,
     and checks each live file against `before_sha256`. Only then does it write (WAL → snapshot →
     atomic write → ledger → git commit).
   - **undo**: `improve revert-change <prm-id>`. Exact restore, or a conflict report with no writes.
4. `busy` (write lock held) retries up to 3 times. `stale` and other refusals are final.

## What promotion refuses (tested)

- No decision, a forged decision file, or a decision for another hash or action.
- An expired decision or proposal.
- A decision already used for another artifact.
- Artifact bytes changed after staging, or local bytes that differ from the database copy.
- A missing or failing required evaluation (default `static` + `sandbox-replay`), or an evaluation
  bound to another artifact or baseline.
- A live file that matches neither the before nor the after hash (stale baseline or conflict).
- A target outside `allowed_targets`, a pinned file or paragraph, or a blocked rule.
- Any run inside an agent session (`CLAUDECODE`, `CLAUDE_CODE_*`, `CODEX_*`, `IMPROVE_SESSION`,
  `RSI_SANDBOX`).

Under `apply_mode: "stage"`, `improve apply` refuses unattended runs too. Only a human at a TTY with
`--human` can still use it.

## Audit trail

- `rsi_artifacts`, `rsi_eval_results`, `rsi_decisions`: append-only. UPDATE, DELETE and TRUNCATE
  raise.
- `rsi_decision_results`: what the worker did with each decision, with attempts.
- The improve ledger (`improve/ledger.jsonl`, in git): `promoted` and `reverted` events with the
  artifact hash, the decision id, the files and their snapshots.

## Trust boundary (read this)

The worker, `improve` and your own shell run as the same macOS user. Anything that runs as that user
and can read the superuser `AMEM_DB_URL` can insert a decision row. What stops an agent today:

- the SQL token check, so only a holder of a phone token can record a phone decision;
- web Apply / Undo need the owner login and a Face ID assertion from a passkey enrolled with a code from
  your terminal (a web Reject needs the login only);
- `amem rsi decide` needs a TTY and refuses agent sessions;
- `improve promote` and `decide-poll` refuse agent sessions;
- the secret-read hooks block agents from reading `.env*` files.

The full fix is a least-privilege login role for the worker (member of `rsi_writer`, no insert on
`rsi_decisions`). The roles exist. Creating the login and moving the worker to it are activation
steps ([activation.md](activation.md), step 7).
