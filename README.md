# agent-memory

Learning loop for Vilmer's agent setup. Routine runs get logged. Later, Jev
(TypeSafe System One) scores outcomes and gates lessons.

Status: v0 foundation. Run log + Jev wrapper + local Postgres. Nothing here
touches production routines or a hosted Supabase.

## Setup

```sh
uv sync                 # Python >= 3.11, installs the `amem` command
cp .env.example .env.local   # optional; env files are never required
supabase start          # optional local stack (fixed ports 5432x)
uv run amem db migrate  # applies supabase/migrations/*.sql to AMEM_DB_URL
```

`AMEM_DB_URL` defaults to the local Supabase DB:
`postgresql://postgres:postgres@127.0.0.1:54322/postgres`.

### Require the remote DB

Set `AMEM_REQUIRE_REMOTE=1` on any machine whose runs must land in the remote
DB. Then every write command (`log-run`, `ingest`, `db migrate`) exits 3 with
a clear message when `AMEM_DB_URL` is empty or its host is local
(`localhost`, `127.x`, `::1`, `*.localhost`, a unix socket, or no host at all).
Messages name the host at most, never the DSN or the password. Default is off,
so tests and worktrees stay local. `amem doctor` prints `require_remote=on|off`.

## Commands

| Command | Does |
|---|---|
| `amem doctor` | Prints `jev_mode=`, `db_host=`, `db=reachable\|unreachable`, `require_remote=on\|off`. Never prints keys. Exits 0. |
| `amem log-run --routine NAME --status pass\|fail\|skip\|error [--source launchd\|scheduled-task\|cloud\|manual] [--summary ..] [--failure-reason ..] [--report-path ..] [--started-at ISO] [--finished-at ISO]` | Inserts one run, prints its id. |
| `amem runs --routine NAME [--limit N]` | Prints recent runs, newest first, as JSON lines. |
| `amem ingest routines-log PATH` | Ingests a `routines.log` into `runs`. Idempotent: prints `ingested N new, M already present, K unparsed`. Safe to run on a schedule. |
| `amem ingest tasks [--parent-dir PATH]` | Ingests dispatch tasks (`<parent>/<repo>/tasks.jsonl` + checkpoints) as `dispatch` runs. Idempotent. See [docs/tasks.md](docs/tasks.md). |
| `amem db migrate` | Applies new migration files in name order. Tracks them in `schema_migrations`. Safe to rerun. |

## Ingesting routines.log

Every local routine appends to `~/.claude/parent/routines.log`:

```
2026-10-12T09:00:14+0200 config-check FAIL pass=36 fail=5 info=0 10s report=~/.claude/parent/reports/config-check-2026-10-12.md
2026-10-12T09:00:14+0200 config-check-fail repos in iCloud: 1: ~/Desktop/Projects/Classifed
```

- One `runs` row per summary line (`<ts> <routine> <STATUS> <text>`).
  `started_at` = ts, `summary` = the text, `report_path` from `report=`.
- Status: `PASS/FAIL/SKIP/ERROR` map to `pass/fail/skip/error`. Other words
  (`TIE`, `WIN`, `INCONCLUSIVE`, ...) become `error`, with `meta.raw_status`.
- `meta`: the `k=v` pairs (numbers as numbers), `duration_s` from a `10s` token,
  `log_source = "routines.log"`, and `failures` = the texts of the detail lines
  (`<ts> <routine>-fail <text>`) with the same routine and ts.
- `source`: `cloud` when the report path is under `.../reports/cloud/` or
  `/root/`, else `launchd`.
- Detail lines with no matching summary are kept: each (ts, routine) group
  becomes one `fail` row with `meta.orphan_detail = true`, `summary` null.
- Lines that fit no shape are counted as unparsed (line numbers go to stderr)
  and not written. A last line without a newline may be mid-write, so it waits
  for the next ingest.
- Idempotency: `runs.source_key` (unique, nullable) = sha256 of the routine plus
  the raw line. A byte-identical repeat line gets an occurrence suffix, so it is
  kept too. Re-ingesting inserts nothing. If a detail line arrives after its
  summary was ingested, the next ingest refreshes only `meta.failures`.

## Jev: fixture vs live

- `live` only when `TYPESAFE_API_KEY` is non-empty **and** `AMEM_JEV_LIVE=1`.
- Otherwise `fixture`: answers come from `tests/fixtures/jev/<key>.json`, where
  `<key>` = `jev.fixture_key(state, questions)`. A missing fixture is an error,
  never a live call.
- Tests never call the API (the SDK client is a tripwire in `tests/conftest.py`).

```python
from agent_memory import jev
results = jev.ask(state, [jev.choice("outcome", "Did it work?", {"pass": None, "fail": None}),
                          jev.score("quality", "How good?", ["bad", "ok", "good"]),
                          jev.noul("needs_human", "Does a human need to look?")])
results["outcome"].value, results["outcome"].confidence   # noul has no confidence (None)
```

## V1: the learning loop

Each part has its own doc:

| Part | Commands | Doc |
|---|---|---|
| Dispatch tasks as runs | `ingest tasks` | [docs/tasks.md](docs/tasks.md) |
| Cloud path (HTTPS, no raw Postgres) | `AMEM_BACKEND=rest`, `ingest git-ref`, `jev record` | [docs/cloud.md](docs/cloud.md) |
| Jev scoring + human labels | `score`, `label`, `escalations`, `calibrate`, `drift` | [docs/scoring.md](docs/scoring.md) |
| Lessons memory | `lessons import/export/for/score`, `lesson propose`, `gate friction` | [docs/lessons.md](docs/lessons.md) |
| A/B proof | `ab report` | [docs/ab.md](docs/ab.md) |

The loop: runs are logged → Jev scores outcomes (low confidence → you label 👍/👎) → Jev gates which runs become lessons → `lessons for` injects the top lessons into briefs (`AMEM_MEMORY=off` turns injection off) → `ab report` shows whether each lesson actually helps → losers retire.

Scoring, lessons and A/B commands use the Postgres backend only in V1; the REST backend covers `runs` (`log-run`, `ingest`, `runs`).

## Tests

```sh
uv run pytest -q
```

Needs Docker. One throwaway `pgvector/pgvector:pg17` container per session, on
an OS-assigned port, removed at the end.
