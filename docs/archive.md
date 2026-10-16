# Transcript archive: encrypted, local, 180 days

Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30). The RSI loop
needs them longer. `amem archive` keeps every distinct version of every Claude Code and
Codex transcript on this Mac, encrypted, for 180 days. Nothing is uploaded. Nothing at the
source is changed or deleted.

```
~/.claude/projects/**  ─┐                 objects/<aa>/<sha256>.bin   zlib → AES-256-GCM
~/.codex/{sessions,     ├─ archive run ──▶ manifest.jsonl              1 encrypted line per record, hash chain
  archived_sessions}/** ┘   (daily)        index.bin                   encrypted: path → size, mtime, sha
                                              ▲
                       key: macOS Keychain ───┘  (service amem-archive, account k1)
```

Code: `src/agent_memory/archive.py` (logic), `src/agent_memory/cli_archive.py` (CLI).

## Commands

| Command | Does |
|---|---|
| `amem archive init [--reuse-key]` | Creates the key (refuses to overwrite one) and the archive dir (mode 700). `--reuse-key` starts a new archive with a key that already exists. |
| `amem archive run [--dry-run] [--projects-dir D] [--codex-home D]` | Archives every new version. Prints counts only: files seen, new versions, unchanged, new objects, bytes. Exit 1 when a file could not be read. |
| `amem archive run --dry-run` | Writes nothing and needs no key. Counts what a run would archive: against the index when the key opens it, else every file counts as new (stored objects still dedupe by name). |
| `amem archive verify [--sample N]` | Decrypts every (or N random) referenced object and checks its sha256. Checks the manifest chain and that the index agrees. Exit 1 on any problem. Names counts, line numbers and object ids only. |
| `amem archive restore --session ID --to DIR [--version latest\|N \| --as-of ISO]` | Writes the session's files below DIR at their path relative to the source root, mode 600, with the recorded mtime. Never overwrites. Refuses a DIR inside a source root or the archive dir. |
| `amem archive prune [--days 180] [--dry-run]` | Retention (below). Writes a compacted manifest atomically, records the prune, then deletes unreferenced objects. |
| `amem archive status` | Counts, total size, last run, and `key=yes\|no`. |
| `amem archive key-export [--allow-non-tty]` | Prints the recovery string. Refuses when stdout is not a terminal, so the key does not land in a log or an agent transcript. |
| `amem archive key-import` | Reads a recovery string from stdin (hidden input on a terminal), checks it, stores the key. Refuses a key that does not open the archive or would overwrite another key. Creates the archive dir when there is none. |

Exit codes: 0 ok, 1 a problem, 2 usage.

## Sources and identity

| Source | Files | `session_id` | `role` |
|---|---|---|---|
| claude | `<projects>/<project>/*.jsonl` | the file stem | `main` |
| claude | `<projects>/<project>/<session>/subagents/agent-<id>.jsonl` | `<session>/agent-<id>` (the `sessions` table rule) | `subagent` |
| claude | the `agent-<id>.meta.json` next to it | `<session>/agent-<id>` | `subagent-meta` |
| codex | `<codex home>/sessions/**/rollout-*.jsonl`, `<codex home>/archived_sessions/**/rollout-*.jsonl` | `codex:<thread id>`: the first line's `session_meta` id, else the id in the file name, else the file name | `codex` |

Roots: `--projects-dir`, else `AMEM_PROJECTS_DIR`, else `<AMEM_CLAUDE_HOME or ~/.claude>/projects`.
`--codex-home`, else `AMEM_CODEX_HOME`, else `.codex` next to the Claude home. Symlinks are
not followed. Files over 1 GiB are skipped (counted).

Each version record holds: `source`, `path` (relative to its root), `role`, `session_id`,
`sha256`, `size`, `mtime_ns`, `birthtime` (`st_birthtime`, when the OS has it), `first_seen`
(the archive clock), `version` (1, 2, … per path) and, from version 2 on,
`prev_version_last_seen`. `last_seen` of the current version lives in the index: it changes
every run, and the manifest is append-only.

**Versions.** A run skips a file whose size and mtime match the index. When either changed,
it re-hashes the file. A new sha256 is a new version of that path. Old versions stay until
prune removes them.

**Resumed sessions.** A resumed transcript is a new file (a copy of the earlier lines plus new
ones). It is its own session with its own versions. A byte-identical copy adds a version
record but no new object (dedupe by plaintext sha256).

**Deleted at the source.** The archive keeps it. `status` counts it under `gone_at_source`.

## Storage layout

The archive dir is `AMEM_ARCHIVE_DIR`, default `~/Library/Application Support/amem-archive`.
The dir and every subdir are mode 700. Every file is mode 600.

