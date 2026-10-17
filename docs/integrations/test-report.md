# Mission 004A: integration test report

Date: 2026-10-16. Branch: `rsi/m004a-integrations` (agent-memory), based on `main` `8594405`.
**Everything below ran locally**: synthetic rows in a throwaway Postgres (Docker, all migrations applied),
local HTTP stand-ins for ClickHouse, Langfuse and past.dev (`tests/integrations_mock.py`), no production
credentials, no external call. Sync and retrieval run as `amem_app`, the routines' least-privilege role.

## Result

**Full agent-memory suite on the merged branch: 1658 passed, 0 failed (280 s)**, with the cross-repo test
(`RSI_TEST_IMPROVE_REPO=~/agent-config`). That is the 1453 existing tests plus 205 new integration tests.
The three adapters were built in parallel on their own branches from the shared core and merged without
conflicts; each adapter's tests also passed on its own branch.

| Area | File | Tests | What they prove |
|---|---|---|---|
| Shared core | `test_integrations_core.py` | 32 | ref grammar round-trips and rejects; deterministic trace ids and uuid5 session ids; the facade asks Supabase first, a down source answers `unavailable`, an error reports its type only, params are checked before any call; `exists`/`usable` re-read `amem:` refs; `record` is idempotent; the sink loop advances the cursor and links together, holds on `unavailable`/`off`, backs off 1/5/30/120 min, goes dead after 6, stops on the time budget; the HTTP client retries and never shows headers |
| ClickHouse | `test_integrations_clickhouse.py` | 12 | ReplacingMergeTree + dedup window; **no text ever leaves** (markers planted in every text column); the same batch twice is stored once; incremental watermark; the 7-day lookback re-sends silent updates; replay backfill gives `parity=True`, a missing row gives `False`; named queries send `param_` values and refuse bad params before any request; unavailable when unconfigured or down; the key never shows |
| Langfuse | `test_integrations_langfuse.py` | 23 | sandbox-only guard (prod or missing experiment id refused); deterministic ids, a re-upload adds nothing; **leak test**: no prompt, tool input, path, file content, stderr or `sk-test-` secret in any request; no agent final response by default (opt-in still redacted and capped at 500); paths, credentials, emails and token-like runs never leave through model, tool, status, usage or baseline fields, and a run whose experiment id, case or run id carries one is refused (added 2026-10-17); Claude JSON and Codex JSONL parsers; round-trip `trace` query returns `amem_session_id`; the sink records `lf:trace` evidence as `amem_app`, idempotent, replayable; unavailable when unconfigured, down or with wrong keys |
| past.dev | `test_integrations_pastdev.py` | 35 | **leak test across the whole sink path**: only sanitized text leaves; payload shape and batch idempotency key; 202 + poll and `unchanged`; recall maps back to `amem:` refs and drops refs that no longer exist; kill switch for sink and retriever; delete in chunks (split on 404); `evaluate()` math; Postgres full-text baseline; unavailable when unconfigured or down |
| Sanitizer | `test_integrations_sanitize.py` | 76 | paths, uuids, hex ids, emails, URLs, hosts, IPs and long tokens are scrubbed (the record's own ref kept); secret shapes refuse the record; team items refused; 1,536-byte cap without half placeholders; deterministic. Removing the team check, the email or uuid scrubber, or the redact step makes the leak test fail |
| NCM | `test_integrations_ncm.py` | 20 | strict `results.json`: task-specific metrics only, not-run needs a reason and no metrics, an ok run records commit, checkpoint hashes and commands; one `ncm:run` evidence row, idempotent, as `amem_app` |
| CLI | `test_cli_integrations.py` | 7 | before activation every sink is `unavailable` and moves no cursor; the kill switch wins; `evidence query` checks params; `sync verify` fails closed without ClickHouse |
| **Total** | 7 files | **205** | |

## Verified against vendor docs vs only mocked

| System | Checked in current docs (URLs in each module docstring) | Only mocked: check on a real server first |
|---|---|---|
| ClickHouse | HTTP INSERT, `X-ClickHouse-User`/`Key`, `database`, `param_<name>`, `wait_end_of_query`, `/ping`; `insert_deduplication_token` needs `non_replicated_deduplication_window > 0`; ReplacingMergeTree + FINAL | **all SQL** (no server or image on this Mac); riskiest: the streak expressions in `routine_streaks` and the JOIN in `rule_weekly_rate` |
| Langfuse v4 | OTLP/HTTP `POST /api/public/otel/v1/traces` is the supported trace path; the legacy `/api/public/ingestion` is deprecated (v4 `events_only` rejects all but `score-create`); `GET /api/public/traces/{id}` is 404 on v4, so reads use Observations v2 and Scores v3; datasets via v2; `dataset-run-items` must not be used on v4; re-exported spans are not reliably deduped | the OTLP attribute mapping, the score upsert key, the dataset responses, the Observations v2 / Scores v3 fields |
| past.dev | base URL, `Bearer past_sk_`, batch ingest + receipt, status poll, recall (identity required, limit ≤ 200), delete (`ids`, all or nothing), 409 on key reuse, 500 req/min. **Differs from the blueprint:** the idempotency key is per batch, not per item | that `sourceId` equals our `id`; that `GET /ingest/<nil uuid>` answers 404 for a valid key |
| NCM | repo facts (MIT, evaluation only, CUDA, tasks and metrics) | no run (by design) |

## Design choices worth knowing

- **No new tables.** Cursors are `rsi_checkpoints` rows (job `sync:<sink>`); external links are `rsi_evidence`
  rows (unique `(system, external_ref)`). The blueprint's trigger outbox stays a later upgrade.
- **ClickHouse watermarks** use `(changed, id)`, where `changed` is the newest timestamp the row has, plus a
  7-day lookback re-send; ReplacingMergeTree keeps the newest version. No per-row evidence links: `ch:<table>/<id>`
  maps 1:1 to `amem:<entity>/<id>`.
- **Langfuse** checks whether a trace exists and sends its spans once (v4 does not reliably dedupe re-exported
  spans); scores upsert. An optional sidecar `<run_id>.rsi.json` (case, rep, scores, proposal) is needed for
  real case and rep values: the sandbox runner does not record them yet.
- **past.dev** batches of ≤ 100; a record that becomes a team item or is deleted in Supabase is removed on the
  next sync; `delete_all()` works even when switched off.

## Not done (by the mission's rules)

No credential, no image pull, no external call, no routine change, no production data. The integration
experiments C1 (ClickHouse parity and finding), C2 (a real traced sandbox run), C3 (past.dev vs full-text
recall on a hand-labelled gold set) and C4 (an NCM run) need the activation steps in
[activation.md](activation.md).
