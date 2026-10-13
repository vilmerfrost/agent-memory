# Cloud: REST backend, `ingest git-ref`, `jev record`

Goal: every run lands in the remote DB, from local machines and from cloud
agents alike.

## Backends: `AMEM_BACKEND=postgres|rest`

Cloud sessions reach the internet only through an HTTP(S) proxy. Raw Postgres
(port 5432) is blocked there. HTTPS to the Supabase REST API (PostgREST) works.

| Setting | Backend |
|---|---|
| `AMEM_BACKEND=postgres` or `rest` | that backend (any other value is an error) |
| unset, `AMEM_SUPABASE_URL` set, `AMEM_DB_URL` unset | `rest` |
| unset, anything else | `postgres` |

The REST backend covers the `runs` table only: `log-run`, `runs --routine`,
`ingest routines-log` and `ingest git-ref`. `db migrate` is always Postgres
(it needs `AMEM_DB_URL`).

Env for REST:

- `AMEM_SUPABASE_URL`: the project URL, e.g. `https://<project-ref>.supabase.co`.
  Requests go to `$AMEM_SUPABASE_URL/rest/v1/runs`.
- `AMEM_SUPABASE_KEY` (optional): sent as the `apikey` header, and nothing else
  (no `Authorization`). Supabase's new `sb_secret_...` keys belong in `apikey`
  only; they are not JWTs. A secret key maps to `service_role` and bypasses RLS,
  which the `runs` table needs (anon/authenticated have no grants on it).
  In cloud sessions, leave it unset: a network secret adds `apikey` on the way
  out. The key is never printed or logged.

HTTP is stdlib `urllib` (no extra dependency). It honours `HTTPS_PROXY` /
`NO_PROXY`.

Transport rules, checked before any request (a failure is an `amem: ...`
error, exit 1; `doctor` prints `db=unreachable` and exits 0):

- `AMEM_SUPABASE_URL` must be an absolute `https://` URL with a host, e.g.
  `https://<project-ref>.supabase.co`. A schemeless value
  (`<project-ref>.supabase.co`) is refused, not guessed.
- Plain `http://` is allowed only to a local host (a test stub on
  `127.0.0.1`). A remote `http://` URL is refused, with or without
  `AMEM_SUPABASE_KEY`, so the `apikey` never travels unencrypted.
- Redirects are never followed. A 30x answer is an error
  (`HTTP 302 redirect refused`): the `apikey` header and the body never reach
  another host, and a redirected write is never counted as written.

### Requests

| Command | Request |
|---|---|
| `log-run` | `POST /rest/v1/runs?select=id`, `Prefer: return=representation`, body `[row]`; prints the returned id |
| `runs --routine R --limit N` | `GET /rest/v1/runs?select=*&routine=eq.R&order=started_at.desc.nullslast,created_at.desc&limit=N` |
| `ingest ...` pre-check | `GET /rest/v1/runs?select=source_key,meta&source_key=in.(k1,...)`, 50 keys per request |
| `ingest ...` insert | `POST /rest/v1/runs?on_conflict=source_key`, `Prefer: resolution=ignore-duplicates,return=minimal`, body = up to 500 rows |
| `ingest ...` failures refresh | `PATCH /rest/v1/runs?source_key=eq.K`, body `{"meta": ...}` |
| `doctor` | `GET /rest/v1/runs?select=id&limit=1` |

### Ingest counts on REST

`ingested N new, M already present` must stay correct, so ingest first GETs
which `source_key`s exist (batched `in.(...)` filter). Missing rows are POSTed
as an upsert that ignores duplicates. Present rows whose `meta.failures`
changed get one PATCH of `meta` (the stored meta with `failures` replaced),
the same rule as the Postgres backend.

The one inexact case: a parallel ingest inserts a row between the GET and the
POST. The server skips it (no duplicate), but this run counts it as new.

`runs` output on REST prints timestamps as PostgREST returns them
(`2026-10-12T09:00:14+00:00`); the Postgres backend prints Python's `str()`.

### `AMEM_REQUIRE_REMOTE=1`

Holds for both backends. On REST, writes exit 3 when `AMEM_SUPABASE_URL` is
empty, not an http(s) URL, or its host is local. `db migrate` always checks
`AMEM_DB_URL`.

