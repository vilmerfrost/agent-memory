# Mission 004C report: cloud connections and shared intelligence

Date: 2026-10-17. Branch: `rsi/m004c` (local, not pushed, not merged). Base: `rsi/m004b-cloud`.

Status per provider:

| Provider | State | What is verified against the real service |
|---|---|---|
| Supabase | authoritative store (unchanged) | n/a: Mission 004C used only disposable local Postgres |
| ClickHouse Cloud | G-CH1, G-CH2 done; **G-CH3 (C1) pending approval** | service, schema, least-privilege writer (11/11 probes), read path from `amem evidence ask` |
| Langfuse Cloud EU | connected, synthetic only (C2 accepted) | traces and scores read back by `amem evidence ask` |
| past.dev | **pending approval** (not connected) | nothing; code fixed against the 2026-10-17 API spec |
| NCM on Modal | **pending approval** (no account step, no run) | upstream pins (commit, HF revisions, 4 sha256) |

Nothing real left Supabase. No recurring sync, nightly investigator, promotion or production schedule was
enabled or changed. Phase I and Mission 003.5 were not touched.

## 1. ClickHouse verification

Service `amem-rsi` (id `ff3ccf61-...`), AWS eu-west-1, Scale org, 1 replica pinned at 8 GiB, idle after
5 min (verified Idle after 10 min untouched; console pages of a *running* service keep it awake), IP list
= the Mac's IPv4 only, crash reports off, org spend alert $100 (email + UI).

G-CH2 (least privilege), done 2026-10-17:
- G-CH2a: `amem` + 9 tables created from the SQL console as the org admin; Cloud made them
  `SharedReplacingMergeTree`. Each statement was hash-checked in the editor before it ran.
- G-CH2b/c: the writer password came from 1Password through the secret popup into the 004B worktree
  `.env.local`; the console only saw `IDENTIFIED WITH sha256_hash` (pasted by the owner from the clipboard,
  clipboard cleared after). `SHOW GRANTS FOR amem_writer` = `GRANT SELECT, INSERT ON amem.* TO amem_writer`.
- G-CH2d: `amem sync grants --sink clickhouse` from the Mac: 11/11 ok. The writer sees only `amem`; CREATE
  DATABASE, CREATE TABLE, ALTER, CREATE USER, GRANT, `system.users`, `system.query_log` are ACCESS_DENIED.
  15 s total from a cold (idle) service.

Synthetic query results: **not yet** (that is C1, G-CH3). The read path is verified: in the Phase D demo
the writer answered `rule_gap` and `rule_weekly_rate` with status ok and 0 rows (empty tables).

Cost so far: Trial credit $0.47 used (all ClickHouse usage since 2026-10-06, incl. the old service);
Startup credit $0 used; billable $0.00. Usage appears with a delay.

G-CH3 is prepared: `amem synth seed` (86 synthetic rows, 465 leak markers, `gold.json`) and
`scripts/c1_clickhouse.py` (sync, verify parity, named queries vs gold, re-sync idempotency, grants,
failure path, marker scan as the writer, timings; `--dry-run` touches nothing). See section 7.

## 2. past.dev verification

Not connected (pending approval). Prepared:
- Code (G-PD0, commit 5b26539 and ad8dfb8): an ingestion counts as sent only when its status is
  `completed` (queued ones are checked later by the stored `ingestionId`, never re-sent); failed ones use
  the free `/retry` (at most 3), then dead-letter; `content-rejected` is dead-lettered, not split and
  re-sent; cap 429s (`project-cap-reached`, `spend-cap-reached`) stop the run as `capped`; the replay key
  is content-based, so a replay of unchanged records ingests nothing; the mock follows the spec.
- Research and the full approval draft: `m004c/pastdev-research.md` (credits: 1 per 350 bytes per data
  point, recall 0.1, deletes/retries/polls free; deleted ids are retired; request-log payloads kept 7 days).

## 3. Shared intelligence demonstration

`scripts/demo_evidence_ask.py --docker --dotenv`, question `repeat_failures` (new): "Why do Claude or Codex
sessions repeatedly fail the same instruction, and has a similar improvement already been tested?"
**All data is synthetic** (banner on every output); the Supabase side is a throwaway local Postgres.

| Step | Provider | Status | Found |
|---|---|---|---|
| recurring deviations | Supabase (synthetic) | ok | 2 rules |
| rule_gap | ClickHouse Cloud (real, empty) | ok | 0 |
| attempts for the top rule | Supabase | ok | 4 |
| rule_weekly_rate | ClickHouse Cloud | ok | 0 |
| recall of earlier decisions | past.dev | **unavailable** (not configured) | - |
| evals for those attempts | Supabase | ok | 2 |
| stored trace links | Supabase | ok | 2 |
| experiment_scores | Langfuse Cloud (real, synthetic traces) | ok | 2 arms |
| failing trace | Langfuse Cloud | ok | 1 |

Answer (synthetic): rule `skill:synth-debugging:reproduce-before-fix` failed 10 times in 7 sessions (4 Claude,
3 Codex), followed avg 0.225; 4 earlier attempts (expired, rejected, suggested, reverted); evals `worse` and
`no-effect`; sandbox arm B failed with 2 tool errors (`lf:trace/e44c80e2...`). `--record` stored 3 rows in
the disposable Supabase: 2 Langfuse score links and one `answer:repeat_failures/<digest>` row holding the
params, per-provider status, steps and the refs it rests on. Every finding carries its source ref and
sample size; unavailable providers are named, never shown as empty success. Not real agent performance.

