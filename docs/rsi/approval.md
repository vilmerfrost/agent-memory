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
- `amem rsi decide` needs a TTY and refuses agent sessions;
- `improve promote` and `decide-poll` refuse agent sessions;
- the secret-read hooks block agents from reading `.env*` files.

The full fix is a least-privilege login role for the worker (member of `rsi_writer`, no insert on
`rsi_decisions`). The roles exist. Creating the login and moving the worker to it are activation
steps ([activation.md](activation.md), step 7).
