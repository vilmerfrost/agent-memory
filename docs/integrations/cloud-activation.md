# Mission 004B: activation checklist (deliverable G)

Each line is its own approval. Approval for one step or one provider does not cover the next. Nothing
here enables recurring sync, the nightly investigator, production-session tracing or promotion.

- **Owner:** steps only the owner can do (accounts, billing, console clicks).
- **Agent:** steps the agent does after the owner's approval.

Secrets go through the `request_secret` popup into `.env.local` of the checkout that runs `amem`. They never
go in chat, logs or source.

Recommended order:
1. **Langfuse:** free and synthetic.
2. **past.dev:** prepaid, with a hard stop.
3. **ClickHouse:** no provider cap.
4. **NCM:** one capped GPU run.

## 0. Code (shared by all four)

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-0 | owner approves, agent runs | Fast-forward `~/Projects/agent-memory` main to `rsi/m004b-cloud`, locally, no push. Run the live suite. | suite passes; `amem sync status` unchanged; Phase I checks still pass | `git reset --hard <previous main>` |

## ClickHouse Cloud

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-CH1 | owner approves; owner or agent clicks | In the **existing** org (BatchCortex: Scale plan, card on file): no new org, no second trial, no plan change. Create one dedicated service `amem-rsi` in AWS **eu-west-1**: Custom configuration, vertical autoscaling, **1 replica, 8 GiB min and 8 GiB max**. Then Settings › Scaling: idle after **5 min**. Billing › Actions › Add spend alerts: one amount (**$10**; alerts at 50/75/90/100%). | the service is Idle; Billing shows both credit grants unchanged and one spend alert; `My first service` untouched | delete `amem-rsi`; remove the alert |
| G-CH2a | owner (admin, Cloud SQL console) | Schema, separately from the user: paste `amem sync schema --sink clickhouse --cloud --part admin` (CREATE DATABASE `amem` + 9 tables, IF NOT EXISTS) and run it as the console admin. | `SHOW TABLES FROM amem` lists 9 tables | `DROP DATABASE amem` |
| G-CH2b | owner, then agent (popup) | Credentials: the owner generates the writer password in the password manager (32 random characters with symbols; Cloud rule: 12+ with upper, lower, digit, special). `request_secret` into the 004B worktree `.env.local`: `AMEM_CLICKHOUSE_URL` (`https://<host>:8443`, from the service's Connect panel), `AMEM_CLICKHOUSE_USER=amem_writer`, `AMEM_CLICKHOUSE_KEY`. | `amem sync schema --part writer --hash-from-env` exits 0 (rule met; prints nothing secret) | remove the three keys |
| G-CH2c | agent prepares, owner runs | Writer: `amem sync schema --sink clickhouse --cloud --part writer --hash-from-env \| pbcopy` puts `CREATE USER amem_writer IDENTIFIED WITH sha256_hash BY '<hash>' DEFAULT DATABASE amem` and `GRANT SELECT, INSERT ON amem.* TO amem_writer` on the clipboard (never in chat or logs). The owner pastes and runs them in the SQL console, then `clearclip`. | `SHOW GRANTS FOR amem_writer` shows only that GRANT | `DROP USER amem_writer` |
| G-CH2d | agent | Verification: `amem sync grants --sink clickhouse` as the writer: SHOW GRANTS is exactly SELECT, INSERT ON amem.*; SELECT and a zero-row INSERT work; only `amem`, `system`, `information_schema` visible; CREATE DATABASE, CREATE TABLE, ALTER, CREATE USER, GRANT, system.users and system.query_log denied (ACCESS_DENIED). Each probe is harmless even if it ran. | all 11 probes ok | as G-CH2c and G-CH2a |
| G-CH3 | owner approves, agent runs | Verification C1 (disposable Postgres, synthetic rows; needs `AMEM_PSEUDONYM_KEY` via the popup, made with `openssl rand -hex 32`). The disposable DB is migrated with a Keychain tripwire (see the separate migrate bug). | every C1 criterion | `TRUNCATE` the tables |
| G-CH4 | owner approves | Review `amem sync plan --sink clickhouse --replay`, then run the real backfill once: `amem sync run --sink clickhouse --replay --budget 600` | `amem sync verify --sink clickhouse --days 30` shows parity; Usage under $1 | `TRUNCATE` the tables; `AMEM_SYNC_CLICKHOUSE=off` |
| later | owner approves | A daily sync (not every 15 min: that keeps the service awake) | — | — |

**G-CH2 done 2026-10-17 (owner approval; Mission 004C Phase A):** G-CH2a: `amem` database and 9 tables created from the console as the org admin (all `SharedReplacingMergeTree`). G-CH2b: `AMEM_CLICKHOUSE_KEY` via the popup into the 004B worktree `.env.local` (Cloud rule met); URL `https://gsgozod89n.eu-west-1.aws.clickhouse.cloud:8443` and user `amem_writer` are passed as plain env (not secrets). G-CH2c: the CREATE USER line (hash only) went from the clipboard into the console by the owner; the clipboard was cleared after; the GRANT ran from the console. `SHOW GRANTS FOR amem_writer` = `GRANT SELECT, INSERT ON amem.* TO amem_writer`. G-CH2d: `amem sync grants` from the Mac: 11/11 ok; the writer sees only the `amem` database; CREATE DATABASE/TABLE, ALTER, CREATE USER, GRANT, system.users and system.query_log all ACCESS_DENIED (15 s total, cold service). Credits after: Trial $0.47 used, Startup $0, billable $0.00. Known limit: the console's query history keeps the CREATE USER text (hash, not password).


**Account state 2026-10-17 (read-only check of the console, no changes):** org BatchCortex, plan Scale, card
on file, next billing period 2026-11-01. Credits: Startup Program $5,000, Active, valid 2026-10-16 to
2027-10-16, used $0; Trial $300, Active, valid 2026-10-06 to 2026-11-06, used $0.43 (remaining
$299.57). Gross usage since 2026-10-06: $0.43 (compute of `My first service`), all covered by Trial credits,
billable $0.00. The docs say trial credits are drawn first until they run out or expire; the order between
the two grants is not documented beyond that. After both, usage is charged to the card: there is no hard cap.
No spend alert exists. One existing service, `My first service` (AWS eu-west-1, 2 replicas, 16-120 GiB,
idle after 15 min, IP list "anywhere", Idle): not used by amem and not changed by this plan. Scale compute
in eu-west-1 is about $0.333 per 8 GiB-hour (us-west-2 $0.29846 times the eu-west-1/us-west-2 ratio of the
Enterprise rates); `amem-rsi` costs that only while awake.

**G-CH1 done 2026-10-17 (owner approval of the same day; the IP list part of G-CH2 was included in it):**
service `amem-rsi`, id `ff3ccf61-f3e4-41e1-a72c-e853ca2582ef`, AWS eu-west-1, Scale plan (unchanged), version
26.6, Regular channel; Custom, vertical autoscaling, 1 replica, pinned at 8 GiB / 2 vCPU; idle after 5 min;
IP access list: 1 specific location (the Mac's public IPv4, 158.174.186.218, same from the browser and the
shell); crash reports off; daily backup kept 1 day (default). Organization spend alert: $100 per billing
period, alerts at $50/$75/$90/$100 by email and UI (org-wide: it counts every service, including `My first
service`). Untouched: the org, the plan, the credits, `My first service`. No database user, no data. Next
steps (G-CH2 onward, renumbered on 2026-10-17: G-CH2 = least-privilege access) need their own approval.

## Langfuse Cloud EU

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-LF1 | owner | Create the Langfuse Cloud **EU** org (Hobby) and one project `amem-rsi`, plus one API key pair. | — | delete the project |
| G-LF2 | agent (popup) | `request_secret`: `AMEM_LANGFUSE_HOST=https://cloud.langfuse.com`, `AMEM_LANGFUSE_PUBLIC_KEY`, `AMEM_LANGFUSE_SECRET_KEY` | `LangfuseClient.from_env().health()` is ok | remove the keys |
| G-LF3 | agent | Verification C2 (synthetic run logs in a temp sandbox folder) | every C2 criterion | delete the synthetic traces (`DELETE /api/public/traces`) |
| G-LF4 | owner approves | Not before the final-response exclusion (default since 2026-10-17). The first real sandbox trace: the next approved sandbox run, uploaded with `amem sync run --sink langfuse` | one trace with spans, scores and the `amem_session_id` round trip | `AMEM_SYNC_LANGFUSE=off`; delete the trace |

**Status 2026-10-17:** G-LF1 to G-LF3 done; C2 accepted by the owner. EU project `amem-rsi`, keys in the
004B worktree's `.env.local` only, synthetic data only, no recurring sync. C2 (2 synthetic traces, about 20
units): root, generation and tool spans with the `amem_session_id` round trip; a re-run sends 0 batches and a
replay adds 0 observations; a later score updates in place; `experiment_scores` shows both arms; a wrong host
is `unavailable` and leaves the cursor; 0 of 8 planted markers in 19.6 KB read back. The two traces are kept.

