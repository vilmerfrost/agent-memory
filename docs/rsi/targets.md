# RSI V1 targets, baselines and tracking

Mission 003, phase C. Every RSI change targets a production config file. This page covers how
those files are listed, snapshotted, checked for drift, and brought under git.

## Target manifest (`amem rsi targets`)

`rsi_targets.discover()` lists the files RSI may change:
- `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`;
- skills: `~/.agents/skills/*/SKILL.md` and `agent-config/parent/skills/*/SKILL.md`;
- scheduled-task prompts: `~/.claude/scheduled-tasks/*/SKILL.md`;
- agent-config: `parent/PARENT.md`, `agents/*.md`, `lessons.md`, `settings.json`, the hooks named in
  `improve/criteria.json` `allowed_targets`, and `launchd/*.plist`.

For each file: path (`~/…`), kind, SHA-256, size, mode, whether it is a symlink, and git state
(repo, tracked, dirty). Read-only. On 2026-10-16 it found 47 files. 9 are not in git: `CLAUDE.md`,
`AGENTS.md` and 7 scheduled-task prompts.

## Baselines (`amem rsi baseline create | check`)

A baseline (`rsi-baseline/1`) is a read-only snapshot of the targets:

```
~/sandbox/baselines/bl-<12 hex>/
  manifest.json      format, created_at, files [{path, sha256, size, mode, kind}], baseline_id
  files/<path without ~/>
```

- `baseline_id = "bl-" + sha256(canonical(manifest without baseline_id))[:12]`.
- The source files are only read. A test proves their bytes and mtimes do not change.
- Content that looks like a secret (`redact` patterns) stops the snapshot.
- Paths outside HOME are refused, in both repos.
- Files and dirs are 0444 / 0555.

`baseline check` reports `drift` and the changed paths. Staleness for an artifact
(`artifact_staleness`): `fresh` (live = before), `applied` (live = after), or `stale` (neither).
`improve stage` refuses a stale baseline. `improve promote` refuses a stale live file.

The sandbox uses the same baselines. `rsi_sandbox.py prepare-arm` verifies the manifest and every
copy, refuses unlisted files, restores file modes from the manifest, and builds the home from
`files/`. With `--artifact H` it adds the artifact's after bytes for the candidate arm.

## Tracking plan (`amem rsi tracking-plan --out DIR`)

It writes two scripts and changes nothing itself:
- `apply.sh`:
  - moves `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md` into `agent-config/home/…`, and symlinks
    them back;
  - adds a line to `links.tsv` for each;
  - mirrors the scheduled-task prompts into `agent-config/home/scheduled-tasks/` (copies, not
    links, because the desktop app rewrites those files);
  - commits, and prints a backup dir.
- `inverse.sh <backup dir>`: restores the exact original files and `links.tsv`.

Each step checks the file's SHA-256 against the plan first, and stops if the file changed. A test
runs both scripts on a fixture HOME and proves an exact round trip.

Running `apply.sh` on the real HOME relinks live global instructions. That needs separate approval
(activation step 5).

## Freeze check (`amem rsi freeze-status`)

Exit 0 and `FROZEN` only when all of these hold:
- the installed and repo `com.vilmerfrost.amem-daily` plists have no `AMEM_EDITS_LIVE`;
- `criteria.json` `max_changes_per_week` is 0;
- the `ab-lessons-monthly` desktop task is disabled.

On 2026-10-16 it printed `NOT FROZEN` with all four reasons. See [freeze.md](freeze.md).
