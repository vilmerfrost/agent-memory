# Mission 004A: backfill and activation plan

Status (2026-10-16): **adapters built and tested against local mocks only. Nothing is activated.** No
credential exists, no external service has been called, no routine runs `amem sync`, and no image was pulled.
Every numbered step below changes something outside the dev branch, so each needs its own approval.

Test evidence: [test-report.md](test-report.md). The NCM track is separate: [ncm-track.md](ncm-track.md).

**Mission 004B supersedes the hosting choices below.** The targets are ClickHouse Cloud, Langfuse Cloud EU,
past.dev and NCM on Modal: see [cloud-readiness.md](cloud-readiness.md) (findings, costs, backfill counts,
risks) and [cloud-activation.md](cloud-activation.md) (the per-provider approval steps).

## Status 2026-10-24: everything on

Vilmer: "everything should be turned on". The sinks' kill switches are on, ClickHouse and Langfuse were backfilled,
and these replace the incremental steps CH7, LF6 and PD6 below:

- **Daily sync.** `amem daily` ends with a sync step (docs/daily.md, step "sync"): `amem sync run --sink <sink>`,
  incremental, 120 s budget each, for every sink that has its settings and is switched on (clickhouse, langfuse,
  pastdev), at most once per local day, so ClickHouse Cloud can idle (cloud-activation.md: daily, not every 15
  min). An unconfigured or switched-off sink is a skip; a failure is an `amem-daily-warn sync ...` line, never a
  failed step, so it can never block the night. Stop one sink with its kill switch; there is no other schedule.
- **past.dev source ids.** Since 2026-10-24 past.dev refuses a source id with `/` (HTTP 400 `id-invalid`), so
  refs leave as `amem:<kind>:<id>` (`pastdev.wire`; `unwire` restores the ref). Links, dead letters and reviewed
  sets keep the ref. The records the first `--replay` dead-lettered for `id-invalid` had no link, so the next run
  sends them under the new ids and drops their dead letters. A reviewed set made before the change has other
  payload hashes (the id is in the payload): make it again with `amem sync plan --sink pastdev --contents --json`.
- **past.dev recall** is open for the investigator (config/rsi-evidence-providers.json, docs/rsi/evidence-providers.md):
  one recall per evidence pack, at most 3 packs a night, 0.1 credit each.

## Ground rules (all steps)

- **Supabase stays the only hub.** Sinks only read Supabase rows (as `amem_app`) and write their own links to
  `rsi_evidence` and their cursors to `rsi_checkpoints`. No new table and no migration are needed.
- **Kill switches:** `AMEM_SYNC_CLICKHOUSE=off`, `AMEM_SYNC_LANGFUSE=off`, `AMEM_SYNC_PASTDEV=off`, and
  `AMEM_PASTDEV=off` (sink and retriever). An unset URL or key means `unavailable`, never an error.
- **Secrets** go into `~/Projects/agent-memory/.env.local` only through the `request_secret` popup. Never in chat.
- **What leaves the Mac** (blueprint C5, enforced by the leak tests): ClickHouse (local) gets numbers, labels and
  ids only; Langfuse (local) gets sandbox runs only, with no prompts and no tool inputs; past.dev (external) gets
  sanitized decision text only; NCM gets nothing.
- **Not in 004A:** the investigator that reads the facade every night (blueprint M10), the sandbox-replay suite
  (Mission 004), and anything that depends on the K7/K4 guard fixes. No integration starts an autonomous coding
  experiment; Langfuse only records sandbox runs someone started.

## Step 0: merge (local)

| # | Step | Verify | Undo |
|---|---|---|---|
| 0.1 | After Phase I is verified, merge `rsi/m004a-integrations` into agent-memory `main` (local, no push) | full suite passes; `amem sync status` prints "no sync has run"; `amem evidence query --source ch rule_gap` answers `unavailable` | `git revert -m 1 <merge>` |

## ClickHouse (blueprint M11, experiment C1)

