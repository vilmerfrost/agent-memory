# Outcome check: did an approved change help?

`amem rsi outcome` runs every day at 09:20 (`deploy/launchd/com.vilmerfrost.amem-rsi-outcome.plist`). It closes the
loop after Night Desk: before 2026-10-26 nothing checked whether a change Vilmer approved worked in daily use.

It only measures and advises. It never edits a rule file and never undoes a change.

```
promoted night proposal ─┐                                   ┌─▶ rsi_outcomes (one row per change)
kept edit-loop edit ─────┴─▶ deviations per session, before ─┼─▶ reports/rsi-outcome-<date>.md + .json
                             vs after the apply time         └─▶ routines.log: amem-rsi-outcome PASS|FAIL ...
```

## Which changes

| Kind | Rows | Rule | Applied at |
|---|---|---|---|
| `proposal` | `rsi_proposals` with `status = promoted`, not reverted | the candidate's `rule_key` (else the `edits` row that names the proposal) | `promoted_at` |
| `edit` | `edits` with `status in (kept, trial)`, not reverted, no Undo requested | `instruction_source` + `rule_id` | `created_at` (written just before `improve apply`) |

## How it measures

- Two windows of the same length around the apply time t: `[t - W, t)` and `[t, t + W)`, with
  `W = min(14 days, days since t)`.
- Only real sessions count: not `sandbox` or `eval`, already scanned for deviations (`scanned_at`), and that
  loaded the rule's file (`sessions.instructions` holds the instruction source). A session is placed by its start.
- Per window: sessions `n`, the rule's deviations `k` in them, rate `k / n` (deviations per session).

## Verdicts

| Verdict | When | Suggested Undo |
|---|---|---|
| `too-early` | applied less than 3 days ago, or fewer than 5 sessions in either window | no |
| `helped` | one-sided exact test (given all `k_b + k_a` deviations, the after count is binomial with `p = n_a / (n_a + n_b)`) at p <= 0.10, and the rate fell by at least 25% | no |
| `worse` | the same test the other way, and the rate rose by at least 25% (or from zero) | yes |
| `no-change` | anything else, also "no deviations before or after" | yes, once the full 14-day window was observed |

The thresholds are constants in `src/agent_memory/rsi_outcome.py` and are stored with each row (`meta.thresholds`).

## Output

- **`rsi_outcomes`** (migration `20261026020000_rsi_outcomes.sql`, RLS on): primary key `(change_kind, change_id)`,
  rewritten on every run with the latest verdict (`first_checked_at` stays). Columns: `rule_key`,
  `instruction_source`, `rule_id`, `title`, `applied_at`, `checked_at`, `window_days`, `sessions_before/after`,
  `deviations_before/after`, `rate_before/after`, `p_value`, `verdict`, `reason`, `suggest_undo`, `undo_hint`, `meta`.
  Readers: `amem_app` (writes), `rsi_reader`/`rsi_writer`, and Night Desk's owner (`authenticated`, owner policy).
- **Report files**: `~/.claude/parent/reports/rsi-outcome-<date>.md` (worse first, then no change, helped, too early)
  and `.json` (`format: rsi-outcome/1`). They keep the day-by-day history.
- **routines.log**: `<ts> amem-rsi-outcome PASS changes=N helped=H no_change=C worse=W too_early=E suggest_undo=U
  <secs>s report=<md path>`, plus `amem-rsi-outcome-fail database: ...` when the database could not be read, and
  `amem-rsi-outcome-warn` for a change it could not name a rule for.

## Commands

```sh
uv run amem rsi outcome --dry-run          # measure and print; writes no row, file or log line
uv run amem rsi outcome                    # what launchd runs (needs a remote database under AMEM_REQUIRE_REMOTE=1)
```

Undo stays a human action: Night Desk's Undo button (or `amem rsi decide <proposal> undo`, `amem edits undo <edit>`
in the terminal). The `undo_hint` column says which.

## Mixed

When the rule's own text changed again inside the before/after windows (a hand edit or another change, from the
rule file's git history), the verdict is `mixed`: the counts are kept, the change is neither credited nor blamed,
and no Undo is suggested. See [hand-edits.md](hand-edits.md).
