# Data families

Generated from `src/agent_memory/data_families.py` by `amem coverage families --markdown`. Do not edit by hand: `tests/test_data_use_registry.py` fails when this file and the registry differ.

Every table and view in `supabase/migrations` belongs to exactly one family below (or to the exclusion list). Consumer kinds: analysis (an automatic routine draws findings from it), export (an automatic sync copies it out), report (an automatic report shows it), operational (bookkeeping), manual (a CLI command a person runs).

## Stage E gaps

Learning families with no automatic analysis consumer:

- **Models, effort, tokens and costs** (`models_costs`)
- **Command and tool timings** (`command_timings`)
- **Test runs and individual test timings** (`test_timings`)
- **Lessons and lesson-use history** (`lessons`)
- **Reports, ledger events and checkpoints** (`reports_checkpoints`)
- **Evaluation drafts** (`eval_drafts`)

## Sessions, outcomes and summaries

- Key: `sessions`
- Purpose: Find recurring problems and assess task results
- Producers: sessions_ingest.ingest (amem sessions ingest, inside amem daily step_deviations); session_live (amem sessions ping, Claude Code hook); session_summary (redacted 1-3 sentence summaries); tools/amem_digest.py (cloud routine/chat POST to cloud_digests)
- Owner job: amem-daily (amem daily)
- Tables: `sessions`; `cloud_digests`
- Views: `session_groups`; `dashboard_sessions`
- Local files: ~/.claude/projects/**/*.jsonl (Claude transcripts, read in place); ~/.codex/sessions/**/rollout-*.jsonl (Codex transcripts, read in place); ~/.claude/parent/amem-session-spool.jsonl (ping spool); amem archive (encrypted transcript archive, docs/archive.md)
- External sinks: ClickHouse sessions (pseudonymized columns)
- Consumers:
  - analysis: deviations scan (`src/agent_memory/deviations.py`)
  - analysis: amem-rsi-night (amem rsi night-job) (`src/agent_memory/rsi_evidence_pack.py`)
  - analysis: amem-rsi-outcome (amem rsi outcome) (`src/agent_memory/rsi_outcome.py`)
  - analysis: evals harvest (agent-config nightly caller, outside this repo) (`src/agent_memory/evals_harvest.py`)
  - report: amem-rsi-brief (amem rsi brief-job) (`src/agent_memory/rsi_brief.py`)
  - export: sync clickhouse (amem daily step_sync) (`src/agent_memory/integrations/clickhouse.py`)
  - report: Night Desk (external web app, owner JWT; dashboard_owner_read policy) (`supabase/migrations/20261024230827_night_desk.sql`)
  - manual: amem sessions find / tokens report (`src/agent_memory/session_find.py`)
- Retention: Rows kept indefinitely (no deletion job in this repo). Transcripts stay on disk; `amem archive prune` ages out local archive copies.
- Exclusion: No transcript text, secrets or raw private content: only redacted, capped fields leave the producer. Sessions with env sandbox/eval are kept out of production evaluations (sessions.env). Night Desk sees only granted columns (no paths, meta or summaries).
- Learning input: yes

## Models, effort, tokens and costs

- Key: `models_costs`
- Purpose: Compare cost and outcome for comparable work
- Producers: sessions_ingest / session_live (tokens_*, models, model, effort, efforts columns); rsi_budget (night cost ledger in the night report)
- Owner job: amem-daily (amem daily)
- Tables: none
- Views: none
- Columns elsewhere: `sessions.tokens_input`; `sessions.tokens_output`; `sessions.tokens_cache_read`; `sessions.tokens_cache_creation`; `sessions.models`; `sessions.model`; `sessions.effort`; `sessions.efforts`; `rsi_nights.costs`
- Local files: config/rsi-prices.json (versioned price table)
- External sinks: ClickHouse sessions token columns
- Consumers:
  - export: sync clickhouse (amem daily step_sync) (`src/agent_memory/integrations/clickhouse.py`)
  - operational: night budget (`src/agent_memory/rsi_budget.py`)
  - report: amem-rsi-brief (amem rsi brief-job) (`src/agent_memory/rsi_brief.py`)
  - manual: amem sessions tokens (`src/agent_memory/tokens_report.py`)
  - analysis: none found (Stage E gap)
