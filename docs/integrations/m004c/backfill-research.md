> Research draft, 2026-10-17, written before the Mission 004C fixes landed. The status of every finding listed here is in ../m004c-report.md, which wins where they differ.

# Mission 004C, Phase E: historical backfill preparation (draft)

Status 2026-10-17: research only. No database was queried, no provider was called, no repo file changed.
Code read: `~/worktrees/agent-memory/m004b`, branch `rsi/m004b-cloud`, commit `4898e32`.
Reference counts: `docs/integrations/cloud-readiness.md` section D (aggregate counts, 2026-10-16).

## 0. Summary

| Provider | Records (2026-10-16 ref) | Upload | Cost | Ready? |
|---|---|---|---|---|
| ClickHouse Cloud | ~3,470 rows, 7 tables with data | ~1.7 MB JSON, ~11 POSTs | ~$0.10-0.25 compute, from Trial credits | **No: fix F1 first** (work-repo name leaks via `deviations.instruction_source` / `rule_id`) |
| Langfuse Cloud EU | **0** (`~/sandbox/experiments` is empty) | 0 | 0 units | Nothing to backfill. G-LF4 stays per-run. |
| past.dev | ~60 records (1 + 1 + 0 + 4 + 54), minus refusals | ~20-40 KB (max 92 KB) | ~60-120 credits (hard max 300) | **No: fix F2 first** (lesson records from work-repo dispatches are never flagged team) |

Recommended order: fix F1-F3 on a dev branch, re-run the read-only checks (section 5), owner reviews the
`plan` output and the full past.dev corpus, then one approval per provider (section 9).

---

## 1. ClickHouse Cloud (G-CH4)

### 1.1 Scope and filter

Command: `amem sync run --sink clickhouse --replay --budget 600` (once). `--replay` = no watermark, so
**every row of every allowlisted table, from the first row to the run time. There is no time filter and no
env/kind filter** (sandbox, eval, cloud and team sessions are all included; team rows are masked). The only
row filter in code: `research_runs` reads `rsi_evidence WHERE system = 'ncm'` (clickhouse.py:312). A row
whose key is not a valid ref id is dropped by `map_row` (clickhouse.py:469-475).

There is no CLI option to narrow the scope (`--tables`, `--since`); `ClickHouseSink(tables=...)` exists
(clickhouse.py:526) but `sink_for` does not expose it (cli_integrations.py:38-47). See F4.

| CH table | Source | Rows 2026-10-16 |
|---|---|---|
| sessions | sessions | 1,340 (1,196 prod, 229 team) |
| runs | runs | 448 |
| deviations | deviations | 237 |
| scores | scores | 678 |
| skill_calls | skill_calls | 765 |
| rsi_proposals | rsi_proposals | 1 |
| rsi_decisions | rsi_decisions | 1 |
| research_runs | rsi_evidence (system ncm) | 0 |
| rsi_eval_results | rsi_eval_results | 0 |

### 1.2 Columns (allowlist, clickhouse.py:276-317). Nothing else is even SELECTed.

| Table | Shipped | Transform |
|---|---|---|
| sessions | id (session_id), experiment_id; kind, tool, env, category, model, effort, outcome; created/started/ended/last_activity/outcome_at; transcript_bytes, turns, 4 token counts, duration/active/api/tool seconds, lines added/removed | project, repo: `'team'` when `meta.team = 'true'` (SQL, clickhouse.py:251), else HMAC-SHA256 pseudonym `p_<16 hex>` with AMEM_PSEUDONYM_KEY (key < 32 chars: null) |
| runs | id; routine, source, status; started/finished/created; duration_s (computed) | labels via LABEL_RE |
| deviations | id, session_id, instruction_sha; instruction_source, rule_id, result_outcome, jev_verdict, jev_mode, writer_model; turn; followed/result grade, objective_result, jev_confidence; created_at | labels via LABEL_RE only: **no team masking** (gap F1) |
| scores | id, run_id; question, kind, jev_mode; value_num, confidence; created_at | — |
| skill_calls | id, session_id; skill, invoked_by, agent; ts, created_at; backfill | project: `'team'` when the matched session is team (clickhouse.py:252-253), else pseudonym. Unmatched calls (session_id null) get a pseudonym, never `'team'` (F7) |
| rsi_proposals | id, artifact_hash, experiment_id, candidate_id; status; confidence; 5 timestamps | — |
| rsi_decisions | id, proposal_id, artifact_hash; action, source, verified; decided_at, expires_at | — |
| research_runs | id; run_id, task, status, gpu, provider, repo_commit, checkpoint/dataset revision; 15 metric floats; observed_at | from `rsi_evidence.metrics` keys only, never `summary` |
| rsi_eval_results | id, artifact_hash, experiment_id, baseline_id, record_sha; suite, suite_version, model, effort, verdict; created_at | — |

