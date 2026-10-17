#!/usr/bin/env bash
# Trial merge of a branch tip into main, in a throwaway worktree (Mission 004C merge plan,
# docs/integrations/m004c/merge-plan.md). Re-run it on the final rsi/m004c tip before the real merge.
#
#   scripts/merge_check.sh [TIP] [BASE]        defaults: TIP=rsi/m004c BASE=main
#   MERGE_CHECK_SKIP_TESTS=1 scripts/merge_check.sh     skip the full suite (fast structural check)
#
# What it does, in order:
#   1. Resolves TIP and BASE to SHAs and prints ahead/behind counts and the merge base.
#   2. Lists production-relevant paths the tip changes: migrations, dependencies, .env.example, the
#      repo's launchd/hook/cloud deploy files, the daily job. Expected for 004C: none. Then every existing
#      file (outside docs/ and tests/) the tip modifies, deletes or renames.
#   3. Lists files changed on both sides since the merge base (conflict candidates).
#   4. In a new detached worktree under $TMPDIR at BASE: `git merge --no-ff TIP`. Conflicts: list them, exit 1.
#   5. Import probe on the merged tree: imports the CLI with every socket connect refused, prints the
#      integration modules that load at startup, and checks `amem --help` and `amem daily --help`.
#   6. The full suite on the merged tree (`uv run --quiet pytest -q`, needs Docker).
#
# Safety: never moves BASE, TIP or any other ref, never touches another worktree's files, never pushes.
# The throwaway worktree has no .env.local (untracked files are not checked out) and runs with
# AMEM_NO_DOTENV=1, so no provider is configured and nothing can be sent. The worktree is removed on exit.
set -euo pipefail

tip="${1:-rsi/m004c}"
base="${2:-main}"
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(git -C "$here" rev-parse --show-toplevel)"
g() { git -C "$repo" "$@"; }

tip_sha="$(g rev-parse --verify "${tip}^{commit}")"
base_sha="$(g rev-parse --verify "${base}^{commit}")"
mb="$(g merge-base "$base_sha" "$tip_sha")"

echo "base       $base $base_sha"
echo "tip        $tip $tip_sha"
echo "merge-base $mb"
echo "ahead      $(g rev-list --count "$base_sha..$tip_sha") commits ($(g rev-list --count --no-merges "$base_sha..$tip_sha") non-merge)"
echo "behind     $(g rev-list --count "$tip_sha..$base_sha") commits"

echo
echo "== production-relevant paths changed by the tip (expected: none)"
prod="$(g diff --name-status "$mb" "$tip_sha" -- supabase pyproject.toml uv.lock .env.example \
        deploy/launchd deploy/hooks deploy/cloud src/agent_memory/daily.py src/agent_memory/cli_daily.py)"
echo "${prod:-none}"

echo
echo "== existing files the tip modifies, deletes or renames, outside docs/ and tests/"
echo "   (each can change what a routine runs; tip 09413ba: only .gitignore and src/agent_memory/cli.py)"
existing="$(g diff --name-status --diff-filter=MDRT "$mb" "$tip_sha" -- . ':!docs' ':!tests')"
echo "${existing:-none}"

echo
echo "== files changed on both sides since the merge base"
both="$(comm -12 <(g diff --name-only "$mb" "$base_sha" | sort) <(g diff --name-only "$mb" "$tip_sha" | sort))"
echo "${both:-none}"

dir="$(mktemp -d "${TMPDIR:-/tmp}/amem-merge-check.XXXXXX")"
cleanup() {
  g worktree remove --force "$dir" >/dev/null 2>&1 || true
  rm -rf "$dir"
}
trap cleanup EXIT
rmdir "$dir"                                  # git worktree add wants to create it
g worktree add --quiet --detach "$dir" "$base_sha"

echo
echo "== trial merge (git merge --no-ff) in $dir"
if out="$(git -C "$dir" merge --no-ff --no-edit "$tip_sha" 2>&1)"; then
  echo "clean: $(git -C "$dir" rev-parse --short HEAD) (tree $(git -C "$dir" rev-parse --short 'HEAD^{tree}'))"
  git -C "$dir" diff --shortstat "$base_sha" HEAD
else
  echo "CONFLICTS:"
  git -C "$dir" diff --name-only --diff-filter=U
  echo "$out" | tail -n 20
  git -C "$dir" merge --abort || true
  exit 1
fi

echo
echo "== import probe on the merged tree (sockets refused; no env files)"
export AMEM_NO_DOTENV=1
uv run --directory "$dir" --quiet python - <<'PY'
import socket
import sys

opened = []


def refuse(self, addr):
    opened.append(repr(addr))
    raise OSError("merge_check: network refused during import")


socket.socket.connect = refuse
import agent_memory.cli as cli  # noqa: E402

new = sorted(m for m in sys.modules if m.startswith("agent_memory.integrations")
             or m in ("agent_memory.synth", "agent_memory.cli_synth", "agent_memory.cli_integrations"))
print("integration modules loaded at CLI import:", ", ".join(new) or "none")
print("sockets opened during import:", opened or "none")
sys.exit(1 if opened else 0)
PY
uv run --directory "$dir" --quiet amem --help >/dev/null && echo "amem --help: ok"
uv run --directory "$dir" --quiet amem daily --help >/dev/null && echo "amem daily --help: ok"

if [ "${MERGE_CHECK_SKIP_TESTS:-0}" = "1" ]; then
  echo
  echo "== suite skipped (MERGE_CHECK_SKIP_TESTS=1)"
  exit 0
fi

echo
echo "== full suite on the merged tree (uv run --quiet pytest -q; needs Docker)"
uv run --directory "$dir" --quiet pytest -q -p no:cacheprovider
