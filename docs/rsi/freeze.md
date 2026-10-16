# Production freeze procedure (prepared, NOT executed)

Goal: stop **autonomous improvement writes** (W1–W4 in [writers.md](writers.md)). Ingestion,
observation, scoring, the deviation scan and every protective rollback (P1–P7) keep running.

Status on 2026-10-16: not executed. It needs Vilmer's approval. Three steps, about 5 minutes. All
of them use controls that already exist, so no new code has to be merged first.

**Deadline:** before **2026-10-17 09:20** (the next `amem-daily` with live edits), and before
2026-10-18 08:00 (`workflow-improve-weekly`).

Run the steps from Vilmer's own terminal, not from an agent session.

## 0. Backup

```bash
B=~/.claude/parent/freeze-backup/$(date +%Y%m%d-%H%M%S); mkdir -p "$B"
cp ~/agent-config/improve/criteria.json "$B/"
cp ~/Library/LaunchAgents/com.vilmerfrost.amem-daily.plist "$B/"
cp ~/agent-config/launchd/com.vilmerfrost.amem-daily.plist "$B/repo-amem-daily.plist"
echo "$B"
```

## 1. Global backstop: no `improve apply` can succeed (W1, W2, W4)

`max_changes_per_week: 0` makes `cap_reached()` true for every apply. Each one is refused, with
reason "weekly cap reached", and is logged as a suggestion [V improve:216-217, 1081-1082]. Apply's
eval write-back, recheck, revert, fence-check and autopin are unaffected.

```bash
sed -i '' 's/"max_changes_per_week": null/"max_changes_per_week": 0/' ~/agent-config/improve/criteria.json
grep -n '"max_changes_per_week"' ~/agent-config/improve/criteria.json      # expect: 0
git -C ~/agent-config commit -m "freeze: max_changes_per_week 0 (RSI V1 approval-first)" -- improve/criteria.json
```

Verify. A dry-run writes nothing and takes no lock [V improve:1057-1059, 1099-1102]:

```bash
printf '# probe\n' > /tmp/freeze-probe.md
~/agent-config/bin/improve apply --dry-run --json --target ~/agent-config/agents/worker.md \
  --rule-id freeze-probe --new-file /tmp/freeze-probe.md --why probe --evidence probe
# expect: {"status": "refused", "reason": "weekly cap reached (0 changes in 2026-W42)", ...}
~/agent-config/bin/improve list | grep -i "changes left"   # expect: 0
```

## 2. amem-daily: stop the edit writer (W1)

`edits run` is off unless `AMEM_EDITS_LIVE=1` [V docs/edits.md]. Removing the variable also stops
the Opus writer calls that step 1 would only refuse afterwards. The ingest, score, escalation and
deviation steps keep running.

```bash
P=com.vilmerfrost.amem-daily
plutil -remove EnvironmentVariables.AMEM_EDITS_LIVE ~/agent-config/launchd/$P.plist
plutil -lint ~/agent-config/launchd/$P.plist
launchctl bootout gui/$(id -u)/$P
cp ~/agent-config/launchd/$P.plist ~/Library/LaunchAgents/$P.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$P.plist
git -C ~/agent-config commit -m "freeze: amem-daily edits step off (RSI V1)" -- launchd/$P.plist
```

Verify:

```bash
plutil -extract EnvironmentVariables.AMEM_EDITS_LIVE raw ~/Library/LaunchAgents/com.vilmerfrost.amem-daily.plist  # expect: error, no value
launchctl print gui/$(id -u)/com.vilmerfrost.amem-daily | grep -c AMEM_EDITS_LIVE                                # expect: 0
# after the next 09:20 run:
grep ' amem-daily ' ~/.claude/parent/routines.log | tail -1     # expect: no "edits=" field
```

## 3. ab-lessons-monthly: disable the task (W3)

`ab-lesson` writes `lessons.md` directly (not through improve), so step 1 does not stop it.

- In the Claude app: Scheduled tasks → `ab-lessons-monthly` → turn it off. Or, from an attended
  session, after approval: `update_scheduled_task(taskId: "ab-lessons-monthly", enabled: false)`.
- Verify: `amem rsi freeze-status` (below) shows `ab-lessons-monthly enabled=False`.

## One-command check

`amem rsi freeze-status` (new, read-only, in this branch) prints:
- whether the installed and repo amem-daily plists set `AMEM_EDITS_LIVE`;
- `criteria.json` `max_changes_per_week` and `apply_mode`;
- the `enabled` flag of every Desktop scheduled task that writes production (from the app's
  `scheduled-tasks.json`, read-only);
- one verdict line: `FROZEN` or `NOT FROZEN: <reasons>`. Exit 0 when frozen, 1 otherwise.

## What stays on (by design)

amem-ingest (ingest + undo-poll), amem-daily ingest/score/escalations/deviations, rsi-nightly,
improve recheck, revert, fence-check and autopin, the canaries, secret-scan, dep-audit,
config-check, routine-audit, Nightly Trials, nightly-trials-weekly-sync, and the read-only tasks.

## Undo the freeze

```bash
B=<backup dir from step 0>
cp "$B/criteria.json" ~/agent-config/improve/criteria.json
cp "$B/repo-amem-daily.plist" ~/agent-config/launchd/com.vilmerfrost.amem-daily.plist
launchctl bootout gui/$(id -u)/com.vilmerfrost.amem-daily
cp "$B/com.vilmerfrost.amem-daily.plist" ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vilmerfrost.amem-daily.plist
git -C ~/agent-config commit -m "unfreeze" -- improve/criteria.json launchd/com.vilmerfrost.amem-daily.plist
# re-enable ab-lessons-monthly in the app
```

## Optional hardening (prepared in code, not installed)

`rsi-prod-fence`: deny file-tool writes to production paths in **unattended** sessions (scheduled
tasks, `-p` runs). This is a hook change in `~/.claude/settings.json`, so it needs separate approval.
See activation.md.
