# Session and parent timing

Timing data lives in the existing agent-memory Supabase project. `execution_spans`
stores one small record per observed operation. Session and parent links keep the
existing identities. No prompt, reasoning text, command arguments or tool result
is stored in this table.

## Read the data

```sh
amem timing coverage
amem timing report --session 'codex:<thread-id>'
amem timing ingest
```

These commands use the configured Postgres backend. `report` returns JSON from
`session_parent_timing`; SQL clients can also query `session_timing_summary` and
`session_timing_coverage`. Access follows the existing owner policies. Timing
views use `security_invoker`, so their underlying row policies still apply.

`sessions.parent_session_id` is generated from the existing `meta.parent_session`.
It is indexed and readable without granting browser access to raw session metadata.
Events may arrive before their session row or parent operation. Their correlation
IDs are retained and join correctly once the related rows arrive.

## What a measurement means

- **Parent elapsed time:** its observed session window, counted once.
- **Observed wall time:** the union of completed non-workflow intervals. Nested
  and parallel operations cover that interval once.
- **Category time:** a union within each category. Categories can overlap; adding
  them does not produce parent elapsed time.
- **Worker time:** cumulative elapsed durations of descendants, reported separately.
  It can exceed parent elapsed time when workers run together.
- **Active time:** the existing transcript-gap estimate. It does not measure thinking.
- **Unclassified time:** session-window time not covered by completed observations.
  It can include work, waiting or idle time; the system does not guess which.

Each span records source, measurement kind, method version, status and optional
exit code. A local monotonic timer is `measured`; a provider's duration is
`provider_reported`; paired transcript boundaries are `observed`. Missing timing
is NULL, not zero. Interrupted/unclosed operations remain visible and do not gain
an invented completion time.

The legacy `sessions cost` numeric fields retain their existing contract for
budget consumers. New callers must use its nullable `timing` values and
`timing_measured_sessions` coverage. Text output uses the nullable values.

## Codex and Claude capture

Native Codex completed items expose tool start/end timestamps and sometimes
reported durations. The transcript ingester stores those facts. Model-output
item windows are stored as `model_output`; they are never substituted for API time.
Claude tool request/result pairs are observed intervals. Those intervals may
include approval latency; they are not a separate permission-wait measurement.

For new Codex API measurements, the local loopback collector accepts OTLP/JSON
`codex.api_request` and `codex.websocket_request` events. It retains duration,
status, correlation identifiers and a short measurement allowlist. Other events
and raw payload fields are discarded before disk persistence. User-prompt export
is disabled. Existing processes may need a new Codex session/process to load the
new exporter configuration; historical API timings cannot be reconstructed from
turn duration or token counts.

The collector binds only to `127.0.0.1:43927`, rejects browser-origin requests,
and acknowledges only after writing sanitized records to a private local spool.
It flushes to Supabase with resumable cursors. A database outage keeps the spool;
retries use stable span IDs and do not duplicate measurements. Start events cannot
downgrade an already completed operation.

Session hooks detach a sync using only transcript-path/session metadata. They
do not save the raw hook payload or block model work on database availability.
The main checkout loads its own database configuration; an inherited worker store
cannot redirect that writer.
Each sync request has a durable private queue file. Requests are removed only
after a successful database transaction; a request arriving during an older sync
cannot be erased by that sync. The collector retries pending requests.

## Parent workflow measurements

`parent-setup`, `parent-run` and `parent-integrate` record workflow/phase spans in
`~/.claude/parent/<repo>/timing-events.jsonl`. Labels cover readiness, canaries,
worktree creation/copy/setup, guard probes, full/targeted tests and cleanup.
`parent-setup verify` streams test output and prints phase durations while it runs.
The same durations appear in its verification report.

Measurements are best effort and do not change command exit codes, safety checks,
timeouts, cleanup, test selection or release receipts. Full logs remain in their
existing files; Supabase receives small counters and redacted measurement metadata.

## Local installation

After the new code and additive migration are deployed:

```sh
uv run --locked python scripts/install_timing.py
```

This installs the managed local collector, preserves existing hook groups, adds
session sync hooks, and adds a loopback-only Codex OTel exporter with prompt export
disabled. Backups stay in their original private configuration directories. An
existing different OTel configuration requires review; it is never silently replaced.
The collector must be healthy before the exporter or hooks are changed.

Check `http://127.0.0.1:43927/health` for counts and database flush status. Health
does not imply API measurements exist: coverage only increases when actual events
arrive. The installer does not start a separate CLI agent or make paid model calls.
