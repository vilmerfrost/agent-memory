# Evals harvest: draft eval cases from what the loop learns

The loop edits rules (`amem edits run`). An A/B eval can only judge an edit when a case
exercises that rule. `amem evals harvest` turns what the loop learned into **draft** eval
cases, one per source item, so every rule the loop edits gets a real case to test it.

It only writes drafts into the folder you name. It never writes into agent-config. A later
step (agent-config, phase 2) runs it nightly and promotes approved drafts into the fenced
`~/agent-config/evals/cases/`.

```
deviations (jev_verdict = edit_instruction) ─┐
worker incidents (report BLOCKED / PARTIAL) ─┼─▶ amem evals harvest ─▶ DIR/<id>.md (draft)
handoffs (public.handoffs)                  ─┘          │              DIR/.harvest-ledger.jsonl
                                                        └─▶ runs row (routine evals-harvest)
                                   phase 2: approve ─▶ ~/agent-config/evals/cases/<id>.md
```

## Command

```sh
amem evals harvest --out DIR [--days 7] [--limit 20] [--dry-run] [--source manual]
```

| Flag | Does |
|---|---|
| `--out DIR` | Folder for the draft files. Created on the first draft. |
| `--days N` | Window: source items created in the last N days (default 7). |
| `--limit N` | Most new drafts per run (default 20). The rest count as `skipped (limit=…)` and come next run. |
| `--dry-run` | Prints the plan. Writes no file, no ledger line and no run row. |
| `--source` | Run log source: `manual` (default), `launchd`, `scheduled-task`, `cloud`. |

Output: one `draft <path>` line per new file, then

```
drafted N new, M already drafted, K skipped (reason=count, ...)
```

Postgres backend only. Without `--dry-run`, `AMEM_REQUIRE_REMOTE=1` refuses a local DB
(exit 3), like the other write commands.

## Sources

| Source | Read from | Source id |
|---|---|---|
| Edit rule | `deviations` with `jev_verdict = 'edit_instruction'` | `deviation:<uuid>` |
| Worker incident | `deviations` of a worker session (`coalesce(category, kind) = 'worker'`) whose `outcome_evidence.report_blocked` or `report_partial` is above 0 | `deviation:<uuid>` |
| Handoff | `public.handoffs` (`session_id, project, handoff_path, reason, created_at`) | `handoff:<session_id>:<sha1 of handoff_path, 10 hex>` |

**Incident signal.** `sessions.outcome` is not used: it is `partial` for any worker that
edited files without a commit, and a worker that committed and then reported PARTIAL shows
`committed`. The report counts in `outcome_evidence` come straight from `Status: BLOCKED` /
`Status: PARTIAL` lines in the worker's own report (`sessions ingest`), so they are the most
direct signal. Any BLOCKED or PARTIAL report counts, even if a DONE came later: the worker
still got stuck once. A draft says which one (`worker_report: BLOCKED|PARTIAL`, BLOCKED
wins). A deviation that is both an edit rule and an incident is drafted once, as an edit rule.

**Handoffs table missing.** The `handoffs` table comes from a sibling migration. When it does
not exist yet, harvest reads 0 handoffs and prints `note: no public.handoffs table yet`.

**Rule key.** `<instruction_source>/<rule key>`, the same key the deviations report and the
scoreboard group by: the catalog `rule_id`, or `ref:<lowercased ref>` for unmatched rows
(e.g. `parent-md/push-and-merge-policy`, `agent:worker/command-form`). A handoff takes the
most frequent rule of its session's deviations, else `unknown`.

## Skip reasons

| Reason | Why |
|---|---|
| `team-repo` | The session is a team repo's (`sessions.meta.team`). The loop never edits those rules. |
| `incomplete-handoff` | A handoff row without a session id or path. |
| `limit` | Over `--limit` this run. |

## Idempotency

A source id is drafted once. Harvest treats a source as already drafted when it is in
`DIR/.harvest-ledger.jsonl` (one JSON line per draft: `source, file, rule, kind,
harvested_at`) **or** in the header of any `DIR/*.md` draft. So promotion may move a draft out
of `DIR` without the next run drafting it again. Keep the ledger.

## Draft file format

agent-config's `evals/run.py` reads a case as `evals/cases/<id>.md`: the whole file is the
prompt for the parent session (`{MAIN}` and `{ESCAPE_DIR}` are filled in), an optional
`<id>.setup.sh` sets up the scratch clone, and `CASES[<id>]` in `run.py` names its grader.
`repo_name()` names the scratch clone after the part of the id before the first `-`.

A draft matches that format:

- **File name** `<prefix><7 hex>-<rule slug>.md`, prefix `dv` (edit rule), `in` (incident) or
  `ho` (handoff). The part before the first `-` is unique per source, so two promoted drafts
  never share a scratch clone.
- **One leading HTML comment** `<!-- amem-eval-draft … -->` with the metadata, one
  `key: value` per line: `status: draft`, `source`, `source_kind`, `rule`, `session`,
  `project`, `source_created_at`, `harvested_at`, the deviation's `rule_title`, `jev`,
  `what` / `why` / `how` (or the handoff's `reason`, `handoff_path`), `expect`, `grader`
  and `promote`. Text is redacted (`agent_memory.redact`) and capped at 400 characters.
- **The prompt** after the comment: the parent opener (`We've talked enough, let's go to
  work.`), or for a handoff the eval resume prompt, then one `TODO(promote)` line.

Example (`dv1a2b3c4-push-and-merge-policy.md`):

```markdown
<!-- amem-eval-draft
status: draft
source: deviation:6f0c…
source_kind: edit_instruction
rule: parent-md/push-and-merge-policy
session: 1c9e…
project: demo
...
expect: The agent follows the rule "Push and merge policy" (parent-md/push-and-merge-policy) where it deviated before.
grader: none yet: add a grade_<id> function and a CASES entry in evals/run.py on promotion
promote: replace the TODO prompt line, strip this comment, move to evals/cases/<id>.md
-->
We've talked enough, let's go to work.

TODO(promote): one small task in junk-orchestra that puts the agent in the situation above, without naming the rule.
```

A draft is not runnable as is: `run.py` refuses an id that has no `CASES` entry. Promotion
(phase 2) writes the task, adds the grader, strips the comment
(`evals_harvest.strip_header`) and moves the file into `evals/cases/`.

## Run log

Every harvest without `--dry-run` logs one `runs` row through the `log-run` code path
(`store.insert_run`): routine `evals-harvest`, status `pass` (or `error` when the read
failed), the summary line, and `meta` = `{drafted, already, skipped, by_kind, files, out,
handoffs_table}`. `amem runs --routine evals-harvest` shows the history.

## Coverage records

After the run row, the harvest writes coverage records (data-use plan R4, `coverage_daily.harvest_records`;
docs/data-use/coverage.md): job `rsi-harvest`, run key `harvest-<UTC time>`, `run_id` = the run row. One
consumption record per source dataset, `deviations:deviations` and `coordination:handoffs`: sources read
(`queried`), not drafted before (`considered`), drafted (`used` = `findings`, each citing its source as an
`amem:deviation/<id>` or `amem:session/<id>` ref, at most 20) and the skip reasons above (`already_drafted`,
`team-repo`, `incomplete-handoff`, `limit`). A handoffs snapshot counts the handoffs in the window; a missing
handoffs table writes none (the guard reads that as `not_collected`). A failed harvest records
`analysis_failed`. A write problem is one stderr line; the exit code and the summary line do not change.
`--dry-run` and `AMEM_COVERAGE_WRITE=0` write nothing.
