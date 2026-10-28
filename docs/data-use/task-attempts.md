# Dispatch attempts in existing runs

`tasks_ingest` keeps one `runs` row per source-provided execution attempt. It adds no table.

The canonical key hashes the repository, producer namespace, logical task and attempt. An explicit `attempt_id` wins. Otherwise the source's session and start timestamp identify the recorded execution. Neither is derived from timestamp proximity. A row with no execution boundary has `attempt_identity_basis=unresolved`; it is not silently assigned a retry relationship.

If the producer has no identity, the namespace is a hash of the actual local source directory, labeled `local-source-path`. This scopes local ingestion; it does not claim to identify a machine globally or prove two copied logs came from the same producer. Producers should supply a stable `producer_id`, `logical_task_id` and `attempt_id` for portable cross-host identity.

`retry_of`, `resumed_from`, parent session and parent span are preserved only when the producer supplies them. Worker `spawn_id`, `spawned`, `first_handback`, `last_handback`, report status, actual supplied completion time, span IDs and denial/info counters survive compaction. Normalized `spawned_at` and `reported_at` are validated source timestamps. A handback does not become an independently observed completion timestamp.

Each attempt retains a map of source observations keyed by canonical source-record hash. Older PARTIAL/BLOCKED states are retained when DONE arrives. Actual source record/end/start timestamps select the current projection, rather than delivery order. With no later timestamp, the status ordering retains a conclusive DONE over PARTIAL; the record remains reported success with `acceptance=unknown`.

Exact duplicate observations are idempotent. A new explicit attempt or changed source execution boundary makes another row; it does not overwrite the first attempt. Cross-repository and producer identities cannot collide merely because task IDs match.

Observation history also retains each worker's source lifecycle fields and span references. `observation_time_basis` names recorded time or the explicit end/start proxy; `projection_policy` makes the tie-breaking rule visible. The current projection is not independently verified acceptance or an invented chronology. One transaction-scoped dispatch-ingest advisory lock serializes concurrent collector runs, including their first insert, so neither loses the other's observation map.

An existing legacy `dispatch:<id>` row is adopted only when repository, task, session and start time match exactly. Its UUID and source key remain stable, preserving score/label/foreign references. Its metadata receives the canonical key for future lookup. Nonmatching legacy rows are not rewritten. This is collector compatibility logic, not a historical backfill performed by this change.

No missing historical boundary, timestamp, human acceptance or retry relationship can be reconstructed from a later summary. Lost source lines remain lost. The collector continues to expose `routine=source=dispatch`, existing status values and metadata keys for dashboards and score consumers.

Focused checks cover explicit retries/resumes, distinct repositories, unresolved boundaries, out-of-order observations, actual worker lifecycle evidence, repeat ingestion and exact legacy-row adoption. Hosted ingestion/backfill was not run.
