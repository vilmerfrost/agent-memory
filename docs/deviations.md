# Deviations: where agents leave their instructions

The deviation finder reads Vilmer's Claude Code and Codex sessions. It finds where an agent
did not follow a skill, a CLAUDE.md, AGENTS.md or PARENT.md rule, an agent definition or a
routine prompt. It writes down why, when and how, gives two raw grades, and Jev makes the call.

It is **read only** toward the setup. It never edits a skill or any instruction file.
Editing is step E4, `amem edits run` (docs/edits.md), through agent-config's `improve apply` / `improve revert`.

```
~/.claude/projects/*.jsonl ─┐
~/.codex/sessions/**/rollout-*.jsonl ─┤
cloud_digests (cloud routines) ─┴─sessions ingest──▶ sessions (tool = claude | codex)
                                                   │ deviations scan (daily) 
                                                   ▼
            digest (redacted, capped) + instructions in force + rule catalog
                                                   │ writer: claude -p, JSON, ref = a rule id
                                                   ▼
            deviations: rule_id, what / why / how, why_category, followed_grade, result_grade
                                                   │ Jev: labels (why_category too) + grades + recurrence only
                                                   ▼
            jev_verdict: edit_instruction | enforce | leave
                                                   │ deviations report --json
                                                   ▼
            candidates file ──amem edits run──▶ improve apply → one commit per edit
```

## Commands

| Command | Does |
|---|---|
| `amem sessions ingest [--days 7] [--projects-dir DIR] [--codex-home DIR] [--tool all\|claude\|codex]` | Indexes Claude Code and Codex transcripts touched in the last N days into `sessions`, with token totals per session (docs/sessions-live.md). Flushes the hook spool first. Prints `sessions: ... (kinds) tools(claude=N codex=M) team=T` (`spool=N` when it flushed any). Idempotent. |
| `amem sessions tokens [--days 7] [--repo R] [--tool claude\|codex] [--json]` | Token totals per repo / tool / kind / model and the top sessions (docs/sessions-live.md). Read only. |
| `amem sessions cost --since ISO [--until ISO] [--category C] [--project P] [--json]` | Tokens and active / api / tool time of the sessions that started in a window, per project (docs/sessions-live.md, "Cost of a window"). Read only. |
| `amem sessions find --agent-id ID [--ingest] [--projects-dir DIR] [--json]` | The sessions row of a subagent transcript `agent-<ID>.jsonl`: prints `session_id`, `category`, `project` (tab-separated). Exit 1 when not ingested; `--ingest` ingests that transcript first. See "Incident scans". |
| `amem sessions instruction-sources [--apply] [--limit 200] [--days N] [--json]` | Backfills `sessions.instruction_sources` (authority and version of every instruction file in force) for rows that have none, newest first. Dry run unless `--apply`; bounded, resumable, idempotent (docs/data-use/instruction-sources.md). |
| `amem sessions ping` | Claude Code hook (SessionStart / Stop): the live row of the running session (docs/sessions-live.md). Always exits 0. |
| `amem deviations scan [--days 7] [--limit 15] [--session ID] [--dry-run] [--timeout 300]` | Scans unscanned sessions. Prints one line per session and `deviations: sessions=N found=D edit=E enforce=F leave=L errors=X`. Exit 1 when a session failed. |
| `amem deviations report [--days 7] [--json]` | Groups deviations by instruction source + rule id, most recurring first. `--json` prints the candidates file. |
| `amem deviations reref [--dry-run]` | Backfills `rule_id` / `rule_title` on stored rows: snaps each row's writer ref against today's rule catalog. No writer, no Jev. Prints one mapping line per source + ref and `reref: rows=N exact=E snapped=S unmatched=U no_source=X changed=C`. Idempotent. |
| `amem deviations rules FILE...` | Prints the rule catalog (`N. id — title`) of instruction files. Read only, no database. |

All but `rules` need the Postgres backend (V1). The write commands (`sessions ingest`,
`sessions find --ingest`, `sessions instruction-sources --apply`, `deviations scan`, `deviations reref`)
honour `AMEM_REQUIRE_REMOTE=1` (exit 3).

