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
amem dispatch refresh [--repo R] [--codex-home DIR] [--no-db] [--no-codex]
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
  (worker tokens, median integration-test time) and it reads the local Codex
  rollouts for the usage seed (below). `start` and `end` never do either.
  `--no-db` skips the database, `--no-codex` skips the rollouts, `--codex-home`
  points at another Codex home. It prints how long it took (about 0.3 s for 87
  rollouts, 577 MB, without the database).
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
parallel, with a 0.5 s timeout each. They import no database code. Wall time with
real Vitals, measured on 2026-10-29 (median of 5 runs):

| Entry point | start | end |
|---|---|---|
| `.venv/bin/python -m agent_memory.cli_dispatch start ...` | 62 ms | 62 ms |
| `.venv/bin/amem dispatch start ...` | 422 ms | 429 ms |
| `uv run --project <checkout> amem dispatch start ...` | 486 ms | 448 ms |

About 280 ms of `amem` is its own startup: the main CLI imports every subcommand.
`python -m agent_memory.cli_dispatch` takes the same arguments (without the word
`dispatch`) and prints the same lines; use it from hooks.

`vitals limits` itself takes about 20 ms, but when its reading is about a minute
old it asks the providers again and blocks for 2 to 4 seconds (seen in 2 of 9
calls). Then `start`/`end` do not wait: they say "usage snapshot follows in the
background" and a detached helper (`python -m agent_memory.dispatch_estimates
late-snapshot`, 15 s limit) appends the snapshot to the spool when Vitals answers.
The worst case is then about 0.9 s.

## Files

| File | What |
|---|---|
| `~/.claude/parent/<repo>/dispatch-events.jsonl` | Append-only spool. One JSON record per line, `v: 1`. Types: `start`, `snapshot`, `estimate`, `end`, `tokens`. Mode 0600, never a symlink, lines over 32 KB are skipped. |
| `~/.claude/parent/<repo>/dispatch-open/<session>.json` | The open run of a session. `end` removes it. |
| `~/.claude/parent/dispatch-estimates.json` | The lookup file `start` reads. Written by `refresh`. Keys `v`, `time`, `usage`, `tokens`, `runs`, plus `seed` (usage seed with its source records). |
| `~/.claude/parent/dispatch-cursors.json` | Upload cursor (offset per spool file, with a lock). |

`AMEM_CLAUDE_HOME` replaces `~/.claude` (tests use a tmp dir). The Codex home is
`--codex-home`, else `AMEM_CODEX_HOME`, else `.codex` next to the Claude home, so a
test with a tmp Claude home never reads the real `~/.codex`.

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
  runs in `test_timing_runs`; a repo with neither adds nothing). Rounds that a
  measured run already covers are skipped.
- Usage, per limit and bucket, first match wins:
  1. Clean runs: with at least 5 clean runs in the bucket, their median and 80th
     percentile delta (the line says `(5 clean runs)`).
  2. Seed (Codex only): `median worker tokens per round / fresh tokens per 1%` from
     the usage seed below (the line says `(seed: Codex rollouts, 138 intervals x 7 rounds)`).
  3. Token ratio: `median worker tokens x learned percent per million tokens` from
     clean runs with tokens (marked "from tokens").
  Without any of them the line shows what real data there is, never a number made up:
  `Claude 5h use: no % history yet, ~1.2M fresh tokens typical (12 rounds)`, or
  `Codex week use: no round token history yet, 1% ~ 27k fresh tokens (seed: Codex rollouts, 138 intervals)`,
  or `no usage history`.
- Tokens per run come from the `sessions` table: worker rows whose parent is the run's
  session and that started inside the run. Fresh tokens are input + output + cache
  creation; cache reads are kept apart. Refresh appends them to the spool as a
  `tokens` record. Offline refresh skips this silently.

## Usage seed (before clean runs exist)

`refresh` seeds usage from real history only (`dispatch_seed.py`). Every number in
the lookup file's `seed` block has its source records next to it, plus `source`, `n`
and `method_version` (`usage-seed/1`). Too little data gives no number.

**Codex: tokens per 1%, from local Codex rollouts.** Codex writes a `token_count`
event per model call into `<codex home>/sessions/**/rollout-*.jsonl` (and
`archived_sessions`): cumulative tokens and the account's plan-limit percent
(`rate_limits.primary`: `used_percent`, `window_minutes`, `resets_at`). The percent is
account-wide and sessions overlap, so one rollout's own ratio is wrong. Refresh uses
one global timeline:

1. Token deltas of all rollouts are merged by time. A forked subagent copies its
   parent's history (ordinal below `subagent_history_start_ordinal`); the copies only
   set the counter baseline, so those tokens count once. A counter that starts over
   counts all of its new value.
2. Percent sightings are grouped per limit window (`resets_at` within 5 minutes).
3. Inside a window, an interval runs from the first sighting of one percent value to
   the first sighting of a value at least 3 points higher. Its tokens are the deltas
   of all rollouts in (start, end]. Anchoring on the first sighting of a new whole
   percent keeps the rounding error small; 3 points keep it under about a third.
   Shorter tails are dropped; so are intervals with no local tokens (the use came
   from elsewhere).
4. Per limit: fresh tokens (input - cached input + output) and total tokens per 1%,
   as median, 20th and 80th percentile over the intervals. Fewer than 3 intervals:
   no number ("no usage history").

Each interval in the lookup file names its start and end record (file, timestamp,
ordinal, percent), the tokens, the number of token events and rollouts.

**Tokens per dispatch round, from the `sessions` table.** For each `tasks.jsonl`
round, the worker rows whose parent is the round's session and that started inside
the round (`DbSource.tokens`): fresh = input + output + cache creation, cache reads
apart. Per parent tool and worker bucket: n, median and 80th percentile; a bucket
with fewer than 3 rounds uses the tool's pooled rounds. A round with no matched
worker row is skipped, never counted as zero. Without a database this source is
skipped and refresh says so; the last database result is kept with its date.

**Claude** has no historical percent source, so Claude lines show tokens only. The
seed never invents a Claude ratio.

Seed limits:

- Codex use from other devices or Codex cloud is not in local rollouts. An interval
  with such use shows too few tokens per 1%. The spread (p20 to p80 was 15k to 108k
  fresh tokens on 2026-10-29) is real, not noise to hide.
- A percent sighting can be late when no session runs: the next session's first call
  ends the interval and its tokens count in it.
- The round tokens of a Codex parent come from Codex worker rows in `sessions`, which
  store input without cached input; both sides use the same fresh definition.

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
