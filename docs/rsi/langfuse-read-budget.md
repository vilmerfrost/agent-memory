# Bounded Langfuse evidence reads

## Observed failure and limits

The saved night `2026-10-26` is partial. Its recorded reason is
`lf.experiment_scores: error (timeout after 20s)`. The provider ledger records two
logical Langfuse request attempts. It does not identify which HTTP attempt,
throttle wait or retry consumed the historical deadline. Therefore the exact
remote cause of that occurrence is unknown.

Two source defects were independently reproduced with deterministic fixtures:

1. Without a dataset, the query fetched roots and then queried scores once per
   trace. Twelve selected traces required 15 calls including health and dataset
   lookup. At the existing 2.1-second Cloud spacing, the request starts alone need
   at least 29.4 seconds. A 20-second provider budget cannot complete that path.
2. The client allowed a fresh 10-second timeout and two retries for every call.
   Throttle waits, retry waits and subsequent pages did not share the outer
   deadline. The outer thread timeout could return while its abandoned read
   continued making requests. Pagination caps also returned partial rows as an
   apparently complete list.

These facts explain why the old path was not safely bounded. They do not prove
that all 12 traces, or all those retries, were reached in the saved failed night.

## Change

```mermaid
flowchart LR
  H[Health and dataset lookup] --> R[Bounded root observations]
  R --> S[One paged bulk score query]
  S --> B[Bind scores to exact selected roots]
  B --> A[Per-arm aggregates]
```

The dataset-less path keeps the existing 25-trace selection cap. It asks Scores
v3 for `traceId=id1,id2,...&fields=subject`, inside explicit timestamp bounds.
Core name/value/type fields remain included. A trace subject identifies its trace
by `id`; an observation subject supplies `traceId`. Session/experiment subjects
do not silently become trace check results. A known score with no binding to a
selected trace fails as `IncompleteRead`. Legacy responses with an explicit
`traceId` remain readable.

The official API contract was checked against
[Langfuse's public API documentation](https://langfuse.com/docs/api-and-data-platform/features/public-api)
on 2026-10-27. It documents traceId OR values, subject response fields and inclusive
fromTimestamp/exclusive toTimestamp boundaries. No new endpoint or write API is
introduced.

`LangfuseClient.read_budget(seconds)` uses a thread-local monotonic deadline.
Nested contexts retain the earlier deadline. It includes throttle waits and all
HTTP pages, clamps each socket attempt to remaining time, and turns retries off
inside that read context. No new request starts after expiry. Late responses are
not accepted as complete on-time evidence. A context refuses non-GET operations.
Normal sink upload behavior outside a read context is unchanged.

`LangfuseRetriever.query()` has a standalone 20-second read budget. The outer
evidence runner can wrap health and all provider queries in
`LangfuseRetriever.read_budget(remaining_seconds)` so several queries share its
actual remaining budget. This integration preserves the existing outer limit;
it does not increase the timeout or add retries.

Paged aggregate reads fail on a page cap with an unread cursor, repeated/invalid
cursors, malformed pages or malformed successful dataset lookup. A dataset is
absent only when its lookup actually returns 404. An intentional one-row trace
existence check can stop after one row; it does not claim to aggregate a complete
trace. The explicit selected-trace cap still returns `truncated=true` and
`population_complete=false`. Missing check scores have a zero *observed-score
count* and an absent pass-rate value, not a measured zero pass rate.

## Verification

Fixtures replace HTTP and use a controllable monotonic clock. They cover:

- Twelve traces and current trace/observation subjects in one score request.
- Complete multipage score responses with explicit cursor progression.
- Throttle, health and pages under a shared deadline; late pages and no requests
  after expiry; nested budgets that cannot extend the parent budget.
- Retry suppression only inside the bounded read context.
- Explicit trace-selection truncation, missing measurements, unbound subjects,
  malformed dataset responses and unread/repeated cursors.
- Intentional one-row existence probes and refusal to write through a read budget.
- Existing Langfuse export, privacy, score aggregation, local round trips and
  Cloud throttle/window checks.

In the deterministic 12-trace fixture, the old shape requires 15 logical calls;
the new shape makes four: health, dataset lookup, roots and one score page. At
2.1-second spacing with 0.1 seconds of fixture latency per call, it completes in
6.4 seconds. This is a fixture measurement, not a live-provider latency claim.

The worker made no live network calls, uploads, database writes or paid calls.
The specific historical server timeout remains unproven. Live diagnostic reads
and the final shared-runner integration are parent verification steps.

The existing read-only provider meter increments before the client call. A
deadline refusal before HTTP can therefore count as a logical request attempt.
Those counters must not be described as exact wire-level requests or charges.


## Streaming transport deadline

A review reproduced a slow-body loophole: urllib's socket timeout measures inactivity, so six body chunks40msapart kept a100msread alive for234ms. The optional HTTP total deadline now shuts down active sockets, including TLS sockets, during headers or response bodies. Late or truncated data cannot become successful evidence. Focused loopback fixtures cover dripping normal/error bodies, chunked transfer and headers, no retries after the deadline, and prompt handler completion. The old non-budgeted upload path is unchanged.

The OS DNS resolver can block before an active socket exists; it is not cancellable by this stdlib socket watchdog. When it returns, an expired connection is refused before request transmission. The existing outer provider deadline still bounds caller waiting. This limit is explicit rather than a claim of universal OS-operation cancellation.
