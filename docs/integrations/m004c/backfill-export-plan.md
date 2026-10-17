# Mission 004C: field-level export plan for the first historical backfill

Status 2026-10-17: plan only. Nothing was sent, no hosted database was read, no provider was called.
Code: branch `worktree-m004c-backfill` (based on `rsi/m004c` 09413ba). Research: `backfill-research.md` (its F1-F9
are fixed; section 0 below lists what changed after it). Owner rules: a SMALL, SANITIZED, EXPLICITLY REVIEWED
first dataset; repository ownership resolved before any real record is exported; no backfill, recurring sync or
production promotion without a separate approval.

## 0. What changed since the research

| Item | Before | Now |
|---|---|---|
| Ownership host | Ingestion stored the remote owner, not the host. `gitlab.example.com/vilmerfrost/x` was `own`. | Ingestion stores `meta.remote_host`. `own` needs host `github.com` AND an owner account. Any other host, an ssh alias, or no host is `private`. |
| Rows ingested before this change | Owner only, so `own`. | No host, so `private`, until `amem sessions hosts --apply` re-derives the host (section 1.2) or a re-ingest reads it. |
| Review of the exact outgoing records | `sync plan` gave counts only. | `amem sync plan --contents` lists every outgoing record: the exact payload, its sha256, class, credit and size estimate. Refused records are listed with reasons and without content. |
| Sending only a reviewed set | Not possible: a run sends everything pending. | `amem sync run --sink pastdev --only reviewed.json` sends only those refs. It refuses the whole run (sends nothing) when any current payload sha256 differs from the reviewed one. |
| Rollback of one set | `pastdev.delete_all` only (every linked id). | `amem sync delete --sink pastdev --only reviewed.json` deletes only that set's data points. |

## 1. Ownership classes (the export rule)

Every session gets one class. Rows tied to a session take its class. The rule is `egress.classify` in Python and
`egress.class_sql` in SQL; a test pins that both agree.

| Class | Rule (from `sessions.meta`) |
|---|---|
| team | `team` is true: the remote owner is not a personal owner, a `mode: team` parent profile, or a same-repo session in the ingest was team. |
| own | `team` is false AND `remote_owner` (case-insensitive) is an owner account AND `remote_host` (case-insensitive) is `github.com`. |
| private | Everything else: no remote, an unreadable owner, another owner, another host, an ssh alias host, no host stored, no session. |

Owner accounts: `AMEM_OWNER_ACCOUNTS`, else `AMEM_PERSONAL_OWNERS`, else `vilmerfrost`.

### 1.1 Ingestion

