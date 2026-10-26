# Relationship contract

Stage B inventory of the links the data-use plan needs (plan section 5, "Relationships"). Each relationship
uses an existing column. Nothing is back-filled and nothing is assigned by guess: an event with no link stays
unlinked and visible. The first Stage C repair covers experiment attempts (R1, see "Experiment attempts" below);
other repairs and reconciliation are still to come.

Check: `amem coverage links [--json]` (`src/agent_memory/coverage_links.py`, read-only: one READ ONLY
transaction, always rolled back). Per relationship it counts:

- **eligible**: child rows that should carry the link;
- **linked**: the link is set and its target exists;
- **unlinked**: the link is not set (null, empty, missing key);
- **unresolvable**: the link is set but the target row is missing (late arrival or dangling).

A relationship whose table is missing reports `missing_table` and the others still run.

## Inventory

| Key | Relationship | Child rows | Link | Target | Enforced | How an unlinked row is visible |
|---|---|---|---|---|---|---|
| `parent_worker_sessions` | parent and worker sessions | `sessions` with kind or category `worker` | `sessions.parent_session_id` (generated from `meta.parent_session`) | `sessions.session_id` | no | worker row with `parent_session_id` null |
| `session_commands` | sessions and commands | `execution_spans` | `execution_spans.session_id` | `sessions.session_id` | no (events may arrive before the session) | span with `session_id` null |
| `command_test_runs` | commands and test runs | `test_timing_runs` | `test_timing_runs.parent_span_id` | `execution_spans.span_id` | no | run with `parent_span_id` null |
| `session_test_runs` | sessions and test runs | `test_timing_runs` | `test_timing_runs.session_id` | `sessions.session_id` | no | run with `session_id` null |
| `test_run_cases` | test runs and individual tests | `test_timing_cases` | `test_timing_cases.run_id` | `test_timing_runs.run_id` | foreign key, not null | cannot be unlinked; a run whose case rows are missing shows as `runs_missing_cases` |
| `experiment_attempt_sessions` | experiments and attempts | `rsi_attempts`, plus `sessions` with env `sandbox` or an `experiment_id` that no `rsi_attempts` row covers | `rsi_attempts.experiment_id` on a `linked` row (attempt key + `attempt_session_id`) | `rsi_experiments.id` | no | `rsi_attempts` row with status `unresolved` and its `reason`, or a sandbox session without an attempt row; eval sessions are excluded and counted (`excluded_eval_harness`, `excluded_eval_other`) |
| `experiment_eval_results` | experiments and evaluation results | `rsi_eval_results` | `rsi_eval_results.experiment_id` | `rsi_experiments.id` | no | result with `experiment_id` null |
| `experiment_traces` | experiments and Langfuse traces | `rsi_evidence` system `langfuse`, kind `trace` | `amem:experiment/<id>` in `subject_refs` | `rsi_experiments.id` | no | trace evidence without an experiment ref |
| `experiment_scores` | experiments and Langfuse scores | `rsi_evidence` system `langfuse`, kind `score` | `amem:experiment/<id>` in `subject_refs` | `rsi_experiments.id` | no | score evidence without an experiment ref |
| `decision_proposals` | decisions and proposals | `rsi_decisions` | `rsi_decisions.proposal_id` | `rsi_proposals.id` | foreign key, not null | cannot be unlinked |
| `decision_results` | decisions and their outcomes | `rsi_decisions` | a `rsi_decision_results` row per decision | `rsi_decision_results.decision_id` | partial (FK on the result side) | decision with no result row (`waiting` in `rsi_decision_status`) |
| `proposal_edits` | proposals and staged edits | `edits` with `meta.mode = stage` or status `staged` | `edits.meta->>'proposal_id'` | `rsi_proposals.id` | no (jsonb) | staged edit without `meta.proposal_id` |
| `edit_undo_requests` | edits and undo outcomes | `undo_requests` | `undo_requests.edit_id` | `edits.id` | foreign key, not null | cannot be unlinked; an open request shows as `outcome_pending` |
| `change_outcomes` | applied changes and outcome checks | `rsi_outcomes` | `change_kind` + `change_id` | `rsi_proposals.id` or `edits.id` | no | always keyed; a dangling `change_id` is unresolvable |

## Relationships without an enforced link

