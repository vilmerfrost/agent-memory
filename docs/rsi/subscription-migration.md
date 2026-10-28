# Subscription replay migration

Approved by Vilmer on 2026-10-25. Claude calls use `claude-opus-5-5`; Codex calls use
`gpt-6.1-sol`. Both use **high** effort, including hypothesis writing and judging.
The installed 01:30 job and deployment template now select
`config/rsi-night.subscription.json`, after dedicated login and live qualification.
The job was reloaded while idle with RunAtLoad false; no production night was triggered.
`rsi-night.first.json` remains the explicit legacy API profile. The rule-promotion
freeze is unchanged (`max_changes_per_week: 0`). Night Desk's reporting update is live.

## Execution boundary

The official, authenticated client runs in a dedicated private home. It has no native
task execution or file tools. It calls the explicit task bridge instead. Each bridge
operation runs in a fresh credential-free Docker worker, without network, Docker socket
or host-home access. Only the task workspace and sanitized arm files are mounted.
Authentication files, tokens and client logs never enter arm snapshots or task mounts.

Claude uses restricted mode, no built-in tools, explicit MCP configuration and disabled
hooks/plugins. It receives the subscription token only in its own environment. The bridge
starts from an empty environment. Codex uses the official app-server protocol pinned to
0.163.0, no environments/executor roots, and source-verified native-tool/extension controls.
Unknown client versions, config conflicts, unavailable models and model changes fail closed.
This pin must be reviewed before upgrading Codex; silently accepting another version is not safe.

Both clients receive high effort explicitly. Codex also verifies the resolved effort in
the thread response. Claude records the explicit client option as its effort evidence;
this is not a claim to observe private reasoning or override organization-enforced caps.

## Profiles and comparison rules

- `config/rsi-night.subscription.json`: routes `.claude` targets to Claude, `.codex` targets
  to Codex, and shared/other targets to both providers.
- `config/rsi-night.subscription-claude.json` and `subscription-codex.json`: bounded
  single-provider qualification profiles.
- A is the current rule and B is the changed rule. Both arms use the same provider,
  exact model, effort, tasks and execution boundary. Fresh task workspaces prevent carryover.
- Shared rules produce separate provider records. A combined passing record requires
  every required provider to improve. Metrics and judge votes are never pooled across models.
- S6 uses the section writer (`stages.implementer: "writer"`, since 2026-10-26): the morning edit
  loop's Opus writer (`edits_writer` strategy, policy, reply shape and `parse_proposal`) rewrites the
  candidate's rule section instead of appending a line. It runs like the hypothesizer: `claude-opus-5-5`,
  high effort, no tools, subscription runner, counted in the night budget; the evidence reaches it only
  inside its untrusted-data envelope. A pinned file or rule, no matching section, a writer or proposal
  error, a budget stop or an `improve stage` refusal (its content guard) makes that one experiment use
  the deterministic stub change; `implementer` ("writer" | "stub-fallback"), `implementer_fallback_reason`
  and `implementation` (section, kind, diff) are on the experiment in report.json and `rsi_experiments.meta`
  (which also keeps the exact new text), and a fallback adds a report note the brief prints. A failed
  subscription call has no usage record, so the budget then stops the night (fail closed). The sandbox
  implementer stays refused under subscription. Jev remains a separate API-backed service.

## Relevance is mandatory

Cases declare exact target paths, kinds and intended behavior. Every changed target must
have a matching case. Before judging, both arms must show successful exposure to those
targets. The subscription bridge records full-file reads and their SHA256 hashes; these
records are constructed by trusted code, not accepted from model output.

Missing target coverage or exposure yields INCONCLUSIVE. Generic regression tasks cannot
establish efficacy; a regression in them still blocks promotion. Old passing records are
superseded before a new provider comparison. Exact-path read evidence proves exposure,
not obedience or semantic correctness of the task design.

