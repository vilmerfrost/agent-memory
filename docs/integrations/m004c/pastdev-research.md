> Research draft, 2026-10-17, written before the Mission 004C fixes landed. The status of every finding listed here is in ../m004c-report.md, which wins where they differ.

# Mission 004C, Phase C: past.dev approval request (draft, 2026-10-17)

Read-only research. Nothing was sent to past.dev. No key was used. Nothing in the repo changed.
Code read: worktree `/Users/vilmerfrost/worktrees/agent-memory/m004b`, branch `rsi/m004b-cloud`, HEAD `4898e32`.

## 0. Sources (all fetched 2026-10-17)

| Source | Notes |
|---|---|
| https://past.dev/openapi.json | OpenAPI 3.1.0, info.version 1.0.0, HTTP Date `Fri, 16 Oct 2026 23:03:21 GMT`, ETag `1bc2e689c85ca9a70e8db0c60d2f9db6`, 221,362 bytes. Read in memory; not saved. |
| https://past.dev/docs/memory-api/api-reference | 34 operations; caps text; retry text |
| https://past.dev/docs/memory-api/how-it-works | unchanged, idempotency, rate limits, credit rule |
| https://past.dev/docs/memory-api/concepts | limits table, projects per plan, payload retention |
| https://past.dev/docs/memory-api/errors | error envelope and codes |
| https://past.dev/docs/memory-api/sending-data | ids, deletion, "a deleted id cannot be sent again" |
| https://past.dev/docs/memory-api/debugging | estimate, usage, request log |
| https://past.dev/pricing.md | last-updated 2026-10-16 |
| https://past.dev/privacy, /security, /terms | metadata last-updated 2026-10-16 |
| https://past.dev/docs/llms-full.txt | full docs text, searched for exact wording |

## 1. Facts

V = verified on an official page today. U = unverified (the docs do not say it).

### 1.1 Projects, caps, keys

- V Pay as you go (PAYG): 1 project per organization, archived projects included. Creating a second one returns `402 project-limit-reached` (concepts limits table; errors page).
- V A project is created in the console, or with `POST /api/v1/projects` (management key `past_mk_`). Delete: "Deletes a project permanently and revokes every key it holds" (MCP tools table, `delete_project`).
- V Two monthly caps per project: `creditCap` ("The project's monthly credit cap") and `ingestionCap` ("The project's monthly data-point cap"). Both are set "under Settings, Projects in the console" (api-reference, `GET /projects`). No API sets them. They reset with the calendar month in UTC.
- V A reached cap answers `429 project-cap-reached` with the JSON error body. Out of credits answers `402 out-of-credits` ("Do not retry"). `429 spend-cap-reached` is an account spend cap per billing period. The per-minute 429 has no body.
- V Caps never refuse recall: "No plan caps the number of recall calls: a recall is never refused for an allowance."
- V Keys: project key prefix `past_sk_` (one project), management key prefix `past_mk_` (one per organization, Settings). Project keys are made on the console's API keys screen, or `POST /projects/{slug}/keys`. Revoke any time (console, or `DELETE /projects/{slug}/keys/{keyId}`). An expired or revoked key answers `401 unauthorized`.
- U The character set and length after `past_sk_`. U Whether a sign-up creates a default project. U Where the account spend cap is set on PAYG.

### 1.2 Endpoints the adapter uses (base `https://api.past.dev/api/v1`, V)

| Call | Verified shape |
|---|---|
| `POST /ingest/batch` | `{items[1..1000] of IngestItem, idempotencyKey <=200}`, additionalProperties false. Item: `content` (required, non-blank), `id`, `timestamp` (date-time, microseconds), `label`, `metadata`, `audience`, `identity` (<=200). `202` queued or `200` when every source is unchanged. Result `{projectId, ingestionId, status, unchanged, items[{ordinal, sourceId, status, unchanged}]}`. Errors 400/401/402/403/409/413/429/503. "One atomic ordered batch", at most 16 MiB. Duplicate ids in one batch: `400 invalid-ingestion`. |
| `GET /ingest/{id}` | `{status: processing/completed/failed, settled, blocked, readiness{raw, comprehension, consolidation, failed}, failure{code, message} or null}`. 404 for an unknown id. Polls are free and outside the per-minute limit. `settled` describes the whole project. |
| `POST /ingest/{id}/retry` | `202 {ingestionId, status: queued}`. Free, "checked against no cap". `409 ingestion-not-stopped` when the send has not stopped. |
| `POST /ingest/estimate` | `{items or files, timeZone, points}` -> `{dataPoints, bytes, credits, batches, fits, fitsFirst, balance{left, resetsAt, projectCapLeft, ingestionCapReached}, metered, ...}`. "Writes nothing, spends nothing, and leaves no row in the request log." |
| `POST /recall` | `{query, identity (both required), limit 1..200 (default 100), level, maxTokens, queryTimestamp, occurredFrom, occurredTo, sort}` -> `{asOf, usedEvidenceTokens, results[{id, rank>=1, occurredAt, content, artifact{id, kind, occurredAt, supersededAt?}, sources[{sourceId, occurredAt, label?, metadata?, excerpts[]}]}]}`. `sources[].label`: "as sent at ingest". |
| `POST /data-points/delete` | `{ids[1..1000]}` -> `202 {operationId, ids}`. "Every id must name a live data point of the project, or nothing is deleted": `404 data-point-not-found`. Free. Purge in background; "The data points leave recall at once". |
| `GET /usage` | `{month{from, to, resetsAt}, balance{left, included, bought, rolledOver, spent}, project{slug, credits, ingestions, recalls, creditCap, creditCapLeft, ingestionCap, ingestionCapLeft}, metered}`. Outside the per-minute budget. |

