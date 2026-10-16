# Mission 004B: cloud readiness report

Status on 2026-10-16: **all four integrations are ready for live verification. None is connected.** No
cloud resource, credential, upload or GPU run exists yet. Every live step needs its own approval
(`cloud-activation.md`). Branch `rsi/m004b-cloud` (agent-memory), from `rsi/m004a-integrations`.

Sources: four research passes over the providers' public docs and specs (2026-10-16), the Langfuse Cloud
OpenAPI spec (`cloud.langfuse.com/generated/api/openapi.yml`), the past.dev spec (`past.dev/openapi.json`) and
the modal 1.6.2 source. "Unverified" means the docs do not say it.

---

## A. Provider readiness

| | ClickHouse Cloud | Langfuse Cloud EU | past.dev | NCM on Modal |
|---|---|---|---|---|
| API fit | HTTPS 8443, same HTTP interface; Shared engines | OTLP + scores array endpoint; v4 only from 2026-11-16 | `/api/v1` as in 004A, plus estimate, usage, retry | public repo + HF files; worker writes results.json |
| Hosting | AWS eu-central-1 / eu-west-1 / eu-west-2 (eu-north-1 is Enterprise-only) | AWS eu-west-1 | AWS, region not stated | Modal `eu` (1.15x price) |
| Adapter changes | done (below) | done | done | worker + script done, not run |
| State | ready for live verification | ready for live verification | ready for live verification | ready for live verification |

**ClickHouse Cloud.** The adapter now has a `cloud` mode (a `*.clickhouse.cloud` host, or
`AMEM_CLICKHOUSE_CLOUD=1`):
- plain http to a non-loopback host is refused;
- the DDL drops `non_replicated_deduplication_window` (Cloud tables are SharedReplacingMergeTree, which
  keeps an insert dedup log by default);
- no auto-schema: amem writes as `amem_writer`, with `SELECT, INSERT` on `amem.*` only, and the owner runs
  the DDL once (`amem sync schema --sink clickhouse --cloud` prints it);
- the health ping waits up to 60 s, because an idle service wakes on the first request;
- every insert sends `async_insert=0`, because Cloud 26.4 turned async inserts on by default.

**Langfuse Cloud EU.**
- Scores moved to `POST /api/public/scores` with a JSON array. Each score has a fixed id and timestamp, so
  a re-send updates it; the Cloud spec says to "send it again with the same id and timestamp".
- The legacy ingestion endpoint is not in the Cloud spec, so it is no longer used. No call uses an
  endpoint removed on 2026-11-16.
- Every call except the OTLP export is throttled to the Hobby limit of 30 requests per minute. A 429
  waits for `Retry-After` when it is 60 s or less.
- Observation reads use a start-time window of 30 days at most.
- Experiment scores come from `/experiment-items` in one paged call.
- Before an export, the sink checks Supabase for the run's `lf:trace` link. On Hobby, a trace older than
  30 days is hidden, not missing, so this check stops a replay from exporting its spans twice.

**past.dev.**
- A deleted id is retired and never re-sent. The docs: "A deleted id cannot be sent again; use a new id."
  Before this change, the sink re-sent such ids.
- 402 (out of credits) and a 429 that names a reached cap stop the run as `capped`.
- New `estimate()` and `usage()` calls, and a local credit estimate (`local_credits`).

**NCM.**
- `ncm_worker.py` turns the eval's stdout, `sha256sum` output and wall time into a results.json that the
  004A validator accepts. Each pin is checked: the repo commit, the HF revisions, and four file hashes.
- `deploy/ncm/modal_sudoku.py` runs sudoku-easy on one L4 GPU, with `timeout=900`, `retries=0` and
  `block_network=True`.
- The contract gained optional fields: dataset revision, dataset hashes, runtime, provider, limitations.

Every adapter: explicit verifying TLS (system CA store, TLS 1.2+), record-level dead letters, client-side
daily limits, kill switches. No provider ever needs a database credential (ClickPipes / FDW are not used).

## B. Implementation