| # | Step | Needs | Verify | Undo |
|---|---|---|---|---|
| CH1 | **Real-server SQL check.** Pull `clickhouse/clickhouse-server` (pin the exact tag and digest at that time; arm64), run a throwaway container on 127.0.0.1 with synthetic rows only, run the adapter tests' SQL against it (schema, insert dedup, the 4 named queries, verify) | approval to download the image (Docker Hub, about 200–300 MB compressed) | every named query returns the expected rows; dedup holds | `docker rm -f`; delete the image |
| CH2 | **Local data stack.** A compose file with a dedicated ClickHouse on 127.0.0.1:18123/19000 (not Langfuse's), low-memory settings, data under `~/sandbox/stack/clickhouse`, one user with a random password | decision B8 (Docker VM memory); approval for the compose file | container healthy after a Mac reboot | `docker compose down` (the cursor holds) |
| CH3 | **Settings.** `AMEM_CLICKHOUSE_URL=http://127.0.0.1:18123`, `AMEM_CLICKHOUSE_USER`, `AMEM_CLICKHOUSE_KEY` via `request_secret` | — | `amem evidence query --source ch rule_gap` answers `ok` | remove the lines |
| CH4 | **Schema.** `clickhouse.ensure_schema()` | — | 8 ReplacingMergeTree tables in `amem` | `DROP DATABASE amem` |
| CH5 | **Backfill.** `amem sync run --sink clickhouse --replay --budget 900` (history since 2026-09-26; on 2026-10-16 about 1,300 sessions, 440 runs, 680 scores, 240 deviations, 770 skill calls, plus RSI rows) | — | `amem sync verify --sink clickhouse --days 30` prints `parity=True` | `--replay` again after a fix; or drop the database |
| CH6 | **C1 report.** Run the 4 named queries; write the report (parity, latency, storage MB, container RAM, the rule-gap finding) | — | report in the evidence folder | — |
| CH7 | **Incremental.** Add `amem sync run --sink clickhouse --budget 60` to `bin/amem-ingest` (every 15 min) and a registry row | approval (a routine change in agent-config) | `sync status` shows a fresh `last_ok_at`; routine-audit green | remove the step; the cursor holds |

Open tuning: each run re-sends rows changed in the last 7 days (`AMEM_CLICKHOUSE_LOOKBACK_DAYS`), so updates
without a moving timestamp are caught. Fine at today's volume; shorten it, or replay weekly, if volume grows.

## Langfuse (blueprint M12, experiment C2)

| # | Step | Needs | Verify | Undo |
|---|---|---|---|---|
| LF1 | **Stack.** The official self-host compose (Langfuse v4 web + worker, its own ClickHouse, Postgres 17, Redis 7, MinIO; arm64), data under `~/sandbox/stack/langfuse`, headless init (`LANGFUSE_INIT_*`) of org, project `rsi-sandbox` and keys | decision B8 (Docker VM memory, about 6–8 GB for this stack); approval to download the images (several GB) | the UI answers on 127.0.0.1; health ok | `docker compose down -v` |
| LF2 | **Settings.** `AMEM_LANGFUSE_HOST=http://127.0.0.1:<port>`, `AMEM_LANGFUSE_PUBLIC_KEY`, `AMEM_LANGFUSE_SECRET_KEY` via `request_secret` | — | `amem evidence query --source lf trace --param trace_id=<x>` answers `ok` | remove the lines |
| LF3 | **Real-server API check** of the parts tested only with mocks: the OTLP attribute mapping, the score upsert key on `/api/public/ingestion`, the dataset responses, the Observations v2 and Scores v3 fields | LF1–LF2 | one synthetic run uploads; the `trace` query returns its `amem_session_id`; a second upload adds nothing | delete the project |
| LF4 | **Runner sidecar.** The sandbox runner writes `<run_id>.rsi.json` (case, rep, scores, proposal) next to its log, so traces get a real case and rep | approval (a change to `sandbox/rsi_sandbox.py` on a dev branch, then merge) | sandbox tests pass; the sink reads the sidecar | revert |
| LF5 | **C2.** One agent-config eval case in the sandbox, both arms (the spend-capped sandbox keys exist), then `amem sync run --sink langfuse` | approval (paid model calls, a few cents) | every id resolves both ways (trace ↔ amem_session_id ↔ experiment); the experiment view compares the arms | delete the traces |
| LF6 | **Incremental.** `amem sync run --sink langfuse` after each sandbox experiment (not on a timer) | approval | — | stop calling it |

Known limits (from the adapter): a re-run of the same run_key keeps the first attempt's spans (Langfuse v4 does
not reliably dedupe re-exported spans), and Claude's `json` output has no per-tool detail.

