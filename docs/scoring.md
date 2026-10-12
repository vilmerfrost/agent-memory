# Scoring: Jev scores, labels, escalations, calibration, drift

Roadmap B2–B6. Jev (TypeSafe System One) answers typed questions about each
routine run. Low-confidence answers go to Vilmer as 👍/👎 labels. The labels
show how far Jev can be trusted and where the escalation threshold should sit.

```
runs ──amem score──▶ scores ──amem escalations──▶ Vilmer ──amem label──▶ labels
                        │                                                  │
                        └──────────── amem calibrate / amem drift ◀────────┘
```

Postgres only (psycopg) in V1: on the REST backend (docs/cloud.md) every
scoring command exits 1 with a clear message. Jev runs in fixture mode unless the live rule
in the README holds. Nothing in this flow pushes a notification: `escalations`
only prints.

## Tables (`supabase/migrations/20261014020000_scores_labels.sql`)

| Table | Columns | Rules |
|---|---|---|
| `scores` | `id`, `run_id` → runs (cascade), `question`, `kind` (choice\|score\|noul), `value` text, `value_num`, `confidence`, `probabilities` jsonb, `jev_mode` (fixture\|live), `created_at` | unique `(run_id, question)` |
| `labels` | `id`, `run_id` → runs (cascade), `verdict` (up\|down), `note`, `labeled_at` | unique `run_id`: one current label; a relabel overwrites verdict, note and `labeled_at` |

- `value`: the label (choice), the expected score (score), or P(yes) (noul).
- `value_num`: null for choice; the number for score and noul.
- `confidence`: from Jev for choice and score. Null for noul: Jev gives none.
- Both tables have RLS on with no policies, and `anon`/`authenticated` lose all
  table rights when those roles exist (Supabase), like the other migrations.

## Label meaning

- `up` (👍): the run is fine. Nothing needs action.
- `down` (👎): something in the run needs a human.

The label is a verdict on the run, not on Jev. Agreement compares it with Jev.

## Question files: `questions/<routine>.toml`

One file per routine. Parsed with stdlib `tomllib`. `AMEM_QUESTIONS_DIR`
overrides the folder (tests use `tests/fixtures/questions/`).

```toml
threshold = 0.7              # choice/score confidence below this escalates the run
pass_question = "verdict"    # a choice or score question: Jev's up/down for the run
pass_options = ["ok"]        # choice pass_question: these labels mean "up", all others "down"
# pass_min = 2.0             # score pass_question instead: score >= pass_min means "up"

[[questions]]                # 3 to 5 entries, sent to Jev in file order
name = "verdict"             # identifier, unique in the file
kind = "choice"              # choice | score | noul
prompt = "Does this run need Vilmer to act?"
[questions.options]          # choice only: label -> description (at least 2)
ok = "..."
needs_action = "..."

[[questions]]
name = "urgency"
kind = "score"
prompt = "How soon should it be fixed?"
levels = ["No action", "Can wait", "This week", "Today"]   # score only: level i = score i; range 0..len-1

[[questions]]
name = "exposure"
kind = "noul"
prompt = "Is anything exposed beyond this machine?"
# [questions.criteria] with optional `true` / `false` descriptions
```

Mapping to Jev: `jev.choice(name, prompt, options)`, `jev.score(name, prompt, levels)`,
`jev.noul(name, prompt, criteria)`. Unknown keys and bad values fail loudly
with the file name.

Shipped files: `config-check`, `evals-parent`, `canary-simplitics-selftest`,
`routine-audit` (has the `triage` choice: real_failure | environment_noise | unclear).
A routine without a file is never scored.

## State string (what Jev reads)

`scoring.format_state(run)`:

```
routine: config-check
status: fail
summary: pass=36 fail=5 info=0 10s report=~/.claude/parent/reports/config-check-2026-10-12.md
failure_reason: (none)
failures:
- Orca LAN reach: answers on 192.168.50.152:6768
- repos in iCloud: 1: ~/Desktop/Projects/Classifed
```

- Fields in this fixed order. `summary` is verbatim, including `report=`: the
  canary selftest's scenario lives in its report file name.
- `failures` = `meta.failures` in order, one `- ` line each, or `failures: (none)`.
- Null or blank values print `(none)`. Any whitespace run, newlines included,
  becomes one space.
- Ids, timestamps and other `meta` keys are left out. Identical outcomes have
  the same state, so they share one fixture.

