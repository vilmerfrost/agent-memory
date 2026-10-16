# Production writer inventory (Mission 003, Phase A)

Status on 2026-10-16: **all writers listed as active are still active. Production is NOT frozen.**
The freeze is prepared in [freeze.md](freeze.md) and needs Vilmer's approval to run.

"Production" = the files agents load (`~/.claude/**`, `~/.agents/**`, `~/.codex/AGENTS.md` and
`config.toml`, `~/agent-config/**`), launchd plists, Desktop scheduled-task definitions, and the
default branches of `agent-config`, `~/.agents` and `agent-memory`. Rows in Supabase and push
notifications are operational records, not production changes.

Classes:
- **improvement**: an automated change to agent behaviour. Frozen, then stage-only.
- **protective**: restores or restricts. Stays automatic.
- **telemetry**: logs, reports, caches, DB rows. Stays.
- **external**: writes another repo or branch, not production. Stays (privacy notes below).
- **human**: run by Vilmer by hand. Unchanged.

Sources: AC = `~/agent-config` (main `907d780`), AM = `agent-memory` (main `32ea623`).
Line numbers are from those commits. [V] = read in code or config on 2026-10-16.

## Improvement writers (must become stage-only)

| # | Writer | Trigger | Modifies | Safety today | Stage-only plan | Activation and verification | Restore |
|---|---|---|---|---|---|---|---|
| W1 | `amem daily` → `edits run` → `improve apply` | launchd `com.vilmerfrost.amem-daily` 09:20, env `AMEM_EDITS_LIVE=1` [V AC/launchd/com.vilmerfrost.amem-daily.plist; same as the installed copy] | one rule section in `~/.claude/CLAUDE.md`, `~/agent-config/parent/PARENT.md`, `~/agent-config/agents/*.md`, `~/.agents/skills/*/SKILL.md`, `~/.claude/scheduled-tasks/*/SKILL.md`, `~/.codex/AGENTS.md` [V AM/edits_targets.py] | improve scope, pins, content guard, kind check, 7-day cooldown, evals **after** the live write [V AC/bin/improve:1117] | Freeze: remove `AMEM_EDITS_LIVE`. Then `AMEM_EDITS_MODE=stage`: `edits run` calls `improve stage` and creates a proposal instead of applying | plist without the variable; the next `amem-daily` routines.log line has no `edits=`; no new `edits` rows | put `AMEM_EDITS_LIVE=1` back in the plist, bootout + bootstrap |
| W2 | `workflow-improve-weekly` → `improve apply` | Desktop task, Sun 08:00, permission mode auto [V scheduled-tasks.json] | PARENT.md, worker.md, lessons.md, `hooks/*`, `~/.agents/skills/**`, `~/.claude/CLAUDE.md`; criteria also allows the scheduled-task prompts, `settings.json` and `~/.codex/AGENTS.md` [V ST prompt:23, criteria.json:20-30] | same as W1; one commit per change; report pushed to `claude/routine-reports` | Freeze: `criteria.json` `max_changes_per_week: 0` makes every `improve apply` refuse with "weekly cap reached" [V improve:216-217, 1081-1082]. Then `apply_mode: "stage"` (new code) | `improve list` shows "changes left this week: 0"; a dry-run apply is REFUSED; the next Sunday ledger has no `applied` events | set `max_changes_per_week` back to `null` |
| W3 | `ab-lessons-monthly` → `evals/ab-lesson --all-trial` | Desktop task, 1st of month 03:30 | `~/agent-config/lessons.md` Status + Evidence lines; **commits and pushes** the checked-out branch [V ab-lesson:222-228, 307-311]. Workers read lessons.md live [V worker.md:24] | refuses when the real lessons.md or worker.md changed during the run [V ab-lesson:247]; git_guard allows only lessons.md | Freeze: disable the Desktop task. Then (new code) ab-lesson stages a lessons.md proposal when `apply_mode` is `stage` | the task list shows `enabled: false`; no `lessons: A/B` commits | re-enable the task |
| W4 | `improve apply` (any caller, including attended sessions) | CLI | any allowed target | as W1 | `apply_mode: "stage"`: apply refuses unless `--human` from a TTY outside an agent session | `improve apply --dry-run` prints the refusal | `apply_mode: "legacy"` or remove the key |
| W5 | `improve amend` | CLI, Vilmer only (refused in improver sessions [V improve:1347]) | an applied change's target | human-only guard | unchanged (human) | — | — |

