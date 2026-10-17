#!/usr/bin/env python3
"""Start a live agent CLI (claude or codex) for one replay run, inside the worker container.

    python3 /mnt/agent/live_entry.py [--fixture /mnt/fixture] --prompt-file /mnt/case/prompt.txt \
        [--work /work] -- claude -p {PROMPT} --model M ...

Copies the fixture into the workspace, then replaces the literal argument `{PROMPT}` with the prompt
file's text and execs the command (no shell, so the prompt is never parsed). Used for the live agents
and the live judges; the stub agent does not need it. Live replays are unverified in Mission 005.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ALLOWED = ("claude", "codex")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("live_entry: missing -- before the command", file=sys.stderr)
        return 2
    i = argv.index("--")
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--prompt-file", required=True, type=Path)
    ap.add_argument("--work", type=Path, default=Path("/work"))
    a = ap.parse_args(argv[:i])
    cmd = argv[i + 1:]
    if not cmd or cmd[0] not in ALLOWED:
        print(f"live_entry: command must start with one of {ALLOWED}", file=sys.stderr)
        return 2
    if a.fixture and a.fixture.is_dir():
        for child in sorted(a.fixture.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.copytree(child, a.work / child.name, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(child, a.work / child.name, follow_symlinks=False)
    prompt = a.prompt_file.read_text(encoding="utf-8")
    cmd = [prompt if c == "{PROMPT}" else c for c in cmd]
    os.chdir(a.work)
    os.execvp(cmd[0], cmd)
    return 127  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