Excluded text-bearing or identifying columns (never selected):
- sessions: `title`, `summary`, `project_dir` (encoded home path), `skills`, `instructions`, `meta` (team,
  remote_owner, routine, agent_type, cwd-like keys), `models`, `efforts`, `outcome_evidence` (commits: sha,
  message, repo, **branch**), `model_last`, `effort_last`, `scanned_at`, `title_updated_at`, `summary_updated_at`,
  `tokens_updated_at`.
- runs: `summary`, `failure_reason`, `report_path` (path), `meta` (dispatch: repo, workers, checkpoint
  excerpts), `source_key`.
- deviations: `what`, `why`, `how` (writer prose), `instruction_ref`, `rule_title` (headings as written),
  `source_key` (contains session id + hash), `jev_answers`, `meta`.
- scores: `value` (text), `probabilities`. labels table: not synced (`note`).
- skill_calls: `cwd` (path), `source_key`, `logged_session`.
- rsi_proposals: `title`, `summary`, `rationale`, `evidence_refs`, `risk`, `expected`, `targets` (paths),
  `change_id`, `result`, `meta`. rsi_decisions: `reason`. rsi_evidence: `summary`, `subject_refs`.
  rsi_eval_results: `record` (jsonb), `baseline_id` is an id.

### 1.3 Classification and privacy checks

Class: internal pseudonymous telemetry (ids, timestamps, counts, fixed-vocabulary labels, model names,
rule keys, keyed pseudonyms). Includes activity metadata of work-repo sessions (times, tokens, outcome),
masked to `team`. The owner should confirm this is acceptable (option: exclude team rows entirely).

What `LABEL_RE` (clickhouse.py:106, `[A-Za-z0-9][A-Za-z0-9._:/@+=\[\]-]{0,127}`) does NOT stop, checked by
running `_value` locally on synthetic strings:

| Value | label | id |
|---|---|---|
| `claude-md:pipeline-poc` | passes | passes |
| `agents-md:pipeline-poc` | passes | passes |
| `deploy/pipeline-poc-staging` (a rule_id slug) | passes | passes |
| `first.last@employer.com` | passes | passes |
| `Users/vilmerfrost/StupidHuman/pipeline-poc` | passes | passes |
| `feature/PIPE-123-azure` | passes | passes |

LABEL_RE only stops spaces and long strings. Concrete leak paths:
1. **deviations.instruction_source = `claude-md:<repo>` / `agents-md:<repo>`** (deviations.py:211-214).
   For a work-repo session this is the work repo's folder name, shipped in clear. Also contradicts the
   design rule "project and repo names are not sent as such" for own repos.
2. **deviations.rule_id** = slug path of headings in that repo's CLAUDE.md / AGENTS.md (rule_catalog.slug).
   A work repo's headings can name the employer, systems or projects.
3. skill_calls.skill: a repo-local skill name from a work repo (low; check distinct values).
4. runs.routine, scores.question, skill_calls.agent, sessions.model/effort: owner-controlled vocab; verify
   distinct values once (section 5.2).
5. Team flag coverage: `team_info` returns `{}` (not team) when no remote and no profile is found
   (sessions_ingest.py:233-246); `spread_team` only spreads within one ingest (252-259). Unflagged work
   sessions get a pseudonym (safe), but their deviations still leak per 1-2.

Safe as designed: project/repo (team mask + keyed pseudonym; key never leaves), all ids are UUIDs, hashes
or opaque session ids (`<uuid>`, `<uuid>/agent-<id>`, `codex:<id>`, `cloud:<id>`), no free-text column.

