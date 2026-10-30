# Nightly evidence consumption

Provider delivery and useful evidence are separate checks. Nightly evidence uses
the existing limits, read budgets, relevance checks and untrusted-data envelope.

## Delta selection

The initial nightly delta keeps at most50 findings. Supported findings are
selected in a deterministic round-robin across stored evidence and live provider
sources. Unsupported context fills only spare slots. A provider is not forced
into the selection when it has no usable evidence.

`delta.finding_selection` records read, selected and `delta_limit` exclusions by
source. Coverage keeps the read denominator independent of selection. A finding
excluded by this limit is never counted as used. The stored-row query cursor
still advances over its read page; unread pages continue next night. Selection
exclusions are deliberate omissions, not a promise to replay those same rows.

This prevents a full page of stored records from silently displacing every fresh
Langfuse finding. The cap, source-authority checks and model evidence envelope
remain in place.

## past.dev references

past.dev selects related references. Its returned prose is not trusted as the
original explanation. For a reference about the candidate's rule or target, the
pack reads the current record from Supabase and applies the same ownership,
secret, deny-term and sanitization rules as the past.dev exporter.

The source lookup accepts at most20 references. Each source query has an indexed
ID predicate. A decision reads only its selected parent proposal. This does not
scan the full exported history for every candidate, make another past.dev call,
or increase the provider budget. The ordinary full exporter keeps its previous
behavior.

The finding's short summary retains bounded portions of the reason, result,
decision and hypothesis. Those fields still pass through the existing model
neutralizer. Missing, deleted, unrelated or withheld memories have no hydrated
summary and cannot gain supporting status from this lookup. Other-rule results
can remain reference-only context.

The additive `exclusion_reason` distinguishes `unrelated_context`, `unknown_scope`,
`missing_authority`, `stale_authority` and `authority_unavailable`. Privacy and
missing-memory hydration share `private_or_missing_memory`; the report does not
disclose private records to distinguish them. Existing status values remain
compatible. Resolved but unrelated context is not a failed provider connection.

## Existing lesson decisions

An `amem:run` memory supports a lesson only when it joins a real accepted or
rejected `lesson_candidates` row. Accepted decisions also need their lesson to
still exist. Scope comes from that lesson's exact `lessons:<slug>` key or an
explicit rule/target recorded on its originating run. An unrelated routine run
does not qualify merely because its metadata names the same rule.

This reads existing historical decisions only. It does not enable lesson
creation, distillation, injection, promotion, or `AMEM_DISTILL_LIVE`.

## Langfuse scores

The model evidence keeps validated counts for known `judge_winner` and
`eval_verdict` categories as scalar keys such as `judge_winner_A` and
`eval_verdict_no_effect`. Negative, non-integer, unknown or oversized counts are
excluded. Arbitrary nested objects remain excluded. Pass counts and rates
continue to use the existing scalar path.

The changes preserve evidence, not causal proof that a provider improved a
decision. A full scheduled night remains a separate runtime verification.

## Exact section contracts

Fresh hypothesis generation receives complete parser-defined section payloads
and SHA-256 hashes. The matched candidate section gets prompt space before the
general file view. If it cannot fit completely, generation defers before a model
call. Code binds the selected hash back to the exact displayed `before` text.
Persisted hypotheses keep their existing before/after format.

The writer still checks current bytes against that contract. Markdown separators
and neighbouring section bytes remain protected. An omitted separator is a
malformed contract, not evidence that somebody edited the live file. Real prose
drift still fails. Old pending hypotheses are not silently repaired or rebased.

## Nightly ClickHouse readiness

Configured, permitted ClickHouse Cloud reads establish authenticated readiness
separately from analytical query time. The readiness phase has the existing
bounded120-second Cloud allowance, after query policy and privacy checks. The
analytical deadline remains unchanged (the initial delta normally shares20s).
Local or unconfigured clients receive no extra wake allowance.

Readiness is cached within one evidence runner and reused across its phases;
separate delta/candidate runners establish their own bounded readiness. Requests
remain metered and sequential within a provider. Authentication/TLS failures do
not receive transient retries. A genuinely slow analytical read still fails its
query deadline.

Saved connection checks retain `phase`, elapsed time, safe `failure_kind` and
`budget_s` when available. They distinguish readiness failure from a query
failure without copying provider response text or credentials. This is not a
guarantee of Cloud availability and does not change automatic idling settings.
