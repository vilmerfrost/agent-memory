# Instruction sources: authority and version

Plan section 5 ("Instruction sources"), Stage C. Code: `src/agent_memory/instruction_sources.py`.

A deviation is only as good as the instruction it is graded against. Before this stage, the scan read
today's files by name. A rule edited after the session made the deviation look wrong, and nothing said so.
Now every session records which instruction files were in force, how sure we are, and which version.

## What resolves

`instruction_sources.candidates(row)` names every source in force, in reading order.

| Session | Sources |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` (`claude-md:global`); the agent definition (`agent:<type>`, plugin agents through the plugin); the routine prompt (`routine:<name>`); PARENT.md for parents (`parent-md`); every skill (`skill:<name>`; user skills, then project skills under `<repo>/.claude/skills`); the repo's `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` in the worktree and the main checkout (`claude-md:<repo>`); the repo's `AGENTS.md` (`agents-md:<repo>`); the same memory files in every ancestor directory up to, not including, `/` (`claude-md:~/<dir>`); every `@import` target |
| Codex | `~/.codex/AGENTS.override.md`, else `AGENTS.md` (`agents-md:global`); skills (shared, Codex home, `.system`); the repo's `AGENTS.override.md`, else `AGENTS.md`, and the same between the git root and the cwd |
| Cloud routine | the texts the routine sent (`cloud_digests.instructions`) |

Plugin skills (`<plugin>:<skill>`) resolve through `~/.claude/plugins/installed_plugins.json`: the install
record of that plugin (user scope, or a project/local install for this repo), then
`<installPath>/skills/<skill>/SKILL.md` (or `commands/<skill>.md`). The entry keeps the plugin id, version,
`gitCommitSha`, `installedAt` and `lastUpdated`. Example: `magicpath:magicpath` resolves to
`~/.claude/plugins/cache/magicpath/magicpath/0.9.0/skills/magicpath/SKILL.md`.

`@import` follows Claude Code: `@path` outside code blocks and code spans, relative to the importing file,
`~/` for the home. At most five hops deep; a cycle stops; a missing target is skipped (Claude Code skips it
too). Only text files (`.md`, `.markdown`, `.txt`) under the allowed roots are read: the Claude home, the
Codex home, agent-config and the session's repo dirs. An env file or a file outside the roots is recorded
as unresolved and never read. An imported file keeps one name however it is found: the repo's AGENTS.md is
`agents-md:<repo>`, PARENT.md is `parent-md`, anything else `import:~/<path>`.

A name that resolves to nothing stays as an explicit unresolved entry with a reason:

- `anthropic-skills:*`: built into the Claude desktop app, no file on disk.
- A plugin that is not installed now, or a skill that is not in the installed plugin.
- A plain skill with no SKILL.md (bundled with Claude Code, or removed), a built-in agent type.

## Authority

Each file gets one authority. The stronger one wins: observed, reconstructed, declared, unresolved.

| Authority | Meaning | Version |
|---|---|---|
| `observed` | The transcript holds the text that was loaded. Claude Code: the `instructions` attachment (path and content of every memory file, imports included), a `Contents of <path>` system-reminder block in older transcripts, the skill body after `Base directory for this skill:` and in `invoked_skills` attachments. Codex: a resolved file whose text appears in an injected `# AGENTS.md instructions` block or `<skill>` tag. Cloud: the sent texts. | `{kind: content, id: <sha256>, basis: transcript \| cloud}` |
| `reconstructed` | The text at a verified version: the file is unchanged since the session started (its mtime is before the start), or git history gives the last commit of the file at or before the start. | `{kind: git, id: <commit>, blob, repo, basis: mtime \| git-history}`, or `{kind: content, id, basis: mtime}` for a file not in git or with uncommitted changes |
| `declared` | Named by the session's metadata (skills list, agent type, routine, cwd), but no version can be verified: the session start is unknown. | none |
| `unresolved` | No text and no version. `reason` says why. | none |

When the transcript lists the loaded memory files (the `instructions` attachment), a memory file that is not
in the list was not in force and is left out (for example a worktree's tracked copy of CLAUDE.md, or an
AGENTS.md no CLAUDE.md imports). A file the list shows but resolution did not predict is added as observed.
A memory file that changed during the session keeps its first version and gets `changed_during_session`.

A current file hash is never recorded as a historical version. A file not in git that changed after the
session started is `unresolved` ("changed after the session started and not in git"), with no version and
no sha. A plugin updated after the session started says so in its reason.

Limits:

- mtime is trusted. A copy that keeps an old mtime (`cp -p`, `rsync -t`) would look unchanged.
- git history gives the committed text. Uncommitted edits in force at the session start are not visible.
- Built-in and bundled skills are observed only while the transcript exists; their text is not stored.

## Storage

`sessions.instruction_sources` (jsonb array) holds one entry per file:
`{name, kind, path, authority, reason?, version?, sha256?, plugin?, imported_by?, changed_during_session?,
stored?}`. `path` is `~/`-relative under the home, else absolute, or a label such as `anthropic-skills:docs`.
`sessions.instructions` keeps the source names for older readers.

`instruction_texts` keeps the text of observed and reconstructed versions recoverable:

- Content-addressed by the sha256 of the raw text; insert-only (`on conflict do nothing`).
- Stored redacted (the shared `redact`), `redacted` says whether anything changed.
- At most 100 kB of raw text; a longer text keeps its version and sha but no stored text.
- Only files under the allowed roots, or the session's own repo and ancestor memory files. Never env files.
- RLS on: amem_app reads and inserts; Night Desk's owner reads; no other role gets anything.

## When it is captured

New sessions: `amem sessions ingest` (every 15 minutes, inside amem-daily too) captures the entries of every
transcript it reads. A replay merges with the stored entries: per file, a stronger authority replaces a
weaker one, a tie keeps the stored one (the earliest capture is the closest to the session start), and no
entry disappears. A replay with nothing new writes nothing.

Historical sessions: `amem sessions instruction-sources` backfills rows whose `instruction_sources` is null,
newest first. It reads today's resolution, the transcript when it still exists, and git history at each
session's start.

```bash
amem sessions instruction-sources                 # dry run: counts per authority and unresolved reason
amem sessions instruction-sources --apply --limit 200
amem sessions instruction-sources --json          # the counts as one object
```

- Dry run by default: nothing is written.
- Bounded: `--limit` rows per run (default 200); `--days N` restricts to recent sessions.
- Resumable and idempotent: `--apply` writes only rows still null, so a rerun continues where the last one
  stopped and a run with nothing left writes nothing. `remaining` counts the rows still to do.
- Historical uncertainty stays explicit: what cannot be recovered is `unresolved` with its reason.

## The deviation scan

`deviations.load_versioned` grades each session against the version in force when one is recorded (texts
from `instruction_texts`, git blobs, today's file when its sha matches, or the transcript itself). A file
without a recoverable version is read from today's file, and the source is graded as `unverified`.
`deviations.instruction_authority` records it per deviation: `observed`, `reconstructed` or `unverified`
(null for deviations scanned before this column). `instruction_sha` is the recorded version's sha for a
single-file source. See docs/deviations.md, "Version in force".