| File | Holds |
|---|---|
| `config.json` | Format version, the current key id, the creation time. The only plaintext file. No paths, no ids. |
| `manifest.jsonl` | Append-only records (`init`, `version`, `run`, `prune`). Each line is base64 of one sealed blob. Each plaintext line has `seq` and `prev` = sha256 of the previous plaintext line (the first line's `prev` is 64 zeros). |
| `index.bin` | Sealed JSON: `manifest_lines`, `manifest_head` (sha256 of the last line), `last_run`, and per path: sha, size, mtime_ns, version, session id, last_seen. |
| `objects/<aa>/<sha256>.bin` | One sealed blob per distinct content. The name is the sha256 of the plaintext. |
| `lock` | `flock`: one writer at a time; verify, status and restore take a shared lock. |

Sealed blob: `magic (4) | format (1) | key id length (1) | key id | nonce (12) | ciphertext + tag`.
The plaintext is zlib-compressed, then encrypted with AES-256-GCM and a fresh random 96-bit
nonce (`cryptography`'s `AESGCM`). The AAD binds `amem-archive`, the blob kind (object, index,
manifest), the format version, the key id and the object id. So a blob moved to another name,
another kind or another key id fails to open.

**What verify catches.** A changed byte in an object, the index or a manifest line (the GCM
tag fails). An object under another object's name (the AAD, and the sha256 check). A removed,
added or reordered manifest line (`seq` / `prev`). A cut tail of the manifest (the index
records the line count and head). The wrong key (nothing opens). `run` does a cheap version
of the same check (the last line and the index) and refuses to write when it fails.

**Crash safety.** Objects and the index are written to a temp file, fsynced and renamed. New
manifest lines are appended in one write and fsynced before the index is written. If a crash
leaves the index behind the manifest, the next run rebuilds it from the manifest (an
authenticated prefix check first). A crash between a compaction and its index write is
recognised by the `prune` record's `compacted_from`. Until that next run, `verify` reports an
index problem.

## Keys

- One random 32-byte key per key id (`k1`). The key id is in every blob header, so a later
  rotation can add `k2`, seal new data with it and still open old blobs.
- **Production backend: macOS Keychain.** A generic password, service `amem-archive`, account
  `<key id>`, value = the key in base64url. Through `/usr/bin/security`. The key never goes into
  argv, because other processes can read argv: `init` writes it with `security -i`, which reads
  the `add-generic-password` command from stdin (checked: piped stdin works, and `security -i`
  exits with the last command's status). Reads use `find-generic-password -w`; the key comes back
  on stdout, is captured, and is never printed.
- **Test backend: a key file.** `AMEM_ARCHIVE_KEY_FILE`: JSON `{"format": 1, "keys": {"k1": "…"}}`,
  mode 600 (a file readable by others is refused). It must not be inside the archive dir.
  `AMEM_ARCHIVE_SECURITY` overrides the `security` path (tests use a fake). Tests never touch
  the real Keychain: a tripwire replaces `security` in every archive test.
- **Recovery string:** `amem-archive-key:1:<key id>:<base64url key>:<8 hex checksum>`. The
  checksum is the first 8 hex of sha256 over the rest, so a typo is caught on import.

## Retention (`prune`)

Retention goes by the source's last modification. A source path whose newest mtime (over its
versions and the index) is older than `--days` (default 180) loses its whole history. Run and
prune records older than the cutoff go too; the `init` record stays. Then:

1. A compacted manifest (re-chained from `seq` 0, ending in a `prune` record with
   `compacted_from`) replaces the old one atomically.
2. The index is updated. A pruned path that is still at the source stays in the index as
   `pruned`, so the next run does not archive the same old file again.
3. Unreferenced objects (also strays from a crash) are deleted.

Nothing to remove: only a `prune` record is appended, nothing is rewritten.

## Refused locations

The archive dir must not overlap the experiment sandbox root (`RSI_SANDBOX_DIR`, else
`~/sandbox`), must not overlap a source root (the ones in use and the defaults
`~/.claude/projects`, `~/.codex`), and must not be inside a git repository. `restore` refuses a
target inside a source root or the archive dir. `archive.forbidden_mount_roots()` returns the
resolved archive dir (configured and default) and the key file, so the sandbox runner can
refuse to mount them.

## Privacy

- `archive.py` and `cli_archive.py` import no network library (`socket`, `ssl`, `urllib`,
  `http`, `psycopg`, `requests`, …), and neither imports `config` (which does). A test checks
  the source and the modules really loaded.
- Output and errors carry counts, ids, line numbers and relative paths only. OS errors print
  their `strerror`, never the file name.
- Every file in the archive dir except `config.json` is ciphertext. A test writes marker
  strings into transcript text, folder names and session ids, and checks that no byte sequence
  of them appears anywhere in the archive dir.
- Known leak, accepted: object names are the sha256 of the plaintext. Someone with the archive
  dir and a copy of a transcript can confirm that the archive holds it. File sizes and counts
  are visible too.

## Limits

- Every changed transcript is stored as a full new object (no deltas). A long session that
  changes every day adds one compressed copy per run.
- `birthtime` is recorded but not restored (Python cannot set it on macOS).
- `run` checks only the last manifest line and the index. A reordered middle line is found by
  `verify`, not by `run`.
- `verify` holds a shared lock. A `run` that waits more than 10 s for the lock exits 1.
- Not archived: `tool-results/` and other files next to a transcript, Codex `session_index.jsonl`.
- The Keychain must be unlocked (the login keychain is, while Vilmer is logged in).