### 1.4 Size and cost

Bytes per row, measured locally with `map_row` + `body_of` on realistic synthetic rows (incl. `version`):
sessions 765, runs 278, deviations 544, scores 246, skill_calls 300, rsi_proposals 440, rsi_decisions 348.

- Upload bytes ≈ Σ n_t × b_t. With the reference counts: 1,340×765 + 448×278 + 237×544 + 678×246 +
  765×300 + 440 + 348 ≈ **1.68 MB** JSONEachRow (compressed on disk: well under 1 MB).
- Requests = Σ ceil(n_t / 500) = 3 + 1 + 1 + 2 + 2 + 1 + 1 = **11 POSTs** (+1 ping). Client limit 100,000
  rows / 2,000 requests per day (sync.py:121): one run.
- Compute: awake hours × ~$0.333 (8 GiB, eu-west-1). Backfill wake ≤ 2 min + inserts < 1 min + 5 min idle,
  plus the console verification session: ~0.3 h → **~$0.10**, ≤ $0.25 with retries. Storage ≈ $0.
  Drawn from the Trial grant ($299.57 left); billable $0. No provider hard cap; org alert at $50.

### 1.5 Idempotency

- Row key: `id` = Supabase key; `ReplacingMergeTree(version) ORDER BY id`; reads use `FINAL`. A second
  backfill rewrites the same ids with a higher `version`; FINAL keeps one row per id.
- Insert dedup: `insert_deduplication_token = sha256(table + "\n" + body)` (clickhouse.py:482-483), sent with
  `async_insert=0`. The body includes `version` (extraction ms), so the token protects HTTP retries inside
  one run; across runs, ReplacingMergeTree does the dedup. Split rows (record errors) get their own token.
- Cursor: `rsi_checkpoints (job 'sync:clickhouse', run_key 'cursor')` in hosted Supabase (written by the run).

### 1.6 Verification after the backfill

1. `amem sync verify --sink clickhouse --days 60 --json` → `parity: true` (per-day counts Supabase vs FINAL;
   data starts 2026-09-26, so 60 days covers everything).
2. `amem sync status` → no failing/dead tables, `dead_letters` 0.
3. Owner, ClickHouse Cloud SQL console (admin), read-only:

```sql
-- one row per id
SELECT 'sessions' t, count() n, uniqExact(id) u FROM amem.sessions FINAL
UNION ALL SELECT 'runs', count(), uniqExact(id) FROM amem.runs FINAL
UNION ALL SELECT 'deviations', count(), uniqExact(id) FROM amem.deviations FINAL
UNION ALL SELECT 'scores', count(), uniqExact(id) FROM amem.scores FINAL
UNION ALL SELECT 'skill_calls', count(), uniqExact(id) FROM amem.skill_calls FINAL
UNION ALL SELECT 'rsi_proposals', count(), uniqExact(id) FROM amem.rsi_proposals FINAL
UNION ALL SELECT 'rsi_decisions', count(), uniqExact(id) FROM amem.rsi_decisions FINAL
UNION ALL SELECT 'research_runs', count(), uniqExact(id) FROM amem.research_runs FINAL
UNION ALL SELECT 'rsi_eval_results', count(), uniqExact(id) FROM amem.rsi_eval_results FINAL;

-- project/repo are only pseudonyms, 'team' or NULL (expect 0, 0)
SELECT countIf(NOT (project IS NULL OR project = 'team' OR match(project, '^p_[0-9a-f]{16}$'))),
       countIf(NOT (repo IS NULL OR repo = 'team' OR match(repo, '^p_[0-9a-f]{16}$')))
FROM amem.sessions FINAL;
SELECT countIf(NOT (project IS NULL OR project = 'team' OR match(project, '^p_[0-9a-f]{16}$')))
FROM amem.skill_calls FINAL;

-- no repo-scoped source in clear (after F1: expect only global/skill/agent/routine/parent-md and *:team / *:p_...)
SELECT instruction_source, count() FROM amem.deviations FINAL GROUP BY 1 ORDER BY 2 DESC;

-- work terms and emails anywhere (repeat per table; extend the term list; expect 0)
SELECT count() FROM amem.deviations FINAL
WHERE multiSearchAnyCaseInsensitive(toString(tuple(*)), ['stupidhuman', 'pipeline-poc'])
   OR match(toString(tuple(*)), '[^@\\s,()]+@[^@\\s,()]+\\.[A-Za-z]{2,}');
```