- `scan` calls the Claude Code CLI. It needs `AMEM_DEVIATIONS_LIVE=1`; without it, it exits 2
  and calls nothing. `AMEM_DEVIATIONS_CLAUDE` may point at another executable (tests, demos).
- `--dry-run` needs no live flag. It builds each digest and finds the instructions, then
  prints `digest_chars=` and `instructions=` per session. No writer, no Jev, nothing written.
- `--session ID` scans one session, also when it was scanned before (a re-scan).
- Jev keeps its own rule: live only with `TYPESAFE_API_KEY` and `AMEM_JEV_LIVE=1`.

## Sessions

One row per transcript. Subagent transcripts (`<session>/subagents/agent-*.jsonl`) get the
id `<parent session>/agent-<id>`.

| Kind | Rule (first match wins) |
|---|---|
| `eval` | The project folder is an eval clone, a probe or the memory observer, or the cwd is under `~/.cache/agent-evals` or `~/.cache/skill-evals`. |
| `worker` | A subagent transcript. `meta.agent_type` holds the agent type (`worker`, `Explore`, ...). |
| `routine` | A scheduled task: the first message holds a `<scheduled-task name=...>` block, or a turn has `turnOrigin = scheduled`. `meta.routine` = the name. |
| `headless` | `claude -p` or the SDK: `entrypoint` starts with `sdk`. |
| `parent` | The session ran `parent-lock`, used the `dispatch` skill or spawned a `worker`. |
| `chat` | Everything else. |

- `repo`: from the working directory (`.../worktrees/<repo>/<name>` → `<repo>`, else the
  folder name). The working directory follows agent-config `lib/sessions.py`: never a Desktop
  scratch workspace.
- `skills`: `Skill` tool calls plus slash commands, without Claude Code built-ins.
- `instructions`: the instruction sources in force whose file exists (see below).
- `instruction_sources`: one entry per instruction file with authority, version and reason
  (docs/data-use/instruction-sources.md). A re-ingest merges: a stronger authority wins, a tie keeps
  the earliest capture.
- `tool`: `claude` or `codex` (see Codex below).
- Cloud routine sessions come from the `cloud_digests` table, not a transcript file: id
  `cloud:<session id>`, kind `routine`, `project_dir` `cloud`, `meta.cloud` true (docs/cloud.md).
- `meta.team`: true for a team repo's session (see Team repos below).
- Tokens: `tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_creation`,
  `models`, `turns`, `last_activity_at`; a parent's `meta.subagent_tokens` sums its
  subagents (docs/sessions-live.md). A row a hook created (`meta.live`) is completed by ingest.
- A re-ingest refreshes the facts and keeps `scanned_at` and the scan keys in `meta`.

## Codex

The ChatGPT desktop app's Codex (and the Codex CLI) writes one rollout per thread to
`~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl` (and
`~/.codex/archived_sessions/`). `~/Library/Application Support/Codex` holds only browser
state, no transcripts. Parsing: `src/agent_memory/codex_sessions.py`.

- Id `codex:<thread id>`. `project_dir` is the folder under the Codex home
  (`sessions/2026/10/13`), `meta.file` the file name.
- Kind (first match wins): `eval` (cwd as above), `worker` (a subagent thread:
  `thread_source = subagent`; `meta.parent_session` = `codex:<parent thread>`), `headless`
  (`originator = codex_exec`), `parent` (used the `dispatch` skill), `chat`. Codex
  automations have no marker in the rollout, so `routine` is not detected.
- Repo: from the cwd; a Codex app worktree (`~/.codex/worktrees/<task>`) names the repo by
  its main checkout.
