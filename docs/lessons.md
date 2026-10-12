# Lessons

A lesson is a short rule learned from a run. The loop:

1. **Propose.** `amem lesson propose` asks Jev whether a run holds a reusable lesson. Only a clear yes is stored, as `trial`.
2. **Inject.** `amem lessons for` picks the best lessons for a task and prints a block for the worker brief. Each injection is logged in `lesson_uses`.
3. **Score.** `amem lessons score` compares runs that got a lesson with runs in the same scope that did not.
4. **Retire.** A lesson with enough uses that never beats the baseline becomes `retired`.

`lessons.md` stays the human view. `amem lessons import` and `amem lessons export` move lessons between the file and the table.

Postgres only in V1. Nothing calls an LLM to write lesson text; Jev only gates.

## Tables

Migration `supabase/migrations/20261014030000_lessons.sql`.

`lessons`

| Column | Meaning |
|---|---|
| `slug` | Unique. The lessons.md `Id:`. |
| `title`, `rule` | The heading, and what a worker should do. Imported lessons have `rule = title`. |
| `scope_repo` | Null = every repo. |
| `scope_task` | Null = every task. Otherwise a phrase the task text must contain (case-insensitive). |
| `status` | `trial`, `kept`, `retired` or `promoted`. |
| `source_run_ids` | The runs the lesson came from (`uuid[]`). |
| `evidence`, `evals`, `promoted_to`, `since` | From lessons.md. |
| `tsv` | Generated `tsvector` (english): title weight A, rule B, evidence C. GIN index. |
| `embedding` | `vector(1536)`, null. Reserved for later; V1 never fills it. Only added when pgvector is installed (on Supabase it lives in schema `extensions`). |

`lesson_uses`: one row per injection. `lesson_id`, `run_id` (nullable), `injected_at`, `context` (the task text), `outcome` (`pass`, `fail`, `unknown` or null).

Both tables have RLS on with no policies, and no grants for `anon`/`authenticated`, like `runs`.

## Commands

| Command | Does |
|---|---|
| `amem lessons import PATH` | Upserts lessons from a lessons.md file by `Id`. Prints `imported N new, M updated`. Unchanged lessons count as neither. |
| `amem lessons export` | Prints every lesson in lessons.md format. |
| `amem lessons for --repo R --task "..." [--limit 5] [--run ID] [--min-relative-rank 0.2]` | Prints a markdown block for a brief and records one `lesson_uses` row per lesson. |
| `amem lessons score [--min-uses N] [--repo R] [--apply]` | JSON lines, one per active lesson. Dry run unless `--apply`. |
| `amem lesson propose --run ID [--repo R] [--task T]` | Jev write gate. Prints `accept ...` or `reject ...`. |
| `amem gate friction --text "..." [--seen N]` | Jev improver gate. Prints `accept\|reject confidence=...`. |

Write commands (`import`, `for`, `propose`, `score --apply`) obey `AMEM_REQUIRE_REMOTE=1` against `AMEM_DB_URL` and exit 3 like the other write commands.

Lessons are Postgres-only in V1. On the REST backend (`AMEM_BACKEND=rest`, see docs/cloud.md) every lessons command except `gate friction` exits 1, because the runs live in another store.

## lessons.md format

```
### <title>
Id: <slug>
Evals: <text>
Promoted-to: <text>
Evidence: <text; may continue over more lines and blank-line paragraphs>
Status: trial | kept | retired | promoted
Since: YYYY-MM-DD
```

- Text before the first `### ` is a preamble and is ignored.
- A key starts a field. Every next line that is not a key continues that field.
- `Status: reverted` (the old word) is read as `retired`. A missing status is `trial`.
- A missing `Id:` becomes a slug of the title.
- Export adds optional keys when the table has the data: `Rule:` (rule differs from title), `Repo:`, `Task:`, `Source-runs:`.
- Unknown status, a bad date, a repeated key or a repeated `Id` stops the import with exit 1. Nothing is written.

Round trip: import, export, import again prints `imported 0 new, 0 updated`.

Import treats the file as the truth for the lessons it holds. Re-importing an old file resets their status, for example a lesson that `score --apply` retired.

## Retrieval: `lessons for`

