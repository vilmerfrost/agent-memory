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
| G-CH1 | owner | Activate the $5,000 Startup credits. Create one **Basic** service in AWS **eu-central-1** or **eu-west-1**. Set spend alerts at 50/75/90/100%. Note the credits' expiry date (Billing page). | service idles; the Billing page shows credits | delete the service |
| G-CH2 | owner | IP access list: only the Mac's public IPv4 (the default is "anywhere"). | a request from another network is refused | edit the list |
| G-CH3 | owner | SQL console: paste the output of `amem sync schema --sink clickhouse --cloud`. Set the `amem_writer` password (12+ characters, mixed case, a digit or symbol) in the CREATE USER line first. | 9 tables exist in `amem`; `amem_writer` has SELECT, INSERT only | `DROP DATABASE amem; DROP USER amem_writer` |
| G-CH4 | agent (popup) | `request_secret`: `AMEM_CLICKHOUSE_URL` (`https://<host>:8443`), `AMEM_CLICKHOUSE_USER=amem_writer`, `AMEM_CLICKHOUSE_KEY`, and `AMEM_PSEUDONYM_KEY` (the owner makes it with `openssl rand -hex 32`; it never leaves the Mac) | `amem evidence query --source ch rule_gap` answers `ok` with 0 refs | remove the four keys |
| G-CH5 | agent | Verification C1 (disposable Postgres, synthetic rows) | every C1 criterion | `TRUNCATE` the tables |
| G-CH6 | owner approves | Review `amem sync plan --sink clickhouse --replay`, then run the real backfill once: `amem sync run --sink clickhouse --replay --budget 600` | `amem sync verify --sink clickhouse --days 30` shows parity; Usage under $1 | `TRUNCATE` the tables; `AMEM_SYNC_CLICKHOUSE=off` |
| later | owner approves | A daily sync (not every 15 min: that keeps the service awake) | — | — |

## Langfuse Cloud EU

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-LF1 | owner | Create the Langfuse Cloud **EU** org (Hobby) and one project `amem-rsi`, plus one API key pair. | — | delete the project |
| G-LF2 | agent (popup) | `request_secret`: `AMEM_LANGFUSE_HOST=https://cloud.langfuse.com`, `AMEM_LANGFUSE_PUBLIC_KEY`, `AMEM_LANGFUSE_SECRET_KEY` | `LangfuseClient.from_env().health()` is ok | remove the keys |
| G-LF3 | agent | Verification C2 (synthetic run logs in a temp sandbox folder) | every C2 criterion | delete the synthetic traces (`DELETE /api/public/traces`) |
| G-LF4 | owner approves | The first real sandbox trace: the next approved sandbox run, uploaded with `amem sync run --sink langfuse` | one trace with spans, scores and the `amem_session_id` round trip | `AMEM_SYNC_LANGFUSE=off`; delete the trace |

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
| G-NCM1 | owner | A Modal account (Starter, card on file). Set a **Workspace budget of $5** on Usage & Billing. | the budget is shown | close the account |
| G-NCM2 | owner | In a separate venv (not the amem one): `pip install modal==1.6.2`, then `modal setup` | `modal profile current` works | revoke the token in the Modal dashboard |
| G-NCM3 | owner approves, agent runs | Register the experiment: one `rsi_experiments` row (`kind='research'`, id `exp-YYYYMMDD-ncm`) | the row exists | it stays as a record (append-style registry) |
| G-NCM4 | owner approves | One run: `modal run deploy/ncm/modal_sudoku.py --experiment-id exp-YYYYMMDD-ncm` | every C4 criterion; spend at most $0.25 | `modal volume rm amem-ncm-results` |
| G-NCM5 | agent | `amem evidence record-ncm ncm-results/<run>/results.json`. With ClickHouse approved, a later sync carries it to `research_runs`. | one `ncm:run` row; `evidence query --source ch research_runs` shows it after a sync | `AMEM_SYNC_CLICKHOUSE=off` |

NCM never runs from the nightly routine. Nothing in amem-daily imports `ncm_worker` or the Modal script.

## Kill switches and status

```bash
amem sync status
```

- **Usage:** today's usage per sink, failing and dead tables, and dead letters.
- **Kill switches:** `AMEM_SYNC_CLICKHOUSE=off`, `AMEM_SYNC_LANGFUSE=off`, `AMEM_PASTDEV=off`.
- **Daily limits:** `AMEM_SYNC_<SINK>_MAX_ROWS_PER_DAY` and `AMEM_SYNC_<SINK>_MAX_REQUESTS_PER_DAY` lower the
  client-side limits. These are not billing caps.