Other V facts:
- Duplicates: same `id` with the same content, label, metadata and timestamp is `200 unchanged: true`, free. A change to any of the four is a new revision: charged, replaces the data point and its memory. Omitted label/metadata/timestamp keep stored values; `audience` resets to project.
- Source id: "Any non-blank string, trimmed; no length limit". Avoid `/` in ids only for the path form `DELETE /data-points/{sourceId}`; "`POST /api/v1/data-points/delete` takes any id in its body".
- Idempotency key: per batch, project-scoped, never expires. Same key + same body returns the original receipt (also while in flight, and for a stopped send). Same key + other body: `409 idempotency-conflict`.
- Deleted ids: "A deleted id cannot be sent again; use a new id." and "A deleted id is refused when sent again". U The status and code of that refusal.
- Failed sends: "A failed send does not clear on its own, and its data points are kept." "Retry is the way back, because sending the same content again does nothing: identical content is unchanged ...". `content-rejected` (the provider answered 400/403/413, or "the text held nothing to index") is the one failure a retry does not fix: "Change the content and send it as a new revision."

### 1.3 Cost, limits, retention, privacy

- V Ingest: 1 credit per 350 UTF-8 bytes of content, rounded up per data point, minimum 1. Charged only for new data points and new revisions. Free: unchanged re-sends, idempotency replays, polls, retries, deletions, refused calls, estimate, usage.
- V Recall: 0.1 credit per call on PAYG ("recall calls x 0.1, rounded up"); free on Flex and Enterprise. On PAYG a recall also answers 402 when out of credits.
- V Rate limit: 500 requests/min per key on PAYG, fixed one-minute window on sends, recalls and answers.
- V Starting credits: 150,000 (400,000 with a work email), "granted once and do not come back". U Any expiry date for them. V Bought credits expire after 90 days on PAYG. V Buying needs a card; PAYG has no auto top-up.
- V Retention on PAYG: data 365 days. API request-log payloads 7 days: `GET /requests/{id}` returns "the stored request and response bodies while the plan's payload retention keeps them"; the call record stays after. U Whether deleting data points purges those payloads.
- V Training: "Your content is not used to train models" (privacy); "past.dev does not use customer data to train models"; model providers "process requests with zero retention" (security).
- V Hosting: "The platform runs on AWS." Region, subprocessor list and DPA "available on request". Language-model providers are not named. V SOC 2 Type II, ISO 27001, ISO 27701. Terms: California law; on closure, 30 days to export, then deletion.
- U Data region. U Names of the LLM providers that read ingested content.

## 2. Mismatches between the adapter and the docs

Paths are relative to the m004b worktree.

