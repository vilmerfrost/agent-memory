"""Connected-intelligence integrations (Mission 004A; design: the Mission 002 blueprint, section C).

Supabase stays the only hub. Each external system has one adapter here, and every adapter speaks the
same three things:

- `refs`: one evidence-ref grammar (`amem:`, `ch:`, `lf:`, `past:`, `ncm:`) and the deterministic ids
  (trace id, session uuid) that let a record be found again in every system;
- `evidence`: `EvidenceRef`, the `Retriever` protocol and the query facade (Supabase first, optional
  retrievers after; a retriever that is down answers `unavailable` and never blocks);
- `sync`: the sink loop (health check, budget, a cursor in `rsi_checkpoints`, links in `rsi_evidence`).

No new tables: cursors live in `rsi_checkpoints` (job `sync:<sink>`) and external links in `rsi_evidence`
(unique `(system, external_ref)`), both from migration 20261022010000_rsi_core.sql. Nothing in this package
is wired into a routine; activation is a separate, approved step (docs/integrations/activation.md).
"""
