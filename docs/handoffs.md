# Handoffs: an event source for the RSI loop

A handoff is the moment a session hands its work to a fresh session (a context limit, or a
`/handoff` on request). Each one is a signal: which sessions run out of room, in which
projects, and whether the sequel actually picked the work up. `amem handoffs` records these
events in `public.handoffs`. `amem evals harvest` reads them.

Who writes:

- `auto`: a Stop hook in agent-config (phase 2) runs `handoffs log` when it hands off.
- `manual`: the handoff skill runs `handoffs log` when you ask for a handoff.
- The sequel session runs `handoffs resume` when it picks the handoff up.

## Commands

| Command | Does |
|---|---|
| `amem handoffs log --session ID --path PATH [--project P] [--reason auto\|manual] [--tokens N] [--pct X] [--title T]` | Inserts one row and prints its id (a uuid). `--reason` defaults to `manual`. `--pct` is 0..100. `--title` is the sequel chat's title. |
| `amem handoffs resume --id ID --session NEW_ID` | Marks the handoff as resumed by `NEW_ID`. Idempotent for the same session. Exit 1 when the id does not exist, or when another session already resumed it (nothing changes). |
| `amem handoffs list [--days 7]` | Handoffs created in the last N days as JSON lines, newest first. Read only. |

Bad arguments exit 2. `log` and `resume` honour `AMEM_REQUIRE_REMOTE=1` (exit 3).

Both backends work: Postgres (`AMEM_DB_URL`) and REST (`AMEM_BACKEND=rest` /
`AMEM_SUPABASE_URL`, docs/cloud.md), so a hook in a cloud session can log too.

## Table

`public.handoffs` (migration `20261020020000_handoffs.sql`):

| Column | Meaning |
|---|---|
| `id` | uuid, the id `log` prints |
| `session_id` | the session that handed off (no foreign key: it may not be ingested yet) |
| `project` | repo or project name, null when unknown |
| `handoff_path` | the handoff file the sequel reads |
| `reason` | `auto` or `manual` |
| `context_tokens`, `context_pct` | context size and use at handoff time, null when unknown |
| `title` | the sequel chat's title, null when none |
| `created_at` | when it was logged |
| `resumed_session_id`, `resumed_at` | the session that resumed it and when; null until resumed |

`amem evals harvest` reads `session_id`, `project`, `handoff_path`, `reason` and
`created_at`. Do not rename them.

RLS is on with no policies, and the Supabase API roles get no grants, like every other table.

## Example

```sh
id=$(amem handoffs log --session "$SESSION" --path ~/.claude/handoffs/x.md --project agent-memory \
       --reason auto --tokens 180000 --pct 90 --title "Sequel: events")
amem handoffs resume --id "$id" --session "$NEW_SESSION"
amem handoffs list --days 1
```