"Local" is the same rule for a Postgres DSN host (and `hostaddr`, and `PGHOST`
when the DSN has no host) and for the REST URL host. Hosts are normalised
first: lower case, IPv6 brackets and zone id (`%lo0`) removed, one trailing
root dot removed. Then a host is local when it is:

- empty, or a Unix socket path (`/tmp`, `@abstract`);
- `localhost`, `*.localhost`, `localhost.localdomain`, `ip6-localhost`,
  `ip6-loopback` (any case, with or without a trailing dot);
- an IP address, in any spelling the resolver accepts, that is loopback
  (`127.0.0.0/8`, `::1`), unspecified (`0.0.0.0`, `::`) or in `0.0.0.0/8`.
  This includes long-form IPv6 (`0:0:0:0:0:0:0:1`), IPv4-mapped and
  IPv4-compatible IPv6 (`::ffff:127.0.0.1`, `::127.0.0.1`) and the legacy
  IPv4 forms `socket.inet_aton` parses: `0`, `127.1`, decimal `2130706433`,
  hex `0x7f000001`, octal `0177.0.0.1`.

Names are not resolved (no DNS lookup). A remote name that merely contains
`localhost` or `127` (`localhost.example.com`, `127.example.com`) is allowed.

### `amem doctor`

```
jev_mode=fixture
backend=rest            # or postgres (or invalid, for a bad AMEM_BACKEND)
db_host=<url host>      # REST: host of AMEM_SUPABASE_URL; Postgres: DSN host
db=reachable            # REST: the cheap GET above
require_remote=on
```

## Cloud digests: cloud routine sessions in the deviation scan

A cloud routine (claude.ai/code: "Nightly Trials", "Fresh Install") runs in a Linux VM.
Its transcript never reaches the Mac, so `sessions ingest` cannot read it. Instead the
routine sends a digest of its own session as its last step.

```
cloud VM: tools/amem_digest.py ──JSON row──▶ curl POST /rest/v1/cloud_digests
Mac:      amem sessions ingest ──▶ sessions (tool claude, kind routine, meta.cloud)
          amem deviations scan ──▶ stored digest + stored instruction texts ──▶ writer, Jev
```

### `tools/amem_digest.py`

One file, Python 3.10+, stdlib only, no `agent_memory` import. Vendor it into the routine's
repo (`tools/amem_digest.py` in nightly-trials; in agent-config for Fresh Install). The
package holds a byte-identical copy (`src/agent_memory/amem_digest.py`); `amem digest-self`
runs it with the same arguments. Tests keep the two files equal and check that its digest
equals `deviations.build_digest` (same lines, same redaction patterns, same 60k cap with
40% head + 60% tail). After an edit: `cp src/agent_memory/amem_digest.py tools/`, bump
`SCRIPT_VERSION`, and copy the file into the routine repos again.