| # | Severity | Where | Mismatch |
|---|---|---|---|
| M1 | high | `src/agent_memory/integrations/pastdev.py:476-494`, `:513-517`; `sync.py:224-240` | After a failed send, the next run re-sends the same records with a new idempotency key (`pastdev.py:445`). past.dev answers `200 unchanged` for identical content, so the adapter records the link as `unchanged` (success) and advances the watermark. The data points stay in the failed send and are never recallable. The test suite enshrines this: `tests/test_integrations_pastdev.py:226-229` (mock) and `:436-438` (asserts `completed` or `unchanged` after the failure). Docs: "Retry is the way back". |
| M2 | high | `pastdev.py:509-512` with `sync.py:160-180` | `content-rejected` raises 422, the loop splits the batch and re-sends each item unchanged. Each single send answers `200 unchanged`, so the bad record is linked as a success and never dead-lettered. One bad record in a batch of up to 100 (`BATCH_MAX`, `:80`) also strands the other 99. Docs: change the content and send a new revision. |
| M3 | medium | `pastdev.py:83` (`POLL_S = 10.0`), `:450`, `:493` | Link metrics keep `sha256, kind, status` but not the `ingestionId`. A send that takes longer than 10 s stays `queued` for good. A later failure is invisible, and the unchanged rule (M1) stops any re-send. |
| M4 | low | `http.py:25`, `:105-111`; `pastdev.py:115` | A 429 with `project-cap-reached` or `spend-cap-reached` is retried 3 times (1, 2, 4 s) before `sync.provider_cap` (`sync.py:139-143`) stops the run. The refusals are free, but the docs say to stop. |
| M5 | low | `pastdev.py:78` | `KEY_RE` assumes `[A-Za-z0-9_-]{8,200}` after `past_sk_`. The docs give only the prefix. A real key with another character reads as "not configured". |
| M6 | low | `pastdev.py:27-29` | Docstring "not verified" items: the trimming is now documented (our ids have no outer spaces, so the echo holds; case preservation is still U). `sources[].label` "as sent at ingest" is now V. The health probe's 404 is V; the 401-before-404 order is U. The deleted-id error code is still U, and the mock's `400 data-point-deleted` (`tests/test_integrations_pastdev.py:224-225`) is an assumption. |
| M7 | low | mock `tests/test_integrations_pastdev.py:237-239`, `:257` | The mock's retry answers `200 {"status": "processing"}`; the spec says `202 {ingestionId, status: "queued"}`. The mock's delete 404 uses code `not-found`; the docs say `data-point-not-found`. The adapter reads only the status, so the behaviour is unaffected. |
| M8 | low | `pastdev.py:527-545`; `docs/integrations/cloud-readiness.md:244` | `delete_all` removes the data points, but the sanitized payloads stay in the 7-day request log (U whether a delete purges them). The rollback text should say so, or end with a project delete. |
| M9 | info | `docs/integrations/cloud-activation.md:76` | It sets only a credit cap of 2,000. The console has two monthly caps (credit and data-point). It also assumes the org has no project yet; PAYG allows one, archived included. |
| M10 | info | `pastdev.py:443` (id = `amem:...` ref, see `refs.py:53`) | Ids contain `/` and `:`. Safe, because only the body form of delete is used. Never add the path form `DELETE /data-points/{sourceId}` without percent-encoding. |
| M11 | info | `pastdev.py:50-51`, `cloud-readiness.md:281` | "estimate still uploads the content": true, but the spec adds "writes nothing ... and leaves no row in the request log". It is a safe free pre-flight for the synthetic set. |

Matches (no change needed): base URL, bearer auth, batch body and receipt, `identity` required on recall, `limit <= 20` inside 1..200, the free retry, the 1000-id all-or-nothing delete and the 404 bisection (`pastdev.py:349-381`), 402/429 cap detection, `local_credits` (`:159-161`), the `/usage` field names, no metadata sent.

## 3. Approval request: G-PD steps (renumbered)

Each step is its own approval. Nothing here enables a recurring sync or the real historical import.

| Step | Who | What | Accept | Undo |
|---|---|---|---|---|
| G-PD0 | owner approves, agent codes (no network) | Fix M1-M3 on the branch: keep each send's `ingestionId` in the link metrics; never mark a record sent until `GET /ingest/{id}` says `completed`; on a stopped send, call the free `/retry` on the next run instead of re-sending; on `content-rejected`, dead-letter that record without re-sending identical content; live runs use `batch_max=1` (same credits: billing is per data point). Fix M4 (no retry on a 429 with a body). Update the mock and the tests. | full suite green; new tests cover failed-then-rerun, content-rejected and queued-then-completed | `git revert` |
| G-PD1 | owner | In the console: confirm the org has no other project (PAYG allows one, archived included). Create project `amem-rsi`. In Settings › Projects set **monthly credit cap 250** and **monthly data-point cap 100**. If the console offers an account spend cap, set it to the minimum. Create one project key named `amem-rsi-c3`. | the project drawer shows both caps | delete the project (revokes its keys) |
| G-PD2 | agent (popup) | `request_secret` for `AMEM_PASTDEV_KEY` into the 004B worktree `.env.local` (the name the code reads, `pastdev.py:77`). Never in chat, logs or argv. Then `PastDevSink().health()` and one `GET /usage` (both free). | health ok; usage shows `creditCap 250`, `ingestionCap 100`, `balance.left` about 150,000 | remove the key line; revoke the key in the console |
| G-PD3 | agent | C3 on the synthetic set (section 4) against a disposable local Postgres only. | every V-check in 4.3 | `delete_all`, then G-PD1 undo |
| G-PD4 | agent | Benchmark (section 5) on the same set, while the data points are still live. | a report with both arms | as G-PD3 |
| G-PD5 | owner approves, later | The real historical import (about 60 records, at most 300 credits). Raise the credit cap to 600 first. | links equal records sent; `/usage` within 20% of the plan | `AMEM_PASTDEV=off`; `delete_all` (ids are retired) |

