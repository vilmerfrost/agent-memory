# Deviations: where agents leave their instructions

The deviation finder reads Vilmer's Claude Code sessions. It finds where an agent did not
follow a skill, a CLAUDE.md or PARENT.md rule, an agent definition or a routine prompt. It
writes down why, when and how, gives two raw grades, and Jev makes the call.

It is **read only** toward the setup. It never edits a skill or any instruction file.
Editing comes later (step E4), through agent-config's `improve apply` / `improve revert`.

```
~/.claude/projects/*.jsonl ──sessions ingest──▶ sessions
                                                   │ deviations scan (weekly)
                                                   ▼
            digest (redacted, capped) + instructions in force
                                                   │ writer: claude -p, JSON
                                                   ▼
            deviations: what / why / how, followed_grade, result_grade
                                                   │ Jev: labels + grades + recurrence only
                                                   ▼
            jev_verdict: edit_instruction | enforce | leave
                                                   │ deviations report --json
                                                   ▼
            candidates file ──(E4, later)──▶ improve apply → one commit per edit
```

## Commands

| Command | Does |
|---|---|
| `amem sessions ingest [--days 7] [--projects-dir DIR]` | Indexes transcripts touched in the last N days into `sessions`. Idempotent. |
| `amem deviations scan [--days 7] [--limit 15] [--session ID] [--dry-run] [--timeout 300]` | Scans unscanned sessions. Prints one line per session and `deviations: sessions=N found=D edit=E enforce=F leave=L errors=X`. Exit 1 when a session failed. |
| `amem deviations report [--days 7] [--json]` | Groups deviations by instruction source + ref, most recurring first. `--json` prints the candidates file. |

All three need the Postgres backend (V1). The two write commands honour
`AMEM_REQUIRE_REMOTE=1` (exit 3).

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
- A re-ingest refreshes the facts and keeps `scanned_at` and the scan keys in `meta`.

## Instructions in force

| Source | File |
|---|---|
| `claude-md:global` | `~/.claude/CLAUDE.md` |
| `agent:<type>` | `~/.claude/agents/<type>.md` (subagents) |
| `routine:<name>` | `~/.claude/scheduled-tasks/<name>/SKILL.md`, else the tag's `file=` when it is a `SKILL.md` under `~/.claude` |
| `parent-md` | `~/agent-config/parent/PARENT.md` (parent sessions) |
| `skill:<name>` | `~/.claude/skills/<name>/SKILL.md` (symlinks are fine). Plugin skills (`ns:name`) have no local file and are left out. |
| `claude-md:<repo>` | `CLAUDE.md` and `CLAUDE.local.md` in the cwd, and in the main checkout when the cwd is a git worktree |

`.env*` files are never read. Each source is capped at 24k characters, all sources at 70k.

## Scan

Which sessions: unscanned, not `eval`, not tiny (at least 20 kB of transcript and at least 4
tool calls + typed prompts; attachments alone can make a file big), started in the window,
failed fewer than 3 times. Order: sessions that used a skill, then `routine`, `parent`,
`worker`, `chat`, `headless`; newest first inside each group. A subagent that is not a
`worker` (Explore, general-purpose, ...) is skipped once its parent session was scanned.

Per session:

1. **Digest**: `[turn N] USER:` prompts (the first one up to 6k characters, so a worker's brief
   stays whole; later ones 1.5k), `ASSISTANT:` text (600), `TOOL <name>: <short args>`,
   `ERROR`, `DENIED`, `HOOK BLOCKED`, `COMMAND /x`, `INTERRUPTED`, task notifications.
   Thinking blocks are left out. Credential-shaped strings are redacted (the `tasks_ingest`
   helper plus a few more key shapes) before the cap. Cap: 60k characters, 40% head + 60% tail.
2. **Writer**: `claude -p --model sonnet --output-format json --tools "" --disable-slash-commands
   --strict-mcp-config --no-session-persistence` (the `lessons distill` flags; `--bare` breaks
   OAuth). Prompt on stdin. It answers `{"deviations": [...]}`, at most 8.
3. **Validation**: the reply must be JSON with a `deviations` list. Every item needs an offered
   `instruction_source`, a ref, a turn (positive integer or null), `what`/`why`/`how` strings
   and both grades in 0..1. Anything else rejects the whole reply (session error). Text fields
   are cleaned: quoted spans become `[quote]`, backtick code `[code]`, paths `[path]`, file
   names `[file]`, secrets `[redacted]`; a code block is a rejection. Max 300 characters each
   (ref 80). Duplicates (same source, ref and turn) are merged.