### 1.7 Rollback and deletion

1. Stop: `AMEM_SYNC_CLICKHOUSE=off` (nothing recurring exists).
2. Data (owner, console as admin; the writer cannot): `TRUNCATE TABLE amem.<t>` for the 9 tables, or
   `DROP DATABASE amem SYNC` (then G-CH2a again before any reuse). Deleting the service removes everything.
3. Backups: the service keeps a daily backup for 1 day; dropped data lives in it up to ~24 h (delete the
   backup in the console if needed).
4. Cursor (hosted Supabase; ask first per standing permissions):
   `delete from rsi_checkpoints where job = 'sync:clickhouse' and run_key = 'cursor';`

---

## 2. Langfuse Cloud EU (G-LF4)

### 2.1 Scope

Source is not Supabase: local run logs `<RSI_SANDBOX_DIR or ~/sandbox>/experiments/*/logs/<run_id>.json`
(+ `.stdout`, `.rsi.json`, `../sanitize-report.json`) (langfuse.py:918-938). Filter: `check()` refuses any
run whose env is not `sandbox`, whose experiment id is not `exp-YYYYMMDD-...`, whose kind is not
sandbox/investigator, or whose experiment/case/run id looks sensitive (langfuse.py:339-366); the sink also
refuses a run whose Supabase session row has env ≠ sandbox (langfuse.py:940-944).

**Count today: 0.** `~/sandbox/experiments` is empty (listed by name only, 2026-10-17; Phase H runs were
cleaned up and their baseline `h-smoke-base` is not an `exp-` id). So there is no historical backfill.
G-LF4 stays: upload the next approved sandbox run.

### 2.2 What one run sends / never sends

Sends: trace name `rsi.<kind>.<agent>`, session = experiment id, user `rsi`, tags [arm, kind], env
`sandbox`; metadata amem_session_id (uuid5), baseline_id, artifact_hash, proposal_id, run_key, arm; root
span: run_id, case, rep, kind, agent, exit_code, timed_out, duration_s, num_turns, status; per generation:
model name, usage counts, cost; per tool: tool NAME and status; scores check_passed / judge_winner /
eval_verdict. Model and tool names that look like a path, email, credential or token are sent as `redacted`
(langfuse.py:179-196).

Never: the agent's final response (`include_result=False` default since e7eca35; the CLI never sets it),
prompts, tool inputs/outputs, permission_denials tool_input, stderr, argv (except agent name and model
flag), file contents, paths.

Gap (low): `sensitive()` knows no work terms, so a case or baseline named after a work repo would pass (F3).

### 2.3 Size, cost, idempotency, verification, rollback

- Size/cost formula: units ≈ runs × (1 trace + 1 root + G generations + T tool spans + ≤3 scores) ≈ 30/run;
  bytes ≈ 3-10 KB OTLP JSON per run. Today: 0 runs → 0 units, $0 (Hobby).