- Retention: Same as sessions (columns of that table).
- Exclusion: Measured cost, estimated cost and subscription tokens are kept apart; token counts alone never justify a cheaper model.
- Learning input: yes

## Command and tool timings

- Key: `command_timings`
- Purpose: Find bottlenecks and repeated failed operations
- Producers: timing collector (deploy/hooks/amem-timing-hook.py -> timing_collector); timing_sync (transcript reconstruction); timing_store.ingest_spools (amem timing ingest)
- Owner job: timing collector (amem timing collect, local service)
- Tables: `execution_spans`
- Views: `session_timing_summary`; `session_parent_timing`; `session_timing_coverage`
- Local files: ~/.claude/parent/**/timing-events.jsonl (spool); timing cursor file next to the spool
- External sinks: none
- Consumers:
  - operational: profile refresh (sessions.api_seconds / tool_seconds) (`src/agent_memory/timing_store.py`)
  - manual: amem timing report / coverage (`src/agent_memory/cli_timing.py`)
  - analysis: none found (Stage E gap)
- Retention: No pruning found: spans are kept indefinitely.
- Exclusion: Operation names and categories only; no command text or output. Reconstructed and estimated spans (measurement_kind) never count as measured latency.
- Learning input: yes

## Test runs and individual test timings

- Key: `test_timings`
- Purpose: Detect slow tests, regressions and worker imbalance
- Producers: scripts/integration_tests.py + test_timings.Capture (pytest plugin); test_timings.upload / flush_queue (via the timing collector)
- Owner job: timing collector under parent-run
- Tables: `test_timing_runs`; `test_timing_cases`
- Views: none
- Local files: <git dir>/test-logs/*.timings.json (batches beside each log); timing upload queue run-*.json
- External sinks: none
- Consumers:
  - operational: retention prune (`src/agent_memory/timing_collector.py`)
  - manual: amem timing tests (`src/agent_memory/test_timings.py`)
  - analysis: none found (Stage E gap)
- Retention: Case rows 30 days (test_timings.prune, run by the collector); run summaries kept (details_retained=false after pruning).
- Exclusion: Measurements and node ids only, never test logs or captured output.
- Learning input: yes

## Skill calls and scores

- Key: `skills`
- Purpose: Identify weak or ineffective skills
- Producers: amem ingest skills (~/.agents/skill-usage/log.jsonl; records rsi_checkpoints skills-ingest/latest with the log read time); amem skills scores --refresh (records rsi_checkpoints skills-refresh/<run> with its input window)
- Owner job: rsi-nightly skill refresh (agent-config routine, outside this repo)
- Tables: `skill_calls`; `skill_scores`
- Views: none
- Local files: ~/.agents/skill-usage/log.jsonl
- External sinks: ClickHouse skill_calls
- Consumers:
  - analysis: amem-rsi-night (amem rsi night-job) discovery (`src/agent_memory/rsi_night.py`)
  - analysis: amem-rsi-night (amem rsi night-job) skill_refresh prerequisite (`src/agent_memory/skill_refresh.py`)
  - export: sync clickhouse (amem daily step_sync) (`src/agent_memory/integrations/clickhouse.py`)
  - manual: amem skills scores / suggest (`src/agent_memory/skills.py`)
- Retention: skill_calls kept indefinitely; skill_scores rewritten on every refresh.
- Exclusion: Skill name, project bucket and cwd only; no prompts.
- Learning input: yes

## Instruction sources and deviations

- Key: `deviations`
- Purpose: Assess adherence against the correct instructions
- Producers: amem deviations scan (amem daily step_deviations); amem deviations reref
- Owner job: amem-daily (amem daily)
- Tables: `deviations`
- Views: none
- Columns elsewhere: `sessions.instructions`; `sessions.skills`
- Local files: instruction files read by rule_catalog (CLAUDE.md, AGENTS.md, skills) at scan time; ~/.claude/parent/amem-daily-state.json (pushed ids)
- External sinks: ClickHouse deviations; past.dev (through proposals/edits)
- Consumers:
  - analysis: amem-rsi-night (amem rsi night-job) (`src/agent_memory/rsi_evidence_pack.py`)
  - analysis: amem-rsi-outcome (amem rsi outcome) (`src/agent_memory/rsi_outcome.py`)
  - analysis: evals harvest (agent-config nightly caller, outside this repo) (`src/agent_memory/evals_harvest.py`)
  - report: amem daily alerts / escalations (`src/agent_memory/alerts.py`)
  - report: amem-rsi-brief (amem rsi brief-job) (`src/agent_memory/rsi_brief.py`)
  - export: sync clickhouse (amem daily step_sync) (`src/agent_memory/integrations/clickhouse.py`)
  - report: Night Desk (external web app, owner JWT; dashboard_owner_read policy) (`supabase/migrations/20261024230827_night_desk.sql`)
- Retention: Kept indefinitely. instruction_sha is the scan-time hash; it is not proof of the historical version (plan section 5).
- Exclusion: The writer's own words only: no transcript quotes, paths, code or secrets.
- Learning input: yes

## Routine runs, scores and labels

- Key: `routine_runs`
- Purpose: Detect recurring failures and weak evaluations
- Producers: amem log-run / store.insert_run (every routine); amem ingest routines-log (amem daily step_ingest); amem ingest tasks (dispatch runs, amem daily step_tasks); amem score (amem daily step_score); amem label (manual)
- Owner job: amem-daily (amem daily)
- Tables: `runs`; `scores`; `labels`
- Views: none
- Local files: ~/.claude/parent/routines.log; cloud routines.log on the routine-reports branch
- External sinks: ClickHouse runs, scores
- Consumers:
  - analysis: amem daily escalations (`src/agent_memory/scoring.py`)
  - analysis: amem lessons distill (amem daily step_distill) (`src/agent_memory/distill.py`)
  - analysis: amem-rsi-night (amem rsi night-job) (`src/agent_memory/rsi_evidence_pack.py`)
  - export: sync clickhouse (amem daily step_sync) (`src/agent_memory/integrations/clickhouse.py`)
  - report: Night Desk (external web app, owner JWT; dashboard_owner_read policy) (`supabase/migrations/20261024230827_night_desk.sql`)
  - manual: amem ab report (`src/agent_memory/ab.py`)
- Retention: Kept indefinitely (dedupe merges duplicates, amem db dedupe).
- Exclusion: Summaries and failure reasons are one line each; report_path points at a local file.
- Learning input: yes

## Tasks, handoffs and worker incidents

- Key: `coordination`
- Purpose: Find coordination failures and missing checks
- Producers: amem handoffs log / resume (Stop hook and handoff skill); amem ingest tasks (dispatch tasks land in runs, source dispatch); sessions ingest (worker report_blocked / report_partial counts in outcome_evidence)
- Owner job: amem-daily (amem daily)
- Tables: `handoffs`
- Views: none
- Columns elsewhere: runs (source = dispatch); `sessions.outcome_evidence.report_blocked/report_partial`
- Local files: ~/.claude/parent/<repo>/tasks.jsonl; handoff files (handoffs.handoff_path); ~/.claude/parent/<repo>/checkpoints/*.md
- External sinks: none
- Consumers:
  - analysis: evals harvest (agent-config nightly caller, outside this repo) (`src/agent_memory/evals_harvest.py`)
  - analysis: amem lessons distill (dispatch runs) (`src/agent_memory/distill.py`)
  - manual: amem handoffs list (`src/agent_memory/handoffs.py`)
- Retention: Kept indefinitely.
- Exclusion: Handoff and checkpoint text stays in local files; only paths, reasons and counts are stored.
- Learning input: yes

## Lessons and lesson-use history

- Key: `lessons`
- Purpose: Preserve useful conclusions and assess their value
- Producers: amem lessons import / distill; amem lessons for (logs lesson_uses)
- Owner job: amem-daily (amem daily)
- Tables: `lessons`; `lesson_uses`; `lesson_candidates`
- Views: none
- Local files: ~/.claude/parent/lessons.md (round trip)
- External sinks: past.dev lesson_candidates
- Consumers:
  - manual: amem lessons for (retrieval into briefs) (`src/agent_memory/lessons.py`)
  - export: sync pastdev (amem daily step_sync) (`src/agent_memory/integrations/pastdev.py`)
  - manual: amem ab report (`src/agent_memory/ab.py`)
  - analysis: none found (Stage E gap)
- Retention: Kept indefinitely; retired lessons keep their row (status retired).
- Exclusion: Lesson text is written by the distill template or a person; no transcript text.
- Learning input: yes

## Experiments, evaluations and outcomes

- Key: `experiments`
- Purpose: Compare attempts and avoid repeating failed approaches
- Producers: amem rsi night (experiments, candidates, artifacts, eval results); rsi_replay sandbox runs; amem rsi night experiment stage (rsi_attempts at creation); amem coverage repair-links (historical rsi_attempts, explicit identifiers only, undo manifest); amem rsi outcome (rsi_outcomes)
- Owner job: amem-rsi-night (amem rsi night-job)
- Tables: `rsi_experiments`; `rsi_candidates`; `rsi_artifacts`; `rsi_eval_results`; `rsi_attempts`; `rsi_outcomes`
- Views: none
- Local files: <RSI sandbox>/experiments/<id>/logs/*.json (sandbox run logs and .rsi.json sidecars); <RSI sandbox>/replays/<id>[/<profile>]/runs/*.json (replay run records); <RSI sandbox>/nights/<night>/report.json; bench/rsi-replay cases
- External sinks: Langfuse traces and scores (sandbox runs); past.dev experiments; ClickHouse rsi_eval_results
- Consumers:
  - analysis: amem-rsi-night (amem rsi night-job) discovery (`src/agent_memory/rsi_night.py`)
  - analysis: amem-rsi-night (amem rsi night-job) evidence pack (`src/agent_memory/rsi_evidence_pack.py`)
  - report: amem-rsi-brief (amem rsi brief-job) (`src/agent_memory/rsi_brief.py`)
  - export: sync langfuse (amem daily step_sync) (`src/agent_memory/integrations/langfuse.py`)
  - export: sync pastdev (amem daily step_sync) (`src/agent_memory/integrations/pastdev.py`)
  - export: sync clickhouse (amem daily step_sync) (`src/agent_memory/integrations/clickhouse.py`)
  - report: Night Desk (external web app, owner JWT; dashboard_owner_read policy) (`supabase/migrations/20261024230827_night_desk.sql`)
  - manual: amem coverage links / repair-links (attempt links) (`src/agent_memory/attempt_links.py`)
- Retention: Kept indefinitely. Artifacts and eval results are append-only (rsi_immutable trigger). Candidates are never deleted, only retired. rsi_attempts: runner rows are never deleted; a repair's rows are removed only by its own undo or when a later repair resolves them.
- Exclusion: Sandbox sessions only; production transcripts never enter an experiment. rsi_attempts holds ids, sandbox-relative paths and refs, never run output. Eval-harness sessions are not attempts.
- Learning input: yes

## Proposals, decisions, edits and undo history

- Key: `decisions`
- Purpose: Understand what was accepted, rejected or reversed
- Producers: amem rsi night / edits writer (proposals, staged edits); amem-decide edge function and amem rsi decide (decisions); amem rsi decide-job (decision results, undo); amem edits undo / undo-poll
- Owner job: amem-rsi-decide (amem rsi decide-job)
- Tables: `rsi_proposals`; `rsi_decisions`; `rsi_decision_results`; `edits`; `undo_requests`
- Views: `rsi_decision_status`; `edit_undo_status`
- Local files: none
- External sinks: past.dev proposals, decisions, edits; ClickHouse rsi_proposals, rsi_decisions
- Consumers:
  - analysis: amem-rsi-night (amem rsi night-job) evidence pack (history) (`src/agent_memory/rsi_evidence_pack.py`)
  - analysis: amem-rsi-outcome (amem rsi outcome) (`src/agent_memory/rsi_outcome.py`)
  - operational: proposal supersede (`src/agent_memory/rsi_supersede.py`)
  - export: sync pastdev (amem daily step_sync) (`src/agent_memory/integrations/pastdev.py`)
  - export: sync clickhouse (amem daily step_sync) (`src/agent_memory/integrations/clickhouse.py`)
  - report: Night Desk (external web app, owner JWT; dashboard_owner_read policy) (`supabase/migrations/20261025213700_rsi_decision_status.sql`)
- Retention: Kept indefinitely. Decisions are append-only; proposals expire by status (72 h), never by deletion.
- Exclusion: Undo token hashes stay owner-only and never reach the browser or a learning input.
- Learning input: yes

## Reports, ledger events and checkpoints

- Key: `reports_checkpoints`
- Purpose: Establish progress, freshness and recovery state
- Producers: amem rsi night / brief (rsi_checkpoints, nights); amem sync run (sync:<sink> cursors); dashboard ingest (rsi_nights, report_snapshots, ledger_events)
- Owner job: amem-rsi-brief (amem rsi brief-job)
- Tables: `rsi_checkpoints`; `rsi_nights`; `report_snapshots`; `ledger_events`
- Views: none
- Local files: ~/.claude/parent/reports/*; <RSI sandbox>/nights/<night>/report.json
- External sinks: none
- Consumers:
  - operational: night/brief dependency checks (`src/agent_memory/rsi_night.py`)
  - operational: sync cursors (`src/agent_memory/integrations/sync.py`)
  - report: Night Desk (external web app, owner JWT; dashboard_owner_read policy) (`supabase/migrations/20261024230827_night_desk.sql`)
  - analysis: none found (Stage E gap)
- Retention: Checkpoints and nights kept indefinitely; report_snapshots one row per kind and date.
- Exclusion: Summaries only; report bodies stay in local files.
- Learning input: yes

## Night Desk read status

- Key: `night_read_state`
- Purpose: Sync the owner's unread report inbox across devices
- Producers: Night Desk explicit Mark as read / Mark unread actions (owner session)
- Owner job: Night Desk owner
- Tables: `night_reads`
- Views: none
- Local files: none
- External sinks: none
- Consumers:
  - report: Night Desk inbox (external web app, owner JWT) (`supabase/migrations/20261026160937_night_desk_reads.sql`)
- Retention: Kept until marked unread or the referenced night is removed.
- Exclusion: Never a learning input. Reading does not resolve failures or approve changes; automation has no access.
- Learning input: no

## Coverage snapshots and routine consumption

- Key: `coverage`
- Purpose: Record what each routine read, used and skipped, and what evidence was missing
- Producers: amem-daily: deviations scan, score, sync (coverage_daily.py); rsi-skills: amem skills scores --refresh (coverage_daily.py); rsi-harvest: amem evals harvest (coverage_daily.py); amem-rsi-night: S1 delta and S4 evidence pack (night_coverage.py)
- Owner job: each consuming routine, through coverage.record_best_effort (R4)
- Tables: `coverage_snapshots`; `routine_consumption`
- Views: none
- Local files: none
- External sinks: none
- Consumers:
  - report: amem-rsi-brief coverage verdict (`src/agent_memory/rsi_brief.py`)
  - manual: amem coverage (contract checks) (`src/agent_memory/cli_coverage.py`)
- Retention: One row per dataset and window, and per job run and dataset (natural keys, upserts). No per-row audit trail.
- Exclusion: Counts, checkpoints and at most 20 evidence refs per row; never row contents. Reporting input (Stage F), not a learning input.
- Learning input: no

## External evidence links

- Key: `evidence_links`
- Purpose: Confirm identity, relevance and current availability
- Producers: amem sync run (evidence links per pushed batch); evidence providers (rsi_evidence_providers); amem integrations ncm record
- Owner job: amem-rsi-night (amem rsi night-job)
- Tables: `rsi_evidence`
- Views: none
- Local files: none
- External sinks: ClickHouse; Langfuse; past.dev; NCM
- Consumers:
  - analysis: amem-rsi-night (amem rsi night-job) evidence pack (`src/agent_memory/rsi_evidence_pack.py`)
  - operational: evidence.resolve (re-read amem: subject refs) (`src/agent_memory/integrations/evidence.py`)
  - export: sync clickhouse (amem daily step_sync) (research_runs) (`src/agent_memory/integrations/clickhouse.py`)
- Retention: Kept indefinitely; valid_until marks staleness, not deletion.
- Exclusion: Summaries sanitized and capped at 2000 chars; external results support a finding only after their amem: refs resolve in Supabase.
- Learning input: yes

## Authentication and recovery records

- Key: `auth_recovery`
- Purpose: Support access control and operational health
- Producers: amem rsi push (decision tokens); amem-decide edge function (TOTP, passkeys, challenges); amem rsi totp-enroll / passkey enroll
- Owner job: amem-rsi-decide (amem rsi decide-job)
- Tables: `rsi_decision_tokens`; `rsi_totp_used`; `rsi_totp_failures`; `rsi_passkeys`; `rsi_passkey_enroll_codes`; `rsi_web_challenges`; `amem_private.dashboard_owner`
- Views: none
- Local files: none
- External sinks: none
- Consumers:
  - operational: decision functions (token, TOTP, passkey checks) (`src/agent_memory/rsi_approval.py`)
  - operational: amem-decide edge function (`supabase/functions/amem-decide`)
- Retention: Tokens and challenges expire by time; TOTP steps and failures are kept for replay and lockout checks.
- Exclusion: Never a learning input. Owner-only: amem_app reads only the status columns of rsi_decision_tokens (never token_hash) and nothing else here. A routine may expose a bounded count (e.g. failed codes), never the records.
- Learning input: no

## Schema and configuration assets

- Key: `config_assets`
- Purpose: Verify that jobs use compatible contracts
- Producers: amem db migrate (schema_migrations); amem cloud publish-script (amem_assets)
- Owner job: amem db migrate (by hand after a merge)
- Tables: `schema_migrations`; `amem_assets`
- Views: none
- Local files: supabase/migrations/*.sql; config/*.json (night, prices, verdict policy, evidence providers); ~/agent-config/improve/criteria.json, pins.json (read only)
- External sinks: none
- Consumers:
  - operational: migration runner (`src/agent_memory/store.py`)
  - operational: cloud setup script (`deploy/cloud/setup.sh`)
  - operational: night config load (`src/agent_memory/rsi_night.py`)
  - operational: data-family registry check (`src/agent_memory/data_families.py`)
- Retention: Kept for the life of the schema.
- Exclusion: Configuration, not behaviour data: never a learning input by itself.
- Learning input: no

## Evaluation drafts

- Key: `eval_drafts`
- Purpose: Turn learned failures into regression cases
- Producers: amem evals harvest (draft case files and the harvest ledger)
- Owner job: evals harvest (agent-config nightly caller, outside this repo)
- Tables: none
- Views: none
- Local files: <drafts dir>/*.md (agent-config evals case format); <drafts dir>/.harvest-ledger.jsonl
- External sinks: none
- Consumers:
  - manual: agent-config promotion step (outside this repo) (`docs/evals-harvest.md`)
  - analysis: none found (Stage E gap)
- Retention: Files stay until promoted or deleted by hand; the harvest cap bounds new drafts per run.
- Exclusion: Drafts are redacted; their instructions are data, not tasks (plan section 10).
- Learning input: yes

## Excluded tables

- none: every table has a family