| Area | Files |
|---|---|
| HTTP | `integrations/http.py`: TLS context, Retry-After, parsed loopback check |
| Sync loop | `integrations/sync.py`: daily limits, `capped`, dead letters, `provider_cap`, `plan()` (dry run) |
| ClickHouse | `integrations/clickhouse.py`: cloud mode, pseudonyms + team masking, `writer_sql`, `skill_outcomes`, `research_runs` table + query |
| Langfuse | `integrations/langfuse.py`: scores endpoint, throttle, windows, Supabase export check, experiment-items, `Rejected` |
| past.dev | `integrations/pastdev.py`: retired ids, estimate, usage, `local_credits`, `cost()`, split + content-rejected |
| Questions | `integrations/questions.py`, `integrations/sb_queries.py`, `evidence.py` (Supabase retriever) |
| NCM | `integrations/ncm.py` (optional fields), `integrations/ncm_worker.py`, `deploy/ncm/modal_sudoku.py` |
| CLI | `cli_integrations.py`: `sync plan`, `sync schema`, `evidence ask`, `evidence questions`, `evidence record-ncm` |
| Tests | `test_integrations_cloud.py`, `test_integrations_questions.py`, `test_integrations_ncm_worker.py`, updates to the 004A files |

Full suite on the branch: **1,718 tests, 1,717 passed, 1 skipped** (2026-10-16). Every provider is a local
mock or stand-in; no real endpoint was called.

No Supabase migration: `research_runs` reads the existing `rsi_evidence`. No live routine, hook, schedule or
production file changed.

## C. Real-provider verification plan (one small synthetic test per service)

Each test uses a **disposable local Postgres** (docker, `amem db migrate`) seeded with the synthetic rows the
test suite uses. These rows carry `ZZLEAK...` markers in every text column. Production Supabase is never the
source in these tests.

**C1 ClickHouse** (after G-CH1 to G-CH4).
1. `AMEM_DB_URL=<disposable> amem sync run --sink clickhouse --json`
2. `amem sync verify --sink clickhouse --days 7`
3. `amem evidence query --source ch rule_gap` and `... skill_outcomes --param min_n=1`
4. Run step 1 again.
5. As `amem_writer` in the SQL console: `CREATE TABLE amem.x (a Int8) ENGINE = Memory`.

Accept when:
- step 2 shows `parity=yes`;
- step 3 returns metrics whose `amem:` subjects re-read;
- step 4 adds no rows after FINAL;
- `SELECT count() FROM amem.sessions WHERE position(toString(tuple(*)), 'ZZLEAK') > 0` is 0 for every table;
- step 5 is denied;
- the Usage page shows under $0.10.

Prepared runner (Mission 004C, not yet run against Cloud): seed the disposable database with
`amem synth seed --db-url <local URL> --out synth-out` (synthetic story + `gold.json`), then
`uv run python scripts/c1_clickhouse.py --db-url <local URL> --gold synth-out/gold.json` runs every step above
plus a named-query comparison against gold, the grant probes, the bad-host failure path, team masking and the
marker leak scan, timed, into `synth-out/c1-report.json`. `--dry-run` prints the plan and touches nothing.

