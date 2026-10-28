# RSI integrity implementation plan — 2026-10-26

Authority: implement, test and commit. The initial no-push instruction was superseded by Vilmer: "push them to main when ready so your changes go live". Publish after the clean integration gate through the normal protected-branch workflow, update the live main checkout and reload the affected timing collector. Preserve the frozen/gated promotion loop, existing cash cap and stronger models. No full paid night/replay, provider write probe, manual production backfill or unrelated configuration change. Publication does not establish future scheduled success.

Baseline: integration 7616f84, identical tree to main 1dd94a5 (PR #70 attempt logs). Existing unrelated main .gitignore/docs/ideas work is preserved. Inputs are the full source recon and live addendum under ~/.local/state/recon/runs/agent-memory-d623eb54/.

## Work and ownership

1. **Budget and writer (P0, budget worker).** Reconstruct latest saved night from reports and call ledger. Distinguish USD configuration from the user's EUR description; do not assume exchange rates. Prove which limit stopped each entry before altering limits. Keep deliberate experiment/call/token/wall ceilings. Fix reservations atomically if missing, release only completed/cancelled reservations, preserve unknown paid usage conservatively, record reproducible limiting-resource snapshots. Independently test current writer candidate→one-section change→A/B bytes. Never count fallback evidence as proof of the intended intervention.
2. **Confirmed outcomes and retained attempts (P0/P1, outcomes worker).** Outcome semantics version 2 counts success from exact result evidence, not invocation. Preserve legacy stored semantics without automatic data rewrite. Calibrate about 30 explicit dispatch sessions against recorded operation evidence; distinguish model/deterministic review from human ground truth. Preserve task/repository/producer/attempt identity, retry/resume relations only when explicit, source timestamps and earlier failed attempts in runs. Root wires shared Codex/session adapters.
3. **Attempt/session and eval ingestion (P1, root).** Preserve logical attempt UUIDs separately from actual native client identities. Capture native Claude session/Codex thread identity from trusted runtime responses and durable sidecars. Link exact identities only; legacy attempts without native/transcript evidence remain unresolved. Ingest verified artifact/evaluation records for all completed replay verdicts, not only improving proposals. Retain immutable evidence and reject experiment/baseline/hash mismatches. Integrate bounded recovery into the existing daily owner; do not run a historical hosted repair.
4. **API attribution (P1, root).** Accept explicit supported OTLP identity locations/aliases, reject conflicting identities and record missing reasons. Keep durations and clocks distinct; no proximity matching or model-output substitution. Preserve stable source event identity and safe late correlation without reassignment of a known actor. Test resource/scope/record identities, conflict/missing paths, late arrival and replay.
5. **Telemetry and coverage (P1, root).** One bounded deterministic analysis owned by an existing report: operation cost, test setup/slow/repeat observations, orchestration intervals, broken links, stale/capture gaps and result contradictions. Every finding carries exact refs and explicit stored/eligible limitations. Record source windows/funnels in existing coverage tables; collection failures remain gaps. Reuse timing union views. No duplicate timing table or paid analysis calls.
6. **Advisory production feedback (P1, feedback worker).** Append idempotent versioned outcome observations to existing rsi_evidence while keeping rsi_outcomes as latest projection. Preserve exact change/rule/text version, cohort/window/coverage/authority and expiry. Only validated new evidence generations can justify bounded reconsideration. Night consumes advisory context, never authorization. Positive/negative/stale/version mismatch/incomplete cases get focused tests.
7. **Verification pilot (P2, root).** Inspect one repository and one supported browser surface. Select 20–30 real candidate verification episodes where available; do not relabel log events as independent task attempts or fabricate missing criteria/build IDs. Bind expectations and observations when available. Incomplete identity is unknown. Use existing evidence for the pilot and justify any later schema only with measured marginal value; avoid new table until that decision is supported.
8. **Integration/readiness (root).** Merge exact local worker commits into managed integration, wire shared seams once, run targeted cross-module checks, commit, then one final parent-integrate test. Inspect installed schedules, executable/config paths, prerequisite semantics, model/effort and costs using read-only/nonbillable preflight. No manual costly run. Report per-stage READY/BLOCKED/UNVERIFIED. OBSERVED SUCCESS requires an actual future scheduled completion.

## Seams

- Budget worker exclusively owns rsi_night.py and temporary rsi_brief.py status edits until its commit. Root wires eval/attempt observations, advisory feedback and deterministic report sections afterward.
- Outcomes worker owns amem_digest.py and identical tools/amem_digest.py plus tasks_ingest.py. Root owns codex_sessions.py and sessions_ingest.py adapter/version wiring.
- Feedback worker owns rsi_outcome.py, rsi_evidence_pack.py and production_feedback helper. Root owns daily/report registration, root telemetry/pilot and API/replay/session linkage modules.
- No worker pushes, changes main/live schedules, reads secrets, runs paid models, creates other agents or edits siblings' work. Focused tests run through parent-run in verified isolated worktrees. No worker full-suite runs; root runs the final integration gate once after integration.

## Acceptance evidence

| Requirement | Evidence required |
|---|---|
| Budget skips | Per-entry ledger with exact branch/resource and recoverable cash values; YES/NO/NOT PROVEN cash-skip answer; deterministic reservation/concurrency/retry/unknown-cost tests |
| Writer | Representative legitimate rule-key-less candidate changes intended section, A/B exact bytes differ, fallback separately labeled and non-promotable as intended evidence |
| Outcome labels | Exact call/result correlation; failed/denied operations cannot produce success; commit proof preserved; legacy method remains explicit; calibration denominator and changed counts |
| Task attempts | Distinct explicit attempts survive re-ingest/out-of-order input and multiple repositories; absent identity/time stays unknown |
| Evidence links | Native/logical IDs distinct, all replay verdicts can be ingested idempotently; artifact/experiment/baseline mismatches rejected; no historical fabricated session links |
| API | Explicit identity attribution and conflict/missing reasons; no guessed IDs, no double counting/reassignment; duration-only events remain nullable intervals |
| Analysis/coverage | Bounded nonempty fixtures, evidence refs and denominators; failure/empty/partial/stale cases distinct; cumulative time is not wall time |
| Feedback | Historical observations retained; exact versions and uncertainty respected; unchanged negative evidence is not reopened repeatedly; approval paths untouched |
| Verification | Real pilot population, coverage/unknowns, measured local overhead/storage, justified table decision |
| Tonight | Installed dependency chain checked; local commits vs live main distinguished; future execution not claimed |

The integrated report will retain any incomplete criterion rather than collapse these into a general PASS. No schema migration, hosted reconciliation or publication is implicit in local test success.
