# Skill usage and scores

Skill calls go into the database. Each call is linked to the session it ran in, when one
matches. Each skill then gets an outcome score from those sessions. A small JSON cache holds
the scores, so a local script (`skillpick`, agent-config) can rank skills without a DB call.

```
~/.agents/skill-usage/log.jsonl
        │  amem ingest skills PATH
        ▼
  skill_calls ──match──► sessions (outcome, project, category)
        │  amem skills scores --refresh
        ▼
  skill_scores
        │  amem skills cache PATH
        ▼
  skills.json  ──► skillpick (local, no DB)
```

All commands need the Postgres backend. On the REST backend they exit 1 and touch nothing.

## Commands

| Command | Does |
|---|---|
| `amem ingest skills PATH` | Ingests the skill-usage log into `skill_calls`. Idempotent: prints `ingested N new, M already present, K unparsed`. Then links unmatched calls to sessions. Records the time it read the whole log (`rsi_checkpoints` job `skills-ingest`, run_key `latest`): the next refresh's input window ends there. |
| `amem skills scores [--refresh] [--skill S] [--all-buckets]` | Prints scores (tab separated). `--refresh` first links unmatched calls to sessions, then rewrites `skill_scores`, as one recorded run (below). Without `--skill` or `--all-buckets` it prints the `*` bucket only. |
| `amem skills cache PATH [--refresh]` | Writes `skill_scores` as JSON to PATH (atomic: temp file, then rename). Its `--refresh` is not a recorded run. |
| `amem skills suggest [--days 30] [--min 10]` | Lists skills run by hand (`by == "me"`) at least `--min` times in the last `--days` days, e.g. `handoff: 69 manual runs in 30 days`. |

## Log format

One JSON object per line:

```json
{"t": "2026-10-07T15:36:30+00:00", "skill": "spark", "by": "me", "agent": "claude", "cwd": "/Users/...", "backfill": true}
```

- `t`: ISO 8601 with an offset (`...Z` or `+00:00`). No offset: unparsed.
- `skill`: non-empty string.
- `by`: `claude` or `codex` (the agent chose the skill) or `me` (typed by hand). Anything else: unparsed.
- `agent`: the tool that ran the session (`claude` or `codex`). Required.
- `cwd`: optional.
- `backfill`: optional. `true` means the line was reconstructed later, not logged live.

Rules:

- Blank lines are ignored. Lines that fit no shape are counted as unparsed. Their line numbers go to stderr. They are never written.
- `source_key` = sha256 of the canonical JSON (sorted keys, no spaces). Re-ingesting writes nothing new.
- A line identical to an earlier line is the same call logged twice. It is kept once and reported on stderr.
- A last line without a newline is used only when it is whole JSON. Otherwise the next ingest picks it up.

## Table `skill_calls`

| Column | Meaning |
|---|---|
| `ts`, `skill`, `agent`, `cwd`, `backfill` | From the log line. |
| `invoked_by` | The log's `by`. (`by` is a reserved SQL word.) |
| `project` | Project bucket of `cwd`, by the same rule as `sessions.project` (`~/worktrees/<repo>/<x>` gives `<repo>`). `unknown` without a cwd. |
| `source_key` | Unique. See above. |
| `session_id` | The matched `sessions` row, or null. |

### Session match

**Direct link first.** Since 2026-10-14 `track.py` logs the hook's `session_id` on every line.
`amem ingest skills` keeps it in `skill_calls.logged_session` (Codex ids get the `codex:` prefix
that `sessions` uses) and links the call to that sessions row as soon as the row exists, whatever
the cwd or time says.

**Otherwise** (older lines, or the session row is missing) a call matches a session when all of
these hold:

1. `sessions.tool = skill_calls.agent`.
2. `skill_calls.cwd` is the session's `meta.cwd` or any cwd it visited (`meta.cwds`, written by
   `sessions ingest`). `meta.cwd` alone is the session's final work folder, so a chat that moved
   from a scratch folder to a repo would miss every call made before the move.
3. `ts` lies in `started_at - 1 min .. greatest(started_at, ended_at, last_activity_at) + 1 min`.

Rows ingested before `meta.cwds` existed get it when `amem sessions ingest` re-reads their transcript.

When several sessions match, the one with the latest `started_at` wins. A call with no match
keeps `session_id` null. Every ingest and every `scores --refresh` retries the null rows, so a
session ingested later still gets linked.

