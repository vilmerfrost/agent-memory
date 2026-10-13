#!/bin/bash
# agent-memory: cloud chat digests, setup v2 (docs/cloud.md, "Cloud chats").
# Paste into the claude.ai/code environment's setup script. It runs as root before Claude Code
# starts and before the network proxy exists, so it fetches nothing. It writes /opt/amem/hook.sh
# (which fetches amem_digest.py at hook time, when the proxy is up) and registers it as a Stop
# and SessionEnd hook in the managed settings (Claude Code rebuilds ~/.claude at launch), in a
# managed-settings.d drop-in and in ~/.claude/settings.json. It never fails the session: every
# path ends with exit 0 and one status line. AMEM_SETUP_ROOT (tests only) prefixes
# /etc, /opt, /var/log and /tmp.
set +e

R="${AMEM_SETUP_ROOT:-/}"
R="${R%/}"
AMEM_DIR="$R/opt/amem"
HOOK="$AMEM_DIR/hook.sh"
ETC="$R/etc/claude-code"
LOG="$R/var/log/amem-setup.log"

log() { { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; } 2>/dev/null; }
say() { log "$1"; echo "$1"; }

write_hook() {
  {
    echo '#!/bin/bash'
    echo '# agent-memory cloud chat hook (Stop, SessionEnd), written by deploy/cloud/setup.sh v2.'
    echo '# Fetches amem_digest.py when missing or older than 24 h, then runs it with --hook.'
    echo '# Never fails, never prints. Log: labels only, never a URL or a value.'
    printf 'AMEM_DIR=%q\nAMEM_LOG=%q\nAMEM_TMP=%q\n' "$AMEM_DIR" "$R/tmp/amem-hook.log" "$R/tmp"
    cat <<'SH'
set +e
exec >/dev/null 2>&1
log() { printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${ev:-hook}" "$*" >>"$AMEM_LOG"; }
script="$AMEM_DIR/amem_digest.py"
in=$(mktemp "$AMEM_TMP/amem-hook.XXXXXX") || { log "no temp file"; exit 0; }
trap 'rm -f "$in" "$script.json.$$" "$script.new.$$"' EXIT
cat >"$in"
ev=$(sed -n 's/.*"hook_event_name" *: *"\([A-Za-z]*\)".*/\1/p' "$in" | head -n 1)
url="${AMEM_SUPABASE_URL%/}"
[ -n "$url" ] || { log "no AMEM_SUPABASE_URL"; exit 0; }

fetch() {  # GET the script (the network secret adds the key), use it only when it compiles
  local api="$url/rest/v1/amem_assets?name=eq.amem_digest.py&select=body" code
  if command -v curl >/dev/null; then
    code=$(curl -s --max-time 3 -o "$script.json.$$" -w '%{http_code}' "$api")
  else
    code=$(python3 -I -c '
import sys, urllib.request as u
class NoRedirect(u.HTTPRedirectHandler):
    def redirect_request(self, *a): return None
try:
    r = u.build_opener(NoRedirect).open(sys.argv[1], timeout=3)
    open(sys.argv[2], "wb").write(r.read()); print(r.status)
except u.HTTPError as e: print(e.code)
except Exception: print("000")' "$api" "$script.json.$$")
  fi
  [ "$code" = 200 ] || { log "fetch failed http ${code:-000}"; return 1; }
  python3 -I -c '
import json, sys
body = json.load(open(sys.argv[1]))[0]["body"]
compile(body, "amem_digest.py", "exec")
open(sys.argv[2], "w").write(body)' "$script.json.$$" "$script.new.$$" \
    || { log "fetch failed: no script or it does not compile"; return 1; }
  chmod 0644 "$script.new.$$"
  mv -f "$script.new.$$" "$script" && log "fetch ok"
}

# Fetch when missing or older than 24 h; after a failed fetch wait 5 min before the next try.
if [ -z "$(find "$script" -mmin -1440 2>/dev/null)" ] && [ -z "$(find "$AMEM_DIR/fetch-failed" -mmin -5 2>/dev/null)" ]; then
  if fetch; then rm -f "$AMEM_DIR/fetch-failed"; else touch "$AMEM_DIR/fetch-failed"; fi
fi
[ -f "$script" ] || { log "no amem_digest.py"; exit 0; }

# The digest has its own 8 s limit; `timeout` keeps fetch + digest under 9 s (hook timeout 10).
left=$((8 - SECONDS))
[ "$left" -ge 1 ] || left=1
if command -v timeout >/dev/null; then
  timeout -s KILL "$left" python3 -I "$script" --hook <"$in"
else
  python3 -I "$script" --hook <"$in"
fi
log "digest rc=$? ${SECONDS}s"
exit 0
SH
  } >"$HOOK.new" && chmod 0755 "$HOOK.new" && mv -f "$HOOK.new" "$HOOK"
}

# Merge {Stop, SessionEnd} -> command into each settings file: other keys and hooks stay,
# v1 entries (~/.claude/amem/amem_digest.py) go, a second run changes nothing.
# Exit code = number of files it could not use.
register() {
  python3 -I - "$@" <<'PY'
import json, os, sys
command, paths = sys.argv[1], sys.argv[2:]
entry = {"type": "command", "command": command, "timeout": 10}
failed = 0
def is_v1(h):
    return isinstance(h, dict) and "/.claude/amem/amem_digest.py" in str(h.get("command", ""))
for path in paths:
    try:
        try:
            with open(path) as fh:
                settings = json.load(fh)
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict) or not isinstance(settings.setdefault("hooks", {}), dict):
            raise ValueError("not a JSON object")
        before = json.dumps(settings, sort_keys=True)
        for event in ("Stop", "SessionEnd"):
            groups = settings["hooks"].setdefault(event, [])
            if not isinstance(groups, list):
                raise ValueError(event + " is not a list")
            for g in list(groups):
                hooks = g.get("hooks") if isinstance(g, dict) else None
                if isinstance(hooks, list) and any(is_v1(h) for h in hooks):
                    g["hooks"] = [h for h in hooks if not is_v1(h)]
                    if not g["hooks"]:
                        groups.remove(g)
            if not any(isinstance(g, dict) and isinstance(g.get("hooks"), list)
                       and any(isinstance(h, dict) and h.get("command") == command for h in g["hooks"])
                       for g in groups):
                groups.append({"hooks": [dict(entry)]})
        if os.path.exists(path) and json.dumps(settings, sort_keys=True) == before:
            print(f"{path}: unchanged")
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".amem.tmp", "w") as fh:
            json.dump(settings, fh, indent=2)
            fh.write("\n")
        os.chmod(path + ".amem.tmp", 0o644)
        os.replace(path + ".amem.tmp", path)
        print(f"{path}: hooks merged")
    except Exception as e:  # unreadable, not JSON, not writable: leave it alone
        failed += 1
        print(f"{path}: not changed ({type(e).__name__})")
sys.exit(failed)
PY
}

amem_setup() {
  log "setup v2 start (AMEM_SUPABASE_URL $([ -n "$AMEM_SUPABASE_URL" ] && echo set || echo 'not set'))"
  command -v python3 >/dev/null 2>&1 || { say "amem: no python3, cloud chat hook off"; return 0; }
  mkdir -p "$AMEM_DIR" "$R/tmp" 2>/dev/null
  if ! write_hook 2>/dev/null; then
    rm -f "$HOOK.new" 2>/dev/null
    say "amem: cannot write $HOOK, cloud chat hook off"
    return 0
  fi
  log "wrote $HOOK"
  local out failed line
  out=$(register "$(printf '%q' "$HOOK")" "$ETC/managed-settings.json" "$ETC/managed-settings.d/50-amem.json" \
    "$HOME/.claude/settings.json" 2>&1)
  failed=$?
  while IFS= read -r line; do log "$line"; done <<<"$out"
  if [ "$failed" -ge 3 ]; then
    say "amem: no settings file changed, cloud chat hook off"
  else
    say "amem: cloud chat hook on ($((3 - failed))/3 settings files; amem_digest.py is fetched at hook time)"
  fi
  return 0
}

mkdir -p "$(dirname "$LOG")" 2>/dev/null
amem_setup
exit 0
