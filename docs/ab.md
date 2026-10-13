# A/B report: does a lesson help?

Roadmap D1–D2. A lesson is proven by A/B, not by feel. `amem ab report` compares,
per lesson, the runs that got the lesson with the runs in the same scope that did not.
It uses the most trusted outcome for each run and lists the runs whose grade is too
shaky to trust, so Vilmer can label them and make the verdict solid.

```
runs in the lesson's scope ──┬── had the lesson injected (lesson_uses.run_id) ──▶ arm "with"  (A)
                             └── did not                                      ──▶ arm "without" (B)
each run ──▶ outcome: label > confident Jev grade > runs.status
both arms ──▶ pass rates, Wilson 95% CI, z ──▶ verdict: insufficient | WIN | LOSS | TIE
```

Postgres only in V1. Read-only. Code: `src/agent_memory/ab.py`, `src/agent_memory/cli_ab.py`.

## Command

```
amem ab report [--lesson SLUG] [--repo R] [--n 10] [--margin 0.1] [--since ISO] [--json [--runs]]
```

| Flag | Meaning |
|---|---|
| `--lesson SLUG` | Only this lesson, whatever its status (retired too). Unknown slug: exit 1. |
| `--repo R` | Only lessons with `scope_repo = R`, like `lessons score --repo`. |
| `--n N` | Runs each arm needs before a verdict. Default 10. |
| `--margin M` | Pass-rate difference beyond which the lesson wins or loses. Default 0.1. |
| `--since ISO` | Only runs started at or after this time (both arms). |
| `--json` | One JSON line per lesson instead of the text table. |
| `--runs` | With `--json`: add `runs`, every run with the source that decided it. Arm B can hold hundreds of runs, so it is off by default. Without `--json`: exit 2. |

Without `--lesson`, the report covers the active lessons (`trial`, `kept`, `promoted`), in the same order as `lessons score`.

`AMEM_MEMORY` has no effect here. It only switches injection off.

## Arms

The scope is the one `lessons score` uses for its baseline. The SQL is shared (`lessons.RUN_SCOPE_SQL`):

- A run's repo is `runs.meta.repo`, else its routine name.
- A repo-scoped lesson looks at runs of that repo. A lesson with no `scope_repo` looks at every run.
- `skip` runs are left out, even when they carry a label.

Then:

- **with** (A): in-scope runs that have a `lesson_uses` row for this lesson with that `run_id`. A run injected twice counts once.
- **without** (B): every other in-scope run.

`lesson_uses` rows with no `run_id` are not in the report: there is no run to grade. `lesson_uses.outcome` is not used: arm B has no such field, so using it would grade the arms differently.

## Outcome per run

The first source that applies decides. Each run records its source.

1. **label**: the human label. `up` = pass, `down` = fail.
2. **jev**: the Jev answer to the routine's `pass_question` (docs/scoring.md), when its verdict confidence (up vs down, docs/scoring.md "Escalation rule") is **at or above** the routine's `threshold`. Jev's up = pass, down = fail.
3. **status**: `runs.status`. `pass` = pass, `fail` and `error` = fail.

A Jev answer below the threshold is not trusted: `status` decides that run. This is the same line `amem escalations` draws (strictly below the threshold escalates). Scores of a routine with no question file are ignored.

## Verdict

- `insufficient`: either arm has fewer than `--n` runs.
- `WIN`: pass rate with − pass rate without > `--margin`.
- `LOSS`: that difference < −`--margin`.
- `TIE`: otherwise. A difference exactly at the margin is a TIE. The difference is rounded to 9 places before the check, so float noise cannot flip it.

## Uncertainty

Two indicators. Neither changes the verdict.

- **Wilson 95% interval** per arm (`ci95`). It stays inside 0..1 and behaves at small N and at 0% or 100%. Two arms whose intervals overlap a lot are not clearly different.
- **z score** (`z`): pooled two-proportion z of rate A − rate B. |z| ≥ 1.96 is about a 5% significance level. `0.0` when both arms are all pass or all fail. Null when an arm is empty.

## Low confidence: what to label

`low_confidence` lists the runs, in either arm, whose Jev grade was below the threshold and that have no label. `status` decided them. These are the runs to 👍/👎 with `amem label <run_id> up|down`. Once labeled, the label decides the run and it leaves the list.

Each item: `run_id`, `routine`, `arm`, `status`, `confidence`, `threshold`. Sorted by arm (`with` first), then lowest confidence first.

## Output

Text, one block per lesson:

```
cli-lesson  [trial, scope: cli-repo]  verdict WIN  (n>=3, margin 0.10)
  arm       runs  pass  rate  95% CI      label  jev status
  with         3     3  1.00  0.44-1.00       0    0      3
  without      3     1  0.33  0.06-0.79       0    0      3
  diff +0.67  z +1.73
  low confidence, label these (amem label <run_id> up|down):
    9f0c…  t-routine  conf 0.50 < 0.7  with  status pass
```

`label`, `jev` and `status` count the runs each source decided.

JSON, one line per lesson: `slug`, `status`, `scope_repo`, `since`, `n`, `margin`, `verdict`, `with` and `without` (each: `runs`, `pass`, `pass_rate`, `ci95`, `sources`), `diff`, `z`, `low_confidence`. With `--runs` also `runs` (every run: `run_id`, `routine`, `arm`, `outcome`, `source`).

No lesson to report: nothing on stdout, a note on stderr, exit 0.

## Guards and exit codes

| Exit | When |
|---|---|
| 0 | Report printed, or no lesson to report. |
| 1 | REST backend (`AMEM_BACKEND=rest`, docs/cloud.md), unknown `--lesson`, bad question file, database error. |
| 2 | Bad arguments, or `--runs` without `--json`. |
| 3 | `AMEM_REQUIRE_REMOTE=1` and `AMEM_DB_URL` is local or unset. |

The report only reads. It still honors `AMEM_REQUIRE_REMOTE=1`: a verdict read from the local default database would be wrong, not empty.

## Relation to `lessons score`

`lessons score` decides retire/keep with a simple rule (with > baseline). `ab report` is the evidence view: same scope, but a better outcome per run, a sample-size floor, a margin and an uncertainty indicator. On runs with no labels and no scores, its arm counts equal the `uses` and `baseline_runs` of `lessons score` for in-scope uses (tested in `tests/test_ab_report.py::test_scope_matches_lessons_score`).