`sessions_ingest.team_info` reads the remote URL (Codex: the URL the transcript recorded; else the repo's
`.git/config`) and stores `remote_owner` and `remote_host` together. The host is lowercase, without user or port.
An ssh alias such as `git@github-work:owner/x` stores `github-work`; it is not resolved, so it is never `own`.
A check of the 31 remote URLs in the local repos (`~/Projects/*`, `~/*`) on 2026-10-17: every one is on
`github.com` (24 with the owner's account, 7 with other owners); none uses an alias, so no own repo is lost to the
alias rule today.

### 1.2 Existing rows: the decision

Rows ingested before this change have `team: false` and `remote_owner` but no `remote_host`. They are **private
by default**. Two things can restore the host:

1. A normal re-ingest of the same transcript (the upsert merges meta). It only reaches transcripts in the ingest
   window whose repo still exists.
2. `amem sessions hosts` (`session_hosts.py`): a dry run by default. For each candidate row (team false, owner
   account, no host) it reads `meta.cwd`, then each `meta.cwds` entry, with `git_remote_url` (the repo's
   `.git/config`, or a worktree's main checkout). No git process, no network. The first path whose remote has an
   owner and a host decides:
   - `rederived`: the remote's owner equals the stored owner. `--apply` writes `remote_host` and
     `remote_host_source: "rederived"`.
   - `owner-changed`: the remote now names another owner. Nothing is written; the row stays private.
   - `no-remote`: no path has a readable remote (worktree removed, repo moved or deleted). The row stays private.
   `--revert` removes every host that `--apply` wrote.

Trade-off:
- Default private is safe but makes every pre-change session private. Until hosts are restored, past.dev refuses
  every proposal, decision and edit that cites those sessions' deviations, and ClickHouse masks their
  project/repo as `private`. Records tied to no session (experiments, lessons without a repo, edits without
  deviations) are not affected by the class, but see the next point.
- The deny list grows too: every repo and project name of a private session is a deny term unless an own
  session has the same name. While no session has a host, own repo names such as `agent-memory` are deny terms,
  so past.dev also refuses records that name them (reason `deny`) and ClickHouse nulls labels that contain them.
  After the merge, the scheduled `amem sessions ingest` (7-day window) adds hosts to recent sessions of repos
  that still exist, which brings the active own names back; `amem sessions hosts --apply` does the same for the
  rest. Read `refused_records` in the preview with this in mind.
- Re-derivation uses the remote the repo has **today**, not at session time. A repo whose remote moved to
  github.com from another host (same owner) counts as own for older work too. The owner check limits this to the
  same account. A moved or deleted repo stays private (the safe direction).
- `--apply` writes to the hosted `sessions` table (meta only). It is not a migration, so it needs its own
  approval. The dry run reads in a read-only transaction.

### 1.3 Synthetic dataset (`amem synth seed`)

Own sessions now store `remote_host: github.com`. The private case `non-github-remote` now stores what ingestion
really stores for that case: `team: false`, `remote_owner: vilmerfrost` and a non-GitHub host (with a leak marker).
Gold counts (sessions own/team/private):

| Rule | sessions | skill_calls | deviations |
|---|---|---|---|
| `after` (today: owner + github.com) | 11 / 2 / 3 | 11 / 2 / 3 | 15 / 2 / 1 |
| `owner_only` (new in gold: F8 before the host) | 12 / 2 / 2 | 12 / 2 / 2 | 15 / 2 / 1 |
| `before` (pre-F8) | 14 / 2 / 0 | 14 / 2 / 0 | 16 / 2 / 0 |

The `after` counts do not change. Before this change the synthetic non-GitHub case stored no owner, which is not
what ingestion does. Now it stores the real shape, and gold's `owner_only` shows that the old rule counted it own.

## 2. past.dev

### 2.1 One item

One item per decision record, in `POST /ingest/batch` (at most 100 items per call). No `metadata`, no
`audience`. The item is exactly what `sync plan --contents` shows as `payload`.

| Field | Value | Transform |
|---|---|---|
| `id` | the record's `amem:<entity>/<id>` ref | raw (our own ref; the only id that leaves) |
| `content` | the lines below, `key: value`, in this order | sanitized (2.3) |
| `timestamp` | the record time, UTC, seconds | raw |
| `label` | the record kind | raw (fixed vocabulary) |
| `identity` | `rsi` | constant |

`sha256` (the value a reviewer signs off) = sha256 of the canonical JSON of the item (sorted keys, no spaces,
UTF-8). A change to any field changes it.

### 2.2 Content lines per source table

Every text line is sanitized (2.3) and cut to its cap. `night` is sent only when it is `YYYY-MM-DD`.

| Kind (table) | `rule` (<=160) | `hypothesis` (<=500) | `change_kind` (<=40) | `result` (<=240) | `decision` (<=40) | `reason` (<=500) | time / `night` |
|---|---|---|---|---|---|---|---|
| proposal (`rsi_proposals`) | latest linked edit `instruction_source/rule_id`, else candidate `source/rule_key` | `summary`, else `title` | `rationale.kind`, else edit `meta.kind`, else candidate `kind` | `status: result` | latest `rsi_decisions.action` | `rationale.why` | `created_at`; experiment night or night of `created_at` |
| decision / rejection (`rsi_decisions`) | the proposal's | the proposal's | the proposal's | `rsi_decision_results.result` | `action` | `reason` | `decided_at` |
| experiment (`rsi_experiments`) | `hypothesis.rule`, else `meta.rule` | `hypothesis.text/statement/claim/summary/why`, else its scalar `k: v` pairs (not `team`, `rule`) | `kind` | `status: verdict` | - | - | `created_at`; `night` |
| edit (`edits`) | `instruction_source/rule_id` | `summary`, else `rule_title` | `meta.kind`, else `verdict` | `status` | - | `why` | `created_at` |
| lesson (`lesson_candidates` + `lessons`, decision accept/reject; ref `amem:run/<run_id>`) | `lessons/<slug>` | `lessons.title` | - | - | `decision` | `reason` | `considered_at` |

Dropped (never in content): proposals `title` (when a summary exists), `targets`, `evidence_refs`, `risk`,
`expected`, `change_id`, `artifact_hash`, `meta`, other `rationale` keys; decisions `artifact_hash`, `source`,
`verified`, `expires_at`, result `change_id` and `detail`; experiments `baseline_id`, `finished_at`, `meta` (except
`rule` and `team`); edits `target_path`, `improve_id`, instruction hashes, `deviation_ids`, `pushed_at`,
`reverted_at`, `meta` (except `kind`); lessons `rule`, `evidence`, `scope_repo`, `scope_task`, `evals`,
`promoted_to`; runs: everything (the run id is only the ref). No session, deviation, transcript or path text.

### 2.3 Sanitizing (`sanitize.sanitize_record`)

Per text field, in order: Unicode NFKC and control/format characters removed; `redact.redact` (credentials);
secret shapes refuse the whole record (past.dev, Stripe, webhook, npm, Hugging Face keys, PEM, `password=...`,
bearer values, long mixed-case base64); scrubbers replace URLs, emails, Windows/UNC/home/absolute/relative paths,
IP addresses, `localhost`, host names, UUIDs, hex ids and long tokens with `[url]`, `[email]`, `[path]`, `[ip]`,
`[host]`, `[id]` (the record's own ref id is kept); whitespace collapsed; cut to the cap. A second pass must
change nothing, and the final text is checked for secrets and deny terms again. The whole record is at most
1,536 bytes.

### 2.4 Team and private handling

A record is never masked; it is **refused** (never sent; listed in `refused_records` with the reason):

| Reason | When |
|---|---|
| `team` | proposals: `team` true in the proposal's `meta`, its experiment's `meta`, its candidate's `metrics` or its latest edit's `meta`, or any cited deviation or session is team. Decisions: their proposal's class (a decision without a proposal is team). Experiments: `meta.team` or `hypothesis.team`. Edits: `meta.team` or a team deviation. Lessons: `runs.meta.team`, or the run's repo or the lesson's `scope_repo` is a repo or project of a team session. |
| `private` | proposals, decisions, edits: any cited deviation or session is not own (no host, another host, another owner, or not found). Lessons: the run's repo or `scope_repo` is not a repo or project of an own session. Experiments are tied to no repo: never private (the reviewer reads them). |
| `deny` | a raw field or the final text names an `AMEM_EGRESS_DENY` term or a team or private repo/project name (names an own session also has are not terms). |
| `secret:*`, `unstable:*`, `cap`, `kind`, `ref` | see 2.3. |

A sent record that later becomes refused (or disappears) is retracted on the next unrestricted run (deleted at
past.dev, its id retired). A run with `--only` never retracts or checks anything outside its reviewed set.

### 2.5 Limits

- 1,536 bytes per record; 100 items per ingest call; 1,000 ids per delete call.
- Client-side per day: 300 rows, 200 requests (`AMEM_SYNC_PASTDEV_MAX_ROWS_PER_DAY` / `_MAX_REQUESTS_PER_DAY`).
- Project caps set by the owner: 250 credits, 100 ingestions per month (a 429 `project-cap-reached` stops the
  run as `capped`, never retried). 500 requests per minute per key.
- Kill switch: `AMEM_PASTDEV=off` (sink and retriever); deletion still works.
- A deleted id is retired forever (past.dev refuses a deleted id).

### 2.6 Cost

    credits = sum over sent items of max(1, ceil(utf8_bytes(content) / 350))      (at most 5 per record)
    recall  = 0.1 credit per query; ingestion polls, retries of a failed ingestion, deletes and /usage are free

`sync plan --contents` gives `credits` per record and `cost` per plan (the same local rule). C3 measured the
rule against `/usage`: 11.6 used vs 11.6 estimated.

## 3. ClickHouse

### 3.1 Transforms

| Name | Meaning |
|---|---|
| raw | sent as stored (ids that match the ref grammar, numbers, booleans; timestamps as UTC `YYYY-MM-DD hh:mm:ss.ffffff`) |
| label | sent when it matches `LABEL_RE` (no spaces, <=128 chars), else null |
| pseudonymized | own row: `p_` + 16 hex of HMAC-SHA256 with `AMEM_PSEUDONYM_KEY` (the key never leaves; no key: null) |
| masked | team row: `team`; private row: `private` (decided by the row's class, never by the value) |
| dropped | never selected from Supabase |

On every text-like column (id, label, pseudonymized, source): a value that matches a deny term or is email-shaped
ships as null; a row whose key does is dropped (counted in `refused_records`, without the key). Every row also
carries `version` (extraction time in ms).

### 3.2 Columns per table

| Table (source) | Column: transform |
|---|---|
| sessions | `id` (session_id): raw. `kind`, `tool`, `env`, `category`, `model`, `effort`, `outcome`: label. `project`, `repo`: own pseudonymized, team/private masked. `experiment_id`: raw id. `created_at`, `started_at`, `ended_at`, `last_activity_at`, `outcome_at`: raw. `transcript_bytes`, `turns`, `tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_creation`, `lines_added`, `lines_removed`: raw. `duration_seconds`, `active_seconds`, `api_seconds`, `tool_seconds`: raw. Dropped: `title`, `summary`, `project_dir`, `skills`, `instructions`, `meta`, `models`, `efforts`, `outcome_evidence`, `model_last`, `effort_last`, the `*_updated_at` and `scanned_at` columns. |
| runs | `id`: raw. `routine`, `source`, `status`: label. `started_at`, `finished_at`, `created_at`: raw. `duration_s` (finished - started): raw. Dropped: `summary`, `failure_reason`, `report_path`, `meta`, `source_key`. |
| deviations | `id`, `session_id`, `instruction_sha`: raw. `instruction_source`: global/skill/agent/routine sources label; a repo-scoped source (`claude-md:<repo>`, `agents-md:<repo>`) of an own row `<prefix>:<pseudonym of repo>`, of a team/private row masked `<prefix>:team` / `<prefix>:private`. `rule_id`: label; null when the source is masked. `turn`, `followed_grade`, `result_grade`, `objective_result`, `jev_confidence`: raw. `result_outcome`, `jev_verdict`, `jev_mode`, `writer_model`: label. `created_at`: raw. Dropped: `what`, `why`, `how`, `instruction_ref`, `rule_title`, `source_key`, `jev_answers`, `meta`. |
| scores | `id`, `run_id`: raw. `question`, `kind`, `jev_mode`: label. `value_num`, `confidence`, `created_at`: raw. Dropped: `value`, `probabilities`. |
| skill_calls | `id`, `session_id`, `ts`, `created_at`, `backfill`: raw. `invoked_by`, `agent`: label. `project`: own pseudonymized, else masked. `skill`: label for an own call; for a team/private call only when the same skill was also called in an own session, else null. Dropped: `cwd`, `source_key`, `logged_session`. |
| rsi_proposals | `id`, `artifact_hash`, `experiment_id`, `candidate_id`: raw. `status`: label. `confidence` and 5 timestamps: raw. Dropped: `title`, `summary`, `rationale`, `evidence_refs`, `risk`, `expected`, `targets`, `change_id`, `result`, `meta`. |
| rsi_decisions | `id`, `proposal_id`, `artifact_hash`: raw. `action`, `source`, `verified`: label. `decided_at`, `expires_at`: raw. Dropped: `reason`. |
| research_runs (`rsi_evidence` where `system = 'ncm'`) | `id`: raw. `run_id`, `task`, `status`, `gpu`, `provider`, `repo_commit`, `checkpoint_revision`, `dataset_revision`: label (from `external_ref` and `metrics`). 15 metric floats (`solved_rate`, `cell_rate`, `valid_rate`, `rouge1`, `rouge2`, `rouge_l`, `gen_ppl`, `sacrebleu_cased`, `sacrebleu_lower`, `chrf`, `length_ratio`, `unigram_entropy`, `cost_usd`, `gpu_minutes`, `runtime_s`): raw. `observed_at`: raw. Dropped: `summary`, `subject_refs`, other metrics. |
| rsi_eval_results | `id`, `artifact_hash`, `experiment_id`, `baseline_id`, `record_sha`: raw. `suite`, `suite_version`, `model`, `effort`, `verdict`: label. `created_at`: raw. Dropped: `record`. |

### 3.3 Team and private handling

- sessions: class from `meta` (section 1). Team and private rows still ship their activity metadata (times,
  tokens, outcome, model); only `project` and `repo` are masked. The research left one owner decision open:
  accept this, or exclude team rows entirely.
- deviations: `team` when the deviation's own `meta.team` is true, else its session's class (no session: private).
- skill_calls: their session's class (no matched session: private).
- runs, scores, rsi_proposals, rsi_decisions, research_runs, rsi_eval_results: no repo link; labels only.

### 3.4 Limits and cost

- 500 rows per insert (one POST each); client-side per day 100,000 rows and 2,000 requests.
- Without `--replay`, each run re-sends rows changed within the lookback window (7 days); ReplacingMergeTree keeps
  one row per id.
- Kill switch: `AMEM_SYNC_CLICKHOUSE=off`.

      upload bytes ~ sum over tables of rows_t x bytes_per_row_t   (measured: sessions 765, runs 278, deviations 544,
                                                                    scores 246, skill_calls 300, proposals 440,
                                                                    decisions 348)
      requests     = sum over tables of ceil(rows_t / 500)  (+1 ping)
      compute      ~ awake hours x ~$0.333 (8 GiB, eu-west-1); a backfill is ~0.3 h -> ~$0.10, <= $0.25 with retries

`sync plan --sink clickhouse --contents` shows each row exactly as it will be inserted (without `version`), with
its sha256 and wire size.

## 4. Review tooling

| Command | What it does | Writes / sends |
|---|---|---|
| `amem sync plan --sink pastdev --contents [--class own] [--limit N] --json` | Every item the next run would send: `ref`, `table`, `class`, `payload`, `sha256`, `bytes`, `credits`; `refused_records` (`ref`, `table`, `class`, `reasons`, no content); `actions` (retractions and ingestion checks, refs only). | Nothing. Read-only transaction; the provider is never called. |
| `amem sync plan --sink clickhouse --contents [--replay] [--class C] [--limit N] --json` | The same for ClickHouse rows; `refused_records` are dropped-row counts per table. | Nothing. |
| `amem sync plan --sink pastdev --only reviewed.json --json` | Checks a reviewed set as the run will: `review.ok`, `review.problems`, `review.states`; the plan narrowed to the set. Exit 1 when the set does not match. | Nothing. |
| `amem sync run --sink pastdev --only reviewed.json --json` | Checks first (read-only); on any mismatch prints `REFUSED`, exits 1, calls nothing, writes nothing. Else sends only the reviewed refs that are new or changed. Not with `--tables` or `--since`. | Sends the reviewed set; writes its links and the cursor. |
| `amem sync delete --sink pastdev --only reviewed.json --json` | Deletes the set's live data points and marks their links deleted (ids retired). | Deletes at past.dev; updates links. |
| `amem sessions hosts [--apply] [--revert] [--json]` | Section 1.2. | Dry run: nothing. `--apply`/`--revert`: `sessions.meta`. |

`review.problems` reasons: `changed` (the current payload has another sha256), `not found` (no such record now),
`refused: <reasons>` (the sanitizer refuses it now). `review.states`: `send`, `already-sent`, `check` (a queued
ingestion), `dead-lettered`, `retired`.

## 5. First backfill proposal (template)

Scope: **past.dev only, own class only, at most 10 records, chosen by the reviewer from the preview.** One run.
No ClickHouse, no Langfuse, no recurring sync, no `--replay`. The record list is left for the parent to fill from
hosted (5.7).

### 5.0 Preconditions

1. This branch is merged and the full suite is green on `integration`.
2. The owner has approved the text in 5.8 (with the record list filled in).
3. Ownership of the chosen records is resolved: each record is `own` in the preview. Optional, separately
   approved: `amem sessions hosts --apply` on hosted, after reading its dry run (section 1.2). Without it, only
   records tied to no pre-change session can be `own`.
4. `AMEM_PASTDEV_KEY` is the `amem-rsi` (`default-project`) key with caps 250 credits / 100 ingestions.
5. The C3 synthetic ids are retired; the project holds no live data points (`/usage` before).

### 5.1 Preview against hosted, read-only (parent; no past.dev key in the environment)

```sh
cd ~/Projects/agent-memory
env -u AMEM_PASTDEV_KEY PGOPTIONS='-c default_transaction_read_only=on' \
  uv run amem sync plan --sink pastdev --contents --class own --json > "$TMPDIR/pd-preview.json"
env -u AMEM_PASTDEV_KEY PGOPTIONS='-c default_transaction_read_only=on' \
  uv run amem sync plan --sink pastdev --contents --class private --json > "$TMPDIR/pd-private.json"
env -u AMEM_PASTDEV_KEY PGOPTIONS='-c default_transaction_read_only=on' \
  uv run amem sessions hosts --json > "$TMPDIR/hosts-dry-run.json"
jq '{records_total, refused_total, cost, masked}' "$TMPDIR/pd-preview.json"
jq -r '.records[] | [.ref, .table, .class, .credits, .bytes] | @tsv' "$TMPDIR/pd-preview.json"
```

`PGOPTIONS` makes every transaction of the session read-only; `sync plan` also opens its own read-only
transaction. If the connection refuses the startup option, stop and use the direct (non-pooler) connection
string; do not drop the option. The preview files hold decision text: keep them local, delete them after 5.6.

### 5.2 Review (the reviewer)

1. Read every candidate's `payload.content` in full. Reject a record that names a person, an employer, a client,
   a work repo, a branch, a path, a host or anything not about the owner's own agent setup.
2. Read `refused_records` (reasons only) and the `private` preview: confirm nothing own was refused by mistake.
3. Choose at most 10 refs. Build the reviewed set from the preview (unchanged records, their sha256 included):

```sh
jq --argjson keep '["amem:...", "amem:..."]' \
   '{records: [.records[] | select(.ref as $r | $keep | index($r))]}' "$TMPDIR/pd-preview.json" > reviewed.json
jq '.records | length' reviewed.json                                      # <= 10
jq '[.records[].credits] | add' reviewed.json                             # the expected credits
jq -r '.records[] | [.ref, .class, .sha256] | @tsv' reviewed.json         # every class is own
```

4. Check the set against hosted again, read-only (exit 0, `review.ok` true, `rows` = the set size, `cost` = the
   expected credits):

```sh
env -u AMEM_PASTDEV_KEY PGOPTIONS='-c default_transaction_read_only=on' \
  uv run amem sync plan --sink pastdev --only reviewed.json --json | jq '{review, rows, cost}'
```

### 5.3 Usage baseline (free GET)

```sh
uv run python -c "import json; from agent_memory.integrations import pastdev as p; \
print(json.dumps(p.Client(p.config()).usage(), sort_keys=True))" > "$TMPDIR/usage-before.json"
```

### 5.4 The run (once, with the approved set only)

```sh
uv run amem sync run --sink pastdev --only reviewed.json --budget 300 --json | tee "$TMPDIR/pd-run.json"
```

Expect: `status` `ok`, `rows` = the set size, `failed` 0, `dead_letters` 0, `review.ok` true, every
`review.states` value `send`. A `REFUSED` (exit 1) means the data changed after review: nothing was sent; review
again from 5.1.

### 5.5 Verification

```sh
# 1. links: exactly the reviewed refs, all completed (read-only; psql with the hosted URL, or the SQL editor
#    inside `begin read only; ... rollback;`)
PGOPTIONS='-c default_transaction_read_only=on' psql "<hosted db url>" -c \
  "select external_ref, metrics->>'status', metrics->>'sha256' from rsi_evidence where system = 'pastdev' order by 1;"
# 2. what was sent is what was reviewed: every state already-sent, review ok
env -u AMEM_PASTDEV_KEY PGOPTIONS='-c default_transaction_read_only=on' \
  uv run amem sync plan --sink pastdev --only reviewed.json --json | jq '.review'
uv run amem sync status --json | jq '.[] | select(.sink == "pastdev")'          # no failing, dead_letters 0
# 3. /usage delta = the expected credits (within 10%; the meter can lag a minute)
uv run python -c "import json; from agent_memory.integrations import pastdev as p; \
print(json.dumps(p.Client(p.config()).usage(), sort_keys=True))" > "$TMPDIR/usage-after.json"
jq -n --slurpfile a "$TMPDIR/usage-before.json" --slurpfile b "$TMPDIR/usage-after.json" \
   '$b[0].project.credits - $a[0].project.credits'
# 4. recall spot check (0.1 credit each): words from one reviewed record; only amem: refs come back, each re-read
uv run amem evidence query --source pd recall --param 'query=<words from one reviewed hypothesis>' --json
# 5. leak scan of the reviewed (= sent) text: expect no output
jq -r '.records[].payload.content' reviewed.json | grep -nEi '@|/Users/|~/|https?://|sk-|past_sk|stupidhuman|<deny terms>'
```

Accept: the links are the reviewed refs and nothing else; `review.states` all `already-sent`; the credits delta
equals the reviewed sum; recall returns at least one reviewed ref and only `amem:` refs; the leak scan prints
nothing. Anything else: roll back (5.6) and report.

### 5.6 Rollback

```sh
export AMEM_PASTDEV=off                                   # stops sink and retriever; deletion still works
uv run amem sync delete --sink pastdev --only reviewed.json --json    # deleted = the set size, not_linked 0
```

Then recall the spot-check query again (expect no reviewed ref; deletion is asynchronous, allow a minute), and
check the links show `deleted: true`. The deleted ids are retired: they can never be sent again under the same
ref. Delete the local preview, run and usage files.

### 5.7 Record list (the parent fills this from hosted)

| # | ref | table | class | sha256 (first 12) | credits | reviewer note |
|---|---|---|---|---|---|---|
| 1 | | | own | | | |
| ... | | | own | | | |
| <=10 | | | own | | | |

Totals: [N] records, [C] credits, [B] bytes. Preview taken at [time] from commit [sha].

### 5.8 Approval text (fill the brackets)

> I approve one import to past.dev project `default-project` (amem-rsi) of exactly the [N] own-class decision
> records listed in reviewed.json (sha256 of the file: [X]), reviewed on [date]. Each record's payload sha256 is
> in the file; the run sends nothing if any differs. Estimated [C] credits (project cap 250). Command, once:
> `amem sync run --sink pastdev --only reviewed.json`. Verification and rollback as in
> `docs/integrations/m004c/backfill-export-plan.md` section 5. Not approved: any other record, `--replay`,
> recurring sync, recall from routines, ClickHouse, Langfuse, `amem sessions hosts --apply`.
