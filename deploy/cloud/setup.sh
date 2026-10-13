#!/bin/bash
# agent-memory: cloud chat digests (docs/cloud.md, "Cloud chats").
# Paste into the claude.ai/code environment's setup script. It runs as root before Claude Code
# starts. It downloads amem_digest.py from the agent-memory database and adds a Stop and a
# SessionEnd hook that send this session's redacted digest. It never fails the session:
# every path ends with exit 0 and one status line.
set +e

amem_setup() {
  local url="${AMEM_SUPABASE_URL%/}"
  [ -n "$url" ] || { echo "amem: AMEM_SUPABASE_URL not set, cloud digests off"; return 0; }
  command -v python3 >/dev/null 2>&1 || { echo "amem: no python3, cloud digests off"; return 0; }

  local dir="$HOME/.claude/amem"
  local script="$dir/amem_digest.py"
  local tmp="$dir/amem_digest.py.new"
  mkdir -p "$dir" || { echo "amem: cannot create $dir"; return 0; }

  # 1. Fetch the script (the cloud network secret adds the apikey header).
  local api="$url/rest/v1/amem_assets?name=eq.amem_digest.py&select=body"
  if command -v curl >/dev/null 2>&1; then
    curl -s --max-time 10 -o "$tmp.json" "$api"
  else
    python3 -c 'import sys, urllib.request; open(sys.argv[2], "wb").write(urllib.request.urlopen(sys.argv[1], timeout=10).read())' \
      "$api" "$tmp.json" 2>/dev/null
  fi
  python3 -c 'import json, sys; rows = json.load(open(sys.argv[1])); open(sys.argv[2], "w").write(rows[0]["body"])' \
    "$tmp.json" "$tmp" 2>/dev/null
  rm -f "$tmp.json"

  # 2. Use it only when it compiles; else keep an earlier copy, if any.
  if [ -s "$tmp" ] && python3 -m py_compile "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$script"
  else
    rm -f "$tmp"
    [ -f "$script" ] || { echo "amem: could not fetch amem_digest.py, cloud digests off"; return 0; }
  fi

  # 3. Merge the hooks into settings.json (other keys and hooks stay; a second run adds nothing).
  local merged=0
  python3 - "$HOME/.claude/settings.json" "python3 \"$script\" --hook" <<'PY' 2>/dev/null || merged=1
import json, os, sys
path, command = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        settings = json.load(fh)
except FileNotFoundError:
    settings = {}
if not isinstance(settings, dict):
    raise SystemExit(1)
hooks = settings.setdefault("hooks", {})
for event in ("Stop", "SessionEnd"):
    groups = hooks.setdefault(event, [])
    present = any(h.get("command") == command for g in groups if isinstance(g, dict)
                  for h in g.get("hooks", []) if isinstance(h, dict))
    if not present:
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".amem.tmp", "w") as fh:
    json.dump(settings, fh, indent=2)
    fh.write("\n")
os.replace(path + ".amem.tmp", path)
PY
  if [ "$merged" -eq 0 ]; then
    echo "amem: cloud digests on (Stop + SessionEnd hooks)"
  else
    echo "amem: settings.json not changed (unreadable or not a JSON object), cloud digests off"
  fi
  return 0
}

amem_setup
exit 0