Next scheduled runs while W1–W3 are still active: **W1 every day at 09:20** (next 2026-10-17), **W2 Sunday
2026-10-18 08:00**, **W3 2026-11-01 03:30**.

## Protective writers (stay automatic)

| # | Writer | Trigger | Modifies | Why it is protective | Change under V1 |
|---|---|---|---|---|---|
| P1 | `improve apply` failed-eval write-back | inside apply | restores the old text when evals regress [V improve:1136-1137] | restores the previous content | none (apply itself is frozen) |
| P2 | `improve recheck` → auto `revert` on regression | weekly improver | reverts a kept change [V improve:1212] | returns to the last approved state | none |
| P3 | `amem edits undo-poll` → `improve revert` | launchd `amem-ingest`, every 15 min | reverts an edit after a **phone Undo** [V AM/edits_push.py:198-205] | human-requested undo | generalized to the V1 decisions poll |
| P4 | `improve fence-check` restore | `improve report` | restores fenced files an improver run changed [V improve:1665-1707] | restores | none |
| P5 | `autopin_scan` | `improve run` | adds pins after a hand edit [V improve:570-601] | only restricts automation | none |
| P6 | `evals/run.py` restore of worker.md and hooks | every eval run | restores files a case changed [V run.py:1502-1505] | restores | becomes moot once evals run in the sandbox |
| P7 | `canary-parent`, `canary-simplitics` | launchd 03:20 / 03:00 | create and delete scratch worktrees and branches in opted-in repos | temporary test scaffolding | none |

## Telemetry writers (stay)

`amem-ingest` (DB rows), `amem daily` ingest, score and deviation steps (DB rows, escalation push),
`rsi-nightly` (DB rows; `~/.claude/parent/skill-cache.json`, read by `skillpick`; eval drafts),
`config-check` + `routine-sync` (routines.log, cloud reports), `routine-audit` (reports, push),
`secret-scan`, `dep-audit`, `deviations-daily` and `abandoned-audit` (read-only DB, reports),
`review-skills-audit` (reports; `~/.agents/skill-usage/SKILL-USAGE.md`), improve suggestion
commits to `improve/suggestions.jsonl`.

Note: `skill-cache.json` is derived data that a local script reads. It is not an instruction change.
It stays telemetry in V1. Reclassify it if `skillpick` starts injecting skills.

## External writers (not production, kept)

| Writer | Writes | Note |
|---|---|---|
| Nightly Trials (cloud, daily 02:07 UTC) | `nightly-trials` branch `claude/nightly`; Supabase `runs`/`cloud_digests` through a network secret that maps to the service role [V docs/cloud.md:25-29] | narrow the credential (Mission 002, M15) |
| `nightly-trials-weekly-sync` (Desktop, Sun 18:00) | `nightly-trials` main: baseline snapshot + mined tasks; **pushes** [V ST prompt:10]; reads `~/.claude/projects` (mine_sessions.py:18) | privacy: mined tasks leave the Mac to a GitHub repo; add a secret-scan gate |
| Fresh install (cloud, Mon 04:00 UTC) | `agent-config` branch `claude/routine-reports` | report branch only |
| `improve report --publish` | `agent-config` branch `claude/routine-reports` [V improve:1957-1962] | report branch only |

## Human-run (unchanged)

`bin/bootstrap` (symlinks, LaunchAgents, `git pull`), `bin/eval-promote`, `improve amend|pin|unpin`,
manual edits, and attended parent sessions that write `lessons.md` (PARENT.md:101-110).

## Residual paths (risks, not routines)

- Desktop scheduled tasks run in permission mode `auto` with prompt-only "read only" rules. An LLM in
  such a session could edit production files with its file tools. The fence hook is on only for
  improver sessions [V lib/fence.py:74-77]. V1 adds a guard in the code paths above. Extending the
  fence to all unattended sessions is a hook change, prepared but not installed (freeze.md, optional).
- `amem` loads a database DSN from `.env.local` that bypasses RLS. Any local process running `amem`
  can write any table, decisions included. The V1 decision path adds checks (token-verified rows,
  a TTY-only CLI, agent-session refusal). It does not remove this same-user trust boundary. A
  least-privilege DSN for automation is an activation item.
