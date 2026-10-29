# Delivery, settlement and recovery

Service access, accepted uploads, completed processing and verified readback are
separate facts. An accessible provider is not proof of complete exported data.

## Shared export result

`ok` means no outstanding work known to the current scope. `partial` includes
queued processing, stored retry debt, dead letters and rejected records. `capped`
retains its cap reason. `unavailable` describes a failed service-access check.
`last_ok_at` advances only for `ok`, not partial or capped runs. Existing historical
timestamps are retained; they are not retroactively certified as complete.

Retries can use a per-record `Batch.failure_key` without changing the cursor key.
A failed record does not block healthy siblings. Sinks must revisit unfinished
records behind an advanced cursor. Stored failures remain visible even when a
later run attempts no new writes. A sink checkpoint hook commits scheduling
progress with its delivered batch and cursor.

## Langfuse

Span phase and score completion are separate durable receipt fields. A confirmed
span upload does not imply scores arrived. The score receipt stores a hash of
the exact deterministic score payload; missing or changed scores can be upserted
without resending spans. Permanent content refusals retain that hash and do not
retry unchanged invalid scores. Authentication, rate and temporary server errors
remain visible retry failures.

Retry scope is per run and stable across daily, experiment and targeted exports.
Targeted exports do not inherit unrelated per-run debt. Unattributable legacy
scope-wide failures remain visible rather than being discarded.

A lost upload response is not permission to resend spans. Trace existence proves
only visibility. Recovery reads every expected span ID before marking span
readback verified. Score delivery receipts still do not certify score readback;
the combined `readback_verified` remains false until both can be established.

## past.dev

Export reports retain completed, queued, failed and rejected counts. Acceptance
by the ingestion API is not completed indexing. Daily analysis can continue with
an export warning; it no longer suppresses partial/client-cap warnings.

`amem sync settle --sink pastdev --budget 30 --limit 20` checks existing queued
ingestion jobs by ID and updates local receipts. It never sends, recalls,
deletes or retries provider content. It honours the kill switch, the shared
past.dev sync lock and the overall status-read deadline. A processing job remains
partial; failed reads or provider failures are explicit. Completed jobs become
completed locally. Content rejection becomes rejected; temporary provider
failures remain available to the existing bounded daily retry owner.

The agent-config maintenance wrapper runs settlement on its existing15-minute
cadence, before uploading routine results. A successful poll with processing
still pending exits successfully and reports the pending count; it does not
claim completed indexing. Install the backend command before that wrapper.

Recall requests reserve space for stale references removed by authority checks,
using one request and the existing20-result provider cap. The returned list still
respects the caller's requested limit. This cannot guarantee semantic relevance;
unrelated context is not promoted into supporting evidence.

## ClickHouse

The existing provider deadline is shared fairly among pending named queries;
health pings honour their active read deadline. Neither limits nor retry counts
are increased.

Forward, lookback and reconciliation phases have durable progress and rotate
after committed batches. Small daily caps resume the next phase rather than
repeating one table. Append-only tables also cycle their history in bounded
pages: immutable records can still commit behind a timestamp cursor.

Reconciliation compares bounded ClickHouse ID pages with the authoritative source.
Successful source absence produces an insert-only `_amem_deleted` marker.
Source errors never count as absence. Analytics and parity checks exclude markers
after `FINAL`. Recreated source IDs can be restored through reconciliation even
when their timestamps are older than the usual lookback window. Recovery and
reconciliation attempts remain in coverage accounting without claiming readback.

### Deployment order and rollback constraint

1. Review and apply `clickhouse-deletion-upgrade.sql` as the database owner.
   Existing rows default active. Writer grants remain SELECT/INSERT.
2. Activate the tested backend. Missing marker columns fail closed with an owner
   upgrade explanation; no production DDL runs automatically.
3. Activate the maintenance wrapper after `sync settle` is installed.
4. Verify the natural export and settlement results separately from historical runs.

The column upgrade itself leaves existing readers compatible before any marker
is written. After tombstones are written, an old reader that ignores the marker
can count deleted rows. A rollback must retain active-row filtering; do not
blindly revert to the previous unfiltered analytics queries.

No provider credentials, ownership classifications, deny terms, cash caps,
instruction promotion rules or company-application releases change here.