## Commands

| Command | Does |
|---|---|
| `amem score [--routine R] [--since ISO] [--limit N]` | Scores unscored runs whose routine has a question file, newest first, at most N (default 100) sent to Jev. Prints `scored N runs, skipped M (no questions), K missing fixtures`. |
| `amem label <run_id> up\|down [--note TEXT]` | Sets the run's label. Prints `labeled <id> <verdict> (new\|relabeled)`. A relabel overwrites the note too (no `--note` clears it). |
| `amem escalations [--routine R]` | JSON lines, newest first: unlabeled scored runs where any choice or score confidence is **strictly below** the routine's `threshold`. |
| `amem calibrate [--routine R] [--min-labels 10] [--target 0.9]` | JSON line per routine with a question file: agreement between Jev and labels, counts, suggested threshold. |
| `amem drift [--routine R] [--window 7d] [--baseline 28d] [--delta 0.2] [--min-runs 5] [--now ISO]` | JSON lines: routines whose pass rate or Jev pass signal dropped. |

`score` and `label` are writes: `AMEM_REQUIRE_REMOTE=1` applies to them (exit 3).
The other three only read. JSON goes to stdout; counts go to stderr.

### score

- Missing fixture (fixture mode): stderr names the run, the fixture key and the
  expected file. The run stays unscored, so the next `amem score` retries it.
  It counts toward `--limit`. Exit code stays 0.
- Other Jev errors (bad fixture, API error): reported, counted as `F failed`,
  exit 1. Already scored runs are kept.
- A scored run is never rescored. Scores for one run are written together.

### escalations

Each line: `run_id`, `routine`, `started_at`, `status`, `summary`, `failures`,
`threshold`, `low` (the questions under the threshold with value and
confidence), `jev_verdict` (up/down from the pass_question) and `answers`.
Noul answers never escalate: they carry no confidence.

### calibrate

- Counted runs: labeled runs that have a score for the `pass_question`.
- **Agree**: Jev's up/down (from `pass_question`, `pass_options`/`pass_min`) equals the label.
- Output: `labels`, `agree`, `agreement`, the four `jev_<up|down>_label_<up|down>`
  counts, the current `threshold`, `suggested_threshold`, `note`.
- **Suggested threshold**: the lowest pass_question confidence `t` such that the
  runs with confidence ≥ `t` number at least `--min-labels` and agree at least
  `--target` of the time. Runs at or above it would be trusted without asking.
- Under `--min-labels` labels (default 10): `suggested_threshold: null`,
  `note: "not enough labels (n < 10)"`. When no level reaches the target, the
  note says so.

### drift

- Window = `[now - window, now)`. Baseline = the period of length `baseline`
  just before the window. They do not overlap.
- Metrics, both 0..1:
  - `pass_rate`: share of runs with status `pass`. `skip` runs are left out.
    Every routine with runs.
  - `jev_pass`: mean Jev pass signal of the `pass_question`. Choice: summed
    probability of `pass_options`. Score: expected score / top level. Only
    routines with a question file, only scored runs.
- Flag when both periods have at least `--min-runs` samples (default **5**) and
  `baseline - window > --delta` (default 0.2). Smaller samples never flag;
  stderr counts them as skipped.
- Each line: `routine`, `metric`, `window_n`, `window_value`, `baseline_n`,
  `baseline_value`, `drop`, `delta`, `min_runs`, `enough_samples`.

## Recording a fixture

Fixture mode reads `tests/fixtures/jev/<key>.json`, `<key>` = `jev.fixture_key(state, questions)`.

1. Run `amem score`. Each missing fixture prints its key and file path.
2. Write that file in the System One response shape (`model`, `usage`,
   `answers`; see `tests/fixtures/jev/*.json`). Add `_note` and `_state` for
   readers; the loader ignores them.
3. Run `amem score` again.

Editing a question file changes its keys: record those fixtures again.

## Demo (throwaway database)

```sh
uv run amem db migrate
uv run amem ingest routines-log tests/fixtures/routines.sample.log
uv run amem score --routine config-check --limit 5   # 5 hand-written fixtures exist
uv run amem escalations
uv run amem label <run_id> down --note "LAN exposure is real"
uv run amem calibrate
```

`tests/test_scoring_cli.py::test_demo_flow_on_sample_log` runs the same flow.