4. **Jev** (`questions/deviation.toml`): one ask per deviation. The state holds only
   `instruction_source`, `instruction_ref`, `session_kind`, the two grades and `recurrence`
   (other sessions in the window with a deviation on the same source + ref). Never transcript
   text and never what/why/how. Questions: `verdict` (edit_instruction / enforce / leave),
   `urgency` (score), `systemic` (noul). All answers go to `jev_answers`.
5. **Store**: deviations upsert on `source_key = dev:<session_id>:<hash of source + ref + turn>`,
   then `sessions.scanned_at` is set, in one transaction.

Errors: a writer error, an invalid reply or a Jev error fails that session only. It is
recorded in `sessions.meta.scan_error` (and `scan_errors` counts up), nothing is stored for
it, and the scan goes on. The session is retried on the next scan. A missing Jev fixture
(fixture mode) is not an error: the deviation is stored with `jev_verdict = null` and
`jev_mode = fixture-missing`.

## Privacy rules

- Transcript text never reaches the database. The writer's what/why/how are its own words,
  cleaned of quotes, paths, code and secrets.
- Jev never sees transcript text or what/why/how.
- The writer gets the redacted digest and the instruction files, with no tools.
- `.env*` files are never read.
- Tests use synthetic transcripts only.

## Report and the candidates file

`amem deviations report` prints one line per instruction (raw grades first, then Jev):

```
deviations (last 7 days): 2 instructions, 6 deviations
claude-md:global / Explaining: count=5 sessions=5 followed=0.30 result=0.80 | jev edit_instruction=5 enforce=0 leave=0 unjudged=0 confidence=0.80
```

`--json` prints the candidates file, the input for the future improve ACT step (E4):

```json
{
 "version": 1,
 "generated_at": "2026-10-15T09:20:00+00:00",
 "window_days": 7,
 "candidates": [
  {
   "instruction_source": "skill:dispatch",
   "instruction_ref": "Brief",
   "target_paths": ["~/.claude/skills/dispatch/SKILL.md"],
   "count": 4,
   "sessions": 3,
   "kinds": {"parent": 3, "worker": 1},
   "followed_grade_mean": 0.35,
   "result_grade_mean": 0.6,
   "jev": {"edit_instruction": 3, "enforce": 1, "leave": 0, "unjudged": 0, "confidence_mean": 0.74},
   "verdict": "edit_instruction",
   "session_ids": ["..."],
   "deviation_ids": ["..."],
   "examples": [{"session_id": "...", "turn": 2, "what": "...", "why": "...", "how": "..."}]
  }
 ]
}
```

- Sorted by distinct sessions, then count, then the lowest followed grade.
- `target_paths`: the files that make up the source today (`~`-relative). Empty when gone.
- `verdict`: the most common Jev verdict (ties: edit_instruction, enforce, leave), or null
  when nothing was judged.
- `examples`: up to 3, newest sessions first.

## Daily

`amem daily` runs `sessions ingest` and `deviations scan` once a week, after the escalations
step. The daily state file holds `deviations_last_scan`; the step runs when it is missing or
at least 7 days old. Off (not a failure) unless `AMEM_DEVIATIONS_LIVE=1`. When it ran, the
log line gets `deviations=D deviation_sessions=N deviations_edit=E deviations_enforce=F
deviations_leave=L deviation_errors=X`. Any session error fails the step. The timestamp moves
when the scan finished, so failed sessions wait for next week; a crash before the scan leaves
it, so the step retries the next day. See [daily.md](daily.md).

## Env

| Variable | Use |
|---|---|
| `AMEM_DEVIATIONS_LIVE=1` | Allows writer calls (scan and the daily step). |
| `AMEM_DEVIATIONS_CLAUDE` | Writer executable (default `claude`). |
| `AMEM_PROJECTS_DIR` | Transcripts folder (default `~/.claude/projects`). |
| `AMEM_CLAUDE_HOME` | Claude home for instructions (default `~/.claude`). Tests only. |
| `AMEM_AGENT_CONFIG_DIR` | agent-config checkout for PARENT.md (default `~/agent-config`). Tests only. |
