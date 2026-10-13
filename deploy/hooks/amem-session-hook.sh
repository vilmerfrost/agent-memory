#!/bin/bash
# Claude Code hook (SessionStart, Stop, optional SessionEnd): live `sessions` rows in agent-memory.
# See docs/sessions-live.md.
#
# Saves the hook payload (JSON on stdin) to a private temp file, starts
# `amem sessions ping --input <file> --delete-input` detached in the background, and exits 0
# at once. It never blocks or fails the session. The ping deletes the payload file after
# reading it, stores numbers and labels only, and logs one line per run.
#
# Overrides (tests, other checkouts):
#   AMEM_HOOK_REPO   agent-memory checkout     (default ~/Projects/agent-memory)
#   AMEM_HOOK_DIR    state, spool and log dir  (default ~/.claude/parent)
#   AMEM_HOOK_UV     the uv executable         (default: uv on PATH, else /opt/homebrew/bin/uv, ~/.local/bin/uv)
#   AMEM_HOOK_OFF=1  do nothing
set +e
umask 077

[ "${AMEM_HOOK_OFF:-}" = "1" ] && { cat >/dev/null 2>&1; exit 0; }

repo="${AMEM_HOOK_REPO:-$HOME/Projects/agent-memory}"
dir="${AMEM_HOOK_DIR:-$HOME/.claude/parent}"
log="$dir/amem-session-hook.log"
tmpdir="$dir/amem-hook-tmp"
mkdir -p "$tmpdir" 2>/dev/null || { cat >/dev/null 2>&1; exit 0; }

uv="${AMEM_HOOK_UV:-}"
if [ -z "$uv" ]; then
  uv="$(command -v uv 2>/dev/null)"
  for c in /opt/homebrew/bin/uv "$HOME/.local/bin/uv" /usr/local/bin/uv; do
    [ -n "$uv" ] && break
    [ -x "$c" ] && uv="$c"
  done
fi

payload="$(mktemp "$tmpdir/payload.XXXXXX" 2>/dev/null)" || { cat >/dev/null 2>&1; exit 0; }
cat >"$payload" 2>/dev/null

if [ -z "$uv" ] || [ ! -d "$repo" ]; then
  rm -f "$payload"
  echo "amem-hook $(date -u +%Y-%m-%dT%H:%M:%SZ) no uv or no repo; nothing done" >>"$log" 2>/dev/null
  exit 0
fi

# Keep the log small: one old copy, about 1 MB each.
if [ -f "$log" ] && [ "$(wc -c <"$log" 2>/dev/null || echo 0)" -gt 1000000 ]; then
  mv -f "$log" "$log.1" 2>/dev/null
fi

# Detached: own stdin/stdout/stderr (so Claude Code's pipes close now), immune to SIGHUP, in the
# background. The ping itself also calls setsid() and has a hard timeout.
nohup "$uv" run --directory "$repo" --quiet amem sessions ping --input "$payload" --delete-input --setsid \
  </dev/null >>"$log" 2>&1 &
disown 2>/dev/null
exit 0
