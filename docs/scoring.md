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
in the README holds. The commands on this page never push a notification:
`amem escalations` only prints. The daily job `amem daily` does push: it scores,
then sends one phone (ntfy) or Mac notification for escalations it has not pushed
before (counts and routine names only). See [daily.md](daily.md).

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
threshold = 0.7              # an escalate_on confidence below this escalates the run
pass_question = "verdict"    # a choice or score question: Jev's up/down for the run
pass_options = ["ok"]        # choice pass_question: these labels mean "up", all others "down"
# pass_min = 2.0             # score pass_question instead: score >= pass_min means "up"
escalate_on = ["verdict"]    # optional: the choice/score questions that escalate (default: pass_question)

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

Shipped files: `config-check`, `evals-parent` (own state shape), `canary-simplitics-selftest`
(own state shape), `dispatch` (docs/tasks.md; own state shape), `routine-audit` (has the
`triage` choice: real_failure | environment_noise | unclear). Every shipped file sets
`escalate_on = ["verdict"]`. A routine without a file is never scored.

Writing questions Jev can answer with confidence:

- Answerable from the state alone. A question about something the state does not
  show (the old dispatch `brief_quality`: the brief text is not in the state) gives
  a coin flip, not a judgment.
- Options and levels with concrete criteria that name state fields
  ("final_status is DONE and the friction line is (none)"), mutually exclusive, with
  a tie rule ("pick the one that cost the most").
- No score question whose answer is a count the state already shows (the old
  routine-audit `breadth`). Prefer a choice or a noul.

## Escalation rule

A run escalates when a question listed in `escalate_on` has a confidence **strictly
below** `threshold`. Noul answers never escalate (no confidence).

- **Default: the pass_question only.** The label Vilmer gives is up/down on the run.
  A low confidence on a secondary question (dispatch `main_cause`, config-check
  `urgency`) cannot be settled by 👍/👎, and calibrate measures only the
  pass_question, so it does not push. A file can list more questions to make them
  escalate too (`tests/fixtures/questions/t-routine.toml` lists `severity`).
- **Verdict confidence** (the pass_question): how sure Jev is of up vs down, not of the
  exact label. Jev's confidence for a choice is `(n * p_top - 1) / (n - 1)` over n
  options (measured on the 2026-10-13 export). Two labels on the same side split
  `p_top` although up vs down is clear (evals-parent: regression 0.61 vs
  harness_problem 0.37, both "down", gave confidence 0.41). So:
  `verdict confidence = max(Jev's confidence, 2 * p_side - 1)`, where `p_side` is the
  summed probability of the side Jev's answer is on (choice: `pass_options` vs the
  rest; score: levels `>= pass_min` vs below). `2p - 1` is Jev's own formula for a
  two-way question, so with two options the value equals Jev's confidence. Without
  probabilities it is Jev's confidence. Code: `QuestionSet.verdict_confidence`.
- Other listed questions use Jev's confidence as is.
- `amem escalations`, `amem calibrate` and `ab report` all use this number for the
  pass_question.

## State string (what Jev reads)

`scoring.format_state(run)` (built in `agent_memory/states.py`). Generic shape:

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

Own shapes (each removes a cause of low confidence seen in the 2026-10-13 export):

- **dispatch** (docs/tasks.md): adds a derived `friction:` line (the signals that cost
  extra rounds) and a fixed `legend:` line that explains every signal name, including
  `brief_gaps` (missing template sections, not missing facts) and tool errors (the
  normal edit-test loop, not friction).
- **canary-simplitics-selftest**: the scenario comes first, from the report file name,
  with what it must produce, and the report path is cut to its file name (the temp dir
  is noise). Each failure is tagged `[planted]` or `[other]` by its check name (the
  text before the first `: `), from `states.CANARY_PLANTED`; a `planted_failed:` line
  counts them. An unknown scenario gets no tags. Keep the map in sync with the
  simplitics canary selftest when it gains a scenario.

  ```
  routine: canary-simplitics-selftest
  status: fail
  scenario: env-local
  expected: FAIL on the planted checks: no .env.local
  summary: pass=8 fail=1 warn=2 3s report=report-env-local.md
  failure_reason: (none)
  planted_failed: 1 of 1 failure lines; other: 0
  failures:
  - [planted] no .env.local: present: .env.local
  ```
- **evals-parent**: the generic shape plus a fixed `legend:` line: a failure line names
  the check that did NOT hold ("parent flagged the planted instruction" = it did not
  flag it), `harness` means the harness failed, and which cases are safety cases.

## Commands

| Command | Does |
|---|---|
| `amem score [--routine R] [--since ISO] [--limit N]` | Scores unscored runs whose routine has a question file, newest first, at most N (default 100) sent to Jev. Prints `scored N runs, skipped M (no questions), K missing fixtures`, plus `, F failed` when Jev failed for F runs (then exit 1). |
| `amem score --rescore --routine R [--since ISO] [--limit N]` | Asks Jev again about R's **already scored** runs (labeled or not), newest first, at most N, and replaces their scores. stdout: one JSON line per rescored run (before/after); stderr: the summary. See "Rescoring". |
| `amem label <run_id> up\|down [--note TEXT]` | Sets the run's label. Prints `labeled <id> <verdict> (new\|relabeled)`. A relabel overwrites the note too (no `--note` clears it). |
| `amem escalations [--routine R]` | JSON lines, newest first: unlabeled scored runs where an `escalate_on` question's confidence is **strictly below** the routine's `threshold` (see "Escalation rule"). |
| `amem calibrate [--routine R] [--min-labels 10] [--target 0.9]` | JSON line per routine with a question file: agreement between Jev and labels, counts, suggested threshold. |
| `amem drift [--routine R] [--window 7d] [--baseline 28d] [--delta 0.2] [--min-runs 5] [--now ISO]` | JSON lines: routines whose pass rate or Jev pass signal dropped. |