## past.dev (blueprint M13, experiment C3)

past.dev is the only **external** service. Records come from `rsi_proposals`, `rsi_decisions`, `rsi_experiments`,
`edits` and `lesson_candidates`; each is our own decision text, sanitized (`integrations/sanitize.py`), at most
1,536 bytes, never a team item. Recalled refs are re-read from Supabase before use.

| # | Step | Needs | Verify | Undo |
|---|---|---|---|---|
| PD0 | **Decision B3:** accept an external SaaS (AWS, region not stated; LLM subprocessors not named) for sanitized decision records only | Vilmer | recorded | — |
| PD1 | **Account.** One past.dev project dedicated to RSI (pay-as-you-go allows one); a project key (`past_sk_...`) as `AMEM_PASTDEV_KEY` via `request_secret` | PD0; a payment method on file is Vilmer's action | `amem evidence query --source pd recall --param query=test` answers `ok` | revoke the key |
| PD2 | **Local dry run** (no network): `pastdev.build_records()` on the live database, as `amem_app`; print counts per kind, sanitizer refusals and 10 sample records for Vilmer to read | approval to read production rows for this | Vilmer accepts the sample; refusals have reasons | — |
| PD3 | **Backfill.** `amem sync run --sink pastdev --replay --budget 600`; batches of 100; 202 then a status poll | PD1, PD2 | every ingested record has an `rsi_evidence` row (system `pastdev`); credits used recorded | `pastdev.delete_all()` |
| PD4 | **C3.** Vilmer hand-labels a gold set of 20 questions with their answer refs; `pastdev.evaluate()` compares past.dev recall with `pastdev.fts_baseline()` (Postgres full-text over the same records): recall@5, MRR, latency, credits | PD3; Vilmer's labels | past.dev "wins" only at recall@5 ≥ baseline + 0.10; otherwise it stays an optional retriever, and the result is recorded | — |
| PD5 | **Delete drill.** Run `delete_all()` against the project once and re-ingest, before relying on it | PD3 | recall returns nothing after the delete; the re-ingest restores it | — |
| PD6 | **Incremental.** `amem sync run --sink pastdev --budget 60` from `bin/amem-ingest` | approval (a routine change) | `sync status` fresh | remove the step |

Behaviour to confirm at PD0 (the adapter's choices):
- A record that later becomes a team item, or is deleted in Supabase, is removed from past.dev on the next sync.
- `delete_all()` works even with `AMEM_PASTDEV=off`, so the kill switch never blocks a deletion.
- An ingestion past.dev fails as `content-rejected` is dead-lettered at once, and its records are not re-sent until
  they change; another failed ingestion gets the free retry by its id (at most 3 in all), then is dead-lettered.
  A record counts as sent only when its ingestion completed (Mission 004C). `sync status` shows the dead letters.
- Lesson records use the ref `amem:run/<id>` (there is no lesson entity).
- Not detected by the sanitizer: machine names without a dot, and bare user names.

## Rollback summary

| Integration | Stop | Remove data |
|---|---|---|
| ClickHouse | `AMEM_SYNC_CLICKHOUSE=off` (the daily sync step then skips it) | `docker compose down -v`; `delete from rsi_checkpoints where job = 'sync:clickhouse'` |
| Langfuse | `AMEM_SYNC_LANGFUSE=off` | `docker compose down -v`; delete the `rsi_evidence` rows with system `langfuse` and the cursor row |
| past.dev | `AMEM_PASTDEV=off` | `pastdev.delete_all()` (deletes every recorded point), then delete the `rsi_evidence` rows with system `pastdev` and the cursor row |
| NCM | nothing runs | delete the `rsi_evidence` rows with system `ncm` |

The `rsi_evidence` and `rsi_checkpoints` rows are our own operational records, not production config.
