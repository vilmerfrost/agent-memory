# Bounded connection recovery and isolated failures

The existing evidence collector already isolates optional sources. This change adds at most two retries for temporary ClickHouse and Langfuse evidence reads. Health and query reads use the same original provider deadline; retries never extend it. A provider thread that exceeds that deadline is marked dead and is not replaced with an overlapping retry thread. The checks run in S1 before paid triage and again when S4 needs candidate evidence. There is no new service or installed schedule.

Only recognized transport failures and HTTP 500/502/503/504 are retryable. Authentication, policy/configuration problems, malformed data, rate limits and responses with Retry-After are not automatically retried. past.dev recall and model calls are never retried by this mechanism. No credentials are refreshed, services restarted, permissions changed or agent repairs invoked. Those actions have no approved executable recovery handlers yet.

A missing optional source leaves its query evidence unavailable while other sources and supported experiments continue. If an evidence pack fails, or an optional source is unhealthy, a candidate must have a currently readable authoritative reference or an OK pack finding bound to authority before hypothesis generation proceeds. Otherwise only that candidate is deferred, without consuming an experiment slot. Existing candidate references are checked through the existing indexed authority lookup; missing records never count as proof.

A missing sandbox now preserves deterministic collection and candidate bookkeeping, while suppressing paid triage and experiments. Candidate rows return to deferred. The night remains partial and its failure diagnostic names the sandbox. Required cross-provider comparisons are never silently narrowed. Database access, valid configuration, the run lock, budgets and configured model credential prerequisites retain their safety checks. In particular, missing required model credentials still stop the scheduled job before starting the investigator; this change does not provide a separate credential-free collection mode for that case.

## Execution evidence

New reports contain optional `connection_recovery`, format `rsi-connection-recovery/1`: fixed policy, capability dependencies and bounded per-step checks. Each records source, stage, step, final recovery state, actual completed attempt count when known, timestamp, elapsed milliseconds when known, fixed reason and affected work. A timed-out/incomplete observation keeps unknown count and duration. Retried failures remain recovered only if the actual read succeeds. A later failure is retained independently. Readiness is not inferred from a model's `live` configuration.

Health and coverage retain failures, unlinked data, missing measurements and policy gaps. Policy exclusions in both delta and packs do not become service outages. Legacy reports are not backfilled, rescored or rewritten. The morning brief includes the same recorded recovery evidence; NightDesk displays historical evidence separately from its current Connections checks. Existing JSON storage carries the additive fields: no schema migration or competing timing table.

## Validation boundary

Deterministic tests cover transport recovery, persistent failure, three-attempt ceiling, shared deadline exhaustion, no overlapping retries, failed health recheck, authentication and rate-limit refusal, Retry-After, policy before network, no past.dev retry, missing candidate authority, later failure preservation and contract validation. Existing outage integration coverage proves supported experiments continue; sandbox outage tests use the real Docker preflight with an unreachable socket and require zero model calls, durable candidate collection and released locks.

No paid replay, extra scheduled run, automatic promotion, hosted backfill, privacy change or budget increase is used for verification. Natural scheduled runtime acceptance remains unverified until a run completes.