- Skills: the `<skill><name>X</name>` block Codex adds when the user invokes a skill, plus
  a SKILL.md the agent read from `~/.agents/skills`, `~/.codex/skills` or `~/.claude/skills`
  (a `read` in the command's `parsed_cmd`). A SKILL.md read inside a repo checkout is not a
  skill use.
- A forked subagent's rollout starts with a copy of the parent's history (below
  `subagent_history_start_ordinal`). That copy is left out of the facts and the digest.
- Digest: the same line shapes as for Claude Code. `[turn N] USER:` typed prompts (the
  AGENTS.md block, `<environment_context>`, developer messages and reasoning are left out),
  `SKILL <name> invoked`, `ASSISTANT:`, `TOOL exec_command: <script>` / `TOOL apply_patch:
  <paths>` / `TOOL mcp:<server>/<tool>` / `TOOL spawn_agent: <task>` from the completed
  items, `ERROR` (failed command with exit code, failed MCP call), `DENIED` (declined),
  `INTERRUPTED`. A subagent's brief is `[turn 1] MESSAGE from /root:`; in a parent, a
  subagent's report is `NOTICE agent-message`. Same redaction and cap.
- The writer prompt names the agent (`You audit one Codex agent session`).

## Team repos

A session's repo is a team repo when its git remote owner is not a personal owner
(`AMEM_PERSONAL_OWNERS`, default `vilmerfrost`), or `~/.claude/parent/<repo>/profile.md`
says `mode: team`. The remote comes from the Codex rollout (`git.repository_url`) or from
the repo's `.git/config` (`origin`, else the first remote; a worktree reads its main
checkout's). `meta.team`, `meta.remote_owner` and `meta.team_reason` (`remote-owner` |
`profile` | `same-repo`) hold the result.

- No owner and no team profile (a worktree that was cleaned up, a repo without a remote):
  unknown. Ingest then writes no `team` key, so a flag stored by an earlier ingest stays.
- A session with an unknown remote is a team session when another session of the same repo
  in the same ingest is one (`same-repo`). This only ever turns the flag on.

The flag is read only here. The future edit step (E4) must never edit a team repo's files:
a candidate whose source is a team repo's own file (`claude-md:<repo>` / `agents-md:<repo>`)
has `team: true`. Global sources and skills are personal files and stay `team: false` even
when the evidence came from team sessions (`team_sessions` counts those).

## Instructions in force

