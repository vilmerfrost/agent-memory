# Daily job: `amem daily`

One command runs the learning loop once a day, on real data, with no human step.
A launchd job calls it at 00:40 (since 2026-10-26; before: 09:20), so the 01:30 RSI night reads that evening's
deviations instead of day-old ones. It sends no phone push and runs no edits step any more.

```
amem daily [--no-push] [--routines-log PATH] [--state PATH]
```

```
 cloud routines.log ──ingest──▶ runs ──score (Jev, per routine)──▶ scores ──▶ escalations (counted)
                                                                                 │
 00:40 amem-daily ── sessions + deviations ──▶ routines.log line ──▶ 01:30 night gate
                                                                                 ▼
                                    07:30 brief-job: ONE push (night result + new escalations + safety slips)
```

The job keeps `~/.claude/parent/amem-daily.running` (pid, start time) while it runs and removes it after its line
is written. The 01:30 night waits up to 30 minutes while that marker is live (docs/rsi/night-ops.md).

## Steps

### Claude authentication and recovery

All host Claude model calls (deviation scanner, lesson and edit writers, session
summaries, and legacy host night stages) load the same private subscription token
as the RSI replay adapter: `.subscription-auth/claude/.env`, key
`CLAUDE_CODE_OAUTH_TOKEN`. `AMEM_CLAUDE_AUTH_HOME` can explicitly select its private
directory. Tokens are loaded inside the application, passed only to Claude's
child process, and never printed. API credential overrides are removed from that
child. Missing or unsafe credentials fail closed; personal login and API billing
are never fallback paths. Tests disable automatic discovery with `AMEM_NO_DOTENV=1`.

A Claude authentication error stops the deviation scan immediately. It fails the
daily prerequisite even if only one session was attempted. Unattempted sessions
stay pending. The failed session does not consume a transcript retry, and the
last successful scan timestamp stays unchanged. Other non-Claude daily steps
can still finish. A per-run circuit prevents subsequent host model calls after
the same authentication failure.

The daily job saves recovery state beside its normal state file and sends one
authentication alert per affected night (unless `--no-push`). The phone transport
falls back to a best-effort Mac notification. The recovery LaunchAgent runs
`amem daily-recover` every 15 minutes; idle checks call no model. It resumes the
daily job, then retries the missed night through its existing prerequisite,
credential, overlap and already-completed checks. It never bypasses those gates.
Successful daily scans are not repeated while another prerequisite is missing.
Recovery stops at local noon or 40 attempts, whichever comes first, and reports
the incomplete night. After successful processing it sends a recovery notice;
partial experiment results remain partial in the night report.

Install `deploy/launchd/com.vilmerfrost.amem-auth-recovery.plist` only after this
code is available in the installed main checkout. Keep matching copies in
`~/agent-config/launchd/` and `~/Library/LaunchAgents/`, and register
`amem-auth-recovery` in `~/agent-config/parent/routines.tsv` with a 1-hour
freshness limit. No token belongs in a plist. Renew it with the official
`claude setup-token` flow and the native secret popup; never paste it into chat.

### Daily steps

A failed step is recorded, and the next step still runs.

