# RSI reliability repairs — 28 October 2026

These changes address the saved paid manual run without repeating it. Installed schedules, active config, model/effort selection, budget caps, minimum experiment policy and human approval gates remain unchanged. No old check score, experiment verdict, instruction or production link is rewritten.

## Check contracts and coverage

Future sandbox-replay records use suite version4 and new parent-policy-v4 checker/receipts. Historical V1/V2/V3 fixtures and the V3 checker remain. Parent ownership now explicitly means end-to-end accountability while workers may implement. Safe extra proof steps are accepted, but omitted/reordered prerequisites, host-denial workarounds, force/admin publication, wrong branches and invented success still fail. Missing profiles retain unknown targets unless explicit defaults are supplied. Announcements are graded only when a receipt actually supplies that obligation. Possible verification is separate from successful proof. A bounded merge/cleanup task checks exact commit/configuration evidence, gate failures, publication confirmation and receipt-backed skips.

Cases declare section IDs. New writer artifacts carry the matching section in their existing rule_id field (`rsi-section-...`). Only cases declaring that section qualify as intervention coverage. Other cases cannot supply its minimum pairs. Planning uses those same section groups, preserving the existing pair and provider caps. Offline decision tasks establish decision evidence, not actual execution of host utilities or profile reading before a live first reply. Exact file reads establish exposure only.

## Exact writer intervention

Live writer-mode hypotheses require edit={section_id,before,after}. The model that proposes the hypothesis supplies the exact before/after section; the writer cannot independently add, omit, paraphrase or relocate it. The baseline must match, the original heading must survive, existing proposal/content guards must pass, and explicit placement in another heading is rejected. Final file bytes must equal the validated declared replacement. Only that exact comparison sets contract_verified.

Legacy unbound pending hypotheses remain retained as unbound evidence and are refreshed through the existing hypothesis call within its existing budget. They are not silently treated as exact contracts. A missing contract or uncovered section defers before consuming a replay slot. Known file/rule/section pins are checked before paid triage/hypothesis work and again before writing. Writer errors/drift and staging refusal no longer stage or replay a stub fallback. Legacy low-level diagnostic fallback reasons remain readable, but they cannot become paid experiment evidence.

## Connection recovery and export

ClickHouse attempts reserve sequential recovery slices within the same20-second provider deadline. A full first-attempt timeout therefore leaves time to recover. No replacement provider thread starts after an uncooperative call times out. Langfuse keeps its full existing paginated-read/pacing allowance. Authentication, policy exclusions, TLS failures, rate limits, Retry-After, paid recall and model calls retain their no-auto-retry boundaries.

After current replay evaluation ingestion, the existing Langfuse sink receives only that experiment's native logs, bounded by min(90seconds, remaining night wall time). This does not replay a model or run a bulk backfill. It preserves the ordinary daily sandbox_runs watermark while sharing existing daily caps and privacy/kill switches. Same-sink syncs serialize through an advisory lock. Existing checkpoint/evidence receipts distinguish pending, confirmed and observed trace delivery and prevent automatic span resend after an uncertain response. A failed later score write does not erase an acknowledged span receipt. Active caller transactions are refused by the scoped export helper; Night uses autocommit.

Export delivery and read-side visibility remain separate: delivered_rows/confirmed_trace_uploads do not manufacture visible_rows. Read-back is not asserted without observed evidence. Export failures/pending work appear in the experiment report/metadata and degrade availability while independent experiments continue. No new table, background routine or service is introduced.

## Correct interpretation of external links

The six past.dev rows in the manual run had resolved authorities about other rules/targets. Their exclusion was correct; they were not missing-authority records that should be guessed or backfilled. Future diagnostics distinguish unrelated context, unknown scope binding, absent subject identity and stale recorded references. Production observations still require current instruction-version/coverage validation. Privacy exclusions stay exclusions. Codex WebSocket send timing remains transport; missing session-bearing inference duration stays unknown.

## Validation boundary

All new verification uses deterministic writers, local fixtures, fake clocks/HTTP services and disposable PostgreSQL. Regression checks cover exact edit success/drift, stale and cross-section contracts, pins before model work, no replay of refused writers, semantic positives/unsafe negatives, section-specific planning, recovery deadline/no overlap, scoped→repeat→daily export isolation, ambiguous upload outcomes and delivery-versus-visibility reporting. Version4 Jev fixtures are explicitly canned wiring fixtures, not new paid evaluations. The complete clean-commit integration gate is recorded in the release receipt. Natural scheduled runtime acceptance remains unverified until tonight executes.
