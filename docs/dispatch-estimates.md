# Dispatch estimates

A parent's "Expect: ..." line comes from past runs, not from a guess.

## What a run is

A run is one parent dispatch: from the moment the parent spawns its workers
(`amem dispatch start`) to the moment it briefs Vilmer (`amem dispatch end`).
It is not the parent chat window. The parent chat idles while workers run, so its
window says nothing about how long the work took.

Usage is the plan-limit percent that Vitals shows (Claude 5-hour session, Claude
week, Codex week), taken once at start and once at end, plus the workers' tokens.

## Commands

```sh
amem dispatch start --repo agent-memory --workers 3 [--brief-chars N] [--files-claimed N] [--tool claude|codex] [--session S]
amem dispatch end [run_id] [--session S] [--status done|partial|blocked|failed|abandoned] [--integration-test-ms N]
amem dispatch refresh [--repo R]
amem dispatch report [--limit N]
amem dispatch ingest
```

- `start` prints two lines. Line 1 is `run_id=<uuid>`. Line 2 is the ready line:
  `Expect: ~25 min (slow case 40), ~6% of Claude 5h, now 25% used — based on 14 past runs`.
  "slow case" is the 80th percentile. A Codex parent shows the Codex weekly limit.
- `end` with no run_id finds the open run of the current session. It prints
  `run_id=<uuid>` and an `Actual: ...` line. A second `end` finds nothing open and
  changes nothing.
- `refresh` recomputes the lookup file that `start` reads. It may use the database
  (worker tokens, median integration-test time). `start` and `end` never do.
- `report` prints estimate vs actual per finished run, as JSON lines.
- `ingest` uploads the spools now. The timing collector also does it every flush.

The session comes from `--session`, else `AMEM_TIMING_SESSION_ID`, else
`CODEX_THREAD_ID` (stored as `codex:<id>`), else `CLAUDE_CODE_SESSION_ID`. The tool
follows the session: a `codex:` session is a Codex parent. A new `start` in a
session that still has an open run closes the old run as `abandoned`.

`start` and `end` always exit 0. Missing Vitals, disabled limits or no history
make the line say what is missing; they never stop a dispatch.

## Speed

`start` and `end` read `vitals limits --json` and `vitals agents --json` in
parallel, with a 0.5 s timeout each. They import no database code. Measured on
2026-10-29 (median of 5 runs):

| Entry point | start | end |
|---|---|---|
| `~/Projects/agent-memory/.venv/bin/amem` | see the worker report | see the worker report |
| `uv run --project ~/Projects/agent-memory amem` | see the worker report | see the worker report |

Most of the time is `amem` startup (the main CLI imports every subcommand), not
the dispatch code. Use the venv entry point from hooks.

## Files

| File | What |
|---|---|
| `~/.claude/parent/<repo>/dispatch-events.jsonl` | Append-only spool. One JSON record per line, `v: 1`. Types: `start`, `snapshot`, `estimate`, `end`, `tokens`. Mode 0600, never a symlink, lines over 32 KB are skipped. |
| `~/.claude/parent/<repo>/dispatch-open/<session>.json` | The open run of a session. `end` removes it. |
| `~/.claude/parent/dispatch-estimates.json` | The lookup file `start` reads. Written by `refresh`. |
| `~/.claude/parent/dispatch-cursors.json` | Upload cursor (offset per spool file, with a lock). |

`AMEM_CLAUDE_HOME` replaces `~/.claude` (tests use a tmp dir).

## Tables

Migration `20261029120000_dispatch_runs.sql`. RLS is on for all three tables.
`amem_app` may read and write; the owner may read.

- `dispatch_runs`: one row per run. `duration_s` is generated from
  `briefed_at - dispatched_at`. `meta.concurrent_by_tool` holds the concurrency per tool.
- `usage_snapshots`: one row per run, phase (start/end) and limit. The account is a
  16-character SHA-256 prefix of the lowercased email, never the email.
- `dispatch_estimates`: the estimate `start` printed, to compare later.
- `dispatch_run_actuals` (view, `security_invoker`): duration and usage delta per
  run and limit, with a `clean` flag.

Upload is idempotent. A replay of the whole spool changes nothing. Snapshots or
estimates whose run never arrived are skipped, not failed.

## The clean-run rule

A usage delta counts for the usage estimate only when it is clean:

1. No other session of the same tool was working, at start or at end. Plan limits
   are per account, so a busy Codex session does not spoil a Claude delta.
   Claude sessions count when Vitals says `status: working` and the session is not
   the parent itself. Codex rows have no status: a Codex process counts when it
   uses at least 2% CPU (minus the Codex parent itself).
2. The limit did not reset during the run (`resets_at` moved by at most 5 minutes).

Time estimates use every finished run, clean or not.

## How refresh estimates

- Time, per parent tool and worker bucket (1, 2, 3-4, 5+): median and 80th
  percentile minutes. A bucket with fewer than 3 runs uses all of the tool's runs.
  No run at all gives a 30-minute default guess, labelled "no history yet".
- `tasks.jsonl` rounds fill in time history. A round runs from the first spawn to
  the last handback, so refresh adds the repo's median integration-test time
  (measured `--integration-test-ms` first, else the database median of full, passing
  runs in `test_timing_runs`). Rounds that a measured run already covers are skipped.
- Usage, per limit and bucket: median and 80th percentile delta over clean runs.
  With fewer than 5 clean runs in the bucket, the estimate is
  `median worker tokens x learned percent per million tokens` (marked "from tokens").
  Without either, the line says "no usage history".
- Tokens per run come from the `sessions` table: worker rows whose parent is the run's
  session and that started inside the run. Fresh tokens are input + output + cache
  creation; cache reads are kept apart. Refresh appends them to the spool as a
  `tokens` record. Offline refresh skips this silently.

## Limits

- The percent is account-wide. Any other use of the same account (a phone chat, a
  session Vitals cannot see) lands in the delta. The clean rule only removes what
  Vitals can see, and only at start and end.
- Codex shows whole percent steps. A short Codex run often shows +0% or +1%.
- Vitals keeps no history of limits. A missed start or end snapshot cannot be
  recovered later.
- Vitals asks the providers every few minutes. Each snapshot's `taken_at` is when
  Vitals last asked (`measured_at - asked_minutes_ago`), not when `start` ran.
- Parent tokens are not split per run (the parent row is cumulative), so `tokens_*`
  counts worker sessions only.
