#!/usr/bin/env python3
"""Machine check for one replay run, in its own fresh worker container (no network, empty home).

    python3 /mnt/agent/check_entry.py --spec /mnt/check/check.json --result /mnt/result \
        --fixture /mnt/fixture [--hidden /mnt/check/hidden] --work /work

The agent's workspace is mounted read-only at --result. It is copied into /work, then the case's check
command runs there with `bash -c` (env: HIDDEN_CHECK_DIR = --hidden, FIXTURE_DIR = --fixture, the untouched
original). The agent never saw the check, and
nothing it left in its own home or workspace can change this container's tools. The diff between the
fixture and the agent's workspace is computed before the check runs.

Prints one JSON line: {check_exit, check_passed, timed_out, log, diff_stat, diff}.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKIP_PARTS = {".git", "__pycache__", ".hidden"}
MAX_DIFF = 15000
MAX_LOG = 2000


def files(root: Path) -> dict[str, Path]:
    out = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in SKIP_PARTS for part in rel.parts) or p.suffix == ".pyc":
            continue
        if p.is_file() or p.is_symlink():
            out[rel.as_posix()] = p
    return out


def read_text(p: Path | None) -> list[str]:
    if p is None:
        return []
    if p.is_symlink():
        return [f"<symlink to {os.readlink(p)}>\n"]
    try:
        return p.read_text(encoding="utf-8").splitlines(keepends=True)
    except (UnicodeDecodeError, OSError):
        return [f"<binary {p.stat().st_size} bytes>\n"]


def diff(fixture: Path, result: Path) -> tuple[dict, str]:
    before, after = files(fixture), files(result)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed, chunks = [], []
    for rel in sorted(set(before) | set(after)):
        a, b = read_text(before.get(rel)), read_text(after.get(rel))
        if a == b:
            continue
        if rel in before and rel in after:
            changed.append(rel)
        chunks.extend(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}"))
    text = "".join(chunks)
    stat = {"added": added, "removed": removed, "changed": changed,
            "files": len(added) + len(removed) + len(changed)}
    return stat, text[:MAX_DIFF]


def copy_into(src: Path, dst: Path) -> None:
    for child in sorted(src.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, dst / child.name, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dst / child.name, follow_symlinks=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--result", required=True, type=Path)
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--hidden", type=Path)
    ap.add_argument("--work", type=Path, default=Path("/work"))
    a = ap.parse_args(argv)
    spec = json.loads(a.spec.read_text())
    stat, text = diff(a.fixture, a.result) if a.fixture else ({}, "")
    copy_into(a.result, a.work)
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/home/rsi"),
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "HIDDEN_CHECK_DIR": str(a.hidden) if a.hidden else "", "FIXTURE_DIR": str(a.fixture) if a.fixture else ""}
    timed_out = False
    try:
        p = subprocess.run(["bash", "-c", spec["check"]], cwd=a.work, env=env, capture_output=True, text=True,
                           timeout=float(spec.get("timeout_s", 120)), stdin=subprocess.DEVNULL)
        code, log = p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        code, log, timed_out = 124, "check timed out", True
    print(json.dumps({"check_exit": code, "check_passed": code == 0 and not timed_out, "timed_out": timed_out,
                      "log": log[-MAX_LOG:], "diff_stat": stat, "diff": text}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
