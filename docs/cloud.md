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
- Same parser and dedupe keys as `ingest routines-log`. Every row gets
  `source=cloud` and `meta.log_source="routine-reports"`.
- Branch or file missing: prints `no routines.log published at ... yet` and
  exits 0.
- Options: `--repo`, `--ref`, `--path`, `--git-dir`.

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