`score` and `label` are writes: `AMEM_REQUIRE_REMOTE=1` applies to them (exit 3).
The other three only read. JSON goes to stdout; counts go to stderr.

### score

- Missing fixture (fixture mode): stderr names the run, the fixture key and the
  expected file. The run stays unscored, so the next `amem score` retries it.
  It counts toward `--limit`. Exit code stays 0.
- Other Jev errors for one run: reported, counted as `F failed`, exit 1. This
  covers SDK errors (HTTP 4xx/5xx, rate limit, connection, timeout), an invalid
  API response, and a fixture that is not JSON or holds an invalid answer. The
  run stays unscored (the next `amem score` retries it) and scoring goes on with
  the next run, so one bad run never blocks the rest of its routine. The summary
  line ends with `, F failed`. Already scored runs are kept. Error text never
  holds the API key.
- `--since` without a UTC offset is read as UTC.
- Plain `amem score` never rescores a scored run. Scores for one run are written together.

### Rescoring

`amem score --rescore --routine R [--since ISO] [--limit N]` re-asks Jev about runs that
already have scores, for example after a state or question change.

- **Replace, not version.** Each run's new answers replace all its old score rows in
  one transaction (delete + insert). A run never holds old and new rows side by side
  (the table keeps `unique (run_id, question)`), and a question dropped from the file
  disappears from the run. `created_at` and `jev_mode` are the new ones. Labels are kept.
- A missing fixture or a Jev error keeps the run's old rows untouched (counted on stderr;
  a Jev error gives exit 1, like `score`).
- The old answers are not kept in the database: the stdout line is the record. Save it.
- Each stdout line: `run_id`, `routine`, `started_at`, `before` and `after`
  (question → value and Jev confidence), `escalated_before_old_rule` (V1 rule: any
  choice/score raw confidence under threshold, on the old answers),
  `escalates_before` and `escalates_after` (the current rule on the old and the new
  answers, ignoring labels), `low_after` (the escalating answers now).
- stderr summary: `amem: rescored N runs (R), M missing fixtures; escalated before (old rule) B, before C, after A`.
- `--rescore` needs `--routine` (it never touches other routines). `AMEM_REQUIRE_REMOTE=1` applies.
- **Live rows are guarded.** stderr first prints the Jev mode. In fixture mode (no
  `TYPESAFE_API_KEY` + `AMEM_JEV_LIVE=1`), when any selected run holds a `jev_mode='live'`
  row, rescore refuses (exit 1) and changes nothing; `--allow-fixture` overrides that.
- Rescoring changes past `jev_pass` values, so `amem drift` compares the new answers.

Measuring a change with live Jev on the same runs (no double counting, because the
rows are replaced):

```sh
uv run amem score --rescore --routine dispatch --limit 20 > rescore-dispatch.jsonl
jq -s '{runs: length, old_rule: map(select(.escalated_before_old_rule)) | length,
        before: map(select(.escalates_before)) | length,
        after: map(select(.escalates_after)) | length,
        low_after: [.[].low_after[].question] | group_by(.) | map({(.[0]): length}) | add}' rescore-dispatch.jsonl
```

`before` vs `after` isolates the state/question change; `old_rule` vs `before` isolates
the rule change.

### escalations

Each line: `run_id`, `routine`, `started_at`, `status`, `summary`, `failures`,
`threshold`, `low` (the escalating questions with `value`, `confidence` = the number
compared with the threshold, the verdict confidence for the pass_question, and
`jev_confidence` = Jev's raw confidence), `jev_verdict` (up/down from the
pass_question) and `answers`. Noul answers never escalate: they carry no confidence.

### calibrate

- Counted runs: labeled runs that have a score for the `pass_question`.
- **Agree**: Jev's up/down (from `pass_question`, `pass_options`/`pass_min`) equals the label.
- Output: `labels`, `agree`, `agreement`, the four `jev_<up|down>_label_<up|down>`
  counts, the current `threshold`, `suggested_threshold`, `note`.
- **Suggested threshold**: the lowest pass_question verdict confidence `t` (see
  "Escalation rule") such that the runs with confidence ≥ `t` number at least
  `--min-labels` and agree at least `--target` of the time.
- What it measures: only the `pass_question`. Calibrate checks how often the
  pass_question verdict agrees with the label at each confidence level. It does
  not look at the other questions.
- What escalations use: the `escalate_on` questions (shipped files: the verdict
  only). So with `threshold` set to the suggestion, a shipped routine's run escalates
  exactly when its verdict confidence is under the calibrated level. A file that
  lists more questions can still escalate on them; the agreement rate says nothing
  about those.
- Under `--min-labels` labels (default 10): `suggested_threshold: null`,
  `note: "not enough labels (n < 10)"`. When no level reaches the target, the
  note says so.

### drift

- Window = `[now - window, now)`. Baseline = the period of length `baseline`
  just before the window. They do not overlap.
- `--now` without a UTC offset is read as UTC (`2026-10-12T00:00:00` =
  `2026-10-12T00:00:00+00:00`).
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

Editing a question's prompt, options, levels or criteria (or a state shape) changes the
keys: record those fixtures again. For a hand-written fixture whose `_state` and answers
still fit, renaming the file to the new key is enough (`jev.fixture_key(_state,
qset.jev_questions())`). Top-level keys (`threshold`, `escalate_on`, ...) are not part of
the key.

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
