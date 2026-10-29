# agent-memory

A setup that edits itself, for Vilmer only.

Deferred work and future questions: [roadmap.md](roadmap.md). Consult it when
discussing what to improve or do next; entries are not automatic work approval.

## Vision (v2)

A routine reads every session: chats, parent dispatches, workers and routines. It finds
where an agent deviated from an instruction: a skill, a CLAUDE.md or PARENT.md rule, or a
routine prompt. It works out why, when and how. It writes two raw grades: did the agent
follow the instruction, and was the result good. Jev (TypeSafe System One) makes the final
call: edit the instruction, enforce it, or leave it.

Later, an agent edits the instruction itself via agent-config's `improve apply`. Each edit is
one commit, `improve revert <id>` undoes it, and evals keep the edit or revert it
automatically. Success: fewer repeated deviations, and agents improve the setup.

```
sessions ──scan──▶ deviations (what / why / how + 2 raw grades) ──Jev──▶ edit | enforce | leave
                                                                           │
                       improve apply (one commit, improve revert undoes) ◀─┘  (E4: amem edits)
```

Status: the deviation finder (E1–E3) is built and read only: `sessions ingest`,
`deviations scan`, `deviations report` ([docs/deviations.md](docs/deviations.md)). The edit
step (E4) and the proof (E5) are `amem edits run|undo|undo-poll|scoreboard`
([docs/edits.md](docs/edits.md)): it rewrites one flagged rule per edit via `improve apply`,
pushes each kept edit with an Undo button, and the scoreboard shows whether the rule now sees
fewer deviations. Live only with `AMEM_EDITS_LIVE=1`. The run log, Jev scoring and labels stay
as they are.

