# Live sessions and tokens per session

Every Claude Code session gets a `sessions` row while it runs, and every row carries its
token counts. Anyone with database access (Vilmer, an agent, a report) can see which
sessions ran, where, and how many tokens each one used.

```
Claude Code ──SessionStart / Stop hook──▶ deploy/hooks/amem-session-hook.sh (exits at once)
                                              │ detached
                                              ▼
                                     amem sessions ping ──▶ sessions row (remote Postgres)
                                              │ DB down or no remote
                                              ▼
                              ~/.claude/parent/amem-session-spool.jsonl ──next ping / sessions ingest──▶ sessions

amem sessions ingest (daily) ──full read of every transcript──▶ the same rows, all facts and tokens
amem sessions tokens ──▶ totals per repo / tool / kind / model, top sessions
```

The hook is the fast, live path. `amem sessions ingest` stays the full, authoritative read:
it sets the real kind, skills and instructions, the subagent rows, and the token totals.

## What is stored

New columns on `sessions` (migration `20261018010000_session_tokens.sql`; RLS stays on, no
API-role grants):

| Column | Value |
|---|---|
| `tokens_input` | Uncached input tokens. |
| `tokens_output` | Output tokens (Codex: includes reasoning tokens). |
| `tokens_cache_read` | Input tokens read from the prompt cache. |
| `tokens_cache_creation` | Input tokens written to the prompt cache. |
| `models` | Per model: `{"<model>": {input, output, cache_read, cache_creation, calls}}`. |
| `turns` | Prompts a person typed (a worker's brief counts as one). |
| `last_activity_at` | The newest transcript event, or the newest hook ping. |
| `tokens_updated_at` | When the token columns last changed. |

Null token columns mean "not measured yet" (an old row, or a row with only a SessionStart).

How the counts are made (`src/agent_memory/session_tokens.py`):

- **Claude Code.** The sum of `message.usage` over the assistant lines. One API response is
  written as several lines (one per content block), each repeating the usage of the same
  `message.id`. Per id, the latest line wins, so a response counts once, and a streamed
  response whose first line had a partial usage counts with its final usage.
  `<synthetic>` messages (no API call) are left out.
- **Subagents.** Each subagent transcript is its own row (kind `worker`, id
  `<session>/agent-<id>`). The parent row's `meta.subagent_tokens` holds their sum
  (`{input, output, cache_read, cache_creation, sessions}`). It is never added to a total,
  so nothing is counted twice. Codex subagent threads work the same way.
- **Resumed sessions.** A resumed Claude Code session's transcript starts with a copy of the earlier session's lines (with the earlier `sessionId`), and its subagent files are copied whole; `sessions ingest` and the Stop ping count tokens, turns, time and outcome signals only from lines whose `sessionId` is the session's own (or missing), so the copy adds nothing (a copied title is kept as a fallback; kind and skills still read every line).
- **Codex.** The `token_count` events hold the thread's running totals; the last one is
  the total. Codex counts cached input inside `input_tokens`, so it is split:
  `input = input_tokens - cached_input_tokens`. Each event's growth is credited to the
  model of the latest `turn_context`. A forked subagent's copy of the parent history is
  skipped.
- **Cloud routines.** `tools/amem_digest.py` (script version 2) adds
  `meta.tokens = {input, output, cache_read, cache_creation, models}` to the digest row;
  `sessions ingest` copies it into the columns. Digests from version 1 have no tokens.

Known limit (inferred from real transcripts, not confirmed by Anthropic docs): subagent
transcripts often hold a lower `output_tokens` than the API really used, because Claude
Code writes some of their lines before the response ends. Input and cache counts look
complete.

## `amem sessions ping`

Reads one hook payload (stdin, or `--input FILE`; `--delete-input` removes the file after
reading). It uses only `session_id`, `transcript_path`, `cwd`, `hook_event_name` and, on
SessionStart, `source`, `model` and `agent_type`. A payload with `agent_id` (a subagent)
is ignored: `sessions ingest` makes the subagent rows.

| Event | Writes |
|---|---|
| `SessionStart` | A minimal row: `session_id`, `tool` claude, `cwd` → `repo`, `project_dir` (the transcript folder), `started_at`, a best-effort `kind` (`eval` for eval clones, `headless` when `CLAUDE_CODE_ENTRYPOINT` starts with `sdk`, else `chat`), `meta.live`, `meta.start_source`, `meta.model`. An existing row keeps its kind and facts; `meta` is merged. |
| `Stop` | Token columns, `models`, `turns`, `last_activity_at`, `ended_at`, `meta.subagent_tokens`, and the profile columns (title, model, effort, time, outcome; see "Profile columns"). SessionStart sets `category` / `project` and the title an earlier scan saw. |
| `SessionEnd` (optional) | As Stop, never debounced, plus `meta.ended`. |

Cheap by design:

- **Incremental.** `~/.claude/parent/amem-session-state.json` keeps, per session, the byte
  offset read so far and the running totals (and the same per subagent file). A Stop reads
  only the new complete lines. A half-written last line is read on the next ping. Entries
  older than 14 days are dropped.
- **Debounce.** A Stop less than 20 s after the last token update of the same session is
  skipped. The next Stop, or `sessions ingest`, catches up.
- **Never fails.** Exit 0 always (bad payload, bad arguments, any error). A hard timeout
  (25 s) ends it. Postgres `connect_timeout` is 5 s. One lock (`amem-session.lock`) guards
  the state and spool; a ping that waits more than 5 s for it skips (Stop) or spools a
  minimal row (SessionStart).
- **Spool.** A ping that cannot write appends its row to
  `~/.claude/parent/amem-session-spool.jsonl` (cap 5 MB). The next ping that reaches the
  database, or `amem sessions ingest`, writes the spool first, then removes it. Token totals
  of a session only grow: a late spooled ping never lowers them.

Where it writes: only to a **remote** Postgres. `AMEM_DB_URL` comes from the repo's
`.env.local` (`config.load_env`). No `AMEM_DB_URL`, a local host (the AMEM_REQUIRE_REMOTE
rule: localhost, loopback, a socket), or the REST backend → spool only, nothing is
written. So the hook never fills a local database by accident. `AMEM_PING_ALLOW_LOCAL=1`
allows a local database; tests only.

Log: one line per run in `~/.claude/parent/amem-session-hook.log`
(`amem-ping <time> <event> <session id prefix> <status> <seconds>`). It never holds the DSN
or transcript text. Over 1 MB it moves to `.log.1`.

## The hook script

`deploy/hooks/amem-session-hook.sh` saves the payload to a private temp file
(`~/.claude/parent/amem-hook-tmp/payload.*`, mode 600), starts
`uv run --directory ~/Projects/agent-memory --quiet amem sessions ping --input <file> --delete-input --setsid`
in the background (`nohup`, own stdin/stdout/stderr, a new session), and exits 0 at once.
Claude Code never waits for the database.

Overrides: `AMEM_HOOK_REPO` (checkout), `AMEM_HOOK_DIR` (log and temp dir), `AMEM_HOOK_UV`
(the uv executable), `AMEM_HOOK_OFF=1` (do nothing).

## Install (the parent does this; workers never edit settings.json)

Add this to the `hooks` object of `~/.claude/settings.json`. `timeout` is in seconds; the
script returns in well under one.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/vilmerfrost/Projects/agent-memory/deploy/hooks/amem-session-hook.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/vilmerfrost/Projects/agent-memory/deploy/hooks/amem-session-hook.sh",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

No `matcher` on SessionStart: it fires for `startup`, `resume`, `clear`, `compact` and
`fork`. Optional: the same entry under `"SessionEnd"` writes the final totals when a
session closes (not debounced).

Check after install: start a session, send one prompt, then
`tail -3 ~/.claude/parent/amem-session-hook.log` shows `SessionStart ... written` and
`Stop ... written`, and `uv run amem sessions tokens --days 1` lists the session.

## Privacy

- No transcript text is stored or logged: only ids, labels (model, cwd, repo, kind) and
  numbers. The Stop payload's `last_assistant_message` is never read; the payload file is
  deleted right after reading.
- The state and spool files hold the same numbers and labels, mode 600.
- `cwd` is stored (as `sessions ingest` already does).
- The session title is the one user-named label stored: redacted, max 200 chars. Outcome
  evidence is signal names and counts, never commands.

## Disable

- Fast: `export AMEM_HOOK_OFF=1` in the environment Claude Code starts with (the script
  then reads stdin and exits).
- For good: remove the two (three) entries from `~/.claude/settings.json`.
- Clean up local files: `~/.claude/parent/amem-session-{state.json,spool.jsonl,hook.log,lock}`
  and `~/.claude/parent/amem-hook-tmp/`. Rows already in `sessions` stay.

## Profile columns

Migration `20261018040000_session_profile.sql`. `sessions ingest` fills them for every tool
(and backfills old rows on the next ingest), `sessions ping` fills them from its incremental
scan, cloud digests (script version 3+) carry them. Null means "not known"; nothing is guessed.

### Title

`title` (+ `title_updated_at`): the name the session was given. Claude Code: the last
`"type": "custom-title"` line's `customTitle` (a rename wins). Codex:
`<codex home>/session_index.jsonl` lines `{id, thread_name, updated_at}`, the latest
`updated_at` per thread id wins. Cloud: the digest's `meta.title`. A title is not content,
but it is user-typed, so it is redacted like other text and capped at 200 chars.

### Project and category

`category` = the session kind: `routine`, `chat` (local or cloud), `parent`, `worker`
(subagents, dispatch workers), `eval`, `headless` (`claude -p`, `codex exec`).

`project` = where the work happened, first match wins:

| Where | project |
|---|---|
| a routine | `routine:<name>` (`routine:nightly-trials`) |
| an eval clone (`~/.cache/agent-evals`, `~/.cache/skill-evals`, `eval-jo-*`, probes) | `evals` |
| a Claude Desktop scratch workspace, a Codex `~/Documents/Codex/<date>/<slug>` folder, a session scratchpad | `scratch` |
| a worktree (`<repo>/.claude/worktrees/<x>`, `~/worktrees/<repo>/<x>`, a git worktree checkout) | its main repo |
| inside a git repo | the repo folder's name (`simplitics drift check`) |
| the home folder | `home` |
| anything else | the folder's basename; never empty (`unknown`) |

Cloud chats use the repo of the session's cwd.

### Model, effort, duration

| Column | Value |
|---|---|
| `model` | the main model: the most output tokens; a tie goes to the most recent |
| `model_last` | the model of the latest reply (a mid-session switch shows) |
| `effort`, `effort_last`, `efforts` | the effort on the most replies (tie: most recent), the latest reply's, `{level: replies}`. Claude Code: `perTurnEffort`, else `effort` on the assistant line; Codex: `effort` / `reasoning_effort` of the turn context |
| `duration_seconds` | wall clock, first to last transcript event (every tool) |
| `active_seconds` | the sum of the gaps between consecutive events; a gap over 10 minutes is idle and left out |
| `api_seconds`, `tool_seconds`, `lines_added`, `lines_removed` | Claude Code `cost-state` lines (the last one wins): `totalAPIDuration`, `totalToolDuration` (milliseconds in the transcript; checked: `totalDuration` 140305 for a 139.6 s session), `totalLinesAdded`, `totalLinesRemoved`. Codex: null |

`<synthetic>` and `unknown` are never a model. One response written as several lines is one reply.

### Outcome

`outcome` (+ `outcome_evidence`, `outcome_at` = when it last changed), from the session's own
tool calls (Claude Code `tool_use`, Codex command / file-change events). The most advanced
state wins, in this order:

| outcome | when |
|---|---|
| `shipped` | `gh pr merge`, a `git push` to main/master, `git checkout main && git merge`, or a deploy (`vercel ... --prod`, `supabase functions deploy`, `supabase db push`, `railway up`, `amem db migrate`) |
| `pushed` | `git push` of another branch, or `gh pr create` |
| `committed` | `git commit`, nothing pushed |
| `partial` | a worker report `Status: PARTIAL` / `BLOCKED` (also in a `SubagentHandback`) |
| `active` | last activity under 2 h ago |
| `abandoned` | last activity over 24 h ago and the session ended on a prompt with no reply (`end` `user`) or a tool call with no result (`end` `tool`), or it edited files, never committed and reported no `Status: DONE`. Never for a worker (below). Ending on a tool result (`end` `result`) is not abandonment: the call got its answer |
| `partial` | files were edited (Edit, Write, MultiEdit, NotebookEdit, apply_patch, a Codex FileChange), nothing committed |
| `failed` | it ended on an API / tool error or an interrupt |
| `no-change` | none of the above: Q&A, research, reviews |

**Workers** (kind `worker`: subagents, dispatch workers, Codex subagent threads). A subagent
transcript normally ends on the tool result of its hand-back (`SubagentHandback`) or on its
final reply, so its end shape says nothing about abandonment. After the git rows, `partial`
from a `Status: PARTIAL` / `BLOCKED` report and `active`: edits without a commit → `partial`;
no report and an error or interrupt at the end → `failed`; else `no-change` (a `Status: DONE`
with nothing committed is `no-change`).

**Stored rows.** Every `sessions ingest` also recomputes the outcome of every stored row from
its `outcome_evidence` (the signals plus `end`), its `last_activity_at` and its kind, with
today's rules and clock (ingest line `outcomes=N` when any changed, `outcome_at` moves). So a
rule change or the clock reaches rows outside the ingest window too. A row with a null
`category` or `project` (an old hook row) gets them from its stored kind and cwd (`places=N`).
The scanner state version did not change: the hook state holds signals and `end`, not the outcome.

`outcome_evidence` holds signal names and counts (`edit`, `commit`, `push`,
`push_main`, `pr_create`, `pr_merge`, `merge_main`, `deploy`, `report_done`,
`report_partial`, `report_blocked`) plus `end` (`reply`, `user`, `tool`, `result`, `error`,
`interrupt`), and `commits` (below) when the session made any. Never command text or paths.

Limits: a heuristic. It is git- and English-centric: the tool must be the command word of a
shell segment (`echo git push` does not count); a push or merge done another way (an IDE,
a script, a merge on GitHub) is not seen; `git merge` counts as a merge into main only
after `git checkout main` / `git switch main` in the same command; `amem db migrate`
counts as a deploy even against a local DB. `active` and `abandoned` depend on the time of
the last ingest or ping.

### Commits

`outcome_evidence.commits`: `[{sha, message, repo, branch}]`, one per commit the session made
(session_commits.py). Read from the output line git prints for each commit
(`[<branch> <sha>] <subject>`, also `(root-commit)` and `detached HEAD`) of a shell call that
runs `git ... commit`: Claude Code `Bash` tool results, Codex `CommandExecution` items
(`aggregated_output`). `sha` is the short sha as printed; `message` the subject, redacted,
max 200 chars; `repo` the repo of the `git -C` path, else of the last `cd <path> &&` before the
commit (a `$VAR` set earlier in the same command is resolved, `$HOME` is `~`), else of the
command's cwd. A target that is a command substitution (`$(...)`, backticks) or a `$VAR` not
set in the command gives `repo: null`; a `mktemp` target (`W=$(mktemp -d); cd $W`) is a
throwaway repo and its commit is left out (no git-log fallback in the session cwd either).
The backfill drops stored entries whose repo is shell syntax (`$(mktemp`).
A quiet `git commit -q` prints nothing. Its subject is the `-m` value or, for `-F -`, the first
line of the heredoc (`<<'EOF' ... EOF`; heredoc bodies are cut out before the command is read,
so a line break or `;` in a message is not an operator). When a later `git log --oneline` line
in the output shows `<sha> <that subject>`, that is the commit, whatever the operators between.
Else, when the same command runs `&& git log --oneline` right after it, the first log line is
the commit (branch from `HEAD -> x` when decorated); else, when the commit is in an `&&`-only
chain or a `set -e` script that exited 0 and its subject is known, it is stored with
`sha: null` and `branch: null`. `git commit` inside quotes (a grep pattern) is not a commit.
Deduped by sha (null sha: by repo + message), at most 50 (the latest). A resumed session's
copied lines count for the earlier session.

**git-log fallback:** when the transcript gives no commit but the outcome signals count one
(`commit` > 0), `git -C <dir> log --no-merges --since=<start> --until=<end> --format=%h|%s`
runs (no shell, 10 s timeout) in each dir where a commit call ran (else the session cwd), on
HEAD only, with `--author=<the repo's user.email>` when it has one. Entries carry
`"source": "git-log"` and `branch: null`. A dir that is not a local git checkout is skipped.
Limits: two sessions of the same user on the same branch of the same checkout in overlapping
windows can pick up each other's commits (separate worktrees cannot); a commit on another
branch than the dir's current HEAD is missed. The vendored `commit` signal also counts a
`git commit` inside a quoted grep pattern split by `\|`, so a few rows run the fallback for
nothing.

`sessions ingest` writes them (the full read is the truth). A hook ping never reads them and
never drops them: when the stored evidence has `commits` and the ping's has none, the stored
list is kept. Not seen in the transcript (the fallback may still find them): a commit made
another way (an IDE, a script), one run in the background (`run_in_background`: the output is
in another file), one whose output was piped away. Cloud sessions (no local transcript, no
local repo) get no commits and no fallback.

### Summary

`summary` (+ `summary_updated_at`): 1 to 3 plain sentences: what was changed or decided, which
files or areas, whether it finished (session_summary.py). A cheap model (`claude -p --model
haiku`, no tools, no MCP, no session file, the subscription login; AMEM_SUMMARY_CLAUDE may point
at another executable) writes it from the title, outcome, `outcome_evidence` (signals and
commits) and a redacted digest of the transcript: its start (1.5k chars) and end (12k chars),
user and assistant text plus shortened tool calls. The prompt frames the record as quoted data
inside `<session_record>` tags and asks for third-person sentences that begin with "The
session". Nothing of the transcript is stored; the reply is redacted, cut to 3 sentences and
600 chars, and dropped when empty or junk (a refusal, JSON, a code fence, under 20 chars) or
when it does not start with "The session" ("This session" is repaired) or is a refusal or a
remark about the prompt even with that prefix ("The session record was not included in this
message, so I cannot describe..."; first person, "cannot describe/summarize", "record/
transcript was not provided", "this message"). A dropped reply gets one retry with a reminder
that the record is above; a second miss counts as failed and nothing is stored. A row whose
digest is under 200 chars or holds no assistant or tool line is skipped (`skipped-empty`,
ingest `summary_empty=N`) and never sent to the model.

A row gets one when it is finished (an outcome other than `active`, and no activity for 30
minutes or the SessionEnd hook was seen), has no summary or one older than its
`last_activity_at`, is not a subagent transcript without commits, is not a cloud session, and
its transcript is on this machine.

- **Automatic:** the last step of `amem sessions ingest`, for sessions active in the ingest
  window. Off unless `AMEM_SUMMARY_LIVE=1`. Time budget `AMEM_SUMMARY_BUDGET` seconds (default
  120): no new call starts with less than 10 s left, so the launchd run stays under its 300 s
  timeout; the rest waits for the next run (`summary_left=N`). `AMEM_SUMMARY_CONCURRENCY`
  calls at once (default 2). The ingest line always shows the step: `summary=off` (the flag is
  not 1) or `summaries=N`, also when N is 0. A failed call is counted (`summary_failed=N`); an
  error of the whole step is `summary_error=<type>`. Neither fails the ingest.
- **Backfill:** `amem sessions summarize --days 7 [--concurrency 4] [--budget S] [--dry-run]
  [--redo-bad] [--commits-only]`: sessions started in the window. It also re-reads each
  transcript's commits (with the git-log fallback) into `outcome_evidence.commits` (other keys
  untouched, stored commits never dropped). Prints `done`, `skipped-no-transcript`,
  `skipped-subagent`, `failed` (plus up-to-date, not-finished, left-for-next-run,
  commits-updated, bad-summaries), then a `commits:` line: committed sessions (signal
  `commit` > 0) with a non-empty commits list before / after, all and top-level. `--redo-bad`
  also summarizes the rows whose summary fails the same check (no "The session" prefix, or a
  refusal). `--commits-only`
  re-reads commits only (no model call). Needs `AMEM_SUMMARY_LIVE=1` unless `--dry-run` or
  `--commits-only`. A top-level transcript that moved to another project folder (the session
  changed directory) is found by its file name.

Cloud sessions (cloud routines and cloud chats, `cloud:` ids) have no local transcript, so no
summary and no commits.

## View `session_groups`

One row per `category` + `project`: `sessions`, token sums (`tokens_input`, ...,
`tokens_total`), `duration_seconds` and `active_seconds` (sum) with
`duration_median_seconds` / `active_median_seconds`, `last_activity_at`, and session counts
per value in `models`, `efforts`, `outcomes` (`{"claude-opus-5-5": 12}`). It is
`security_invoker`, so RLS on `sessions` applies; `anon` / `authenticated` have no grants.

## Report: `amem sessions tokens`

```
amem sessions tokens [--days 7] [--repo R] [--tool claude|codex] [--top 10]
                     [--by category|project|repo|tool|kind|model|effort|outcome ...] [--json]
```

A session counts when its last activity (else end, start or creation time) is in the
window. Output: the total (with wall-clock and active time), then the sections. Default:
`by category / project` (each category, then its projects indented), then `by model`.
`--by` (repeatable) picks the sections. Every group shows sessions, tokens and active time
(sum and median). `by model` splits each session's tokens by model; every other section
counts a session under its own value (`(none)` when null). The top sessions show project,
model, effort, outcome, active time and title. Subagent rows (kind `worker`) count once; a
parent's `(subagents ...)` note is shown, not added. `--json` prints every section
(`by_category` with `projects`, `by_project`, `by_repo`, `by_tool`, `by_kind`, `by_model`,
`by_effort`, `by_outcome`, `top`). Postgres backend only (exit 1 on REST), read only.

```
tokens: last 7 days: 42 sessions (40 measured), total 812.4M
  input 51.2k  output 2.1M  cache read 780.3M  cache write 30.0M

by repo
  agent-memory   30 sessions  total  640.1M  out    1.6M
...
top sessions
  2026-10-13 16:20  claude/parent    agent-memory  b1b2011c  total  234.3M  out  439.2k  44 turns  (subagents 61.0M)
```

## Cost of a window: `amem sessions cost`

```
amem sessions cost --since ISO [--until ISO] [--category eval|routine|chat|parent|worker|headless]
                   [--project P] [--json]
```

Totals of the sessions that **started** (`started_at`, else `created_at`) in `[since, until)`.
`--until` defaults to now; an ISO time without an offset is UTC. Each row counts once
(subagents are their own rows; `meta.subagent_tokens` is never added). Unmeasured columns
count as 0. Postgres backend only (exit 1 on REST), read only. Nothing matches: zeros, exit 0.

`--json` is a contract: agent-config's `improve` calls it (`--category eval|routine`) to show
the cost of each kept change. Keep these keys:

```json
{"sessions": 2,
 "tokens": {"input": 11, "output": 22, "cache_read": 303, "cache_creation": 44, "total": 380},
 "active_seconds": 61.5, "api_seconds": 30.0, "tool_seconds": 5.0,
 "by_project": {"evals": {"sessions": 2, "tokens": {"...": "same keys"}, "active_seconds": 61.5,
                          "api_seconds": 30.0, "tool_seconds": 5.0}}}
```

Without `--json`: a total line, a token / time line and one line per project.