The three generic cases (`docs-typo`, `fix-add`, `report-todos`) cover `~/.claude/CLAUDE.md`.
They can qualify the transport. They do not qualify a skill change: a typo task never reads
a skill.

## Skill coverage (2026-10-25)

Three cases now cover real skill files. Each asks for the skill's first planning step and
writes one file that a hidden check grades. Nothing renders, speaks, searches or calls out.

| Case | Target | What the hidden check grades |
|---|---|---|
| `meltdown-brief` | `~/.agents/skills/meltdown/SKILL.md` | Intro hard rules and style defaults: reel, 15 s, `eleven_v4` with Vilmer's voice id, a separate reviewer, 2–4 word lines, about 25 words. |
| `pepe-lock` | `~/.agents/skills/pepe/SKILL.md` | Phase 3 Lock: after one suggestion the user's two picks stand; every other session idea goes to `docs/ideas/parked.md`, one line each with a revisit trigger; no briefs yet. |
| `anatomy-plan` | `~/.agents/skills/anatomy/SKILL.md` | Workflow steps 1–4: a Figure of 30–90 min, a real object, 40–80 parts in the five groups, one lit subject part, the request's true numbers, no geometry. |

Crafted compliant and non-compliant outputs live in `tests/data/rsi_skill_cases/`. The tests
also break each rule alone and check that the hidden check fails.

**Which cases run.** With `replay.cases: null`, an experiment replays only the cases that
declare its changed file. Only those cases count towards the verdict's `min_pairs` (3), so
one skill case runs 3 repetitions. That fits each provider's share of the night: 3 pairs for
Claude and 3 for Codex on a shared skill (6 of 6). A `CLAUDE.md` change still runs the three
generic cases once. An explicit case list is replayed as given.

**Exposure in both clients.** The arm home holds the skill at its real path. The runner also
copies it to the place the other client looks (`~/.agents/skills/<n>` ↔ `~/.claude/skills/<n>`),
with that arm's bytes. It is a copy because the sanitizer strips symlinks. In the subscription
transport both clients read the listed file with `task_read_arm`; the bridge now returns files up
to 48 KiB (Anatomy is 31 KB). With native Claude tools, a completed `Skill` call by name counts
only when exactly one staged skill has that name; two different staged files with one name stay
unknown.

**Triage.** A candidate whose file no case declares cannot take the experiment slot. It gets no
paid triage call. The report lists it with `skip_reason: "no test case uses this file"`, and
covered candidates fill the triage slots first.

**Limits.**

- Coverage is per file, not per section. A Meltdown change outside the intro or style table is
  still graded by `meltdown-brief`; that is evidence of no regression there, not of the change.
- Each skill has one task. Three repetitions of one task measure noise in that task, not breadth.
- The checks grade the plan's decisions, not the quality of the script, ideas or object.
- Exposure proves the file was returned in full, not that the model followed it. A client may
  still truncate a long tool result before the model sees it.
- The hypothesizer may name a different target than its candidate. Such an experiment still ends
  INCONCLUSIVE when no case declares that file.
- Other skills (for example `handoff`), `~/.codex/AGENTS.md` and scheduled tasks have no case.
  Their candidates wait with the reason above.

## Limits and accounting

Since 2026-10-25 the combined profile runs up to **three experiments** per night (Vilmer's
approval, after the skill cases went live). Each limit has a scope:

- **Per candidate:** at most 2 hypotheses (one hypothesizer reply). The night runs one
  experiment per candidate. The second hypothesis waits, pending, for a later night and
  needs no new model call. So 3 experiments means 3 different candidates. With fewer
  covered candidates, the night runs fewer experiments.
- **Per experiment (per replay):** at most 10 provider-specific A/B pairs (`max_ab_pairs`):
  exactly 5 A + 5 B per provider (Vilmer, 2026-10-28).
- **Per night:** model calls, sandbox runs, wall clock, API dollars and the token stop.

