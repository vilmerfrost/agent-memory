# Night operations: the unattended RSI night and the morning brief

Mission 005.5, phases D to G. **Prepared, not installed.** Nothing here is loaded into launchd, copied to
`~/Library/LaunchAgents` or added to `~/agent-config`. Each gate at the end needs Vilmer's approval.

Code: `src/agent_memory/rsi_schedule.py` (the two jobs), `src/agent_memory/rsi_rehearsal.py` +
`scripts/m0055_rehearsal.py` (rehearsals), `config/rsi-night.first.json` (first-night limits),
`deploy/launchd/com.vilmerfrost.amem-rsi-night.plist` and `com.vilmerfrost.amem-rsi-brief.plist`.
Tests: `tests/test_rsi_schedule.py`, plus the Apply rule in `tests/test_rsi_brief.py` and the resume test in
`tests/test_rsi_night.py`.

## The two jobs

| Job | launchd label | When | Command | Writes |
|---|---|---|---|---|
| Night | `com.vilmerfrost.amem-rsi-night` | 01:30 | `amem rsi night-job --config config/rsi-night.first.json` | `~/sandbox` (staged artifacts, replays, `nights/<night>/report.json`), RSI rows in the hosted DB (candidates, experiments, a staged proposal, the night's checkpoint), one `routines.log` line |
| Brief | `com.vilmerfrost.amem-rsi-brief` | 07:30 | `amem rsi brief-job --wait-s 5400 --out ~/.claude/parent/reports` | `reports/rsi-brief-<night>.md` and `.json`, one `routines.log` line |

Both plists set `AMEM_HOOK_OFF=1`, `AMEM_REQUIRE_REMOTE=1`, `RSI_SANDBOX_DIR=~/sandbox`, `StandardInPath`
`/dev/null` and `RunAtLoad` false. The night also sets `AMEM_JEV_LIVE=1`. Neither sets an edits mode, a push flag,
a key or a database URL (the routines read the database URL from the repo's env file, as amem-daily does).

## Dependencies: completion checkpoints, not the clock

```
08:45 amem-archive ──PASS──┐
09:20 amem-daily ───PASS───┼──▶ 01:30 night-job ──gate──▶ amem rsi night (S0-S9) ──▶ report.json
every 15 min amem-ingest ──┘        │ not done: SKIP + reason          │ lock: one night at a time
                                     │ key missing: FAIL + reason       │ wall clock 45 min + watchdog
                                     ▼                                  ▼
                              routines.log ◀── amem-rsi-night line   07:30 brief-job (waits for lock + report)
                                     │                                  │
                              09:10 routine-audit ◀── amem-rsi-brief line, reports/rsi-brief-<night>.md
```

The night key is the date the night belongs to: a run between 00:00 and 11:59 counts as the previous day. The
01:30 run on 19 October is night `2026-10-18`. The gate (`check_prerequisites`) reads `routines.log` only:

| Routine | Rule | Why |
|---|---|---|
| `amem-daily` | its latest line is `PASS`, or a `FAIL` whose failed steps are all ones the night does not read (below), and is dated on the night's date | the night reads the sessions, deviations and runs amem-daily wrote that day |
| `amem-archive` | its latest line is `PASS` and is dated on the night's date | transcripts are archived before anything else reads them |
| `amem-ingest` | its latest line is `PASS` and younger than 25 h | routine results reached the database |

A routine that ran but failed blocks the night, unless every step it names in its `<routine>-fail <step>: ...` lines
is in that prerequisite's `nonblocking_steps`. Then the run counts as completed and the night runs with one warning
per failed step: an `amem-rsi-night-warn` line, the report's `prerequisite_warnings`, and a `- Warning:` line in the
brief. A FAIL with no step line, or with a step name the gate does not list, still blocks (fail closed).

Which amem-daily steps the night reads (`DAILY_NONBLOCKING_STEPS` in `rsi_schedule.py`):

| Step (`amem-daily-fail <step>`) | Writes | The night | A failure |
|---|---|---|---|
| `ingest` | `runs` (cloud routines) | S1 delta: `routine_failures` | blocks |
| `tasks` | `runs` (dispatch tasks) | S1 delta: `routine_failures` | blocks |
| `deviations` | `sessions`, `deviations` | S1 delta: `sessions`, `deviations`; S2 deviation candidates | blocks. Since 1ac4a74 only a systemic failure (2+ errors and at least half the sessions) is a FAIL; one bad session is an `amem-daily-warn` line and the day is a PASS |
| `score` | `scores` | S4 evidence packs only (`sb.jev_for`), re-read live every night | warning |
| `edits` | `edits` | S4 evidence packs only (`sb.attempts_for`) | warning |
| `distill` | `lessons` | nothing | warning |
| `escalations` | a phone push, the state file | nothing | warning |
| `rsi-push` | a phone push | nothing | warning |
| `state` | `amem-daily-state.json` | nothing (the scan data is already in the database) | warning |

The 2026-10-19..2026-10-22 nights were skipped because amem-daily logged `FAIL` for one or two deviation-session
errors. Those old lines name the `deviations` step, so the gate still skips them (the step is needed); today's
amem-daily writes the same situation as `PASS` plus an `amem-daily-warn` line, and the night runs. A missed 01:30
start (Mac asleep) runs at wake; the gate still applies.

The rules live in `schedule.prerequisites` of the night config. Each entry: `routine`, `same_night` (default true),
`max_age_h` (optional), `nonblocking_steps` (optional list of step names; omitted means the built-in default for that
routine: `DAILY_NONBLOCKING_STEPS` for amem-daily, none for the others; `[]` restores the strict rule "any FAIL
blocks"). Configs without the key keep working unchanged.

## What the night job guarantees

| Requirement (brief, phase E) | How |
|---|---|
| Waits for prerequisite routines | the gate above; `SKIP` with one reason line per missing checkpoint |
| Completion checkpoints, not clock time | the gate reads `PASS` lines; the brief waits for the night's lock and report |
| Model and provider budgets | `rsi_budget` caps from the config: per-model call caps, `max_usd`, sandbox runs, A/B pairs, wall clock |
| No overlapping runs | the night's `rsi-night` lock (O_EXCL file with pid and expiry, a stale lock is taken over); an overlapping start logs `SKIP overlap` and leaves the other night's lock and report alone |
| Fails safely on missing credentials | presence checks only: `AMEM_DB_URL` (remote), `AMEM_JEV_LIVE` + `TYPESAFE_API_KEY`, the sandbox Keychain items via `security find-generic-password` **without `-w`** (attributes only, no dialog), the `claude` CLI, `max_usd > 0`. Missing => nothing runs, `FAIL credentials ...` |
| Resumes interrupted experiments | a stopped experiment (SIGTERM, wall clock) is marked `interrupted`, the candidate stays `deferred` with its hypotheses pending; the next night runs them first with no new model call (`test_interrupted_experiment_resumes_next_night_without_a_new_model_call`) |
| Records failures for routine-audit | `<ts> amem-rsi-night PASS/FAIL/SKIP night=... status=... usd=... report=<path>` plus `<ts> amem-rsi-night-fail <item>: <detail>`: the format `routine-audit` reads (item = text before `: `) |
| Keeps production changes staged | the night stages artifacts and proposals only; it never calls decide, apply or promote (`FORBIDDEN_CALLS`, tested); promotion stays disabled |
| No interactive prompts | stdin is `/dev/null`; `claude -p` gets `--permission-prompts none`; credentials are never read by the gate |
| No host hooks | `AMEM_HOOK_OFF=1`; `claude -p` gets `--settings '{"disableAllHooks": true}'`, no tools, no MCP, no session file; sandbox containers have no host hooks |
| Hard timeout | the night's own SIGALRM at `wall_clock_s` (clean abort); a watchdog sends SIGTERM 10 min later, then logs `FAIL hard timeout` and exits 124 after 2 more minutes. While the job runs, a SIGTERM outside the night's stages (unwinding, final DB writes) is noted, not obeyed (`term_guard`), so the watchdog's FAIL line is always written |

Mapping of night statuses to the log line: `ok` → `PASS`; `partial`, `budget_exhausted`, `aborted`, other
preflight failures → `FAIL` with items (`health langfuse: unavailable`, `budget: usd`, `experiment <id>: ...`);
lock held or night already done → `SKIP`. A skipped night (gate or credentials) writes a `preflight_failed`
report with the reasons as notes and an optional `skipped` object (`by`, `reason`: `prerequisites` or
`credentials`, `summary`, `items`). The night never read anything, so the zero counts and `skipped` health in that
report mean "not analyzed", not "nothing found". The brief's headline names the cause ("The night did not run:
prerequisite amem-daily failed (deviations: ...)", "The night did not run: the credentials and budget check failed
(...)"), its sections say "not analyzed" / "not checked", and its cost basis says no call was made. A night whose
own preflight failed (no `skipped`) keeps the old headline.

Since 2026-10-23 (verify run of night 2026-10-23, partial, $0.14):

- **Targets improve refuses leave the pool.** Allowlisted targets that improve refuses (a file in
  `improve/pins.json`, or a path outside `criteria.json` `allowed_targets` with the `fence.txt` nuance, read through
  `edits_targets.pinned_file` / `is_allowed` like the edits step) are removed from the night's allowlist, so their
  candidates are never triaged or hypothesized. The report notes
  `candidates: N on a target improve refuses (pinned or out of scope: ...), not considered`.
- **A failed experiment or hypothesizer still advances the checkpoint.** The sightings are stored before S3 and the
  candidate stays `deferred` (or `retired` after repeated failures) in `rsi_candidates`, so the pool retries it;
  rereading the window added nothing and kept the checkpoint at 2026-10-14 for every failing night. The status is
  `partial`. The checkpoint still does not advance after an unexpected error in the night itself (`error:` note),
  a preflight failure, an abort or a budget stop.
- **Provider health carries its reason.** The failing evidence steps' detail (`ch.<query>: error (timeout ...)`)
  goes to the report's optional `health_reasons` map, the brief's Service health lines and the `-fail health` line.
- **Quiet skips.** A lock overlap or a night that is already `done` logs `SKIP` with `-warn` lines, not `-fail`,
  so routine-audit (which flags a SKIP with `-fail` lines) stays green. A night whose run failed is retried by the
  next start (the 01:30 job after a failed manual run), as before.

## First-night limits (`config/rsi-night.first.json`)

| Limit | Value | Why |
|---|---|---|
| Experiments per night | 1 | smallest useful scope (brief) |
| Hypotheses | ≤ 2 | hard ceiling in code; the second waits for a later night without a new model call |
| A/B pairs per experiment | 3 (3 cases × 1 rep), `max_ab_pairs` 3 | the verdict policy needs 3 pairs; three task types catch a regression a single case would miss. A bigger replay is refused before any container |
| Sandbox runs | 15 | 6 agent + 6 check + 3 judge containers, exactly one replay |
| Stages | Jev triage (live), Sonnet hypothesizer (`claude -p`), stub implementer, Haiku agent, Codex `gpt-6-luna` blind judge, live Jev | the stages G2b proved, plus the hypothesizer. The implementer stays the deterministic stub (the hypothesis appended as one rule line): the live sandbox implementer is unverified |
| Candidates considered | 5 | caps the triage at 5 Jev calls |
| Wall clock | 2700 s (45 min) | G2b's replay took 33 s of agent time; staging, Docker and the hypothesizer fit with a wide margin |
| `max_usd` | 1.00 | worst case 0.91 (below); expected about 0.07 plus the hypothesizer |
| `model_retries` | 0 | no retry at all, so no retry can pass a budget limit |
| Unknown paid models | refused (`default_model_calls` 0) | only the named models can run |
| Promotion | none | the night never decides; Apply is Vilmer's terminal command |
| Network | `replay.ensure_network` true | the live replay brings `rsi-internal` and the egress proxy up and takes them down again if they were down |

### Cost estimate (G2b basis)

`amem rsi night-job --plan --config config/rsi-night.first.json` (or `scripts/m0055_rehearsal.py --paid-plan`):

```
triage:        5 x jev-1.14.0       expected $0.0500  worst $0.0500
hypothesize:   1 x sonnet           expected unmeasured  worst $0.4000
replay-agent:  6 x claude-haiku-5-5 expected $0.0081  worst $0.3000 (CLI cap $0.05 per run)
replay-judge:  3 x gpt-6-luna       expected $0.0053  worst $0.1500
replay-jev:    1 x jev-1.14.0       expected $0.0100  worst $0.0100
Total: expected $0.0734 + the Sonnet call, worst case $0.9100, hard cap $1.00
```

G2b measured: 6 Haiku runs $0.0081 (CLI-reported), 3 Codex judge calls $0.0053 (tokens × price table), 1 Jev call
$0.01 (flat table price), $0.0234 in total. The budget refuses a call whose estimate would pass `max_usd` and stops
after any call that crossed it, so a night can pass `max_usd` by at most one call's real cost.

### Verified billing vs local estimates

Every booked cost carries its basis (`rsi_budget.COST_KINDS`); the report's `costs` has `by_basis`,
`model_bases`, `reported_usd` and `estimated_usd`, and the brief prints them:

- **provider-reported** (`reported`): the provider's own CLI reported the call's cost (Claude Code's
  `total_cost_usd`). It is not reconciled with an invoice.
- **estimate** (`table`, `call`, `default`, `unknown-usage`, `cli-cap`): `config/rsi-prices.json` (assumed prices,
  read from third-party listings) or a per-run cap. Live Jev (triage and replay) and the Codex sandbox implementer
  report no cost, so they are always booked here, never as provider-reported.
- **free** (`free`, `none`, `on-call`): stand-ins, check runs, and a judge run whose cost is booked on its call.

No cost in the night is verified provider billing. Verification is manual: the morning after, compare the
Anthropic and OpenAI console usage for the sandbox keys and the TypeSafe usage with `reported_usd` and
`estimated_usd` (gate 3).

## The morning brief

`amem rsi brief` / `brief-job` shows, in fixed sections: new sessions analyzed, candidates considered, experiments
(baseline A and candidate B pass rates, verdict, status, cost), the Codex judge's and Jev's results per experiment,
costs by model with their basis, provider health, failed, inconclusive and interrupted experiments with reasons and
notes, and every proposal awaiting approval (this night's and earlier nights') with its exact artifact hash,
evidence refs and judgments.

**Apply** is printed only when all of these hold (`rsi_brief.apply_blockers`): verdict IMPROVES; the artifact
verifies (and matches the database copy); every required evaluation passes; the `sandbox-replay` record bound to
the hash says `improves` from an independent judge (Codex or Claude, at least one pair judged) and live Jev that
answered; and the database holds a pending proposal for exactly that hash. Otherwise the brief prints
"Apply: not offered: <reasons>" and the block says "staged, NOT ready to apply". NO_EFFECT, WORSE and INCONCLUSIVE
experiments are never staged and never show Apply. Reject and Details always stay. The brief job never pushes.

## Rehearsals

### Mock rehearsal ($0, run on 2026-10-17)

`uv run python scripts/m0055_rehearsal.py` runs the scheduled path in the Mission 005 demo world: a throwaway
Postgres on 127.0.0.1, a temp HOME and RSI sandbox (never `~/sandbox` or `~/sandbox-dev`), synthetic sessions,
provider stand-ins, no env file, the Keychain tripwire. The config is the first-night config with every paid stage
replaced by its stand-in and `max_usd` 0. Steps: the gate without amem-daily (`SKIP`), then with it (`PASS`), the
night S0-S9 with the real `improve stage` and the Docker stub replay, the brief job, and the checks. Result:
IMPROVES staged, the brief with "Apply: not offered" (stand-in judge and fixture Jev), $0, production fingerprint
equal before and after, the real `~/sandbox/nights` listing and the real `routines.log` unchanged.

The first run found a real gap: the first-night `sandbox_run_estimate_usd` (0.05) refused every stub run when
`max_usd` is 0. The mock config now sets it to 0; the live config keeps it, so each live run is checked against
the cap before it starts.

### Paid rehearsal (gate 2, not run)

`uv run python scripts/m0055_rehearsal.py --live --approve-max-usd 1` runs the same world and checks with the
first-night config's live stages: live Jev (needs `TYPESAFE_API_KEY` in the caller's env, for example from the
secret popup), the sandbox Keychain items `rsi-sandbox/anthropic` and `rsi-sandbox/openai`, and the host
`claude -p` hypothesizer (Sonnet). It prints the paid plan first and refuses without `--approve-max-usd` equal to
the config's `max_usd`. **Unverified:** the host `claude -p` under the rehearsal's temp HOME has never run; if it
fails, the night records "hypothesizer failed" and spends nothing on the replay.

## Recovery and rollback

| Situation | What happens | What to do |
|---|---|---|
| Prerequisite not done | `SKIP` line, skip report | nothing: the next night reads the same delta (the checkpoint did not advance) |
| Credential missing | `FAIL credentials ...`; routine-audit raises it | store the key (secret popup / Keychain), then wait or run `amem rsi night-job` by hand |
| Night aborted or over the wall clock | experiment `interrupted`, containers reaped, lock released, checkpoint kept | nothing: the next night resumes the pending hypothesis |
| Process killed (watchdog exit 124, power loss) | the lock expires (wall clock + 15 min) or is taken over when its pid is dead; experiment rows may stay `running` | `amem rsi night-job` again; `python3 sandbox/rsi_sandbox.py recover` if containers remain |
| Budget exhausted | `FAIL budget: <kind>`, hypotheses kept pending | raise the cap only with a new approval |
| A staged proposal looks wrong | it expires after its TTL | `amem rsi decide <id> reject --hash <H>` |
| Turn the night off | — | `launchctl bootout gui/$(id -u)/com.vilmerfrost.amem-rsi-night` (and `...-rsi-brief`), remove the plists from `~/Library/LaunchAgents` and `~/agent-config/launchd/`, remove the registry rows |

Nothing the night writes changes production files: artifacts stay in `~/sandbox` until a human applies them.

## Install commands (for Vilmer, after gate 3; not run)

routine-audit requires every plist in `~/agent-config/launchd/` to have a `parent/routines.tsv` row, and the
installed copy to be identical:

```
cp deploy/launchd/com.vilmerfrost.amem-rsi-night.plist deploy/launchd/com.vilmerfrost.amem-rsi-brief.plist ~/agent-config/launchd/
printf 'amem-rsi-night\t26\tlaunchd:com.vilmerfrost.amem-rsi-night\namem-rsi-brief\t26\tlaunchd:com.vilmerfrost.amem-rsi-brief\n' >> ~/agent-config/parent/routines.tsv
plutil -lint ~/agent-config/launchd/com.vilmerfrost.amem-rsi-*.plist
cp ~/agent-config/launchd/com.vilmerfrost.amem-rsi-night.plist ~/agent-config/launchd/com.vilmerfrost.amem-rsi-brief.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vilmerfrost.amem-rsi-night.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vilmerfrost.amem-rsi-brief.plist
~/agent-config/bin/routine-audit --no-push
```

## Activation checklist

### Gate 1: local merge (approval: merge into local `main`)
- [ ] The full suite and the sandbox suites (docs/rsi/m005-report.md, table B) pass on the merged tree.
- [ ] `plutil -lint deploy/launchd/com.vilmerfrost.amem-rsi-*.plist` OK.
- [ ] `uv run python scripts/m0055_rehearsal.py` prints `REHEARSAL OK`, the fingerprint is equal before and after.
- [ ] `amem rsi night-job --plan --config config/rsi-night.first.json` matches the estimate above.

### Gate 2: paid rehearsal (approval: paid calls up to $1.00, on synthetic data)
- [ ] `TYPESAFE_API_KEY` available to the command (secret popup); sandbox Keychain items present.
- [ ] The Docker images from `sandbox/images/lock.json` are built; the isolation suite passes.
- [ ] `uv run python scripts/m0055_rehearsal.py --live --approve-max-usd 1` → `REHEARSAL OK`; costs within the cap,
  reported vs estimated split recorded; production fingerprint equal; containers and networks cleaned up.
- [ ] Compare the provider consoles' usage with the rehearsal's `reported_usd` / `estimated_usd`.

### Gate 3: first unattended night (approval: install the two jobs)
- [ ] Gates 1 and 2 passed. `amem rsi freeze-status` prints `FROZEN`; promotion disabled; weekly cap 0.
- [ ] `.env` has `TYPESAFE_API_KEY`; `AMEM_DB_URL` is the routines' remote credential.
- [ ] The latest amem-daily, amem-archive and amem-ingest lines are `PASS` (else the night will skip, by design).
- [ ] Install with the commands above; `routine-audit --no-push` shows both rows.
- [ ] The next morning: read `reports/rsi-brief-<night>.md`, the `amem-rsi-night` and `amem-rsi-brief` lines, and
  the provider consoles; production fingerprint unchanged except the staged area.
- [ ] Rollback: the "Turn the night off" row above.

## Known limits

- The live hypothesizer (`claude -p` with hooks disabled), the live sandbox implementer and the hosted-database
  night are unverified. The first night keeps the implementer as the stub.
- `security find-generic-password` without `-w` is assumed not to show a dialog (it reads no secret). Gate 2 is the
  first run that uses it on the real Keychain.
- The prices in `config/rsi-prices.json` are assumptions; no cost is verified billing (see above).
- The watchdog's hard exit can leave experiment rows `running`; the next night's `_unwind` only touches its own
  night. They are history, not active work.
