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

## Commands

| Command | Does |
|---|---|
| `amem doctor` | Prints `jev_mode=`, `db_host=`, `db=reachable\|unreachable`. Never prints keys. Exits 0. |
| `amem log-run --routine NAME --status pass\|fail\|skip\|error [--source launchd\|scheduled-task\|cloud\|manual] [--summary ..] [--failure-reason ..] [--report-path ..] [--started-at ISO] [--finished-at ISO]` | Inserts one run, prints its id. |
| `amem runs --routine NAME [--limit N]` | Prints recent runs, newest first, as JSON lines. |
| `amem db migrate` | Applies new migration files in name order. Tracks them in `schema_migrations`. Safe to rerun. |

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

## Tests

```sh
uv run pytest -q
```

Needs Docker. One throwaway `pgvector/pgvector:pg17` container per session, on
an OS-assigned port, removed at the end.