**How the 5 pairs are spread.** `rsi_replay.plan_pairs` aims for exactly one provider's share
(`max_ab_pairs` / providers = 5) over the target's eligible cases. It keeps a covering case for
every changed file, then fills in id order, at most 5 cases. The replay runs repetition-major and
stops at 5 pairs: 1 case runs 5 times, 2 cases run 3 + 2, 3 cases 2 + 2 + 1, 5 or more cases
the first 5 once. It never plans more than the cap. If the cap were below the verdict's
`min_pairs`, the cap wins and the verdict reports too few pairs.

**The judge sees the rule under test.** For an artifact whose file carries a section rule id
(an exact declared edit the writer reproduced), the replay recovers the section from the
artifact's own verified bytes and shows the judge its heading and new text as "The rule under
test", without saying which attempt had it. The judge picks the attempt that follows that rule
better while staying correct (the check result stays strong evidence). Length counts neither way.
Legacy artifacts get the old prompt without the rule and without the "short explanation"
preference. Judge prompt version 2 is recorded in replay.json, the eval record and each judge row.

One shared-skill experiment at its worst case: 1 Opus hypothesis, 1 Opus section writer, 5 pairs per provider
(20 agent runs: 10 Opus, 10 Sol), 10 Sol judgments and 2 Jev calls. Each agent run reserves
8 tool containers and 1 check container, so one experiment takes at most 180 sandbox runs.
After each agent run the replay gives back the tool reservations it did not use
(`Budget.release_sandbox_runs`, numbered by the bridge's tool evidence). An unknown use (an
error, a crash) keeps every reservation. The report's `costs.sandbox_releases` and each
run's `runtime.tool_containers` show reserved, used and released.

| Limit (per night) | Need for 3 experiments | Cap |
|---|---|---|
| Opus calls | 3 × 12 = 36 | 40 |
| Sol calls | 3 × 20 = 60 | 64 |
| Jev calls | 5 triage + 3 × 2 = 11 | 12 |
| Sandbox runs (worst case, every tool slot used) | 3 × 180 = 540 | 540 (code maximum 600) |
| Observed subscription tokens | 3 × 2,000,000 | 6,000,000 |
| Wall clock (every stage at its timeout) | 600 + 3 × 8,960 = 27,480 s | 17,940 s (code maximum 18,000 s) |
| API dollars (Jev only) | 11 × $0.01 = $0.11 | $1.00 |

**The wall clock does not fit the worst case.** The bound counts every model stage at its own
timeout: hypothesizer 300 s, section writer 300 s, `improve stage` 300 s, each agent run at its
case's `timeout_s` (240 s for the skill cases), each judge call at `rsi_replay.JUDGE_TIMEOUT_S`
(300 s), 10 s per check run and 30 s per Jev call. Per provider: 10 × (240 + 10) + 5 × 300 + 30
= 4,030 s. One experiment: 900 + 2 × 4,030 = 8,960 s. At every timeout only 1 of 3 experiments
finishes inside 17,940 s, and 3 would need 27,480 s, over the 5 h code ceiling
(`HARD_MAX_WALL_CLOCK_S`). The runtime enforcement is unchanged: the night stops cleanly at the
wall clock and records later experiments as not run; nothing is cut silently. Observed on
2026-10-27 at 3 pairs: 1,350 s for the whole night, so a 5-pair night is expected near 2,300 s.
`amem rsi night-job --plan` prints the worst-case line. Proposals, if a worst-case fit is
wanted: lower the skill cases' `timeout_s` from 240 s towards their observed durations, or lower `JUDGE_TIMEOUT_S`; raising the ceiling past 5 h would push the watchdog's
hard exit into the 07:30 brief. Not done here.

**Why the sandbox maximum went from 400 to 600.** 400 was 324 (3 pairs) plus a margin. The
5-pair worst case is 540. With releases the cap binds on containers that actually ran, plus at
most one in-flight run's 8 reservations.