`parent_worker_sessions`, `session_commands`, `command_test_runs`, `session_test_runs`,
`experiment_attempt_sessions`, `experiment_eval_results`, `experiment_traces`, `experiment_scores`,
`proposal_edits` and `change_outcomes` have no foreign key. `decision_results` is enforced on the result side
only. Most are unenforced on purpose: the child often arrives before its target (a span before its session is
ingested, a trace before its experiment row syncs). The check therefore separates "unlinked" from
"unresolvable" instead of rejecting the row.

## Schema changes

Stage B added none: every relationship above could already say "unlinked" (a null column, an empty ref list, a
missing jsonb key or a missing result row). Stage C (R1) adds one table, `rsi_attempts`
(`20261026171500_rsi_attempts.sql`), for experiment attempts.

## Experiment attempts (R1)

**What was wrong.** Until R1, `experiment_attempt_sessions` counted every `sessions` row with env `sandbox` or
`eval`. On 2026-10-26 all 909 unlinked rows were eval-harness sessions (agent-config's eval clones under
`~/.cache/agent-evals`), not attempts. The real attempts had no Supabase row at all: their identity lived in the
sandbox run log, its `.rsi.json` sidecar, the replay run record and the Langfuse trace. And a two-provider replay
gave the claude and the codex attempt of one case the same session id, because the id ignored the profile.

**Eligibility now.** An attempt is arm A (baseline) or B (candidate) of one case and repetition in a sandbox
replay. Not attempts, so not eligible: eval sessions (env `eval`, no experiment id; counted as
`excluded_eval_harness` and `excluded_eval_other`), and judge (J), check (K, `-chk`) and implementer (I) runs.

**One identity.** `(experiment_id, profile, arm, case, rep)`. Key: `refs.run_key(experiment_id, arm, case_key,
rep)` with `case_key = <profile>.<case>` for a provider-profile replay. `attempt_session_id =
refs.session_uuid(key)`. An unprofiled attempt keeps the id it always had, so existing Langfuse traces still match.

**At creation.** `rsi_replay` puts the identity in the run record (`session_id`, `profile`) and the sidecar
(`experiment_id`, `arm`, `profile`, `case` = case key, `attempt_session_id`). A subscription attempt has no
container, so the replay writes its sandbox run log itself (docs/rsi/replay.md, Evidence); its sidecar and its
Langfuse trace then carry the same identity. Nights before 2026-10-26 have no such log: their attempts were never
traced and are not backfilled. The night's experiment stage writes
one `rsi_attempts` row per attempt (`source = runner`) right after the replay returns. A failed write is a night
note, not a failed experiment.

**Classification (read-only).** `amem coverage links --classify [--sandbox DIR] [--json]` reads the RSI sandbox
dir and the database in one READ ONLY transaction and reports:

| Class | Meaning |
|---|---|
| `eval_harness_session` | env `eval`, no experiment id, project under the eval-harness paths: an eval case run |
| `sandbox_attempt` | an attempt with an explicit identity, with its sources (`on_disk`, `langfuse`, `supabase`) and whether `rsi_attempts` has it |
| `experiment_without_attempts` | an `rsi_experiments` row with no known attempt (for example it failed before the replay) |
| `ambiguous` | env `eval` outside the eval-harness paths, no experiment id |
| `unresolved` | attempt-like records without a full explicit identity, each with its reason |

It also counts the excluded runs (judge, check, implementer) and three divergences: `session_id_collision`
(a run record carries the old profile-less id), `langfuse_default_case` (a trace exported without a sidecar, so
its case and rep are the sink's defaults) and `experiment_row_missing` (attempts of an experiment with no
`rsi_experiments` row).

**Historical repair.** `amem coverage repair-links` is a dry run by default. It builds rows only from explicit
identifiers: the replay run record (arm, case, rep), the sandbox log (experiment, arm) with its sidecar (case,
rep), and Langfuse trace metadata (`rsi_evidence` metrics). Records of one run are merged by their exact run id;
a session is linked only when its id equals an `attempt_session_id`. A run whose records name no full identity,
or disagree, becomes `unresolved` with the reason. Start times are never compared: a sandbox session that started
next to an attempt stays unresolved.

- `--apply` writes in one transaction. It first saves an undo manifest (`~/.local/state/amem/repair-links/`,
  or `--manifest-dir`): every changed row's previous and new value. It never rewrites a runner row. It removes an
  `unresolved` repair row only when a later repair links the same run.
- `--undo <manifest>` restores those rows exactly (timestamps included). It refuses, and changes nothing, when a
  row changed after the repair.
- Applying again changes nothing and writes no manifest.
- A non-local database needs `--hosted-ok`. Applying to hosted data needs Vilmer's yes first.