## past.dev

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-PD1 | owner | Create the dedicated project `amem-rsi`. Pay as you go allows one project; this uses it. Set a **project credit cap of 2,000**. Create one project key (`past_sk_...`). | the usage page shows the cap | delete the project (this revokes its keys) |
| G-PD2 | agent (popup) | `request_secret`: `AMEM_PASTDEV_KEY` | `PastDevSink().health()` is ok | remove the key |
| G-PD3 | agent | Verification C3 (synthetic decision records) | every C3 criterion | `delete_all`, then delete the project |
| G-PD4 | agent | Controlled comparison: past.dev recall vs Supabase full text on the same synthetic sanitized set (`pastdev.evaluate`) | a report with recall@k for both | — |
| G-PD5 | owner approves | Real historical import: review `amem sync plan --sink pastdev --replay` (about 60 records, at most 300 credits), then `amem sync run --sink pastdev --replay` | links equal records sent; `/usage` is within 20% of the plan | `AMEM_PASTDEV=off`; `delete_all` (the ids are retired) |

## NCM on Modal

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-NCM1 | owner | A Modal account (Starter, card on file). Set a **Workspace budget of $5** on Usage & Billing (Modal's docs call it the hard outer cap; it bounds the workspace, not one run), plus a spend limit. | the budget and the limit are shown | close the account |
| G-NCM2 | owner | In a separate venv (not the amem one): `pip install modal==1.6.2`, then `modal setup` | `modal profile current` works | revoke the token in the Modal dashboard |
| G-NCM3 | owner approves, agent runs | Register the experiment: one `rsi_experiments` row (`kind='research'`, id `exp-YYYYMMDD-ncm`) | the row exists | it stays as a record (append-style registry) |
| G-NCM4 | owner approves | One run: `modal run deploy/ncm/modal_sudoku.py --experiment-id exp-YYYYMMDD-ncm` | every C4 criterion; spend at most $0.25 per run (worst case ~$0.22: `docs/integrations/ncm-track.md`, N2 caps) | one run: `modal volume rm amem-ncm-results /<run_id> -r`; all: `modal volume delete amem-ncm-results` |
| G-NCM5 | agent | `amem evidence record-ncm ncm-results/<run_id>/results.json` (`run_id` = `ncm-YYYYMMDD-HHMMSS-sudoku-easy`). With ClickHouse approved, a later sync carries it to `research_runs`. | one `ncm:run` row; `evidence query --source ch research_runs` shows it after a sync | `AMEM_SYNC_CLICKHOUSE=off` |

NCM never runs from the nightly routine. Nothing in amem-daily imports `ncm_worker` or the Modal script.

## Kill switches and status

```bash
amem sync status
```

- **Usage:** today's usage per sink, failing and dead tables, and dead letters.
- **Kill switches:** `AMEM_SYNC_CLICKHOUSE=off`, `AMEM_SYNC_LANGFUSE=off`, `AMEM_PASTDEV=off`.
- **Daily limits:** `AMEM_SYNC_<SINK>_MAX_ROWS_PER_DAY` and `AMEM_SYNC_<SINK>_MAX_REQUESTS_PER_DAY` lower the
  client-side limits. These are not billing caps.