| Argument | Use |
|---|---|
| `--routine NAME` | Required. Slugged: `Nightly Trials` → `nightly-trials`. |
| `--transcript PATH` | The transcript. Default: see below. |
| `--projects-dir DIR` | Where to look (default `~/.claude/projects`). |
| `--max-age-minutes N` | A newest transcript older than this is not this session (default 120, 0 = no limit). |
| `--prompt-from-transcript` | The session's first user message (the routine prompt) is the `prompt` instruction. |
| `--prompt PATH` | A file holding the routine prompt (wins over `--prompt-from-transcript`). |
| `--instruction NAME=PATH` | Another instruction file, e.g. `nightly-md=NIGHTLY.md`. Repeatable, at most 12. |
| `--repo NAME` | Repo name (default: the session's working folder name). |

Which transcript: `--transcript`, else the newest `*.jsonl` (modification time) directly in a
project folder of `~/.claude/projects`. Subagent transcripts (`<session>/subagents/...`) are
never picked. The running session appends to its own transcript while the step runs, so
the newest file is this session.

It never fails the routine. No transcript, none recent enough, or any other error: one
`amem_digest: ... Nothing to send.` line on stderr, nothing on stdout, exit 0. A missing
or `.env*` instruction file is left out with a stderr line. In routine mode it reads no
environment value and prints none; it sends nothing itself (hook mode does: see "Cloud chats").

Output: one JSON object, the `cloud_digests` row.

| Key | Value |
|---|---|
| `source_key` | `cloud:<session id>` |
| `routine` | the slug |
| `session_id` | the transcript's `sessionId` (else the file name) |
| `started_at`, `ended_at` | first and last event time, ISO 8601 UTC |
| `digest`, `digest_sha` | the redacted, capped digest and its sha256 |
| `instructions` | `[{name, sha256, text}]`: text redacted, capped at 100k chars; sha256 of the text as read |
| `skills` | `Skill` calls and slash commands, no built-ins |
| `meta` | `{cloud: true, repo, script_version, transcript_bytes, tool_calls, prompts, tokens}`; `tokens` (script version 2+) = `{input, output, cache_read, cache_creation, models}`, the session's token totals (docs/sessions-live.md). Script version 3+ adds `title` (when named), `profile` (model, effort, duration) and `outcome` (`{signals, end}`: signal names and counts only) |

### The routine's last step

Put this at the end of the routine prompt. It needs no key in the session: the cloud
network secret adds the `apikey` header for the Supabase host, as for the `runs` POST.

````text
## Last step: session digest (always run it, also after a failure; never let it block your reply)

Run exactly this one Bash command from the repo root, then finish your reply. It prints
only an HTTP status code (201 = sent, 000 = no network, "skipped" = nothing to send).
Do not retry it and do not debug it.

```bash
python3 tools/amem_digest.py --routine nightly-trials --prompt-from-transcript --instruction nightly-md=NIGHTLY.md > /tmp/amem_digest.json 2>/dev/null; if [ -s /tmp/amem_digest.json ]; then curl -s -o /dev/null -w '%{http_code}\n' --max-time 20 -X POST "$AMEM_SUPABASE_URL/rest/v1/cloud_digests?on_conflict=source_key" -H 'Content-Type: application/json' -H 'Prefer: resolution=ignore-duplicates,return=minimal' --data @/tmp/amem_digest.json || echo 000; else echo skipped; fi; true
```
````

For Fresh Install: `--routine fresh-install`, no `--instruction` (its only instruction is
the prompt), and the path to the vendored script in agent-config.

- `curl -s` without `-S`: an error message could name the host. Only the code is printed.
- `--max-time 20` and `; true`: the step always ends, with exit 0.
- `resolution=ignore-duplicates` (+ `on_conflict=source_key`, the primary key): a second
  POST of the same session is ignored, not an error and not a second row.

**The routine prompt as an instruction.** `--prompt-from-transcript` is the simplest
reliable way: the routine prompt is the session's first user message, so the script takes
it from the transcript. Nothing to write first, and the text is exact (a prompt the agent
re-types into a file can drift, and then its sha changes every run). If a routine ever keeps
its prompt in a file in its repo, pass `--prompt <file>` instead.

### Table, ingest and scan

`cloud_digests` (migration `20261017040000_cloud_digests.sql`): the row above plus
`ingested_at` and `created_at`. `source_key` is the primary key. RLS on, no policies,
`anon` / `authenticated` grants revoked (the POST uses the secret key, which bypasses RLS).

- `amem sessions ingest` (Postgres backend) also reads every `cloud_digests` row with
  `ingested_at` null: one `sessions` row each (id = `source_key`, `tool` claude, `kind`
  routine, `project_dir` cloud, `meta.cloud` true, `meta.routine`, `repo`), then sets
  `ingested_at`, in one transaction. `meta.tokens` of the digest fills the token columns
  (`turns` = `meta.prompts`). Idempotent; a re-ingest keeps `scanned_at`. The summary
  line ends with `cloud=N` when it ingested any (`cloud_bad=N`: rows it could not use).
  `--tool codex` skips them.
- `sessions.instructions`: `routine:<routine>` for the prompt, `cloud-file:<name>` for each
  file (`cloud-file:nightly-md`).
- `amem deviations scan` reads the stored digest and instruction texts for these sessions,
  never a local transcript or a local file (the Mac's CLAUDE.md was not in force in the VM).
  The rule catalog is built from the stored texts and `instruction_sha` is the stored sha256,
  so snapping, recurrence, the report and `reref` work as for local sessions. The candidates
  file has `target_paths: []` for these sources: the edit step has no local file to edit.

## Cloud chats

Interactive claude.ai/code sessions (not only routines) send their digest too: every new
cloud chat gets a `sessions` row with its tokens, model, title and outcome. Only the redacted
digest leaves the VM.

```
claude.ai/code environment setup script (deploy/cloud/setup.sh, runs once per new session)
   │ GET /rest/v1/amem_assets?name=eq.amem_digest.py   ──▶ ~/.claude/amem/amem_digest.py
   │ merge Stop + SessionEnd hooks into ~/.claude/settings.json
   ▼
cloud session: Stop / SessionEnd ──▶ python3 ~/.claude/amem/amem_digest.py --hook
   │ POST /rest/v1/cloud_digests?on_conflict=source_key   (merge-duplicates, kind chat, ingested_at null)
   ▼
Mac: amem sessions ingest ──▶ sessions (tool claude, kind chat, meta.cloud) ──▶ deviations scan, tokens
```

### What to paste where

1. The parent publishes the script once per change, on the hosted DB:
   `uv run amem cloud publish-script` (Postgres backend; upserts `tools/amem_digest.py`
   into `amem_assets`; prints `(new)`, `(updated)` or `(unchanged)`).
2. Vilmer pastes `deploy/cloud/setup.sh` into the setup script field of the claude.ai/code
   environment that has `AMEM_SUPABASE_URL` and the Supabase network secret. It runs as root
   before Claude Code starts, only when a new session starts.

`setup.sh` never fails a session: `set +e`, every path ends with `exit 0` and one status line:

| Line | Meaning |
|---|---|
| `amem: cloud digests on (Stop + SessionEnd hooks)` | script fetched (or an earlier copy kept), hooks merged |
| `amem: AMEM_SUPABASE_URL not set, cloud digests off` | nothing done |
| `amem: could not fetch amem_digest.py, cloud digests off` | no answer, not JSON, or the body does not compile (`python3 -m py_compile`) and no earlier copy exists |
| `amem: settings.json not changed (...)` | settings.json is not a JSON object: left alone |

The merge keeps every other key and hook in `~/.claude/settings.json`. A second run adds
nothing (the hook command is matched exactly). The hook entries:
`{"type": "command", "command": "python3 \"$HOME/.claude/amem/amem_digest.py\" --hook", "timeout": 10}`
under `Stop` and `SessionEnd`.

### Hook mode: `amem_digest.py --hook` (script version 3)

- Reads the hook JSON on stdin (`session_id`, `transcript_path`, `cwd`,
  `hook_event_name`). Other events and subagent payloads (`agent_id`) are ignored.
- Builds the same redacted, capped digest and token totals as routine mode, plus
  `kind: chat`, no `routine`, `ingested_at: null`, `meta.hook` (the event), and as
  instructions the `CLAUDE.md` (`claude-md`) and `AGENTS.md` (`agents-md`) of the hook's
  cwd (else of the git root above it): redacted, capped at 100k chars each.
- POSTs it itself with stdlib `urllib` (no curl), which honours `HTTPS_PROXY` / `NO_PROXY`:
  `$AMEM_SUPABASE_URL/rest/v1/cloud_digests?on_conflict=source_key`,
  `Prefer: resolution=merge-duplicates,return=minimal`. Redirects are refused. Only
  `https://`, or `http://` to a local host (tests). No key is sent: the network secret adds it.
  CA bundles named by `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS` are
  trusted too (a TLS-inspecting proxy's CA is often only there).
- Debounce: a Stop less than 60 s after the last successful POST of the same session is
  skipped (state: `~/.claude/amem/hook-state.json`). SessionEnd is never skipped. A failed
  POST does not count, so the next Stop tries again.
- Never blocks or fails the session: hard timeout 8 s (HTTP timeout 5 s), always exit 0,
  nothing on stdout or stderr. One log line per run in `~/.claude/amem/hook.log`
  (`amem-digest <time> <event> <session id prefix> <status> <seconds>`; status `posted 201`,
  `debounced`, `failed 500`, `failed 000` = no answer, `no AMEM_SUPABASE_URL`, ...). The log
  never holds the URL or any text.

Limits: the transcript can lag the current turn, and a chat's last Stop may fall in the
60 s debounce. SessionEnd (when Claude Code sends it) or the next Stop catches up. A session
that just goes idle keeps the digest of its last successful POST.

### Chat or routine

`cloud_digests.kind` is `chat` (hook) or `routine` (a routine's own last step); `routine` is
null for a chat (migration `20261018030000_cloud_chats.sql`). One cloud session can send
both. The routine's step is authoritative for kind, routine name and instructions, in either
order (two triggers):

- hook after routine: the merge keeps kind, routine and instructions; digest, times, skills
  and meta (tokens) come from the hook, which read more of the session.
- routine after hook: the routine's ignore-duplicates POST folds the chat row into a routine
  row (the newer digest of the two stays) and resets `ingested_at`.

`amem sessions ingest` reads every row with `ingested_at` null, so each hook POST refreshes
the `sessions` row (tokens, last activity, title, model, outcome; `scanned_at` stays).
Kind `chat` or `routine` comes from the row; a session already stored as a routine is never
turned into a chat. Chats get `tool` claude, `meta.cloud` true, `project_dir` cloud and the
repo from the session's cwd. `amem deviations scan` treats a cloud chat like a chat: it
reads the stored digest and the stored `cloud-file:claude-md` / `cloud-file:agents-md`
texts (never the Mac's files).

### Verify

1. Start a new cloud session in the environment. The setup log shows the status line.
2. In the session, run `/hooks`: Stop and SessionEnd list the `amem_digest.py --hook` command.
3. Send a prompt, wait for the reply, then on the Mac: `uv run amem sessions ingest` and
   `uv run amem sessions tokens --tool claude` lists the session under `chat` with project
   = the repo (or `uv run amem sessions tokens --by category`).
4. In the cloud session, `tail ~/.claude/amem/hook.log` shows `posted 201`.

### Disable

- Remove the lines from the environment's setup script. New sessions then get no hooks.
- In a running session: delete the two entries from `~/.claude/settings.json`, or
  `unset AMEM_SUPABASE_URL` before Claude Code starts (the hook then logs
  `no AMEM_SUPABASE_URL` and sends nothing).
- Stored rows stay in `cloud_digests` and `sessions`.

### Privacy

Same rules as routine digests: the digest is redacted (credential shapes, URL passwords,
private keys) before the 60k cap, instruction texts are redacted and capped, no environment
value is read except the URL, HOME, proxy and CA-bundle variables, no `.env*` file is read,
and the log holds labels only. The full transcript never leaves the VM.

### Table `amem_assets`

Migration `20261018020000_amem_assets.sql`: `name` (pk), `body`, `sha256`, `version`,
`updated_at`. RLS on, no policies, `anon` / `authenticated` revoked; the cloud fetch uses the
secret key through the network secret.

## `amem ingest git-ref`

Cloud routines publish their `routines.log` to branch `claude/routine-reports`,
path `reports/routines.log`, of GitHub repo `vilmerfrost/agent-config`.

```sh
amem ingest git-ref                         # gh api GET of the contents API (base64)
amem ingest git-ref --git-dir ~/clone --ref origin/claude/routine-reports   # git show, no fetch
```

- Reads only: `gh api repos/<repo>/contents/<path>?ref=<ref>` (GET), or
  `git show <ref>:<path>` in a clone you name. Never fetches or writes.
  Files over 1 MB fall back to the raw media type.
- Same parser and dedupe keys as `ingest routines-log` (see "Dedupe keys"
  below). Every row gets `source=cloud` and `meta.log_source="routine-reports"`.
- Branch or file missing: prints `no routines.log published at ... yet` and
  exits 0.
- Options: `--repo`, `--ref`, `--path`, `--git-dir`.

## Dedupe keys

One cloud run reaches `runs` by two paths:

- `amem ingest git-ref` reads the line as published on `claude/routine-reports`.
- `routine-sync` (agent-config) copies that line into the local
  `~/.claude/parent/routines.log`. It rewrites a trailing `report=<dir>/<file>`
  to `report=~/.claude/parent/reports/cloud/<file>`. Then
  `amem ingest routines-log` reads the copy.

Both paths must give the same key. So the key ignores the part routine-sync
changes:

```
canonical line = raw line, each `report=<path>` token cut to `report=<basename>`
source_key     = sha256(routine + "\n" + canonical line [+ "\n#<n>"])
```

Example: both lines below have the canonical line
`2026-10-11T08:26:30+0000 fresh-machine PASS pass=4 fail=0 2s report=fresh-machine-2026-10-11.md`.

```
published: ... 2s report=/tmp/tmp.HIatBDBcYC/.claude/parent/reports/fresh-machine-2026-10-11.md
local:     ... 2s report=~/.claude/parent/reports/cloud/fresh-machine-2026-10-11.md
```

Rules:

- The key keeps the timestamp (as written), the routine, the verdict word and
  every other field. Two lines that differ in any of these are two runs.
- Whichever ingest sees a run first writes the row. The other ingest counts it
  as `already present`. This holds on both backends.
- `#<n>` (n = 2, 3, ...) marks a byte-identical raw line seen again in the same
  file. Such a repeat is a separate event and gets its own row. n counts raw
  lines, so routine-sync's copy (different raw text) gets n = 1, the key of its
  original.
- A routine-sync copy in the same file as its original (the local log holds
  both) is dropped by the parser: same run. Detail lines that follow it attach
  to the original.

### `amem db dedupe-runs [--apply]`

Rows stored before canonical keys have old keys (sha256 of the raw line), so a
cloud run may be stored twice. This command fixes stored data. Postgres only
(it refuses the REST backend). It honors `AMEM_REQUIRE_REMOTE=1`, also for the
dry run.

- It rebuilds each row's line from its columns (ts offset tried from -12:00 to
  +14:00) and checks the stored key, old or canonical. A row whose key matches
  no rebuilt line is left alone and listed as unmatched.
- Rows with the same canonical key are one run. The oldest row (`created_at`)
  stays. References on the others move to it: `scores` (same question: the
  newer score wins), `labels` (the newer label wins), `lesson_uses`, any other
  foreign key to `runs.id`, and `lessons.source_run_ids`. Then the duplicates
  are deleted.
- Every kept row gets its canonical `source_key`, so later ingests dedupe.
- Without `--apply` it prints the plan and changes nothing. With `--apply` it
  does everything in one transaction, under a lock on `runs`. A second run
  finds nothing to do.

```sh
amem db dedupe-runs           # dry run: groups, references, keys to rewrite
amem db dedupe-runs --apply
```

## `amem jev record`

```sh
AMEM_JEV_LIVE=1 TYPESAFE_API_KEY=... \
  amem jev record --questions questions/x.toml --state @state.txt --out tests/fixtures/jev/
```

- Runs the questions live once and writes `<out>/<key>.json`, where
  `<key> = jev.fixture_key(state, questions)` (the key fixture mode reads).
  The file holds `{"_note", "model", "usage", "answers"}`.
- Live only when `TYPESAFE_API_KEY` is set and `AMEM_JEV_LIVE=1`. Otherwise it
  exits 2 and calls nothing.
- `--state TEXT` or `--state @path`. A file is read exactly; its trailing
  newline is part of the key.
- The answers are validated like fixture mode before the file is written.
  An existing fixture is kept unless you pass `--force`.

### Questions file (TOML, same schema as `questions/*.toml`)

```toml
threshold = 0.7          # scoring keys; jev record ignores them
pass_question = "verdict"
pass_options = ["ok"]

[[questions]]            # ordered; the order is part of the fixture key
name = "verdict"
kind = "choice"          # choice | score | noul
prompt = "Did the routine do its job?"
[questions.options]      # choice: label -> description
ok = "All work done"
needs_action = "Something needs a human"

[[questions]]
name = "severity"
kind = "score"
prompt = "How bad is it?"
levels = ["Fine", "Minor", "Major"]   # score: level i = score i

[[questions]]
name = "flag"
kind = "noul"
prompt = "Does a human need to look?"
[questions.criteria]     # noul, optional: true / false
true = "Yes"
false = "No"
```

Mapping: `jev.choice(name, prompt, options)`, `jev.score(name, prompt, levels)`,
`jev.noul(name, prompt, criteria)`.
