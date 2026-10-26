# Relationship contract

Stage B inventory of the links the data-use plan needs (plan section 5, "Relationships"). Each relationship
uses an existing column. Nothing is back-filled and nothing is assigned by guess: an event with no link stays
unlinked and visible. Repairs and reconciliation are Stage C.

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
| `experiment_attempt_sessions` | experiments and attempts | `sessions` with env `sandbox`/`eval` or an `experiment_id` | `sessions.experiment_id` | `rsi_experiments.id` | no | sandbox/eval session with `experiment_id` null |
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

None. Every relationship above can already say "unlinked" (a null column, an empty ref list, a missing jsonb
key or a missing result row), so Stage B adds no link column. Experiment attempts have no table of their own:
an attempt is a sandbox run log (`experiments/<id>/logs/<run_id>.json`) whose deterministic ids
(`refs.run_key`, `refs.trace_id`, `refs.session_uuid`) reach Supabase only through `sessions.experiment_id`
and Langfuse trace evidence. If Stage C needs attempt-level reconciliation, an attempt table is a decision for
that stage.
