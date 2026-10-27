# Session and parent timing

## Integrity follow-up (2026-10-26)

API attribution reads explicit thread/conversation IDs from resource, scope and record attributes. Conflicting or malformed IDs remain unassigned with a reason. Native request/trace identity is independent of actor/duration corrections, so exact late attribution updates one measurement rather than adding a second duration. A known actor cannot be reassigned. Without an explicit native ID, request time or the collector's environment never supplies one. The old sanitized payloads cannot recover discarded identity.

RSI logical attempt UUIDs remain distinct from actual client session/thread IDs. Trusted Claude init/result and Codex thread/start responses carry native identity into replay records and sidecars. A native-session observation is explicitly sandbox-scoped, with zero transcript bytes and unverified task acceptance; it is not a fabricated full transcript. Missing historical client IDs remain unknown. Later real transcript ingestion preserves the proven experimental scope and fills the transcript-availability fact.

The night and existing daily owner now ingest verified artifact/evaluation records for non-improving as well as improving replays. Artifact, baseline and experiment bindings must match. Raw fallback artifact verdicts remain artifact-only context, not evidence that the intended candidate improved. Existing immutable records are not rewritten into passing results.

`amem timing analyze --days 7 --limit 5` reads a bounded deterministic report. The existing daily routine writes the same analysis beside other reports; the morning brief reads a recent bounded summary. It flags expensive captured operations, slow/setup-heavy test observations, repeated tests for review, parent unclassified time, missing links, stale checkpoints and completion/test contradictions. Each lead cites evidence and limitations. Repetition is not proved unnecessary; cumulative time is not wall time; stored rows are not the whole eligible source population. No new routine, model call or timing table is added.

Night coverage also requires `experiments:replay-evidence` and `experiments:native-attempt-sessions` when the new report supplies them. Completed replays/provider run counts define expectations: an empty evaluation/attempt table cannot masquerade as no eligible work. New consumption gaps make the morning conclusion incomplete/findings, not an all clear. Historical reports stay compatible.

Timing data lives in the existing agent-memory Supabase project. `execution_spans`
stores one small record per observed operation. Session and parent links keep the
existing identities. No prompt, reasoning text, command arguments or tool result
is stored in this table.

## Read the data

```sh
cd ~/Projects/agent-memory
uv run --locked amem timing coverage
uv run --locked amem timing report --session 'codex:<thread-id>'
uv run --locked amem timing ingest
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

An unset API event clock remains unknown. The parser uses the provider's observed
timestamp when available, and records that clock source. If both clocks are absent,
it keeps the reported duration with NULL interval bounds. Events without a session
identifier remain unassigned; receiving an API event alone does not prove that a
session has API timing coverage.

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

## Individual test timings

The integration runner saves one structured `.timings.json` batch beside its full
pytest log. Each test has a stable key, name, outcome, worker ID, and separate
setup, call and teardown durations in milliseconds. Missing phases stay NULL;
setup failures and teardown failures remain errors. Incomplete tests are marked.
Parameterized values are replaced by a stable hash so fixture data does not enter
the database. Tracebacks, captured output and command arguments stay out of it.

The batch has a unique run ID, repository, commit, dirty flag, worker count,
session ID and parent command span ID where provided by `parent-run`. It records
the actual pytest exit code, collection/worker error counts and time until
collection is ready. That last value includes worker startup and collection.
Default runs are marked `full`; runs with extra pytest arguments are
conservatively marked `custom`.

Under `parent-run`, the runner atomically queues one batch in
`~/.claude/parent/<repo>/test-timing-queue`. It does not connect to a database.
The existing collector imports each run in one transaction and only removes the
queue file after commit. Replay does not duplicate rows. Database failures keep
the batch queued and never change pytest's exit code. A direct runner invocation
without parent context saves the local artifact but does not enqueue an upload.

`test_timing_runs` keeps summaries and log filenames. `test_timing_cases` keeps
individual results for 30 days. The collector checks retention hourly while it
runs; cleanup resumes after downtime. Expired batches cannot recreate deleted
details. Local full logs and timing artifacts follow the existing local log
retention policy; database cleanup does not delete those files.

Retrieve the slowest tests from the latest run, a particular run, or a session:

```sh
uv run --locked amem timing tests
uv run --locked amem timing tests --run '<run-id>' --limit 10
uv run --locked amem timing tests --session 'codex:<thread-id>'
```

To compare a test across recent runs, filter by repository, test key and comparable
worker counts, selection and dirty flags. For example:

```sql
select r.run_id,r.head,r.ended_at,r.workers,r.selection,r.dirty,
       c.nodeid,c.setup_ms,c.call_ms,c.teardown_ms,c.total_ms,c.outcome
from public.test_timing_cases c join public.test_timing_runs r using(run_id)
where r.repo = 'agent-memory' and c.test_key = '<test-key>'
order by r.ended_at desc;
```

Per-test durations are cumulative work, not run wall time. Parallel tests overlap.
Shared fixture setup is charged to the test that triggered it. Collection, worker
startup and runner overhead do not belong to individual test bodies. Machine load
can affect measurements. These results locate delays, but do not alone prove the
cause of a regression. Access uses the existing owner-only read policy.

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


## Native API scope after the 2026-10-27 scheduled check

`codex.api_request` also names process-wide `/models` catalog requests. Those have no conversation ID or model. The collector preserves them as `unclassified` client-control observations with `api_scope=client_control`; they are excluded from task-session eligibility and inference time, not assigned to the collector's actor. Unknown endpoint/no-ID events remain genuine attribution gaps. Historical rows lack the discarded endpoint, so this does not retroactively relabel them.

Timing method 3 also recognizes native `http.response.status_code` and timezone-qualified `event.timestamp`, retains explicit conversation IDs, and reports separate accepted/session/unattributed/control counters in health. The legacy `accepted_api_spans` counter counts API observations; `accepted_spans` counts all accepted timing records, including control traffic. No raw payload, prompt, auth field or unknown URL is persisted.

The isolated nightly Codex client must explicitly route its native OTel logs to the existing loopback receiver; its dedicated auth home does not inherit the interactive user's exporter settings. Prompts remain disabled. Do not substitute client wall time or generated-token timing for missing API measurements. Natural inference capture still needs the next scheduled run; tests and a metadata-only `model/list` request are not a paid inference run.