Why exact cwd: worktree sessions each have their own cwd, so parallel workers in one repo do
not get mixed up. A repo-level match would assign a call to the wrong worker.

## Table `skill_scores`

One row per skill and bucket. `scores --refresh` deletes and rewrites the whole table in one transaction.

| Column | Meaning |
|---|---|
| `bucket` | `*` (all calls), `project:<project>`, or `category:<sessions.category>` |
| `n_calls` | Calls in the bucket, matched or not |
| `n_sessions` | Distinct matched sessions |
| `n_scored` | Matched sessions with a final outcome |
| `outcomes` | `{outcome: sessions}` over matched sessions |
| `score` | See below. 0..1 |
| `refreshed_at` | When the row was written |

A call's project bucket uses the session's `project` when matched, else the call's own `project`.
The category bucket exists only for matched calls.

### Recorded refresh run

`amem skills scores --refresh` records itself (`src/agent_memory/skill_refresh.py`), so the nightly investigator
can check which refresh it reads and how far its input reached:

- `rsi_checkpoints` row: job `skills-refresh`, run_key = the run id `sr-<UTC>-<6 hex>`. Status `running` first,
  then `done` in the same transaction as the new scores, or `failed` with the error.
- meta: `window_start` (the earliest skill call scored), `window_end` (the last complete log read recorded by
  `amem ingest skills`; without one, the newest `skill_calls.created_at`, basis `latest_insert`), `scored`
  (score rows), `calls`, `matched`, `basis`, `snapshot_at`.
- The last stdout line is fixed (the agent-config wrapper copies `run=` and `window_end=` into routines.log):

```
skills-refresh run=sr-20261026T011002Z-3fa9c1 window_start=2026-04-06T19:34:40+00:00 window_end=2026-10-26T01:10:00+00:00 status=ok scored=212
```

  A failed run prints `window_start=- window_end=- status=failed scored=0` and exits 1. The night's
  `skill_refresh` prerequisite reads these rows: docs/rsi/night-ops.md.

### Score

Outcome weights (`sessions.outcome`):

| Outcome | Weight |
|---|---|
| shipped | 1.0 |
| pushed | 0.9 |
| committed | 0.75 |
| partial | 0.4 |
| no-change | 0.25 |
| abandoned | 0.1 |
| failed | 0.0 |

`active` and null are not final. They count in `n_sessions` and `outcomes`, but not in the score.

```
score = (sum of weights + 0.5 * 2) / (n_scored + 2)
```

The prior (0.5 with weight 2) keeps a skill with one lucky session from ranking first. A skill
with no scored sessions gets 0.5.

A session counts once per bucket, however many times the skill ran in it.

## Cache

`amem skills cache PATH` writes:

```json
{
  "version": 1,
  "generated_at": "2026-10-14T16:20:00+00:00",
  "refreshed_at": "2026-10-14T16:19:58.123+00:00",
  "score_rule": {
    "weights": {"shipped": 1.0, "pushed": 0.9, "committed": 0.75, "partial": 0.4,
                "no-change": 0.25, "abandoned": 0.1, "failed": 0.0},
    "prior": 0.5,
    "prior_weight": 2
  },
  "skills": {
    "handoff": {
      "*":                     {"score": 0.71, "n_calls": 80, "n_sessions": 52, "n_scored": 40,
                                "outcomes": {"shipped": 20, "committed": 12, "partial": 8, "active": 3}},
      "project:agent-memory":  {"score": 0.80, "n_calls": 12, "n_sessions": 9, "n_scored": 8, "outcomes": {}},
      "category:worker":       {"score": 0.66, "n_calls": 30, "n_sessions": 20, "n_scored": 18, "outcomes": {}}
    }
  }
}
```

- `version`: bumped when the shape changes. A reader checks it.
- `generated_at`: when the file was written. `refreshed_at`: the newest `skill_scores.refreshed_at` (null if the table is empty).
- `skills.<skill>.<bucket>`: same fields as the `skill_scores` columns.
- Keys are sorted. The file is written with a temp file and a rename, so a reader never sees half a file.

A reader picks the most specific bucket it has (`project:<p>`, then `category:<c>`, then `*`).

## Suggest

`amem skills suggest` replaces the planned `skillstats suggest`. It counts `invoked_by = 'me'`
calls with `ts >= now() - days`. A skill run by hand often is a candidate for a routine or an
automatic trigger.

```
$ amem skills suggest --days 30 --min 10
handoff: 69 manual runs in 30 days
```
