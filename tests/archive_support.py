"""Synthetic transcripts and an isolated environment for tests/test_archive*.py. Holds no tests.

Every byte here is made up. MARK appears in the transcript text, a project folder name and a
session id, so a scan of the archive dir for it proves nothing is stored in plaintext.
The `arc` fixture points every archive, key and source path at a tmp dir and replaces the
`security` executable with a tripwire, so no test can touch the real Keychain.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_memory import archive

MARK = "PLAINTEXT-MARKER-q7x2"
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
PROJ = f"-Users-someone-{MARK}-repo"
MAIN_ID = f"sess-{MARK}-main"
AGENT = "agent-a1b2"
CODEX_TID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"   # in session_meta
NAME_TID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0003"    # in the same file's name: session_meta wins
FILE_TID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002"    # only in a file name (no session_meta)
# Words that appear only inside transcript text (MARK is also in paths and ids).
CONTENT_WORDS = ("hello main", "worker brief", "codex one", "codex bare", "codex archived", "session_meta")


@dataclass
class Arc:
    tmp: Path
    archive: Path
    projects: Path
    codex: Path
    key_file: Path
    sandbox: Path
    tripwire: Path

    @property
    def layout(self) -> archive.Layout:
        return archive.Layout(self.archive)

    def env(self, **extra: str) -> dict[str, str]:
        """The archive env for a CLI subprocess (on top of conftest.base_env)."""
        return {
            "AMEM_ARCHIVE_DIR": str(self.archive), "AMEM_ARCHIVE_KEY_FILE": str(self.key_file),
            "AMEM_PROJECTS_DIR": str(self.projects), "AMEM_CODEX_HOME": str(self.codex),
            "AMEM_CLAUDE_HOME": str(self.tmp / "claude"), "AMEM_ARCHIVE_SECURITY": str(self.tripwire),
            "RSI_SANDBOX_DIR": str(self.sandbox), **extra,
        }


@pytest.fixture
def arc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Arc]:
    a = Arc(tmp=tmp_path, archive=tmp_path / "archive", projects=tmp_path / "claude" / "projects",
            codex=tmp_path / "codex", key_file=tmp_path / "keys" / "archive-key.json",
            sandbox=tmp_path / "sandbox", tripwire=tmp_path / "bin" / "security-tripwire")
    a.tripwire.parent.mkdir()
    a.tripwire.write_text(f'#!/bin/sh\necho called >> "{a.tripwire}.calls"\nexit 99\n')
    a.tripwire.chmod(0o755)
    for name, value in a.env().items():
        monkeypatch.setenv(name, value)
    yield a
    assert not Path(f"{a.tripwire}.calls").exists(), "the real-keychain tripwire was called"


def line(text: str, kind: str = "user") -> bytes:
    return json.dumps({"type": kind, "message": {"content": f"{MARK} {text}"}}).encode() + b"\n"


def codex_meta_line(thread: str) -> bytes:
    return json.dumps({"timestamp": "2026-10-13T10:00:00Z", "type": "session_meta",
                       "payload": {"id": thread, "cwd": "/tmp/x", "originator": "codex_exec"}}).encode() + b"\n"


def make_sources(a: Arc) -> dict[str, Path]:
    """Six transcript files (all distinct) plus files the archive must ignore."""
    proj = a.projects / PROJ
    subs = proj / MAIN_ID / "subagents"
    files = {
        "main": (proj / f"{MAIN_ID}.jsonl", line("hello main") + line("reply", "assistant")),
        "sub": (subs / f"{AGENT}.jsonl", line("worker brief") + line("worker reply", "assistant")),
        "meta": (subs / f"{AGENT}.meta.json", json.dumps({"agentType": "worker", "note": MARK}).encode()),
        "codex_meta": (a.codex / "sessions/2026/10/13" / f"rollout-2026-10-13T10-00-00-{NAME_TID}.jsonl",
                       codex_meta_line(CODEX_TID) + line("codex one")),
        "codex_file": (a.codex / "archived_sessions" / f"rollout-2026-10-12T09-00-00-{FILE_TID}.jsonl",
                       line("codex archived, no meta")),
        "codex_bare": (a.codex / "sessions/2026/10/14" / "rollout-nothread.jsonl", line("codex bare")),
    }
    for path, data in files.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        set_mtime(path, NOW - timedelta(days=1))  # the tests' clock, not the machine's
    (proj / "notes.txt").write_text(f"{MARK} not a transcript")
    (proj / MAIN_ID / "tool-results").mkdir()
    (proj / MAIN_ID / "tool-results" / "x.txt").write_text(MARK)
    (a.codex / "session_index.jsonl").write_text(json.dumps({"id": CODEX_TID, "thread_name": MARK}) + "\n")
    return {name: path for name, (path, _data) in files.items()}


def set_mtime(path: Path, when: datetime | int) -> int:
    ns = when if isinstance(when, int) else int(when.timestamp()) * 1_000_000_000
    os.utime(path, ns=(ns, ns))
    return ns


def keyring() -> archive.Keyring:
    return archive.Keyring(archive.key_store(), archive.DEFAULT_KEY_ID)


def records(a: Arc) -> list[dict[str, Any]]:
    m = archive.read_manifest(a.layout, keyring())
    assert m.ok, (m.bad_lines, m.chain_breaks)
    return m.records


def versions(a: Arc) -> list[dict[str, Any]]:
    return [r for r in records(a) if r["type"] == "version"]


def by_path(a: Arc, path: Path, root: Path) -> list[dict[str, Any]]:
    rel = path.relative_to(root).as_posix()
    return sorted((r for r in versions(a) if r["path"] == rel), key=lambda r: r["version"])


def snapshot(root: Path) -> dict[str, tuple[int, int, int, str | None]]:
    """Every entry below root: mode, size, mtime_ns, content hash."""
    out: dict[str, tuple[int, int, int, str | None]] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if stat.S_ISREG(st.st_mode) else None
        out[p.relative_to(root).as_posix()] = (stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns, digest)
    return out


def all_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def object_paths(a: Arc) -> list[Path]:
    return sorted(p for p in (a.archive / "objects").rglob("*.bin"))


FAKE_SECURITY = r'''#!@PYTHON@
"""A stand-in for /usr/bin/security: a JSON file instead of the Keychain. Logs argv (never stdin)."""
import json, sys
STORE = @STORE@
with open(STORE + ".argv", "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")


def load():
    try:
        with open(STORE) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def opt(args, flag):
    return args[args.index(flag) + 1]


def run(args):
    data = load()
    name = opt(args, "-s") + "/" + opt(args, "-a")
    if args[0] == "find-generic-password":
        if name not in data:
            return 44
        if "-w" in args:
            print(data[name])
        return 0
    if args[0] == "add-generic-password":
        if name in data:
            return 45
        data[name] = opt(args, "-w")
        with open(STORE, "w") as fh:
            json.dump(data, fh)
        return 0
    return 1


if sys.argv[1:] == ["-i"]:
    rc = 0
    for cmd in sys.stdin:
        if cmd.strip():
            rc = run(cmd.split())
    sys.exit(rc)
sys.exit(run(sys.argv[1:]))
'''


def fake_security(tmp: Path) -> tuple[Path, Path]:
    """(the fake `security` executable, its JSON store)."""
    store = tmp / "fake-keychain.json"
    exe = tmp / "bin" / "security-fake"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(FAKE_SECURITY.replace("@PYTHON@", sys.executable).replace("@STORE@", repr(str(store))))
    exe.chmod(0o755)
    return exe, store