**Why 6,000,000 tokens.** The 2,000,000 stop was approved for one experiment; three get three
times that. The qualification replays used about 163,000 tokens for both providers together; the
2026-10-27 night used about 1.42M at 3 pairs, so about 2.4M is expected at 5 pairs.

`amem rsi night-job --plan --config config/rsi-night.subscription.json` prints this check as
"Room for 3 experiment(s)". It lists every per-night limit with its need and cap and says
"DOES NOT FIT" for a cap that would stop the second or third experiment.

The single-provider qualification profiles (`subscription-claude.json`, `subscription-codex.json`)
stay at one experiment and the 45-minute wall clock.

Subscription usage is excluded from the dollar subtotal. The token stop is a local resource
limit, not a conversion to provider allowance. There is no API fallback.

The budget stops subsequent calls when token usage is missing or the observed-token limit
is reached. A running request can consume additional tokens before it ends; this is not
a provider-enforced token ceiling. Client dollar estimates are not counted as subscription
bills. Reports carry subscription tokens, unknown per-run billing and unknown allowance
draw separately from API-reported/estimated costs. Historical reports keep their original values.

Night Desk shows exact setup, task names, judge reasons, target-use gaps and separate
provider results. It labels the dollar subtotal as the API portion when subscriptions are used.

## Authentication and activation

Dedicated homes are under `~/Projects/agent-memory/.subscription-auth/{claude,codex}`.
They must be owner-only. `/.subscription-auth/` is locally excluded through Git's
`info/exclude`, so unrelated `.gitignore` edits are preserved.

- Claude: the native secret popup writes `CLAUDE_CODE_OAUTH_TOKEN` into
  `.subscription-auth/claude/.env`. The token comes from the official `claude setup-token`
  flow. The application loads only that value, with interpolation disabled, and refuses
  symlinks or permissive file ownership/mode. Never paste the token into chat.
- Codex: sign in with the official client using the dedicated `CODEX_HOME` and
  file-backed storage. The client owns refresh. Runs sharing that store are serialized.
  Do not copy the everyday `~/.codex/auth.json` into an experiment or snapshot.

Activation sequence: finish both dedicated sign-ins; verify client status and model
access; run bounded synthetic qualification for each provider; inspect the actual model,
effort, tool boundary, usage and relevance evidence; then change the scheduled config to
the combined profile. A transport smoke test does not authorize rule promotion.
If qualification fails, keep the existing scheduled configuration and report the fixed
error code. Rollback is an explicit config selection, never a silent switch to API billing.

## Validation recorded during implementation

204 backend tests passed in a networkless Docker test runner with read-only source,
empty environment, bounded tmpfs, memory, CPU, processes and wall time. Nine database
tests were skipped because the runner does not expose a database service. An installed
Codex metadata-only check accepted every required isolation configuration override;
no inference was requested. A fresh read-only change review found no actionable defect.

Both providers subsequently passed live subscription access and isolated-tool checks.
Three synthetic tasks were replayed on A and B for each provider: twelve task runs and
six Sol judgments, with no runtime errors, complete usage records and verified target
reads. Claude used 70,974 input / 3,189 output tokens; Codex used 86,544 / 1,777.
These figures include each replay's Sol judge calls, and are usage, not billed dollars.
The Claude synthetic change was judged WORSE; Codex's change was INCONCLUSIVE because
Jev was intentionally disabled. Neither qualification promoted a rule or wrote production DB rows.

Live qualification exposed and resolved two client compatibility details: built-in
Claude mods require explicit plugin disable entries, and Codex 0.163.0 restricted reads
use a named empty-filesystem permission profile rather than legacy readOnly.access.
Claude also creates a Library cache inside its private HOME; real account homes are
rejected explicitly using both environment and OS account identity.
This qualifies the controlled replay transport, not every real skill or production workload.

## Official interface references

- [Claude authentication](https://code.claude.com/docs/en/authentication)
- [Claude CLI options](https://code.claude.com/docs/en/cli-reference)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)
- [Codex trusted private automation](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