## 4. Historical backfill proposal

Full draft: `m004c/backfill-research.md` (scope, columns, privacy checks, SQL to count, size, idempotency,
verification, rollback, approval text). Status of its blockers:
- Fixed on `rsi/m004c`: F1 (deviation sources/rule ids now `<prefix>:team` / pseudonym), F2 (team lessons
  detected by repo/scope), F3 (`AMEM_EGRESS_DENY` + team repo/project names; email-shaped labels null),
  F4 (`--tables`, `--since` on `sync plan|run`), F5 (past.dev replay key).
- Still open: F6 (no CLI to dump/delete the past.dev corpus), F7 (team skill call without a session),
  F8 (unknown-remote sessions not flagged team; proposed `AMEM_TEAM_PATHS`), F9 (a project named `team`).
- Counts in the draft are from 2026-10-16 (ClickHouse ~3,470 rows; past.dev ~60 records; Langfuse 0).
  Before approval: refresh them with the read-only SQL in section 5 of the draft and `amem sync plan`.

Recommendation: no real backfill until F8 is fixed (work sessions with an unknown remote would otherwise be
pseudonymized, not masked) and the owner has read the label list from query 5.2.

## 5. NCM readiness

Verified 2026-10-17: upstream `HazyResearch/ncm` main = our pinned commit `c70274273f48`; HF revisions and all
4 sha256 match; the eval flags and the metrics parser match upstream; modal 1.6.2 is current. Fixed
(W3, 9 commits): the script crashed on import in the container (F1); per-run worst case is now
**$0.22** (startup 120 s + run 600 s + idle 2 s, GPU+CPU+memory at the EU 1.15x rate), expected
$0.02-0.06; failed runs still write `results.json`; exact dependency pins plus a pip-freeze hash;
run ids carry the time. Details and the approval text: `m004c/ncm-research.md`, `ncm-track.md`.

## 6. Implementation report

Commits on `rsi/m004c` since `rsi/m004b-cloud`: G-CH2 prep (4898e32), G-CH2 record (f8468f0), W3 NCM
(5b8df09..d530919), W2 synth/demo (81c3ab7, b901bdc, d81768d; merge 695820c), W1 egress/past.dev
(17e3c31..191f6b7; merge), CLI help (87baa67), this report. Earlier in the mission: Langfuse final-text
exclusion (e7eca35), G-CH1/G-LF docs.

New modules: `integrations/egress.py`, `synth.py`, `cli_synth.py`; new scripts: `c1_clickhouse.py`,
`demo_evidence_ask.py`; new CLI: `amem sync grants`, `amem sync schema --part/--hash-from-env`,
`amem synth seed`, `sync plan|run --tables/--since`; new question `repeat_failures`.

Tests: full suite on the combined branch (87baa67): 1874 passed, 1 skipped, 0 failed (319 s). Workers proved each fix fails before its commit.

Known limitations:
- C1 has never run against a real ClickHouse server; query semantics are checked by a fake and gold data.
- The console's query history keeps the CREATE USER text (hash, not password).
- `amem db migrate` reads the production Keychain admin item even for a local URL (separate bug, logged
  in the parent state file, not fixed by instruction); every local migrate here used a tripwire.
- Tests and the synthetic dataset use a work-repo-like name as a leak marker (private repo only).
- The ClickHouse retriever checks only `project` and `rule` params against the deny list.
- `evidence.record()` drops `finished_at`; `ncm.parse_results` does not validate timestamps.

## 7. Next activation steps (each needs its own approval)

1. **G-CH3, C1 on ClickHouse Cloud.** Owner makes `AMEM_PSEUDONYM_KEY` (`openssl rand -hex 32`) and pastes
   it into the popup. Agent: throwaway Postgres + `amem synth seed`, then `scripts/c1_clickhouse.py`.
   Uploads 86 synthetic rows (planted markers stay in Supabase). Accept: parity, named queries = gold,
   0 new rows on re-sync, grants 11/11, failure path `unavailable` with the cursor unchanged, 0 markers in
   ClickHouse, usage < $0.10. Undo: owner `TRUNCATE` the 9 tables from the console.
2. **past.dev G-PD1-G-PD4.** Owner: project `amem-rsi`, monthly caps 250 credits / 100 data points, one
   project key. Agent: popup `AMEM_PASTDEV_KEY`, health + `/usage`, C3 on 16 synthetic records with markers,
   then the recall benchmark vs Supabase full text. Expected 30-45 credits, worst case 176. Undo:
   `delete_all`, delete the project (also drops the 7-day request-log payloads).
3. **NCM G-NCM1-G-NCM4.** Owner: Modal Starter, workspace budget $5, spend limit; separate venv with
   `modal==1.6.2`. Then one `rsi_experiments` row (hosted DB write) and one capped run (<= $0.22).
4. **Merge `rsi/m004c` into `main` (G-0)** after review; then the K7/K4 and migrate-Keychain fixes in their
   own mission.
5. **Backfills (G-CH4, G-PD5)** only after F8, refreshed counts and the owner's read of the label list.