### Why 250 credits and 100 data points

Worst case for G-PD3 and G-PD4:
- 16 records x 5 credits (the 1.5 KB sanitizer cap) = 80 for the first send;
- 2 changed records = 10;
- one full re-send with fixed content after a `content-rejected` = 80;
- about 60 recalls x 0.1 = 6;
- total about 176.

The realistic total is about 30-45 credits: records of 300-600 bytes cost 1-2 credits each. 250 covers the worst case with margin. It is 0.17% of the balance (about $0.08 at the $0.30 per 1,000 credit price). The data-point cap of 100 stops a runaway loop even if the credit math is wrong. Recall is not capped by either cap, so the harness bounds it: at most 80 recall calls (8 credits).

Client-side limits for the run: `AMEM_SYNC_PASTDEV_MAX_ROWS_PER_DAY=40` and `AMEM_SYNC_PASTDEV_MAX_REQUESTS_PER_DAY=80`.

With no card on file, the balance is the hard stop. Sends answer 402 at zero, and nothing can be billed.

## 4. G-PD3: C3 on synthetic data

### 4.1 Database

- Use a disposable local Postgres (docker), migrated with `amem db migrate`, as in C1.
- The harness sets `AMEM_DB_URL` to it explicitly and checks the host before any send.
- Production Supabase is never read.

### 4.2 The synthetic set: about 19 rows, of which 16 are sent

Build it with a small generator that reuses two things from `tests/test_integrations_pastdev.py`:
- the insert SQL of `seed()` (lines 87-172);
- the marker function `leaky()` (lines 458-463).

Records are spread over six rule topics and one distractor:

| Topic | Rule | Records |
|---|---|---|
| A | `claude-md:global/shell-habits` (quote zsh globs) | proposal (promoted), decision (apply), edit, lesson (accept) |
| B | `claude-md:global/background-long-commands` | proposal, rejection ("already covered by the testing rule"), experiment (no effect) |
| C | `parent-md/dod-gate` | experiment (improves), proposal (promoted) |
| D | `claude-md:global/secrets` | edit (reverted), lesson (reject) |
| E | `claude-md:global/diagram-first` | proposal, rejection ("explanations got longer") |
| F | `agent-config/autohandoff-threshold` | experiment (inconclusive), edit |
| G | distractor: "quote the commit subject line" | lesson |

Three more rows are planted to be refused:
- a team proposal on topic A (`meta.team = true`);
- a team edit on topic B;
- an edit whose `why` holds `past_sk_ZQLEAK...` (refused as `secret:past-key`).

Each sent record:
- is 300-600 bytes, so it is not "nothing to index";
- has one free-text field with markers `zqmark<n>`: a home path, an email, a URL, a host, an IPv4 address, a foreign UUID, a hex id and an `sk-ant-api03-...` value.

The generator writes:
- `gold.json`: the queries and their relevant refs;
- `markers.json`: every planted string.

It is deterministic (fixed UUIDs), so a re-run gives the same ids. Do not reuse ids after G-PD3: deleted ids are retired.

### 4.3 Verification list (each item is a pass/fail check)

1. **Pre-flight** (free): `estimate()` of the 16 items gives `credits == local_credits(items)`, `fits = true` and `projectCapLeft = 250`. The estimate leaves no request-log row.
2. **Async ingestion completion**: `amem sync run --sink pastdev` with `batch_max=1`. Every receipt is 202. Every `ingestionId` reaches `completed` within 180 s (free polls). Project `readiness.failed = 0` and `settled = true`.
3. **Stable source-reference mapping**:
   - every `items[0].sourceId` equals the sent `amem:` id byte for byte;
   - the `rsi_evidence` links number 16 (`system = 'pastdev'`);
   - each link's subject re-reads in the disposable database.
