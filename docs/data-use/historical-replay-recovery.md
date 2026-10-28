# Historical replay recovery

The daily telemetry report for 2026-10-27 recorded 36 generic `ValueError` failures and six unresolved native identities. The source bug was a profile-format assumption, not 36 corrupt replay executions.

## Root cause

The three provider-profile replays from night 2026-10-25 wrote files such as:

`replays/exp-20261025-93b69041/claude/runs/exp-20261025-93b69041-claude-anatomy-plan-A-r0.json`

Their per-run records omit `profile`. The owning `replay.json` records `config.profile=claude`, and the directory and exact run ID independently contain that profile. The collector previously created an unprofiled attempt from the absent row field, then rejected it because the directory was profiled.

The historical `session_id` values are deterministic, profile-less logical attempt UUIDs. Claude and Codex records for a case/repetition share those old UUIDs. They are not provider-native session IDs. The recovery must not use them to manufacture native sessions.

## Change

`replay_evidence.attempt_record` validates the experiment directory, replay configuration, provider, case, arm, repetition, exact run ID and filename together. It recovers an omitted legacy profile only when config, directory and run ID agree. An explicitly conflicting row/config profile, provider, experiment or unrelated logical UUID is rejected.

Current canonical logical IDs and the known profile-less legacy UUID construction are both recognized. Only the canonical experiment/profile/case/arm/repetition identity is written to the attempt contract. The historical logical UUID is never substituted for `native_session_id`. Missing native IDs and start timestamps remain missing.

Diagnostics now include a stable reason code and relative record locator. Detail strings are redacted and capped at 50 entries; total failure counts and per-code counts remain complete, with an explicit omitted-detail count. Judge/check/implementer records remain excluded from A/B attempt counts.

Late historical records cannot downgrade an existing trusted native identity. A native link is counted only when its stored trusted-client identity resolves to a sandbox session for the same experiment and attempt. Reconciliation also checks the session's explicit attempt reference, preventing a rejected same-experiment actor collision from reappearing as a link later. It does not relabel production sessions or infer actors from nearby clocks.

## Read-only inventory of actual saved records

The new normalizer was run against the actual files, without database or model calls and without rewriting them:

| Saved experiment | Records | Profiles recovered | Recorded native IDs | Recorded start clocks |
|---|---:|---:|---:|---:|
| exp-20261025-95f15ef6 | 12 | 12 | 0 | 0 |
| exp-20261025-63a57bf4 | 12 | 12 | 0 | 0 |
| exp-20261025-93b69041 | 12 | 12 | 0 | 0 |
| exp-20261024-9fc4ddc3 | 6 | 0 (unprofiled) | 0 | 0 |
| Total | 42 | 36 | 0 | 0 |

All 42 exact attempt identities validate. None of the inspected records, replay metadata or corresponding runtime evidence contains a native client ID. The older subscription attempts have no attempt run logs or sidecars from which such IDs could be recovered; their retained logs are check runs. No native identity or timestamp was recovered by guessing. The native-link gap for these records therefore remains unresolved unless another explicit trusted source is later supplied.

## Verification

Deterministic disposable-Postgres fixtures reproduce both provider directories and the old profile-less UUID collisions. Tests cover exact profile/config/run-ID disagreement, old unprofiled records, current native-ID records, idempotency, stronger existing evidence, native actor collisions, symlink/invalid input rejection and bounded diagnostics. No hosted repair/backfill or nightly replay was executed.

The source fix makes the 36 generic ingestion errors avoidable on the next authorized collection. It does not claim to close 42 irrecoverable historical native-session links.