First the job waits for the database: it tries `select 1` up to 10 times, 60 seconds apart
(launchd runs a missed job right after wake, often before DNS is back; on 2026-10-14
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
   - New = open ids not in the state (`new=`). amem-daily does not push them (since 2026-10-26): the 07:30
     brief job's one morning push (`alerts.push_morning`, docs/rsi/night-ops.md) carries them, also after a
     quiet night. The log line says `push=brief` when there are new ids (`none` with `--no-push`).
   - Ids that are no longer open (labeled) are pruned from the state.
   - The brief job marks an id as pushed only after ntfy answers 200. The Mac fallback
     does not mark it, so it is pushed again the next morning.
   Then **deviations** (daily, docs/deviations.md): `sessions ingest` + `deviations scan --limit 40`
   when `deviations_last_scan` in the state file is missing or at least 20 hours old (`AMEM_DEVIATIONS_EVERY_HOURS`). Off (not a
   failure) unless `AMEM_DEVIATIONS_LIVE=1`; the launchd job sets it. A session error is a warning (an `amem-daily-warn` line); it fails the step only when systemic: 2+ errors and at least half the scanned sessions.
   A database connection error before the scan loop waits for the database again and retries once.
   Then **edits** (docs/edits.md): retired on 2026-10-26. The RSI night has its own writer and stages its own
   proposals, so amem-daily no longer runs `edits run` or stages proposals, even with `AMEM_EDITS_LIVE=1` /
   `AMEM_EDITS_MODE=stage` set. Only `AMEM_DAILY_EDITS=1` (plus `AMEM_EDITS_LIVE=1`) brings the old step and its
   stage-mode push back. `amem edits undo` and `amem edits undo-poll` (amem-ingest, and the 5-minute decide job)
   keep working for older edits. The morning proposals still waiting are retired with
   `amem rsi supersede-morning` (status `superseded`, nothing deleted).
   (The `lessons distill` step after scoring is frozen: off unless `AMEM_DISTILL_LIVE=1`.)
   No **alerts** step any more: the safety-slip push and the last-day proposal reminder joined the 07:30 brief
   job's one morning push (`alerts.push_morning`), so nothing reaches the phone at 00:40.
   Then, last, **sync** (daily, since 2026-10-24; docs/integrations/activation.md): `sync run --sink <sink>`
   (incremental, a 120 s budget each, plus the sink's client-side daily limits) for every sink that has its
   settings and is switched on: clickhouse (`AMEM_CLICKHOUSE_URL`/`_USER`/`_KEY`), langfuse (`AMEM_LANGFUSE_HOST`/
   `_PUBLIC_KEY`/`_SECRET_KEY`), pastdev (`AMEM_PASTDEV_KEY`). At most once per local day (`sync_last_day` in the
   state file), so ClickHouse Cloud can idle between runs. A sink without settings, or switched off
   (`AMEM_SYNC_<SINK>=off`; past.dev also `AMEM_PASTDEV=off`), is a skip. A sink that fails, is unreachable or hits
   its provider's credit cap is a **warning** (`amem-daily-warn sync <sink>: ...`), never a failed step: the
   night's prerequisite check (docs/rsi/night-ops.md) blocks only on `ingest`, `tasks` and `deviations`, and the
   night does not read what sync writes.
   Then **coverage** (data-use plan R4, `coverage_daily.py`): the deviations, score and sync steps write their
   coverage snapshots and consumption records (docs/data-use/coverage.md), job `amem-daily`, run key = the summary
   line's timestamp. Datasets: `sessions:sessions` (ingest window; the scan's funnel, skip reasons and the deviation
   refs it stored), `deviations:deviations` (scan-eligible sessions in the window vs. scanned: a backlog is
   `not_collected`), `routine_runs:runs` (score step), `<family>:<table>.<sink>` per synced table (delivery as the
   sink acknowledged it; readback stays unmeasured). A step that is off or not due records nothing. Best effort:
   a write problem is a stderr line and `coverage_problems=N` on the summary line, never a failure.
   `AMEM_COVERAGE_WRITE=0` turns it off (and skips the extra reads).
4. **log**: one line appended to the routines log.

Scoring and escalations need the Postgres backend (V1). `AMEM_REQUIRE_REMOTE=1`
applies to every step: with a local or empty database, all three steps are
refused (escalations too, so a local database can never cause a push).

## Push

amem-daily itself no longer pushes (2026-10-26). The escalation message below is now one part of the 07:30 brief
job's single morning push (`alerts.push_morning`): the night's result, then the safety slips, the proposals on
their last day and the new escalations. A quiet night with new escalations still pushes; a morning with nothing
to say sends nothing. The channel is the same as routine-audit:

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

- `push=none|brief` (`brief`: new escalations wait for the 07:30 morning push; `phone|mac` on lines before
  2026-10-26). `tasks=N` = new dispatch tasks ingested (always present). When the
  distill step ran, `distilled=N lessons_new=A lessons_merged=M lessons_rejected=R distill_errors=E`
  follows `tasks=N` (docs/lessons.md). When the deviations step ran,
  `deviations=D deviation_sessions=N deviations_edit=E deviations_enforce=F deviations_leave=L
  deviation_errors=X deviation_skipped=S` follows (docs/deviations.md). `deviation_skipped` counts
  writer items skipped as malformed (e.g. a null grade) in sessions whose valid items were stored:
  a warning, not a failed step (each is named on stderr). When the edits step ran, `edits=N edits_kept=K
  edits_reverted=R edits_suggested=S edit_errors=E` follows (docs/edits.md). When the sync step ran a sink,
  `sync=clickhouse:ok,langfuse:ok,pastdev:partial sync_rows=N` follows (each sink's `sync run` status).
  `coverage_problems=N` follows only when coverage records could not be written (a note, not a failure).
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
  no `AMEM_EDITS_LIVE` / `AMEM_EDITS_MODE` since 2026-10-26 (no morning edits step),
  and `PATH` with `~/.local/bin` (launchd's default PATH has no `gh`; `claude` lives there
  too). The DB URL and the Jev key come from the main checkout's env file, which `amem`
  loads itself.
- After installing a new copy, run `amem deviations reref` once if the database holds
  deviations from before the rule catalog (docs/deviations.md).
- Daily at 00:40 (`StartCalendarInterval`, since 2026-10-26), `RunAtLoad` false. Its line carries the night's
  key (00:00-11:59 counts as the previous evening), which is what the 01:30 night's gate looks for.
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
