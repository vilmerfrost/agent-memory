# Internal identifiers and targeted evidence recovery

The private project name `repo` collided with the word `report` in the shipped
`worker-final-report` case and `agent:worker:final-report` rule. These are internal
identifiers, not references to the private project. Do not remove `repo` from the
deny list or reclassify the unknown project as owned.

The exception is confined to exact identifiers derived from the committed replay
catalog under `bench/rsi-replay/cases`. Environment-selected catalogs are not used.
Global rule keys must come from known global target paths and catalog sections.
Replay cases must be known catalog IDs; a provider-qualified case and generated
run ID must agree exactly on experiment, provider, case, arm and repetition.

Within those verified identifiers, derived private names match complete lexical
words. `repo` still blocks `parent-repo-profile`, but does not block `report`.
Unknown identifiers and arbitrary text retain substring matching. Explicit
`AMEM_EGRESS_DENY` terms always retain substring matching, even for catalog IDs.
Secret, email, path, environment, scope and ownership checks remain unchanged.

Only typed `rule` parameters for the named rule queries receive this treatment.
Past.dev recall must equal the words derived from the verified subject rule.
Arbitrary query text and recall requests without a known subject remain blocked.

ClickHouse's mapper applies the same treatment to catalog-verified global
`instruction_source`/`rule_id` pairs, but only when the caller supplies separate
derived-name provenance. The sink loads explicit and derived terms separately;
ordinary `map_row(..., deny=...)` callers remain fully strict. Other labels and
IDs keep substring checks, and repo-scoped team/private rule IDs remain null.
This fixes future projections. It does not backfill historical ClickHouse rows.

## Status meaning

Policy-excluded queries remain visible as coverage gaps and `excluded` recovery
checks. They do not become transport failure reasons. Scoped Langfuse export
results retain their status and add `issue_kind`, `local_refusals`,
`retry_selected`, and `already_confirmed_skipped`. Local refusals do not change a
previously healthy service into `unavailable`; the night may still be partial
because its exported data is incomplete. Upload receipts never imply verified
readback. Genuine failed reads, uploads and uncertain delivery remain failures.

Brief rendering can explain historical local-refusal counts without modifying
the original report's health or experiment verdicts. A successful recovery is new
evidence, not a rewritten successful night.

## Retry exactly the blocked traces

Use `integrations.langfuse.export_experiment` with the original experiment ID and
`retry_run_ids=frozenset({...})`, containing only the exact refused log stems.
The root must first inspect the local validator's outcomes and preserve that
selection as its retry receipt. No model inference or experimental run occurs.

The selected logs must exist, be agent observations, and pass the same privacy,
identity and session-environment checks. The retry has its own cursor; ordinary
daily and experiment export watermarks stay unchanged. Existing confirmed upload
receipts and trace evidence suppress all resend work, including scores. Unknown
upload outcomes retain the existing cautious readback/deduplication behavior.
Existing sink locks, daily limits, time budgets and kill switches apply.

Afterward record selected IDs, delivered rows, refused rows, prior-confirmed
skips, confirmation counts and readback status separately from the old night.
Do not reset the night checkpoint or run the night again.

## Retry exactly the blocked queries

Build the normal policy-gated provider set with the candidate rule as
`subject_rule`. Call only the blocked named queries through its `ch` and `pd`
retrievers. For the recorded final-report rule these are `rule_gap` with
`rule/days/min_n/limit`, `rule_weekly_rate` with `rule/weeks/limit`, and `recall`
with the exact `_recall_query(rule)` and `limit`. Preserve the original query
parameters and capture the new result or error. These clients are read-only.
Do not use the broad evidence probe or a whole night to retry these few reads.
