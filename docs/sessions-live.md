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
| `Stop` | Token columns, `models`, `turns`, `last_activity_at`, `ended_at`, `meta.subagent_tokens`. |
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

## Disable

- Fast: `export AMEM_HOOK_OFF=1` in the environment Claude Code starts with (the script
  then reads stdin and exits).
- For good: remove the two (three) entries from `~/.claude/settings.json`.
- Clean up local files: `~/.claude/parent/amem-session-{state.json,spool.jsonl,hook.log,lock}`
  and `~/.claude/parent/amem-hook-tmp/`. Rows already in `sessions` stay.

## Report: `amem sessions tokens`

```
amem sessions tokens [--days 7] [--repo R] [--tool claude|codex] [--top 10] [--json]
```

A session counts when its last activity (else end, start or creation time) is in the
window. Output: the total, then sums by repo, tool, kind and model, then the top sessions.
Subagent rows (kind `worker`) count once; a parent's `(subagents ...)` note is shown, not
added. `--json` prints the same as a document (`totals`, `by_repo`, `by_tool`, `by_kind`,
`by_model`, `top`). Postgres backend only (exit 1 on REST), read only.

```
tokens: last 7 days: 42 sessions (40 measured), total 812.4M
  input 51.2k  output 2.1M  cache read 780.3M  cache write 30.0M

by repo
  agent-memory   30 sessions  total  640.1M  out    1.6M
...
top sessions
  2026-10-13 16:20  claude/parent    agent-memory  b1b2011c  total  234.3M  out  439.2k  44 turns  (subagents 61.0M)
```