**Frozen:** the v1 lessons memory (`lessons import/export/for/score`, `lesson propose`,
`lessons distill`, and `lessons for` injection into briefs). The code and tables are kept,
but nothing new is wired to them: the output of the loop is now an edit to the instruction
itself, via `improve`, not a separate lesson. `lessons distill` in `amem daily` stays off
unless `AMEM_DISTILL_LIVE=1`.

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
  the canonical line (`report=<path>` cut to its basename), so a cloud run that
  arrives by `ingest git-ref` and by routine-sync's copy is one row. A
  byte-identical repeat line gets an occurrence suffix, so it is kept too.
  Re-ingesting inserts nothing. If a detail line arrives after its summary was
  ingested, the next ingest refreshes only `meta.failures`. Full rule and
  `amem db dedupe-runs` for old rows: [docs/cloud.md](docs/cloud.md#dedupe-keys).

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

## Parts

Each part has its own doc:

| Part | Commands | Doc |
|---|---|---|
| Deviation finder (v2) | `sessions ingest`, `sessions find --agent-id` (incident scans), `deviations scan`, `deviations report [--json]` | [docs/deviations.md](docs/deviations.md) |
| Handoff events | `handoffs log`, `handoffs resume`, `handoffs list` | [docs/handoffs.md](docs/handoffs.md) |
| Edits + proof (v2) | `edits run`, `edits list`, `edits undo`, `edits undo-poll`, `edits scoreboard` | [docs/edits.md](docs/edits.md) |
| Evals harvest (draft eval cases) | `evals harvest --out DIR [--days 7] [--limit N] [--dry-run]` | [docs/evals-harvest.md](docs/evals-harvest.md) |
| Daily job | `daily` | [docs/daily.md](docs/daily.md) |
| Dispatch tasks as runs | `ingest tasks` | [docs/tasks.md](docs/tasks.md) |
| Skill usage + scores | `ingest skills`, `skills scores [--refresh]`, `skills cache PATH`, `skills suggest` | [docs/skills.md](docs/skills.md) |
| Cloud path (HTTPS, no raw Postgres) | `AMEM_BACKEND=rest`, `ingest git-ref`, `jev record`, `tools/amem_digest.py` / `digest-self` (cloud routine sessions) | [docs/cloud.md](docs/cloud.md) |
| Jev scoring + human labels | `score`, `label`, `escalations`, `calibrate`, `drift` | [docs/scoring.md](docs/scoring.md) |
| Lessons memory (frozen) | `lessons import/export/for/score/distill`, `lesson propose`, `gate friction` | [docs/lessons.md](docs/lessons.md) |
| A/B proof | `ab report` | [docs/ab.md](docs/ab.md) |
| Transcript archive (encrypted, local, 180 days) | `archive init`, `archive run [--dry-run]`, `archive verify`, `archive restore`, `archive prune`, `archive status`, `archive key-export`, `archive key-import` | [docs/archive.md](docs/archive.md) |
| RSI V1 approval-first (stage, proposals, decisions, promotion worker, undo) | `rsi targets`, `rsi baseline create/check`, `rsi tracking-plan`, `rsi freeze-status`, `rsi propose`, `rsi push`, `rsi decide`, `rsi decide-poll`, `rsi verify-decision`, `rsi show`, `rsi checkpoint` | [docs/rsi/approval.md](docs/rsi/approval.md) |
| RSI sandbox (isolated Docker workers) | `python3 sandbox/rsi_sandbox.py init/build/net-up/prepare-arm/run/record-eval/cleanup` | [docs/sandbox.md](docs/sandbox.md) |

The v2 loop: `sessions ingest` indexes transcripts → `deviations scan` finds deviations, writes
what/why/how and two raw grades, Jev gives a verdict → `deviations report --json` hands the
candidates to `edits run` → one commit per edit (`improve apply`), a push with an Undo button,
`improve revert` undoes it → `edits scoreboard` shows fewer / same / more deviations per edited rule.

The v1 loop (frozen, kept for history): runs are logged → Jev scores outcomes (low confidence → you label 👍/👎) → Jev gates which runs become lessons → `lessons for` injects the top lessons into briefs (`AMEM_MEMORY=off` turns injection off) → `ab report` shows whether each lesson actually helps → losers retire.

Scoring, lessons, A/B and deviation commands use the Postgres backend only; the REST backend covers `runs` (`log-run`, `ingest`, `runs`) and `handoffs`.

## Tests

Session/parent measurements, coverage, retrieval commands and collector installation:
[Session timing](docs/session-timing.md).

In the managed integration checkout, the full acceptance gate is:

```sh
parent-integrate test
```

It uses the command saved in the local parent profile and records a passing
result for the clean commit and configuration. A failed run records no passing
result. Agents should read `AGENTS.md` for this workflow (`CLAUDE.md` imports it).

For the same full suite with compact live progress in another managed worktree:

```sh
parent-run -- uv run --locked --quiet python -u scripts/integration_tests.py
```

The runner uses four pytest processes with file grouping. Every five seconds it
prints elapsed time, completed/total tests and outcome counts. It reports failures
immediately and keeps running to collect the remaining results. A test counts as
complete after teardown, so cleanup failures are included. The runner returns
pytest's exit code unchanged; it does not retry or suppress failures.

Full verbose pytest output, including failure tracebacks and captured failure
output, is saved under `test-logs/` in this worktree's Git metadata. These logs
are not tracked or shared between worktrees. The runner prints the exact path
and a `tail -f` command; `latest.log` in the same directory points to the latest
run. Keep the live command output visible instead of piping it through `tail -15`.

Needs Docker. Each test process owns one throwaway `pgvector/pgvector:pg17`
container on an OS-assigned port, removed at the end. Use `--workers 2` when
other suites are competing for resources, or `--workers 0` for serial debugging.
Optional pytest arguments go after `--`, for example `-- --durations=25`.

After a failure, fix and rerun the specific test, or rerun cached failures in
the same checkout:

```sh
parent-run -- uv run --locked pytest -q --lf --last-failed-no-failures none
```

If there are no cached failures, this runs no tests; that is not a passing full
suite. Once the focused checks pass, commit the fix and run `parent-integrate
test` again. Passing results from before a code change cannot certify the new
commit, and a direct `parent-run` does not create an integration release receipt.

The original serial command is still available: `uv run pytest -q` (prefix it
with `parent-run --` inside a managed worktree).