**C2 Langfuse** (after G-LF1 to G-LF3). Synthetic run logs (the suite's `write_run`) go in a temp
`RSI_SANDBOX_DIR`.
1. `amem sync run --sink langfuse`
2. `amem evidence query --source lf trace --param trace_id=<id>`
3. Run step 1 again, then add a later score file and run step 1 a third time.
4. Two arms, then `experiment_scores`.
5. Set a wrong host and run step 1.

Accept when:
- the trace shows root + generation + tool spans and the `amem_session_id` round trip matches;
- the second run exports 0 spans;
- the later score updates without a duplicate;
- `experiment_scores` shows both arms;
- the wrong host answers `unavailable` and the cursor does not move;
- no marker appears in Langfuse;
- the usage page shows under 200 units.

**C3 past.dev** (after G-PD1 to G-PD3). Set a project credit cap of 2,000 first. Use the suite's synthetic
decision records.
1. `amem sync run --sink pastdev`
2. Recall: `amem evidence query --source pd recall --param query=...`
3. Run step 1 again.
4. Delete the data points (`pastdev.delete_all`), then run step 1.
5. Read usage with `GET /usage`.

Accept when:
- ingestion completes and the poll settles;
- recall returns `amem:` refs that re-read in the disposable database;
- the second run reports `unchanged` and spends 0 credits;
- after the delete, the deleted ids are listed as retired and nothing is re-sent;
- credits spent are at most 100.

**C4 NCM** (after G-NCM1 to G-NCM3). Run `modal run deploy/ncm/modal_sudoku.py --experiment-id exp-YYYYMMDD-ncm`,
then `amem evidence record-ncm ncm-results/<run>/results.json`.

Accept when:
- the status is `ok`, the checkpoint hashes equal the pins, and `solved_rate >= 0.98` (the paper reports 99.5%);
- `cost.usd <= 0.25`, and the Modal dashboard agrees to within $0.05;
- one `ncm:run` evidence row is linked to the experiment.

A failed run is recorded as `failed` with its reason, never as a result.

## D. Historical backfill plan (production counts, read-only aggregates, 2026-10-16)

The counts came from aggregate SELECTs through the Supabase connector. They are counts only, no row
content. `amem sync plan --sink <s> --replay` (read-only, provider never called) gives exact per-sink counts
once the code runs where the production connection is configured.

| Source table | Rows | To ClickHouse | To past.dev |
|---|---|---|---|
| sessions | 1,340 (1,196 prod; 229 team) | all rows; numbers, labels, ids; project/repo as pseudonyms; team rows as `team` | no |
| runs | 448 | yes | no |
| deviations | 237 (236 prod) | yes (no what/why/how) | no |
| scores | 678 | yes | no |
| skill_calls | 765 | yes (project masked) | no |
| rsi_proposals / rsi_decisions | 1 / 1 | yes | yes (sanitized), if not team |
| rsi_experiments / rsi_eval_results | 0 / 0 | eval results only | experiments only |
| edits | 10 (6 team) | no | 4 candidates |
| lesson_candidates (accept/reject) | 54 (0 team) | no | 54 candidates |
| rsi_evidence (system ncm) | 0 | `research_runs` | no |
| sandbox run logs (local) | 0 | no | no (Langfuse only) |

Volumes:
- **ClickHouse:** about 3,470 rows. At an estimated 400 bytes per row, that is about 1.5 MB, sent in about
  11 requests: one day at the client limit.
- **past.dev:** about 60 records, minus what the sanitizer refuses. That is at most 5 credits each (1.5 KB
  cap, 350 bytes per credit), so 300 credits or fewer; about 100 is realistic. It fits the 300 rows/day
  limit, so one day.
- **Langfuse:** 0 now.

Verification after the backfill:
- `amem sync verify --sink clickhouse --days 30` must show parity. Rows the mapper drops (a key that is not
  a ref id) show as mismatches.
- `amem sync status` must show no failing tables, and no dead letters (or reviewed ones).
- For past.dev: the count of rsi_evidence links with `system='pastdev'` equals the records sent, and
  `/usage` matches the local estimate within 20%.

The real backfill is `amem sync run --sink <s> --replay`, run once per approved provider and reviewed
first (G-*-5). Recurring sync stays off.

## E. Shared intelligence

`amem evidence ask <question>` answers the brief's six questions, plus one added in Mission 004C:
- recurring_failures
- best_combinations
- attempted_before
- why_rejected
- failed_eval_traces
- after_approval
- repeat_failures (Mission 004C): why sessions keep failing the same rule (Claude vs Codex), and whether a
  similar change was already tested (attempts, recall, evals, stored trace links, the failing trace).
  `scripts/demo_evidence_ask.py --docker` runs it on the synthetic dataset.

Each question is a fixed plan of named queries, Supabase first. Every finding carries:
- the source service;
- the authoritative Supabase ref, re-read now;
- the timestamp;
- the evidence type;
- measurement metadata (basis, n, confidence, rank or rate);
- a status: `authoritative`, `linked`, `stale` or `unlinked`.

`--record` stores linked external findings in `rsi_evidence`, valid for 30 days. That is the only write.
Since Mission 004C it also stores one `answer` row (the structured finding: params, status per provider, steps,
the Supabase refs it rests on), and it never re-writes a link a Supabase step only read back, or a past.dev
recall (whose ref is the past.dev sink's own link key).

Example flow (tested on the disposable database, `test_integrations_questions.py`): "Has rule
`claude-md:global:q-quote-globs` been attempted before?"
1. Supabase `attempts_for` finds a promoted proposal, a rejected proposal and a reverted improve edit. These
   are authoritative, and their titles are scrubbed.
2. past.dev `recall` ("q quote globs") would return `amem:` refs, which are re-read before use. Here it
   answered `unavailable`, and the answer still completed.
3. `after_approval` on the promoted proposal: Supabase counts the rule's deviations in equal windows before
   (3 in 3 sessions) and after (1 in 3 sessions). Phase 2 then asks ClickHouse `rule_weekly_rate` for the
   same rule key.
4. `failed_eval_traces` follows Supabase's `lf:trace` link for the failing arm into a live Langfuse trace
   read. The trace is `linked` to `amem:experiment/...`.
5. `--record` keeps the ClickHouse `rule_gap` finding as an `rsi_evidence` row. The row is idempotent and
   carries `metrics.question`.

No provider answer changes a proposal, decision, instruction or schedule.

## F. Cost and usage

| | Free credit / tier | Expected monthly use | After credits | Provider cap | Client-side limit |
|---|---|---|---|---|---|
| ClickHouse | existing org (Scale plan): Startup $5,000 valid 2026-10-16 to 2027-10-16 and Trial $300 to 2026-11-06, both active (verified 2026-10-17); then the card on file | `amem-rsi` 1 x 8 GiB in eu-west-1, ~$0.333/h awake: C1 under $0.10; a daily sync (10-15 min awake) about $2-3/month | the same ~$2-3/month, charged to the card after the credits | **none**; spend alerts only notify | 100,000 rows / 2,000 requests per day |
| Langfuse | Hobby: 50k units/month, free | ~30 units/run; 40 runs/week is about 5k units (10%) | Hobby stays free; Core $29/month if it is ever needed | **none**, no spend alerts on Hobby (source code suggests blocking at 250k units, unverified) | 500 rows / 1,000 requests per day; 30 req/min throttle |
| past.dev | ~150,000 signup credits (expiry unverified); bought credits expire in 90 days | backfill ~100 credits; then <50/month plus recall at 0.1 each | prepaid: at 0 credits sends answer 402 and nothing is billed | **yes**: the credit balance (402) and a project credit cap (429) | 300 rows / 200 requests per day |
| NCM / Modal | Starter: $30/month included credit; card required | one run, est. $0.05 | per second | **yes**: Workspace budget stops workloads; `timeout=900` caps one run at ~$0.23 | one run per approval |

The client-side limits bound what this code sends. **They are not billing caps.** A provider bills
whatever it receives, and another client (or a person in the console) can still spend. ClickHouse and
Langfuse have no hard cap, so watch:
- ClickHouse spend alerts, the Usage page, and `clickhousectl cloud org usage`;
- Langfuse's usage page;
- `amem sync status`, which shows usage today per sink.

Data that leaves:
- **ClickHouse:** allowlisted numbers, labels and ids; pseudonyms (the key never leaves the Mac). Never text.
- **Langfuse:** sandbox runs only: ids, model and tool names, tool statuses, tokens, cost and scores. Never
  prompts, tool inputs or the agent's final response (excluded by default since 2026-10-17; a model or tool
  name that looks like a path, credential, email or token is sent as `redacted`).
- **past.dev:** sanitized decision records of at most 1.5 KB. No team items, paths, emails or secrets.
- **NCM:** nothing leaves. The run uses public files only.

Retention and deletion:
- **ClickHouse:** `DROP DATABASE amem`, or delete the service. Deleting it cannot be undone.
- **Langfuse:** 30 days visible on Hobby; deletion by trace id through the API (async); delete the project.
- **past.dev:** 365 days on Pay as you go; `delete_all` (deleted ids are retired); delete the project.
- **Modal:** delete the `amem-ncm-results` volume.

Disable:
- `AMEM_SYNC_CLICKHOUSE=off`, `AMEM_SYNC_LANGFUSE=off`, `AMEM_PASTDEV=off`;
- unset the provider URL or key, which makes the integration `unavailable`;
- or stop or delete the provider resource.

## H. Remaining risks

**Blockers for "connected"**: all of these are expected, because nothing is authorized yet.
1. No provider account, credential or resource exists. Each needs its own approval (G).
2. ClickHouse SQL has never run on a real server. The riskiest queries are `routine_streaks`,
   `rule_weekly_rate` (join), `skill_outcomes` and `research_runs`. C1 is the first real run.
3. Assumptions not yet checked against live services:
   - ~~Langfuse: the OTLP attribute mapping on Cloud.~~ Verified by C2 on 2026-10-17 (see cloud-activation.md).
   - past.dev: that `sourceId` echoes our id, the deleted-id error code, and the health probe.
   - Modal: the script, torch 2.8 on the image, and the L4 wake and run times.
   C1 to C4 check each one.

**Open risks by decision (unchanged):**
- T3: the Supabase connector keeps full access.
- R2: the same-user production-file write bypass, so no unattended promotion.
- K7/K4 hardening is not started (separate mission).

**Cost risks:**
- ClickHouse and Langfuse cannot be hard-capped by the provider.
- ClickHouse credits can run out without notice, and a 15-minute sync keeps the service awake (keep it daily).
- The ClickHouse IP allowlist defaults to "anywhere", and scanners can wake the service. G-CH2 restricts it.

**Privacy notes:**
- past.dev does not state its region or subprocessors (a DPA is on request).
- Langfuse lists US support tools and ClickHouse affiliates.
- Both get only sanitized or synthetic content.

**Optional improvements (not needed for V1):**
- a ClickHouse TTL and a separate read-only user;
- a past.dev `/ingest/estimate` pre-flight before a large send (it uploads content, so it needs approval);
- the Langfuse dataset mode for sandbox experiments;
- ClickHouse Billing API polling.