- Idempotency: trace id = sha256(run_key)[:32]; span ids = sha256(run_key#part)[:16]; score id =
  `<trace>-<name>` with the run's end time (upsert); spans exported only when neither the Supabase link
  `rsi_evidence (langfuse, lf:trace/<id>)` nor the trace exists; OTLP export is one attempt (no HTTP retry).
- Verify: `amem evidence query --source lf trace --param trace_id=<id>` (amem_session_id round trip);
  `select count(*) from rsi_evidence where system = 'langfuse';` equals traces uploaded.
- Rollback: `AMEM_SYNC_LANGFUSE=off`; `DELETE /api/public/traces` with the trace ids (async); delete the
  `rsi_evidence` rows with system `langfuse` and the `sync:langfuse` cursor (ask first); or delete the project.

---

## 3. past.dev (G-PD5)

### 3.1 Scope and filter

`build_records` (pastdev.py:265-305) reads every row of: rsi_proposals, rsi_decisions (+
rsi_decision_results), rsi_experiments, edits, lesson_candidates with `decision in ('accept','reject')`
(joined to runs and lessons). No time filter. Then `sanitize_record` refuses team items, bad refs, secrets,
unstable text, > 1,536 bytes (sanitize.py:170-213). On a first run every non-refused record is sent (no
links exist). Reference: 1 + 1 + 0 + 4 (10 edits, 6 team) + 54 lessons = ~60 records.

**Run it without `--replay`.** With `--replay` the batch idempotencyKey includes the run time
(pastdev.py:445), so a second invocation is a new request that re-sends every record. Without it, the
first run still sends everything (no links), and any rerun is a true no-op (`unchanged`).

### 3.2 Fields (the whole content; no metadata is sent)

Item = `{id: amem:<entity>/<uuid|slug>, content, timestamp, label: kind, identity: "rsi"}`. Content lines:
kind, ref, rule (`<source>/<rule_id>`, ≤160), hypothesis (≤500), change_kind (≤40), result (≤240),
decision (≤40), reason (≤500), night (YYYY-MM-DD).

| Kind | Built from (text-bearing, sent after scrubbing) |
|---|---|
| proposal | summary or title; rationale.why, rationale.kind; status + result; latest decision action; rule from edits / candidates |
| decision / rejection | the proposal's rule, hypothesis, change_kind; rsi_decisions.reason; rsi_decision_results.result |
| experiment | hypothesis (jsonb text/statement/claim/summary/why or k: v pairs); kind; status + verdict; hyp/meta rule |
| edit | summary or rule_title; meta.kind or verdict; status; why; rule `instruction_source/rule_id` |
| lesson | lessons.title; lessons.slug (as `lessons/<slug>`); lesson_candidates.decision and reason |

Never sent: edits.target_path, deviation text, session text, rsi_proposals.targets/evidence_refs/risk,
lesson rule/evidence/scope_repo, any id except the record's own ref.

Scrubbers (sanitize.py:88-108) replace URLs, emails, paths, IPs, hosts, UUIDs, hex ids and long tokens;
secrets refuse the record. They do NOT catch repo names, branch names without a file extension, person
names or employer names. Checked locally: a lesson record with `pipeline-poc`, `feature/PIPE-123-ingest`,
`Anna Svensson` and `StupidHuman` passes with only the `~/...` path scrubbed; an edit with rule
`claude-md:pipeline-poc/deploy` passes unchanged. So the **team flag is the only protection**.

### 3.3 Classification and team checks

Class: sanitized free text written by agents and the owner (highest risk of the three).

Team detection per kind: proposals (p/x/c/e meta.team + evidence refs to team deviations/sessions,
pastdev.py:198-213), decisions (inherit; orphan = team), experiments (`meta.team` / `hypothesis.team` only,
pastdev.py:292), edits (meta.team or a team deviation), lessons (`runs.meta.team`, pastdev.py:226-232).

**Gap F2:** `tasks_ingest` never writes `runs.meta.team` (meta at tasks_ingest.py:286 has `repo` only), so
the lesson team check is always false. A lesson distilled from a dispatch in the work repo would go out with
its title, slug and reason. The "54 (0 team)" in cloud-readiness.md is therefore not evidence of no work
lessons. Check with SQL 5.3.

### 3.4 Size and cost

- credits = Σ max(1, ceil(utf8_bytes(content_i) / 350)) (pastdev.py:156-161); recall 0.1 each.
- Expect 250-600 bytes per record → 1-2 credits → **~60-120 credits**; hard max 60 × 5 = 300. Upload
  ~20-40 KB (max ~92 KB). 1 batch request per table (≤100 items) + polls (free). Fits 300 rows / 200 requests
  per day. Project cap 2,000 stops overrun (429), balance 0 stops at 402.

### 3.5 Idempotency

- Item id (past.dev sourceId) = the record's `amem:` ref; checked to echo byte for byte (pastdev.py:490).
- Batch idempotencyKey = sha256(canonical items + table failure count [+ run time if --replay]).
- Link: `rsi_evidence (system 'pastdev', external_ref 'past:<amem ref>')`, `metrics.sha256` = sha256(content);
  a record is re-sent only when its sha changes. Deleted ids are retired forever.

### 3.6 Verification

1. `select count(*) filter (where not coalesce((metrics->>'deleted')::boolean, false)) as live,
   count(*) filter (where metrics->>'status' in ('completed','unchanged')) as settled,
   count(*) filter (where metrics->>'status' = 'queued') as queued
   from rsi_evidence where system = 'pastdev';` → live = records in the approved plan.
2. `amem sync status` → no failing tables, dead_letters 0.
3. `GET /usage` (project spend) within 20% of the plan's credits.
4. One recall: `amem evidence query --source pd recall --param query=quote globs` → only `amem:` refs.
5. Rerun `amem sync plan --sink pastdev` → 0 rows.

### 3.7 Rollback and deletion

1. `AMEM_PASTDEV=off` (stops sink and retriever; deletion still works).
2. `pastdev.delete_all(conn)` (pastdev.py:527-545): deletes every linked live id, ≤1,000 per call, and
   marks the links deleted. No CLI exists (F6); one-liner from the 004B checkout:
   `uv run python -c "from agent_memory import store; from agent_memory.integrations import pastdev as p; c = store.connect(); print(p.delete_all(c))"`
3. Deleted ids are retired: they can never be sent again under the same id.
4. Last resort: delete the past.dev project (revokes keys; retention otherwise 365 days).
5. Optional (ask first): delete `rsi_evidence` rows with system `pastdev` and the `sync:pastdev` cursor.

---

## 4. Code gaps to fix before a real backfill

| # | Sev | Where | Gap | Fix |
|---|---|---|---|---|
| F1 | **Blocker (CH)** | clickhouse.py:287-290 (+ deviations.py:211-214) | `deviations.instruction_source` (`claude-md:<repo>`, `agents-md:<repo>`) and `rule_id` ship in clear; no team masking; repo names of own repos too | New column kind `source`: repo-scoped sources → `<prefix>:team` for team sessions (SQL `exists (select 1 from sessions s where s.session_id = deviations.session_id and s.meta->>'team'='true') or deviations.meta->>'team'='true'`), else `<prefix>:` + pseudonym(repo); `rule_id` → null for team repo-scoped rows. Translate the `rule` param in `rule_gap` / `rule_weekly_rate` like `project`. Leak test with a `pipeline-poc` deviation. |
| F2 | **Blocker (PD)** | pastdev.py:226-232; tasks_ingest.py:286 | lesson team flag never set | `team` = runs.meta.team OR `r.meta->>'repo'` in team repos (`select repo from sessions where meta->>'team'='true'` union project) OR parent profile `mode: team`; also lessons.scope_repo in team repos. Set `meta.team` in tasks_ingest via `sessions_ingest.team_info`. |
| F3 | High | clickhouse.py:106, 445-456; sanitize.py:170-213; langfuse.py:179-186 | No deny list: LABEL_RE passes emails and paths; sanitizer passes repo/branch/person names | Local-only deny terms (`AMEM_EGRESS_DENY`, comma list, never sent) + team repo/project names read from Supabase at run start. CH: label/id/pseudo input containing a term or an email shape → null. past.dev: refuse with reason `deny-term`. Langfuse: `sensitive()` true. |
| F4 | Medium | cli_integrations.py:38-47, 281-297; clickhouse.py:526 | Approval scope cannot be narrowed | `--tables a,b` (and optional `--since`) on `sync plan` and `sync run`. |
| F5 | Medium | pastdev.py:445; cloud-activation.md G-PD5 | `--replay` key includes run time: reruns re-send | Run G-PD5 without `--replay`, or make the replay key content-only. |
| F6 | Medium | pastdev.py:308-315, 527-545 | No CLI to dump the exact corpus for review, or to delete | `amem sync corpus --sink pastdev` (local file, read-only conn); `amem sync delete --sink pastdev`. |
| F7 | Low | clickhouse.py:252-253, 293-295 | Unmatched team skill calls get a pseudonym, not `team`; `skill` label unmasked | Add `or project in (team projects)` to the mask; null `skill` for team rows not in the owner's skill library (or via F3). |
| F8 | Low | sessions_ingest.py:233-246, 252-259 | Unknown remote = not team | Path rule (cwd under `~/StupidHuman/`, configurable `AMEM_TEAM_PATHS`) → team; SQL 5.2 finds unflagged rows. |
| F9 | Low | clickhouse.py:258-259 | A project literally named `team` ships as `team` | Pseudonymize the literal value before masking, or use a reserved marker. |

---

## 5. Read-only SQL for the owner / parent (hosted Supabase; do not run from a worker)

Run in a read-only transaction (`begin read only; ... rollback;`). Exact per-sink numbers after the mapper
and sanitizer: `amem sync plan --sink clickhouse --replay --json` and `amem sync plan --sink pastdev --json`
(both open the connection read-only and never call the provider; `sync.py:264-274`).

### 5.1 ClickHouse row counts (same tables and filters as the code)

```sql
begin read only;
select 'sessions' t, count(*) n, min(created_at) first_at, max(created_at) last_at,
       count(*) filter (where meta->>'team' = 'true') team,
       count(*) filter (where coalesce(env, 'prod') in ('sandbox', 'eval')) sandbox_eval,
       count(*) filter (where session_id !~ '^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$') dropped_by_mapper
from sessions
union all select 'runs', count(*), min(created_at), max(created_at), null, null, null from runs
union all select 'deviations', count(*), min(created_at), max(created_at),
       count(*) filter (where exists (select 1 from sessions s where s.session_id = d.session_id
                                      and s.meta->>'team' = 'true') or d.meta->>'team' = 'true'), null, null
       from deviations d
union all select 'scores', count(*), min(created_at), max(created_at), null, null, null from scores
union all select 'skill_calls', count(*), min(created_at), max(created_at),
       count(*) filter (where exists (select 1 from sessions s where s.session_id = k.session_id
                                      and s.meta->>'team' = 'true')),
       count(*) filter (where k.session_id is null), null
       from skill_calls k
union all select 'rsi_proposals', count(*), min(created_at), max(created_at), null, null, null from rsi_proposals
union all select 'rsi_decisions', count(*), min(decided_at), max(decided_at), null, null, null from rsi_decisions
union all select 'research_runs', count(*), min(observed_at), max(observed_at), null, null, null
       from rsi_evidence where system = 'ncm'
union all select 'rsi_eval_results', count(*), min(created_at), max(created_at), null, null, null from rsi_eval_results;
-- existing sync state (expect no rows / 0 on a first backfill)
select job, status, finished_at from rsi_checkpoints where job like 'sync:%';
select system, count(*) from rsi_evidence where system in ('clickhouse', 'langfuse', 'pastdev') group by 1;
rollback;
```

### 5.2 ClickHouse label review (every distinct value that would leave; owner reads the list)

```sql
begin read only;
select 'sessions.model' col, model v, count(*) n from sessions group by 2
union all select 'sessions.effort', effort, count(*) from sessions group by 2
union all select 'sessions.experiment_id', experiment_id, count(*) from sessions group by 2
union all select 'runs.routine', routine, count(*) from runs group by 2
union all select 'deviations.instruction_source', instruction_source, count(*) from deviations group by 2
union all select 'deviations.rule_id', rule_id, count(*) from deviations group by 2
union all select 'deviations.writer_model', writer_model, count(*) from deviations group by 2
union all select 'deviations.jev_mode', jev_mode, count(*) from deviations group by 2
union all select 'scores.question', question, count(*) from scores group by 2
union all select 'skill_calls.skill', skill, count(*) from skill_calls group by 2
union all select 'skill_calls.agent', agent, count(*) from skill_calls group by 2
order by 1, 3 desc;
-- deviations from team sessions: the exact source/rule pairs F1 must mask
select d.instruction_source, d.rule_id, count(*)
from deviations d join sessions s on s.session_id = d.session_id
where s.meta->>'team' = 'true' or d.meta->>'team' = 'true'
group by 1, 2 order by 3 desc;
-- team flag coverage: work-repo sessions that are NOT flagged (expect 0 unflagged)
select coalesce(meta->>'team', 'unknown') team, tool, count(*)
from sessions
where project_dir ilike '%stupidhuman%' or repo ilike any (array['%pipeline-poc%', '%stupidhuman%'])
   or project ilike any (array['%pipeline-poc%', '%stupidhuman%'])
group by 1, 2;
rollback;
```

### 5.3 past.dev candidate counts (upper bound before the sanitizer)

```sql
begin read only;
select 'rsi_proposals' t, count(*) n,
       count(*) filter (where coalesce(meta->>'team' = 'true', false)) team_meta from rsi_proposals
union all select 'rsi_decisions', count(*), null from rsi_decisions
union all select 'rsi_experiments', count(*),
       count(*) filter (where meta->>'team' = 'true' or hypothesis->>'team' = 'true') from rsi_experiments
union all select 'edits', count(*),
       count(*) filter (where coalesce(e.meta->>'team' = 'true', false) or exists (
           select 1 from deviations d join sessions s on s.session_id = d.session_id
           where d.id = any(e.deviation_ids) and (d.meta->>'team' = 'true' or s.meta->>'team' = 'true')))
       from edits e
union all select 'lesson_candidates', count(*),
       count(*) filter (where r.meta->>'team' = 'true')
       from lesson_candidates lc join runs r on r.id = lc.run_id where lc.decision in ('accept', 'reject');
-- F2 check: lesson records per dispatch repo, and whether that repo is a team repo
select r.meta->>'repo' repo, lc.decision, count(*) n,
       bool_or(r.meta->>'repo' in (select repo from sessions where meta->>'team' = 'true' and repo is not null
                                    union select project from sessions where meta->>'team' = 'true' and project is not null)) team_repo
from lesson_candidates lc join runs r on r.id = lc.run_id
where lc.decision in ('accept', 'reject') group by 1, 2 order by 3 desc;
select l.scope_repo, count(*) from lesson_candidates lc join lessons l on l.id = lc.lesson_id group by 1;
select instruction_source, status, count(*) from edits group by 1, 2 order by 3 desc;
rollback;
```

### 5.4 Langfuse (local, not SQL)

`find "${RSI_SANDBOX_DIR:-$HOME/sandbox}/experiments" -path '*/logs/*.json' ! -name '*.rsi.json' | wc -l`
(2026-10-17: the folder is empty → 0), then `amem sync plan --sink langfuse`.

---

## 6. Approval request text (fill the bracketed numbers from section 5 / `sync plan`)

**ClickHouse (G-CH4), only after F1 (and F3) are merged and section 5.2 is reviewed:**

> I approve one historical backfill to ClickHouse Cloud service `amem-rsi` (AWS eu-west-1), database
> `amem`, as `amem_writer`. Source: hosted Supabase `agent-memory` (read-only). Scope: all rows, first row to
> run time, of sessions, runs, deviations, scores, skill_calls, rsi_proposals, rsi_decisions, research_runs
> (rsi_evidence system ncm) and rsi_eval_results; only the allowlisted columns of `clickhouse.TABLES` at
> commit [sha]; project/repo as keyed pseudonyms or `team`; repo-scoped deviation sources masked (F1). Plan
> reviewed on [date]: [N] rows, [B] bytes, [R] requests. Command, once:
> `amem sync run --sink clickhouse --replay --budget 600`, then `amem sync verify --sink clickhouse --days 60`
> and the section 1.6 checks. Spend limit $1. Not approved: recurring sync, any other table or column,
> Langfuse, past.dev. Rollback: `AMEM_SYNC_CLICKHOUSE=off` and `DROP DATABASE amem`.

**past.dev (G-PD5), only after F2 (and F3) are merged and the full corpus is read:**

> I approve one import to past.dev project `amem-rsi` of exactly the [N] sanitized decision records I
> reviewed in [corpus file], sha256 [X] ([n_p] proposals, [n_d] decisions, [n_x] experiments, [n_e] edits,
> [n_l] lessons; 0 team items). Estimated [C] credits (max 300; project cap 2,000). Command, once:
> `amem sync run --sink pastdev` (no `--replay`). Not approved: recurring sync, recall from routines, any
> record not in the reviewed set. Rollback: `AMEM_PASTDEV=off`, then `pastdev.delete_all` (ids retired).

**Langfuse:** no backfill request: 0 sandbox run logs exist. G-LF4 is approved per sandbox run.