| Source | File |
|---|---|
| `claude-md:global` | `~/.claude/CLAUDE.md` |
| `agent:<type>` | `~/.claude/agents/<type>.md` (subagents) |
| `routine:<name>` | `~/.claude/scheduled-tasks/<name>/SKILL.md`, else the tag's `file=` when it is a `SKILL.md` under `~/.claude` |
| `parent-md` | `~/agent-config/parent/PARENT.md` (parent sessions) |
| `skill:<name>` | `~/.claude/skills/<name>/SKILL.md` (symlinks are fine), else `<repo>/.claude/skills/<name>/SKILL.md` |
| `skill:<plugin>:<name>` | the plugin's `<installPath>/skills/<name>/SKILL.md` from `~/.claude/plugins/installed_plugins.json` (version and commit recorded). `anthropic-skills:*` are built into the app: unresolved. |
| `claude-md:<repo>` | `CLAUDE.md`, `.claude/CLAUDE.md` and `CLAUDE.local.md` in the cwd, and in the main checkout when the cwd is a git worktree |
| `agents-md:<repo>` | `AGENTS.md` in the cwd and the main checkout (Claude Code and Codex sessions) |
| `claude-md:~/<dir>` | the same memory files in every ancestor directory of the cwd, up to (not including) `/` |
| `import:~/<path>` | an `@import` target of a memory file (five hops at most; a target that is another source keeps that source's name, e.g. `parent-md`, `agents-md:<repo>`) |

Resolution, authority (observed / reconstructed / declared / unresolved) and versions are described in
[data-use/instruction-sources.md](data-use/instruction-sources.md). Names that resolve to no file are not
offered to the writer; `sessions.instruction_sources` records them with a reason.

Codex sessions have their own sources (Codex reads no CLAUDE.md):

| Source | File |
|---|---|
| `agents-md:global` | `~/.codex/AGENTS.override.md`, else `~/.codex/AGENTS.md` |
| `skill:<name>` | the first of `~/.claude/skills/<name>/SKILL.md` (the shared `~/.agents/skills`), `~/.codex/skills/<name>/SKILL.md`, `~/.codex/skills/.system/<name>/SKILL.md` |
| `agents-md:<repo>` | as above |

Cloud routine sessions (`meta.cloud`, id `cloud:<session id>`) have no local files in force.
Their sources are the texts the routine sent ([cloud.md](cloud.md#cloud-digests-cloud-routine-sessions-in-the-deviation-scan)):

| Source | Text |
|---|---|
| `routine:<name>` | the routine prompt (`cloud_digests.instructions`, name `prompt`) |
| `cloud-file:<name>` | an instruction file of the routine's repo (`nightly-md` = NIGHTLY.md) |

A file that two sources name (a `CLAUDE.md` symlinked to `AGENTS.md`) is read once, by the
first. The rule catalog treats AGENTS.md like any other markdown source.

`.env*` files are never read. Each source is capped at 24k characters, all sources at 70k.

### Version in force

A session with `instruction_sources` entries is graded against the versions recorded for it
(`deviations.load_versioned`): per file, the text of its observed or reconstructed version
(`instruction_texts`, a git blob, today's file when its sha matches, or the transcript itself). A file
without a recoverable version is read from today's file. The deviation's `instruction_authority` says
which: `observed` or `reconstructed` (the weaker of the source's files), or `unverified` when any file of
the source came from today's file. Sessions ingested before versions were recorded have no entries: all
their sources are `unverified` (until `amem sessions instruction-sources --apply` backfills them). A file
the transcript shows was not loaded is not offered. `instruction_sha` is the recorded version's sha for a
single-file source, else the hash of the texts read. Later stages discount `unverified` deviations.

## Rule catalog

Each source is split into rules with a stable id (`src/agent_memory/rule_catalog.py`). The
writer must name a rule by id, so one rule gets one id however the writer would spell it.
Recurrence, the report and Jev all count by `instruction_source` + `rule_id`.

| Markdown | Rule |
|---|---|
| The first heading, when it is the only one at the shallowest level (`# Parent role`) | The title. Not a rule. |
| Text between the title and the first rule | `intro` (title `Parent role (intro)`) |
| Each heading one level below the title (`## Safety guards`), or the shallowest level when there is no title | A rule: `safety-guards` |
| The first deeper level under it (`### Core rule`) | A child rule: `explaining-completed-work/core-rule`, title `Explaining Completed Work > Core rule` |
| Deeper headings, bullets, text, code | Belong to the nearest rule above |
| A file without rule headings (a routine prompt) | One rule `whole` |

- Ids are slugs of the heading without a leading step number: `## 4. Spawn` → `spawn`,
  `## 0b. Profile` → `profile`. Renumbering steps keeps the ids. The title keeps the text
  as written (`4. Spawn`).
- A repeated id gets `-2`, `-3` in reading order (`notes`, `notes-2`).
- Headings inside fenced code and YAML frontmatter are ignored. Setext headings
  (underlined) are not headings here.
- A source of several files (a repo's `CLAUDE.md` + `CLAUDE.local.md`) lists the files'
  rules one after the other (`whole`, `whole-2`).
- The catalog is built from the full files (not the capped text). `instruction_sha` is the
  sha256 of the files as read, so a later step can tell deviations before and after an edit.

**Snapping** (`rule_catalog.snap`): the writer's ref → a rule.

1. Equal to an id (any case) → `exact`.
2. Else the best similarity of the ref, of the ref as a heading path (`Spawn > Brief`), and
   of its first segment before `:`, `/`, `(`, ` - ` and the like (`Safety guards: never
   read ...` → `Safety guards`; `10. Record / 9. Clean` → `10. Record`) against each rule's
   id and title. Same title or slug: 1.0; a title that starts the ref: 0.92; a ref that
   starts a title: 0.85; else the difflib ratio. At least 0.8 → `snapped`.
3. Else, or when two rules score the same, → `unmatched`. A one-rule catalog (`whole`)
   takes every ref.

An unmatched deviation is still stored (`rule_id = unmatched`, `meta.ref_match =
unmatched`) and the scan line ends with `unmatched_refs=N`. Unmatched rows, and rows from
before the catalog until `reref` runs, group by their lowercased ref.

## Scan

Which sessions: unscanned, not `eval`, not tiny (at least 20 kB of transcript and at least 4
tool calls + typed prompts; attachments alone can make a file big), started in the window,
failed fewer than 3 times. Order: sessions that used a skill, then `routine`, `parent`,
`worker`, `chat`, `headless`, then subagents that are not a `worker` (Explore,
general-purpose, ...); newest first inside each group. Such a subagent is skipped once its
parent session was scanned, so it comes last: its parent is usually scanned first and covers it.
The daily job scans at most 40 sessions a run (docs/daily.md); a manual scan defaults to 15.

Per session:

1. **Digest**: `[turn N] USER:` prompts (the first one up to 6k characters, so a worker's brief
   stays whole; later ones 1.5k), `ASSISTANT:` text (600), `TOOL <name>: <short args>`,
   `ERROR`, `DENIED`, `HOOK BLOCKED`, `COMMAND /x`, `INTERRUPTED`, task notifications.
   Thinking blocks are left out. Credential-shaped strings are redacted (the `tasks_ingest`
   helper plus a few more key shapes) before the cap. Cap: 60k characters, 40% head + 60% tail.
2. **Writer**: `claude -p --model sonnet --output-format json --tools "" --disable-slash-commands
   --strict-mcp-config --no-session-persistence` (the `lessons distill` flags; `--bare` breaks
   OAuth) plus `--json-schema <REPLY_SCHEMA>`. Prompt on stdin: the numbered rule list per source
   (`N. <id> — <title>`), the instructions, the digest. `instruction_ref` must be an id from that
   list. It answers `{"deviations": [...]}`, at most 8. The schema makes the CLI enforce that
   shape (both grades required numbers in 0..1, never null): the CLI re-prompts the model on a
   mismatch and returns the validated object in the envelope's `structured_output` field (the
   scan falls back to the `result` text when that field is missing). If the CLI gives up
   (`error_max_structured_output_retries`, exit 1 with the envelope on stdout) or the reply is
   still malformed after validation (no valid item, not JSON), the scan retries once, in the
   same run, with the parse error appended to the prompt. At most one extra writer call per
   session; a reply with at least one valid item is not retried.
3. **Validation**: the reply must be JSON with a `deviations` list. Every item needs an offered
   `instruction_source`, a ref, a turn (positive integer or null), `what`/`why`/`how` strings,
   a `why_category` (see "Why category") and both grades as numbers in 0..1. A reply that is not
   JSON, has no list or has more than 8 items is rejected whole (session error). A malformed item
   (e.g. `"followed_grade": null`, a missing or out-of-range grade, a missing or unknown
   `why_category`, a bad turn, a source that was not offered) is skipped, and
   the session's valid items are kept: the scan counts it (`skipped=N` on the session line and
   in the summary) and stores `sessions.meta.scan_skipped` (the count) and
   `scan_skipped_reasons` (redacted, one line each, e.g. `deviation 0: followed_grade must be a
   number in 0..1, got None`). A grade is never filled in. A reply whose items are all
   malformed is retried once in the call; malformed again, it is a session error (retried on
   the next scan, reason ends with `(after 1 retry)`). The prompt says grades are never null and a
   deviation that cannot be graded is left out. Text fields
   are cleaned: quoted spans become `[quote]`, backtick code `[code]`, paths `[path]`, file
   names `[file]`, secrets `[redacted]`; a code block makes that item malformed. Max 300 characters each
   (ref 80). Duplicates (same source, ref and turn) are merged.
4. **Resolve**: each ref is snapped to its source's catalog (see Rule catalog), which sets
   `rule_id`, `rule_title`, `instruction_sha` and `meta.ref_match` / `meta.ref_score`. Two
   findings on the same rule and turn become one.
5. **Jev** (`questions/deviation.toml`): one ask per deviation. The state holds only
   `instruction_source`, `instruction_rule` (the id), `rule_title` (the heading; for an
   unmatched ref the writer's ref plus `(not in the rule list)`), `why_category` (the label and
   its fixed gloss, see "Why category"), `session_kind`, the two
   raw grades, `objective_result` (see "Objective result"; `unknown` while the session is
   active or has no outcome) and `recurrence` (other sessions in the window with a deviation
   on the same source + rule). Never transcript text and never what/why/how. Questions: `verdict`
   (edit_instruction / enforce / leave), `urgency` (score), `systemic` (noul). All answers go
   to `jev_answers`.
6. **Store**: deviations upsert on `source_key = dev:<session_id>:<hash of source + ref + turn>`
   (still the writer's ref, so keys of older rows stay valid), then `sessions.scanned_at` is
   set, in one transaction.

Errors: a writer error, an invalid reply (or one with no valid item) or a Jev error fails that session only. It is
recorded in `sessions.meta.scan_error` (and `scan_errors` counts up), nothing is stored for
it, and the scan goes on. The session is retried on the next scan. A missing Jev fixture
(fixture mode) is not an error: the deviation is stored with `jev_verdict = null` and
`jev_mode = fixture-missing`. A malformed deviation next to valid ones (e.g. a null grade:
the writer could not grade it) is not an error either: only that deviation is skipped, the
session's other findings are stored, and it shows as `skipped=N` in the scan line and
`sessions.meta.scan_skipped` / `scan_skipped_reasons` (see Validation).

Released transcripts: when the Claude Desktop app deletes a session it removes `<id>.jsonl`
(and the `<id>/` folder with its subagents) and leaves `<id>.desktop-released.json`, e.g.
`{"v": 1, "releasedAt": "2026-10-15T14:14:15.516Z", "reason": "delete"}`. Such a session (or a
subagent of it) is `skipped`, not an error: the scan line reads `skipped <id> ...: transcript
released by Claude Desktop (reason delete at ...)`, the summary adds `skipped_sessions=N`, and
`sessions.meta.scan_status = skipped` (with `scan_skip_reason`) keeps it from being picked
again. `scanned_at` stays null, so the scoreboard does not count it as a clean session. A
missing transcript without that marker is still an error (`transcript file not found`).

## Incident scans

A blocked or failed worker is an incident: scan its session now, not on the next daily run.
The parent knows the worker's agent id (the transcript is
`<projects>/<project>/<parent session>/subagents/agent-<ID>.jsonl`; the session id is
`<parent session>/agent-<ID>`).

```sh
amem sessions find --agent-id <ID> --ingest     # prints: session_id <TAB> category <TAB> project
amem deviations scan --session <session_id>     # --dry-run first to see the digest size
```

`sessions find` without `--ingest` only reads: exit 1 with `not ingested` when no row
exists yet. `--ingest` reads that one transcript (`--projects-dir`, default
`AMEM_PROJECTS_DIR` or `~/.claude/projects`) and upserts its row first, like
`sessions ingest`; exit 1 when no such file exists. `--json` prints JSON lines with `kind`,
`started_at` and `scanned_at` as well. Postgres only. `--ingest` honours
`AMEM_REQUIRE_REMOTE=1` (exit 3). `deviations scan --session` scans the session even when
it was scanned before; it still needs `AMEM_DEVIATIONS_LIVE=1` without `--dry-run`.

## Objective result

The writer's `result_grade` is its own reading of the digest. `objective_result` is the
same question answered from facts: the session's `outcome` (docs/sessions-live.md, "Outcome":
its own git, edit and report signals). Migration `20261019010000_deviation_objective.sql`
adds two columns to `deviations` (RLS unchanged):

| Column | Value |
|---|---|
| `result_outcome` | the session's outcome when the deviation was stored |
| `objective_result` | 0..1 from that outcome |

| outcome | objective_result |
|---|---|
| `shipped` | 1.0 |
| `pushed` | 0.9 |
| `committed` | 0.8 |
| `no-change` | 0.7 on a Q&A session (kind `chat`, `headless`, `routine`, or a subagent whose agent type is not `worker`); else null (a parent or dispatch worker that changed nothing is not graded) |
| `partial` | 0.4 |
| `abandoned` | 0.1 |
| `failed` | 0.0 |
| `active`, unknown | null |

- Scan: both columns are stored with the deviation, and Jev's state gets the
  `objective_result` line next to the raw `result_grade`. The question prompt tells Jev to
  trust the objective one when both are known.
- Outcomes change later (an active session ends, a branch gets pushed). `deviations report`
  recomputes from the session's current outcome (`objective_result_mean`, `outcomes`), and
  `deviations reref` writes the new values (`objective=N` in its summary when rows moved;
  also for rows whose source file is gone).

## Why category

The writer's `why` is free text, and Jev never reads it. So the writer also picks one label,
`why_category`, and Jev reads that label. Migration `20261027220000_deviation_why.sql` adds
`deviations.why_category` (text, check constraint on the six values, null for rows scanned
before it; RLS unchanged, no Night Desk grant). The scan's upsert writes it.

| why_category | Meaning | Points Jev's verdict to |
|---|---|---|
| `unclear` | the instruction is unclear | edit_instruction |
| `wrong` | the instruction is wrong or outdated | edit_instruction |
| `conflict` | the instruction conflicts with another instruction | edit_instruction |
| `missing_context` | the agent lacked context it needed | edit_instruction |
| `ignored` | the agent ignored a clear instruction | enforce (leave for a harmless one-off) |
| `tool_blocked` | a tool or hook got in the way | fix the setup: edit_instruction to fit it, or leave a one-off glitch |

- The reply schema makes `why_category` required with exactly these values. An item without
  it, or with another value, is malformed and skipped like a null grade.
- Jev's state line is `why_category: <label> (the writer's view of why: <meaning>)`. The
  meaning is the fixed text above, never the writer's words.
- The `verdict` question in `questions/deviation.toml` states the mapping. Grades,
  `objective_result` and recurrence overrule it only when they clearly disagree. The
  `systemic` question uses it too: `ignored` points to a slip, the others to the instruction
  or the setup.
- The verdict options stay edit_instruction / enforce / leave.

## Privacy rules

- Transcript text never reaches the database. The writer's what/why/how are its own words,
  cleaned of quotes, paths, code and secrets.
- Jev never sees transcript text or what/why/how. It sees the `why_category` label only.
- The writer gets the redacted digest and the instruction files, with no tools.
- `.env*` files are never read.
- Tests use synthetic transcripts only.

## Report and the candidates file

`amem deviations report` prints one line per rule, `title [id]` (raw grades first, then Jev):

```
deviations (last 7 days): 2 rules, 6 deviations
parent-md / Safety guards [safety-guards]: count=5 sessions=5 followed=0.30 result=0.80 objective=0.85 | jev edit_instruction=5 enforce=0 leave=0 unjudged=0 confidence=0.80
claude-md:global / A rule nobody wrote [unmatched]: count=1 sessions=1 ...
agents-md:app / Tests [tests]: count=2 sessions=2 ... | team repo
```

Scan lines carry `tool=codex` for Codex sessions and `team=1` for team-repo sessions.

Rows without a rule id yet show `[no rule id]`; run `amem deviations reref`.

`--json` prints the candidates file, the input for the edit step (E4, `amem edits run`, docs/edits.md):

```json
{
 "version": 2,
 "generated_at": "2026-10-15T09:20:00+00:00",
 "window_days": 7,
 "candidates": [
  {
   "instruction_source": "skill:dispatch",
   "rule_id": "spawn",
   "rule_title": "4. Spawn",
   "instruction_ref": "spawn",
   "refs": {"spawn": 3, "4. Spawn": 1},
   "instruction_shas": ["<sha256 of the skill at scan time>"],
   "target_paths": ["~/.claude/skills/dispatch/SKILL.md"],
   "count": 4,
   "sessions": 3,
   "kinds": {"parent": 3, "worker": 1},
   "tools": {"claude": 4},
   "team": false,
   "team_sessions": 0,
   "followed_grade_mean": 0.35,
   "result_grade_mean": 0.6,
   "objective_result_mean": 0.85,
   "outcomes": {"committed": 1, "shipped": 2},
   "cost": {"sessions": 3, "tokens": 182000000, "active_seconds": 5400.0, "score": 272.0},
   "jev": {"edit_instruction": 3, "enforce": 1, "leave": 0, "unjudged": 0, "confidence_mean": 0.74},
   "verdict": "edit_instruction",
   "session_ids": ["..."],
   "deviation_ids": ["..."],
   "examples": [{"session_id": "...", "turn": 2, "what": "...", "why": "...", "how": "...", "team": false}]
  }
 ]
}
```

- One candidate per source + rule. `rule_id` is `unmatched` or null for groups keyed by a
  ref (see Rule catalog). `instruction_ref` is the most common writer spelling, `refs` all
  of them. `instruction_shas`: the distinct source versions the deviations were found on.
- Version 2 (version 1 grouped by the writer's ref).
- Sorted by distinct sessions, then count, then the lowest followed grade.
- `target_paths`: the files that make up the source today (`~`-relative). Empty when gone.
- `verdict`: the most common Jev verdict (ties: edit_instruction, enforce, leave), or null
  when nothing was judged.
- `examples`: up to 3, newest sessions first.
- `objective_result_mean`: the mean objective result over the distinct sessions (from their
  outcome now; null when none is graded). `outcomes`: distinct sessions per outcome.
- `cost`: what the deviating sessions cost, each session once: `tokens` (all four kinds),
  `active_seconds`, and `score = tokens / 1,000,000 + active_seconds / 60` (1M tokens weigh
  as much as one active minute). `edits run` orders by it (docs/edits.md, "Order").
- `tools`: deviations per agent tool. `team`: the source is a team repo's own file, so an
  edit must not touch it (see Team repos). `team_sessions`: sessions in a team repo.

## Daily

`amem daily` runs `sessions ingest` and `deviations scan` once a day, after the escalations
step. The daily state file holds `deviations_last_scan`; the step runs when it is missing or
at least 20 hours old (`AMEM_DEVIATIONS_EVERY_HOURS`). Off (not a failure) unless `AMEM_DEVIATIONS_LIVE=1`. When it ran, the
log line gets `deviations=D deviation_sessions=N deviations_edit=E deviations_enforce=F
deviations_leave=L deviation_errors=X deviation_skipped=S`. Any session error fails the step.
Skipped writer items (`deviation_skipped`) are a counted warning, not a failure: each one gets
a `deviations: invalid item skipped: session <id>: <reason>` line on stderr. The timestamp moves
when the scan finished, so failed sessions wait for next week; a crash before the scan leaves
it, so the step retries the next day. See [daily.md](daily.md).

## Env

| Variable | Use |
|---|---|
| `AMEM_DEVIATIONS_LIVE=1` | Allows writer calls (scan and the daily step). The launchd job sets it. |
| `AMEM_DEVIATIONS_CLAUDE` | Writer executable (default `claude`). |
| `AMEM_PROJECTS_DIR` | Transcripts folder (default `~/.claude/projects`). |
| `AMEM_CLAUDE_HOME` | Claude home for instructions (default `~/.claude`). Tests only. |
| `AMEM_CODEX_HOME` | Codex home (default: `.codex` next to the Claude home, so `~/.codex`). |
| `AMEM_PERSONAL_OWNERS` | Comma list of git remote owners that are personal (default `vilmerfrost`). Any other owner makes a team repo. |
| `AMEM_AGENT_CONFIG_DIR` | agent-config checkout for PARENT.md (default `~/agent-config`). Tests only. |
