# Edits: the loop changes the instruction itself

The edit step (roadmap v2 E4) takes the rules Jev flagged in the deviations report and
rewrites them. agent-config's `improve apply` writes each change as one commit, runs the
evals and keeps it, keeps it on trial, or reverts it. Every kept edit reaches Vilmer's
phone with an **Undo** button. The scoreboard (E5) shows whether the edited rules really
see fewer deviations afterwards.

```
deviations report ──▶ candidates (edit_instruction | enforce, confidence >= 0.6, not team)
                         │ target: the real file (or improve suggest for repo files)
                         ▼
          writer: claude -p rewrites ONE rule section ──▶ validate + splice into a temp copy
                         │
                         ▼
          improve apply --json ──▶ kept | trial | auto-reverted | refused   (edits row)
                         │ kept / trial
                         ▼
          ntfy push "Agent edit: <rule>" + Undo button ──▶ edge function amem-undo
                                                              │ undo_requests row
          improve revert <id> ◀── amem edits undo-poll (every 15 min) ◀┘
```

## Commands

| Command | Does |
|---|---|
| `amem edits run [--dry-run] [--limit 5] [--min-confidence 0.6] [--days 7] [--no-push]` | Plans and applies edits. Prints one line per candidate and `edits: edits=N kept=K reverted=R refused=F suggested=S skipped=X errors=E`. Exit 1 when a candidate failed. |
| `amem edits list [--days 28]` | Recent edits, newest first: id, time, status, rule, file, improve id, summary. |
| `amem edits undo <edit_id> [--no-push]` | Reverts one edit now (`improve revert <id> --reason "manual undo"`). |
| `amem edits undo-poll [--no-push]` | Handles pending phone undo requests. Exit 0 when there is nothing to do. |
| `amem edits scoreboard [--days 28] [--json]` | Deviations per scanned session before vs after each edit. |

All need the Postgres backend. The write commands honour `AMEM_REQUIRE_REMOTE=1` (exit 3).

- `edits run` calls the writer and `improve` only with `AMEM_EDITS_LIVE=1`. Without it (or
  with `--dry-run`) it prints the plan and calls and writes nothing:

  ```
  plan edit skill:dispatch spawn -> ~/.agents/skills/dispatch/SKILL.md: edit_instruction 0.80, dev:3 deviations, 3 sessions
  plan suggest claude-md:demo-repo whole -> ~/work/demo-repo/CLAUDE.md: repo file: never edited, suggested instead
  skip parent-md merge -> -: jev confidence 0.40 < 0.60
  edits (dry run, nothing called or written): plan_edit=1 plan_suggest=1 skip=1
  ```

- `--limit` caps edits + suggestions per run (writer calls cost money and time); the rest
  waits (`deferred=N`).

## Candidates

From `deviations report --json` (`deviations.candidates`, same data), most recurring first:

- Jev verdict `edit_instruction` or `enforce`, mean Jev confidence >= `--min-confidence`.
  `leave` and unjudged groups are not candidates.
- `team: true` → skipped. Team repos are never changed (a missing flag means false).
- A real rule id (not `unmatched`; run `amem deviations reref` for old rows).
- Not handled at this version. Handled = an `edits` row exists for the same source + rule
  whose `instruction_sha_before` is the file's current sha, or covers every sha the
  candidate's deviations were found on. So a rule is edited once per version; a new
  deviation found on the edited text makes it a candidate again; an undone edit (the file
  is back at the old sha) is never retried.

## Targets

| Source | File | Action |
|---|---|---|
| `skill:<name>` | the real `SKILL.md` under `~/.agents/skills` (symlinks resolved) | edit |
| `claude-md:global` | `~/.claude/CLAUDE.md` | edit |
| `parent-md` | `~/agent-config/parent/PARENT.md` | edit |
| `agent:<type>` | `~/agent-config/agents/<type>.md` | edit |
| `routine:<name>` | `~/.claude/scheduled-tasks/<name>/SKILL.md` | edit |
| `agents-md:global` | `~/.codex/AGENTS.md` | edit |
| `claude-md:<repo>`, `agents-md:<repo>` | a file inside a repo | `improve suggest`, never edited |

An "edit" file outside improve's `allowed_targets` (read from
`~/agent-config/improve/criteria.json`) becomes an `improve suggest` too. Today that is the
routine prompts, `~/.codex/AGENTS.md` and agents other than `worker`, until agent-config
allows them. A skill whose real file is not under `~/.agents/skills` is suggested as well.

## Writer

