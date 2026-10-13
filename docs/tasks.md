# Dispatch tasks: `amem ingest tasks`

The parent's real agent work lands in `runs`, next to the routine runs. One row per
finished dispatch, so Jev can score it and lessons can learn from it.

```
<parent-dir>/<repo>/tasks.jsonl ──┐
<parent-dir>/<repo>/checkpoints/  ─┴─ amem ingest tasks ──▶ runs (routine = dispatch)
                                                             ├─▶ amem score (questions/dispatch.toml)
                                                             └─▶ amem lessons distill (sibling)
```

```
amem ingest tasks [--parent-dir PATH]     # default ~/.claude/parent
```

Prints `ingested N new, M already present, K unparsed (P repos)`. P = the number of
`<repo>/tasks.jsonl` files read. Detail goes to stderr: unparsed `repo/tasks.jsonl:line`
numbers, repeated task ids, a skipped unterminated last line, and rows refreshed.

The source files are read only. Nothing is ever written under the parent dir.

## Source

- `<parent-dir>/<repo>/tasks.jsonl`: one JSON object per finished dispatch, written
  by `improve record`.
- `<parent-dir>/<repo>/checkpoints/<worker name>.md`: each worker's checkpoints and
  final report. A missing file is skipped (`checkpoint_excerpt: null`).

## Row contract

| Column | Value |
|---|---|
| `routine`, `source` | `dispatch`, `dispatch` (migration `20261015010000_runs_source_dispatch.sql` adds `dispatch` to the source check) |
| `status` | `DONE` → `pass`; `PARTIAL`, `BLOCKED` → `fail`; anything else or missing → `error` (raw word in `meta.raw_status`). Case-insensitive. |
| `started_at`, `finished_at` | `started`, `ended` (ISO 8601; no offset = UTC; missing or invalid = null) |
| `summary` | One deterministic line, e.g. `junk-orchestra: BLOCKED, 1 worker, dod_first_pass=false, denials=1 (heredoc), reasons=denied-command` |
| `failure_reason` | `blocked_partial_reasons` joined with `, `, or null |
| `source_key` | `dispatch:<task id>` |

Summary parts: repo, the raw final status (`UNKNOWN` when missing), worker count
(`n_workers`, else the worker list length), `dod_first_pass` (true/false/null),
`denials` (else the sum over workers), the denied command shapes in brackets
(sorted, only when any), `reasons=` (only when any).

`meta`:

- Always: `repo` (the task's `repo`, else the folder name; the lessons scope),
  `task_id`, `final_status`, `raw_status`, `log_source = "tasks.jsonl"`, `content_sha256`.
- Task fields: `session`, `parent_model`, `parent_effort`, `duration_s`, `tokens`,
  `dod_first_pass`, `integration_first_pass`, `follow_ups`, `nudges`, `denials`,
  `escalations`, `merge_conflicts`, `clarifying_questions`, `n_workers`,
  `blocked_partial_reasons`.
- `workers`: one compact object per worker: `name`, `agent_id`, `model`, `effort`,
  `final_status`, `first_status`, `denials`, `denied_shapes`, `errors`, `brief_gaps`,
  `reasons`, `tool_errors`, `repeat_cmds`, `fg_sleeps`, `lessons_read_first`,
  `tokens`, `duration_s`, `handbacks`, `nudges`, `follow_ups`, `open_questions`,
  `checkpoint_excerpt`.

Missing or mistyped fields never fail a line: numbers and flags become null, lists
`[]`, maps `{}` (a string where a list belongs becomes a one-item list). A line is
**unparsed** only when it is not a JSON object or has no `id`. Blank lines are ignored.
An unterminated last line is kept when it is already a whole JSON object; otherwise it
waits for the next ingest.

## Checkpoint excerpt

At most 1500 characters per worker:

1. Prefer the report sections `Decisions:`, `Denied:`, `Open:` (the last of each, in
   that order). A section runs until a blank line, a heading or the next `Field:` line.
2. Without them: the newest `### HH:MM — ...` checkpoint blocks that fit whole (the
   last one holds `Status:`). A last block over the limit keeps its head, ending `...`.
3. Credential-shaped strings (`sk-…`, `ghp_…`, JWTs, `user:pass@` in URLs) become
   `[redacted]` first.
4. A worker name that is not a plain file name (`/`, `..`, leading dot) gets no excerpt.

Limit: checkpoint files are per worker **name**. When a name is reused across tasks in
one repo (e.g. `worker-d`), every such task gets the same file's excerpt.

## Idempotency

`meta.content_sha256` hashes the row as it would be written (columns + meta, excerpts
included). On re-ingest, a row with the same hash is left untouched. When the hash
differs (the task line changed, or a checkpoint grew), the columns are refreshed and
the meta keys merged (`runs.meta || new meta`): keys other tools added, such as a
distill mark, are kept. Refreshed rows count as "already present". Scores already
given to a refreshed row are kept. A task id repeated across lines keeps its last line.

## Backends

Postgres only in V1. On the REST backend the command exits 1 and reads nothing, like
the scoring commands. `AMEM_REQUIRE_REMOTE=1` applies (exit 3 on a local database).

## Daily step

`amem daily` runs this right after the git-ref ingest (docs/daily.md). The parent dir is
the routines log's folder (`~/.claude/parent` by default). No `*/tasks.jsonl` at all is
not a failure. Any other error is a failed step (`amem-daily-fail tasks: ...`); the
later steps still run. The house line gets `tasks=N` (new tasks ingested).

## Jev state and questions

`questions/dispatch.toml`: `verdict` (choice clean | struggled | failed; pass = clean),
`brief_quality` (score 0..3), `main_cause` (choice brief_gap | tool_denial |
test_failure | scope | none), `lesson_worthy` (noul). Threshold 0.7.

`scoring.format_state` has a dispatch branch (routine `dispatch` only; every other
routine's state is byte-identical, so recorded fixture keys do not change):

```
routine: dispatch
repo: junk-orchestra
status: fail
final_status: BLOCKED
summary: junk-orchestra: BLOCKED, 1 worker, dod_first_pass=false, denials=1 (heredoc), reasons=denied-command
failure_reason: denied-command
signals: dod_first_pass=false integration_first_pass=(none) follow_ups=0 nudges=0 denials=1 escalations=1 merge_conflicts=0 clarifying_questions=0
workers:
- worker-d: final_status=BLOCKED first_status=BLOCKED denials=1 denied_shapes=heredoc:1 errors=isolation-refusal:1 tool_errors=1 repeat_cmds=0 reasons=denied-command brief_gaps=vilmer-words,dod,siblings,skills,standing-rules lessons_read_first=false
  checkpoint: (none)
```

No ids, timestamps, tokens or durations. Lists are comma-joined, maps are sorted
`k:v`, an excerpt is collapsed to one line.

## Tests

`tests/test_tasks_*.py`. `tests/fixtures/tasks/` is a read-only copy of the real
`tasks.jsonl` files (41 lines, 3 repos) and their checkpoints.