4. **Recall relevance (smoke)**: 3 queries through `amem evidence query --source pd recall` return `ok` and only `amem:` refs that re-read. Full metrics are in section 5.
5. **Duplicate handling**:
   - a second `sync run` sends 0 requests;
   - a `--replay` gets `200 unchanged` for all 16 with a credit delta of 0 (`/usage` before and after);
   - one replayed request with the same idempotency key and body returns the original receipt;
   - changing one record sends 1 revision; the credit delta equals its `local_credits`; after `completed`, recall shows the new text's topic.
6. **Deletion and non-recreation**:
   - flip one proposal to team: the next run retracts it and its decision, and the delete answers 202;
   - within 60 s, recall no longer returns those ids;
   - flip it back: the ids are listed in `retired`, no ingest request is made, and there are 0 dead letters;
   - optional (owner opt-in): one direct ingest of a deleted id, to record the real refusal status and code; refused calls are free.
7. **Latency**: p50/p95 of the ingest call, of send-to-`completed` and of the recall call.
8. **Credits**: the `/usage` `project.credits` delta per step matches the local estimate within 10%; the total is at most 100.
9. **No sensitive text exported**:
   - (a) local: every request body the harness sent has 0 of the planted markers, 0 team ids and 0 secret-row ids;
   - (b) remote: `GET /requests` and `GET /requests/{id}` (free, 7-day payloads) show stored bodies with 0 markers;
   - (c) a recall for each marker token returns 0 results;
   - (d) `refused` lists exactly the 3 planted rows.

### 4.4 Rollback

1. `delete_all`: every live id gets 202 and the links are marked deleted.
2. `AMEM_PASTDEV=off`.
3. Delete the project. This revokes the key. Without it, the request-log payloads remain for up to 7 days.
4. Remove the key line from `.env.local` (through the popup tool's flow, never by hand).
5. Drop the disposable database.

## 5. G-PD4: benchmark, past.dev recall vs Supabase full-text

- **Same data**: the 16 sanitized records. The baseline is `pastdev.fts_baseline(conn, pastdev.corpus(conn))` (english tsvector, OR of lexemes, `ts_rank_cd`; it writes nothing). The other arm is `pastdev.recall_fn(PastDevRetriever(...), limit=10)`. Scoring uses `pastdev.evaluate(gold, retrieve, k=5)` and the same call with `k=10`.
- **Gold set**: 15 queries in `gold.json`, written by the generator from the topic table. The owner reviews the file before the run.
  - 5 lexical, for example "quote zsh globs" -> topic A.
  - 5 paraphrases with no shared words, for example "wildcard patterns break shell commands" -> A, or "context window fills up and the session moves elsewhere" -> F.
  - 5 question-style, for example "why was the background test proposal rejected" -> B's rejection and proposal, or "what was reverted" -> D's edit.
  - The topic G distractor punishes plain word matching on "quote".
- **Metrics per arm**:
  - recall@5 and recall@10;
  - MRR;
  - per-query hits;
  - latency p50/p95 (wrap `retrieve` with `perf_counter`);
  - credits (past.dev: `/usage` delta, expected 15-30 recalls x 0.1 = at most 3; FTS: 0);
  - the share of returned refs that re-read;
  - the leak count.
  - Run the past.dev arm twice to check stability.
- **Decision rule**: `pastdev.c3_winner` stays the primary rule. past.dev wins only if its recall@5 is at least the baseline + 0.10. It must also pass four guards:
  - MRR no worse than the baseline - 0.05;
  - p95 recall latency at most 3 s;
  - 0 leaks;
  - 100% of returned refs re-read.

  Otherwise the baseline wins, and past.dev stays an optional `pd` retriever only.
- **Caveat**: 15 queries are noisy (0.10 is about 1.5 queries). Report the per-query table and the paraphrase subset separately; semantic recall should matter most there.

## 6. Open questions for the owner

1. Does the org already have a project (for example one made at sign-up)? PAYG allows one, archived included. Use it, rename it (the slug never changes), or delete it first?
2. Is a card on file? Without one, the balance is the hard stop. Is there an account spend-cap setting to lower?
3. The region, LLM subprocessors and DPA are "on request". Ask past.dev first, or accept for synthetic data only?
4. Is the 7-day request-log payload retention after `delete_all` acceptable, or must the project be deleted at the end?
5. Approve G-PD0 (the M1-M3 fixes) before any send? The alternative is C3 with `batch_max=1`, stopping on any failed send.
6. Approve the optional deleted-id probe (one refused, free ingest)?
7. After G-PD4, keep `amem-rsi` (empty) for G-PD5, or delete it?
8. Are the caps OK (250 credits and 100 data points per month)?
