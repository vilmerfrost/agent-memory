# Nightly evidence consumption

Provider delivery and useful evidence are separate checks. Nightly evidence uses
the existing limits, read budgets, relevance checks and untrusted-data envelope.

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