- Query: `websearch_to_tsquery('english', task)`. AND is relaxed to OR, so any matching term retrieves. A query with a negation (`-word`) stays strict.
- Filter: status `trial`, `kept` or `promoted`; `scope_repo` null or equal to `--repo`; `scope_task` null or contained in the task.
- Order: `ts_rank` desc, then repo-scoped before global, then promoted, kept, trial.
- Noise cut: a hit ranked below 0.2 x the top hit is dropped and not recorded. OR matching would otherwise pull in a lesson that shares one common word with the task. Set `--min-relative-rank` or `AMEM_LESSONS_MIN_RELATIVE_RANK` (0 keeps all).
- No match, or only stop words: prints nothing, records nothing.
- `AMEM_MEMORY=off` is the A/B switch: prints nothing, records nothing, exit 0.

Example output:

```
## Lessons from past runs

Follow these unless the task says otherwise:

- **Workers: no compound `cd && ...` or heredoc chains ...** (`no-compound-cmds`, promoted)
```

## Write gate: `lesson propose`

Jev gets a state built from the run: routine, status, repo, task, summary, failure reason and `meta.failures`. The state holds no ids or timestamps, so fixtures stay stable.

Questions:

- `reusable`: noul "Is there a reusable lesson in this run?" (value = P(yes))
- `generalizable`: score "How generalizable (0-1)?" with levels `Specific to this one run`, `Applies to many future runs` (value = expected score in 0..1)

Accept when both pass:

| Threshold | Flag | Env | Default |
|---|---|---|---|
| P(reusable) | `--min-reusable` | `AMEM_PROPOSE_MIN_REUSABLE` | 0.6 |
| generalizable | `--min-generalizable` | `AMEM_PROPOSE_MIN_GENERALIZABLE` | 0.5 |

On accept, a deterministic template writes the lesson. Slug: `<routine>-<first 8 chars of run id>`. Title and rule come from the failure reason, else the first failure, else the summary. Status `trial`, `since` = the run date, `source_run_ids = [run]`. Proposing the same run again leaves the stored lesson alone.

Exit 0 on accept or reject. Exit 1 on an unknown run, a database error or a missing Jev fixture.

## Improver gate: `gate friction`

Question `recurring`: noul "Is this a recurring friction, not a one-off?". The state is the text plus `Seen N time(s) so far.` when `--seen` is given.

- Accept when P(recurring) >= `--min` / `AMEM_FRICTION_MIN` (default 0.5).
- Output: `accept|reject confidence=C p_recurring=P threshold=T`. `confidence` is the probability of the printed decision (P for accept, 1 - P for reject).
- Exit 0 accept, 1 reject, 2 no decision (Jev error, missing fixture).

## Scoring: `lessons score`

Per active lesson (`trial`, `kept`, `promoted`):

- **Uses**: `lesson_uses` rows with a known outcome. `outcome` pass/fail wins. Otherwise the linked run's status: `pass` is pass, `fail`/`error` is fail. A run counts once.
- **Baseline**: non-skip runs in the lesson's repo scope that never got this lesson. A run's repo is `runs.meta.repo`, else its routine name. A null-scope lesson uses every run. `scope_task` is not used here (runs have no task field).
- **Decision**:
  - `insufficient`: uses < `--min-uses` (default 3, or `AMEM_SCORE_MIN_USES`)
  - `no_baseline`: no baseline runs
  - `keep`: pass rate with the lesson > baseline pass rate
  - `retire`: otherwise, for `trial`/`kept`
  - `review`: otherwise, for `promoted`. A promoted lesson is part of the worker rules, so a human decides.
- `--apply` writes `status = retired` for `retire` rows. It never promotes.

Each line: `slug`, `status`, `scope_repo`, `uses`, `uses_pass`, `with_pass_rate`, `baseline_runs`, `baseline_pass`, `baseline_pass_rate`, `decision`, `applied`.

## Jev fixtures

Tests never call Jev. Fixtures live in `tests/fixtures/jev/<key>.json`, where `key = jev.fixture_key(state, questions)`. Use `lessons.propose_state(...)` or `lessons.friction_state(...)` with `lessons.PROPOSE_QUESTIONS` / `lessons.FRICTION_QUESTIONS` to compute a key for a new case.