A headless `claude -p` (the `lessons distill` flags: no tools, no skills, no MCP;
`AMEM_EDITS_CLAUDE` may point at another executable) gets:

- the current text of the one rule section and the rules before and after it (redacted),
- the deviations' summaries in the scanner's own words (what / why / how), the raw grades,
  Jev's verdict and confidence, and the recurrence (count, sessions, kinds),
- Vilmer's policy: the goal is adherence. `enforce` = make the rule clearer, more prominent
  or more concrete (a hook only via `improve suggest`); `edit_instruction` = rewrite the
  rule to match what works. Never add an ask-Vilmer permission gate on his own projects.
  Never remove or loosen anything about spending money. When two instructions conflict,
  fix the conflict. Minimal change, same style, one rule = one topic.

It answers `{"new_section", "topic" (2-5 words), "why" (<= 200 chars), "summary" (<= 140 chars)}`.

**Validation** (any failure is that candidate's error; nothing is written):

- JSON object with all four fields; `why` and `summary` one line, no ids, cleaned of paths
  and code like the deviation fields.
- `new_section` keeps the rule's heading line exactly, adds no heading (the file's rule list
  must stay the same), adds no code block, holds no secret, no scanner placeholder
  (`[path]`, `[quote]`, ...), no session or deviation id, and no 8-word run copied from a
  deviation text. It may grow the rule by at most 1500 characters (improve's guard).
- Policy checks on the text: no added line that asks for approval / permission /
  confirmation (unless negated: "without waiting for approval" is fine), and every
  existing line about spending money stays word for word.
- A reply equal to the current section → `skipped` (recorded, not retried at this version).

## Splice

The section is found by the same rules as the rule catalog (`edits_sections.py`; its spans
are checked against `rule_catalog.build`). Only that span changes; its leading and trailing
blank lines, the rest of the file and a missing final newline stay as they were. The result
goes to a temp file, and the target file must still have the sha it had when the writer
started.

## The improve contract

```
improve apply --target <file> --rule-id <id> --new-file <tmp> --topic ... --why ... \
              --evidence "dev:<n> deviations, <m> sessions" --json
```

- Exactly one JSON line on stdout: `{"id": "<change id or null>", "status":
  "kept|trial|auto-reverted|refused", "reason": "...", "revert": "improve revert <id>"}`.
  Exit 0 for kept / trial / auto-reverted, 2 for refused. Other output lines are ignored; the
  last JSON line counts.
- `--rule-id` is namespaced by the source (`skill-dispatch--spawn`,
  `claude-md-global--explaining-completed-work--core-rule`), kebab-case, at most 61
  characters: improve blocks a reverted rule id everywhere, and catalog ids such as `intro`
  repeat across files.
- No JSON line, an unknown status, a status/exit mismatch, kept/trial without an id, or a
  missing executable: an error. The row was written as `proposed` before improve ran, so
  it stays `proposed` with `meta.error`, and the rule is not retried at that version
  (improve may have touched the file).
- `improve revert <id> --reason "..."` exit 0 = reverted. `improve suggest --title --why
  --evidence [--target]` exit 0 = logged.
- `AMEM_IMPROVE_BIN` points at another executable (tests); default `~/agent-config/bin/improve`.

## Push and undo

For each kept or trial edit:

- A random token per edit; only `sha256(token)` is stored (`edits.undo_token_hash`).
- ntfy, published as JSON to the server root (topic and server from
  `~/.claude/parent/notify.env`, as in `amem daily`; the topic is never printed). Title
  `Agent edit: <rule title>`, body = summary, `Why: ...`, `File: <short name>` (and `Trial: ...`).
- An `http` action **Undo**: `POST <project URL>/functions/v1/amem-undo` with
  `{"edit_id", "token"}`, `clear: true`. The project URL is `AMEM_SUPABASE_URL`, else derived
  from `AMEM_DB_URL` (`db.<ref>.supabase.co`, or a pooler user `postgres.<ref>`);
  `AMEM_EDITS_UNDO_URL` overrides the whole URL. No URL → the body names
  `amem edits undo <id>` instead of a button.
- ntfy missing or not 200 → a macOS notification naming `amem edits undo <id>`.

**Edge function** `supabase/functions/amem-undo` (`handler.ts` + `index.ts`): POST only,
body at most 2 kB, `edit_id` a uuid, `token` 16-200 chars, else 400. It reads the edit with
the service role (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` from the function env),
compares `sha256(token)` in constant time (mismatch or unknown edit: 403 `Not allowed`,
nothing else), inserts `undo_requests` with `on_conflict=edit_id` + ignore-duplicates (a
second tap does nothing) and answers 200 `text/plain` "Undo queued — reverts within 15
minutes". It never reverts anything itself. `verify_jwt = false` (supabase/config.toml):
the button sends no JWT; the token is the auth.

- Tests: `supabase/functions/amem-undo/handler_test.ts` runs under `deno test` and
  `node --test` (pytest runs it with node when node is installed). `deno check` was not run
  where deno is missing.
- Deploy (the parent, never a worker): `supabase functions deploy amem-undo --no-verify-jwt`.
- Manual test after deploy: `amem edits list` → take an edit id; a wrong token must give
  403: `curl -s -o /dev/null -w '%{http_code}' -X POST <project URL>/functions/v1/amem-undo
  -H 'Content-Type: application/json' -d '{"edit_id":"<id>","token":"wrong-token-0123456789"}'`
  → `403`. The real token only lives in the phone notification; tapping Undo gives the
  200 text and one `undo_requests` row.

**Undo poll** (`amem edits undo-poll`; agent-config's amem-ingest calls it every 15 minutes):
each pending request whose edit is kept / trial → `improve revert <improve id> --reason
"phone undo"` (`"manual undo"` from `amem edits undo`). Exit 0 → request handled
(`result = reverted`), edit `status = reverted`, `reverted_at` set, a short "Agent edit
undone" push. A failed revert is recorded (`attempts`, `result = failed (attempt N): ...`)
and retried on the next poll; after 3 attempts the request is closed and an "undo failed"
push goes out. An edit with nothing to revert (auto-reverted, refused, already reverted)
closes the request with `nothing to revert (status ...)`.

## Scoreboard (E5)

`amem edits scoreboard` lists each applied edit (kept, trial, reverted) of the last N days:

```
edits scoreboard (last 28 days): 2 edits, 1 fewer, 0 same, 0 more, 1 too early
- skill:dispatch / 4. Spawn [spawn] (kept, 2026-10-15, ~/.agents/skills/dispatch/SKILL.md): before 0.50 per session (4 in 8) -> after 0.08 per session (1 in 13): fewer
```

- before: deviations on the same source + rule id in sessions that started in the N days
  before the edit, divided by the scanned sessions (not eval) that had the source in force.
- after: the same from the edit until now (or until it was reverted), counting only
  deviations not found on the old text (`instruction_sha` differs from the edit's
  `instruction_sha_before`).
- verdict: `too early` until both windows hold 5 scanned sessions; `fewer` when after < 75%
  of before; `more` when after > 125% of before and at least 0.05 more per session; else
  `same`.

## Ask a Claude chat: is the loop working?

Paste this into a Claude Code chat on the Mac (the commands need the main checkout's env):

> Run `uv run --directory ~/Projects/agent-memory amem edits scoreboard`,
> `uv run --directory ~/Projects/agent-memory amem edits list --days 28` and
> `uv run --directory ~/Projects/agent-memory amem deviations report`. Tell me in five lines:
> how many edits were kept, how many I undid, which edited rules now see fewer or more
> deviations, and which are too early to judge.

The scoreboard line per edit is the proof: `fewer` means the edited rule is followed more
often than before. `more` on a kept edit is a reason to undo it (`amem edits undo <id>`).

## Daily

`amem daily` runs `edits run` after the deviations step, only with `AMEM_EDITS_LIVE=1` (the
launchd job sets it; off is not a failure). Kept edits are pushed (not with `--no-push`).
When the step ran, the log line gets `edits=N edits_kept=K edits_reverted=R
edits_suggested=S edit_errors=E` (N = edits improve answered or garbled; K = kept + trial;
R = auto-reverted by improve's evals). Any candidate error fails the step; the other
candidates still run. See [daily.md](daily.md).

## Tables

`edits` and `undo_requests` (migration `20261017010000_edits.sql`), RLS on with no policies,
no grants for `anon` / `authenticated`. The edge function uses the service role.

## Env

| Variable | Use |
|---|---|
| `AMEM_EDITS_LIVE=1` | Allows writer and improve calls (`edits run`, the daily step). The launchd job sets it. |
| `AMEM_EDITS_CLAUDE` | Writer executable (default `claude`). |
| `AMEM_IMPROVE_BIN` | improve executable (default `~/agent-config/bin/improve`). Tests only. |
| `AMEM_EDITS_UNDO_URL` | Full undo URL (local stack, tests). |
| `AMEM_AGENTS_HOME`, `AMEM_CODEX_HOME` | `~/.agents`, `~/.codex` for tests. |
