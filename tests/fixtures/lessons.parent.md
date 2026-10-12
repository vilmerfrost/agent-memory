# Lessons

Each lesson: `Id:` (for `evals/ab-lesson <id>`), `Evals:` (eval cases that exercise it),
`Evidence:`, `Status: trial | kept | reverted | promoted`, `Since:`. A trial lesson becomes `kept` only
with an A/B win and `reverted` with an A/B loss (`evals/ab-lesson`); the A/B line is appended to Evidence.

### Workers: no compound `cd && ...` or heredoc chains — use `git -C` / `npm --prefix <worktree>` and Edit/Write
Id: no-compound-cmds
Evals: f-live
Promoted-to: agents/worker.md#Command form
Evidence: junk-orchestra run 2026-10-10. All 3 workers (stats, users, tags) had one chained bash command (cd/heredoc + git) refused by the worktree-isolation check. Each recovered with `git -C` plus Edit/Write, costing one retry each.
Re-check 2026-10-10 (worker-todos): lesson was in the brief, worker still chained heredoc + `cd && npm test` and was refused once. Lesson named only git, so widened to any `cd &&` chain. Still trial.
Re-check 2026-10-10 (simplitics drift check, worker-legacy + worker-deploy): both still refused several times: heredoc + chained curl, `cd`/variables + git, `bash script.sh`, `docker … sh -c`, multi-line docker with $PWD, and the venv python by quoted absolute path (repo path has spaces). Each recovered by splitting commands, no task impact. Lesson reduces but does not prevent it; cost stays one retry. Still trial.
Promoted 2026-10-10 into agents/worker.md "Command form" as a stable rule (Vilmer: the lesson failed again).
A/B 2026-10-11 (evals/ab-lesson, N=3, f-live): with 3/3 pass, drift 0.0; without 3/3 pass, drift 0.0 → TIE. Report: ~/.claude/parent/reports/ab-lesson-no-compound-cmds-2026-10-11.md
A/B regrade 2026-10-11 of the 2026-10-11 run (denial count fixed): with 3/3 pass, drift 0.0; without 3/3 pass, drift 1.0 → WIN (was TIE). Report: ~/.claude/parent/reports/ab-lesson-no-compound-cmds-2026-10-11.md
Status: promoted
Since: 2026-10-10

### Prove safety guards live through the real run path before dispatch
Id: prove-guards-live
Evals: f-live
Evidence: simplitics drift check, 2026-10-10. `.env.worktree` set `QPI_ENABLED=false` as a fail-safe, but nothing loaded it. The operator reads only process env, and docker compose fed it the tracked `qpi-operator.env` (`QPI_ENABLED=true`, real PDQ host, real client key). Only a live `dataops-qpi-operator run` plus `docker compose config` in a hook-created worktree exposed the gap. After the fix: `disabled_no_work`, `AME POST occurred: no`.
Status: kept
Since: 2026-10-10

### UI briefs: browser-check a mid width (1024–1280 px), not only desktop and phone
Id: ui-mid-width
Evals: none (no UI eval case yet)
Evidence: simplitics drift check Phase 5, 2026-10-11. The parent's DoD told all four screen workers to screenshot 1440 and 375 px. Every worker passed, but the parent's live walk found the Inbox Task column at 78 px on a 1024 px viewport (one word per line), because fixed columns ate the width. Fixed in 1635723. Laptops and split screens live between the two checked widths.
Status: trial
Since: 2026-10-11
