# Daily job: `amem daily`

One command runs the learning loop once a day, on real data, with no human step.
A launchd job calls it at 09:20.

```
amem daily [--no-push] [--routines-log PATH] [--state PATH]
```

```
 cloud routines.log ──ingest──▶ runs ──score (Jev, per routine)──▶ scores
                                                                     │
          routines.log ◀── one summary line ◀── push new ids ◀── escalations
```

## Steps

A failed step is recorded, and the next step still runs.

First the job waits for the database: it tries `select 1` up to 10 times, 60 seconds apart
(launchd runs a missed 09:20 job right after wake, often before DNS is back; on 2026-10-14
every step failed on that). The steps run either way.

1. **ingest**: the `ingest git-ref` logic (docs/cloud.md): `gh api` GET of
   `reports/routines.log` on `vilmerfrost/agent-config@claude/routine-reports`.
   Called in-process, not as a subprocess. A 404 (branch or file missing, or no
   access to the private repo) **is** a failure: `ingest: routines.log not found at ...`.
   Then **tasks**: the `ingest tasks` logic (docs/tasks.md) over the routines log's
   folder (`~/.claude/parent`). No `*/tasks.jsonl` is not a failure.
2. **score**: the `score` logic for each `questions/<routine>.toml`, one routine
   at a time (at most 100 runs per routine). A Jev error or a crash in one
   routine is a failed step for that routine only; the others still run.
   A missing fixture (fixture mode only) is not a failure, the same as
   `amem score`: the run stays unscored and is retried tomorrow.
   `AMEM_JEV_LIVE=1` without `TYPESAFE_API_KEY` is a failed step (Jev would
   silently stay in fixture mode).
3. **escalations**: the open escalations (docs/scoring.md). The state file holds
   the run ids already pushed.
   - New = open ids not in the state. A push goes out only when there are new ids.
   - Ids that are no longer open (labeled) are pruned from the state.
   - An id is marked as pushed only after ntfy answers 200. The Mac fallback
     does not mark it, so it is pushed again the next day.
   - `--no-push` never pushes and never marks; it still prunes.
   Then **deviations** (daily, docs/deviations.md): `sessions ingest` + `deviations scan --limit 40`
   when `deviations_last_scan` in the state file is missing or at least 20 hours old (`AMEM_DEVIATIONS_EVERY_HOURS`). Off (not a
   failure) unless `AMEM_DEVIATIONS_LIVE=1`; the launchd job sets it. A session error is a warning (an `amem-daily-warn` line); it fails the step only when systemic: 2+ errors and at least half the scanned sessions.
   A database connection error before the scan loop waits for the database again and retries once.
   Then **edits** (docs/edits.md): `edits run` after the deviations step. Off (not a failure)
   unless `AMEM_EDITS_LIVE=1`; the launchd job sets it. Kept edits are pushed with an Undo
   button (not with `--no-push`). Any candidate error fails the step; the others still run.
   (The `lessons distill` step after scoring is frozen: off unless `AMEM_DISTILL_LIVE=1`.)
   Then **alerts** (`alerts.py`, since 2026-10-25): one phone reminder for each staged proposal that expires within
   30 hours (its Reject / Review tokens are re-armed, so the reminder's buttons work), and one push for slips of the
   safety rules (approval without handing work back, safety guards, push and merge policy) from the last 3 days.
   Each is sent once (`rsi_proposals.meta.reminded`, `deviations.meta.alert`). Not with `--no-push`. A failure is a
   **warning**, never a failed step: an alert must not block the night.
   Then, last, **sync** (daily, since 2026-10-24; docs/integrations/activation.md): `sync run --sink <sink>`
   (incremental, a 120 s budget each, plus the sink's client-side daily limits) for every sink that has its
   settings and is switched on: clickhouse (`AMEM_CLICKHOUSE_URL`/`_USER`/`_KEY`), langfuse (`AMEM_LANGFUSE_HOST`/
   `_PUBLIC_KEY`/`_SECRET_KEY`), pastdev (`AMEM_PASTDEV_KEY`). At most once per local day (`sync_last_day` in the
   state file), so ClickHouse Cloud can idle between runs. A sink without settings, or switched off
   (`AMEM_SYNC_<SINK>=off`; past.dev also `AMEM_PASTDEV=off`), is a skip. A sink that fails, is unreachable or hits
   its provider's credit cap is a **warning** (`amem-daily-warn sync <sink>: ...`), never a failed step: the
   night's prerequisite check (docs/rsi/night-ops.md) blocks only on `ingest`, `tasks` and `deviations`, and the
   night does not read what sync writes.
4. **log**: one line appended to the routines log.

Scoring and escalations need the Postgres backend (V1). `AMEM_REQUIRE_REMOTE=1`
applies to every step: with a local or empty database, all three steps are
refused (escalations too, so a local database can never cause a push).

## Push

Same as routine-audit:

- Topic from `~/.claude/parent/notify.env`: `NTFY_TOPIC=`, optional `NTFY_SERVER=`
  (default `https://ntfy.sh`). Only these two keys are read. The topic is never printed.
- `POST <server>/<topic>`, body = text, headers `Title`, `Priority: default`, `Tags: label`.
- No topic, a non-200 answer or an error: `osascript display notification` instead (`push=mac`).
- The body names counts and routines only, never run summaries, failures or ids:

  ```
  Title: amem: 3 new runs to label
  3 new escalations (5 open): config-check 2, routine-audit 1. Run `amem escalations`, then `amem label <run_id> up|down`.
  ```

## Routines log line

Default `~/.claude/parent/routines.log`; `--routines-log` or `AMEM_ROUTINES_LOG`
overrides it. House format (`%Y-%m-%dT%H:%M:%S%z`):

```
2026-10-13T09:20:04+0200 amem-daily FAIL scored=4 escalations=2 new=2 push=phone tasks=3 3s
2026-10-13T09:20:04+0200 amem-daily-fail score t-other: 1 Jev error; first: upstream 500
```

- `push=none|phone|mac`. `tasks=N` = new dispatch tasks ingested (always present). When the
  distill step ran, `distilled=N lessons_new=A lessons_merged=M lessons_rejected=R distill_errors=E`
  follows `tasks=N` (docs/lessons.md). When the deviations step ran,
  `deviations=D deviation_sessions=N deviations_edit=E deviations_enforce=F deviations_leave=L
  deviation_errors=X deviation_skipped=S` follows (docs/deviations.md). `deviation_skipped` counts
  writer items skipped as malformed (e.g. a null grade) in sessions whose valid items were stored:
  a warning, not a failed step (each is named on stderr). When the edits step ran, `edits=N edits_kept=K
  edits_reverted=R edits_suggested=S edit_errors=E` follows (docs/edits.md). When the sync step ran a sink,
  `sync=clickhouse:ok,langfuse:ok,pastdev:partial sync_rows=N` follows (each sink's `sync run` status).
  One `amem-daily-fail <reason>` line per failed step, one `amem-daily-warn <reason>` line per warning.
- `amem ingest routines-log` reads these back as routine `amem-daily`
  (`meta.scored`, `meta.escalations`, `meta.new`, `meta.push`, `meta.tasks`, `meta.duration_s`,
  `meta.failures`).
- stdout repeats the appended lines. Exit 0 on PASS, 1 on FAIL.

## State file

Default `~/.claude/parent/amem-daily-state.json`; `--state` or `AMEM_DAILY_STATE`
overrides it. Written atomically, mode 600:

```json
{"pushed_run_ids": ["<uuid>", "..."], "updated_at": "2026-10-13T09:20:04+0200",
 "deviations_last_scan": "2026-10-13T09:20:04+0200", "sync_last_day": "2026-10-13"}
```

Each step keeps the other keys when it writes.

An unreadable file is a failed step and is treated as empty (worst case: one repeat push).

## launchd job

`deploy/launchd/com.vilmerfrost.amem-daily.plist`:

- Runs `/opt/homebrew/bin/uv run --directory /Users/vilmerfrost/Projects/agent-memory --quiet amem daily`.
- Env: `AMEM_REQUIRE_REMOTE=1`, `AMEM_JEV_LIVE=1`, `AMEM_DEVIATIONS_LIVE=1` (the daily
  deviations step runs: one `claude -p` writer call per scanned session, at most 40 a day),
  `AMEM_EDITS_LIVE=1` (the edits step runs: at most 5 writer calls and `improve apply` runs a day),
  and `PATH` with `~/.local/bin` (launchd's default PATH has no `gh`; `claude` lives there
  too). The DB URL and the Jev key come from the main checkout's env file, which `amem`
  loads itself.
- After installing a new copy, run `amem deviations reref` once if the database holds
  deviations from before the rule catalog (docs/deviations.md).
- Daily at 09:20 (`StartCalendarInterval`), `RunAtLoad` false.
- stdout and stderr: `~/.claude/parent/reports/launchd.log`.

Install (by hand; nothing in this repo installs it):

```sh
plutil -lint deploy/launchd/com.vilmerfrost.amem-daily.plist
cp deploy/launchd/com.vilmerfrost.amem-daily.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vilmerfrost.amem-daily.plist
launchctl kickstart gui/$(id -u)/com.vilmerfrost.amem-daily   # optional: one run now
tail -n 5 ~/.claude/parent/routines.log
```

Remove: `launchctl bootout gui/$(id -u)/com.vilmerfrost.amem-daily`, then delete
the copied plist.

## Try it safely

Against a throwaway database, in fixture mode, without pushing:

```sh
AMEM_DB_URL=postgresql://postgres:postgres@127.0.0.1:<port>/postgres \
  uv run amem daily --no-push --routines-log /tmp/amem-demo/routines.log --state /tmp/amem-demo/state.json
```

Tests: `tests/test_daily_*.py` (ntfy, osascript and `gh` are stand-ins; no network).
