"""`amem archive` CLI, the Keychain backend (through a fake `security`) and the no-network rule."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_memory import archive
from archive_support import CONTENT_WORDS, MAIN_ID, NOW, PROJ, Arc, arc, fake_security, make_sources, object_paths  # noqa: F401
from conftest import AMEM, base_env

SRC = Path(archive.__file__).resolve().parent
# Network libraries and the modules that pull them in (config imports socket, urllib, psycopg).
FORBIDDEN = {"socket", "_socket", "ssl", "_ssl", "urllib", "urllib3", "http", "httpx", "httpx2", "httpcore",
             "httpcore2", "requests", "aiohttp", "asyncio", "ftplib", "smtplib", "poplib", "imaplib", "xmlrpc",
             "psycopg", "psycopg_binary", "typesafe_sdk", "dotenv", "agent_memory.config", "agent_memory.store",
             "agent_memory.backend_rest", "agent_memory.sessions_ingest"}


def _amem(a: Arc, *args: str, stdin: str | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
    res = subprocess.run([AMEM, "archive", *args], env=base_env(**a.env(**extra)), input=stdin,
                         capture_output=True, text=True, timeout=60)
    for stream in (res.stdout, res.stderr):  # relative paths may show; transcript text never
        assert not any(w in stream for w in CONTENT_WORDS) and "Traceback" not in stream, stream
    return res


def test_cli_end_to_end(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = make_sources(arc)
    res = _amem(arc, "status")
    assert res.returncode == 0 and "initialized=no key=no" in res.stdout
    res = _amem(arc, "run", "--dry-run")
    assert res.returncode == 0 and "files=6 new_versions=6" in res.stdout and "dry run" in res.stdout
    assert not arc.archive.exists()
    res = _amem(arc, "run")
    assert res.returncode == 1 and "not initialized" in res.stderr

    res = _amem(arc, "init")
    assert res.returncode == 0 and "key k1 in the file backend" in res.stdout and "key-export" in res.stdout
    assert _amem(arc, "init").returncode == 1
    res = _amem(arc, "run")
    assert res.returncode == 0 and "files=6 new_versions=6 unchanged=0 new_objects=6" in res.stdout
    empty = tmp_path / "empty"
    empty.mkdir()
    res = _amem(arc, "run", "--projects-dir", str(empty), "--codex-home", str(empty))
    assert res.returncode == 0 and "files=0" in res.stdout
    res = _amem(arc, "status")
    assert res.returncode == 0 and "initialized=yes key=yes versions=6 sessions=5 paths=6" in res.stdout
    res = _amem(arc, "verify")
    assert res.returncode == 0 and res.stdout.startswith("archive verify: ok") and "checked=6" in res.stdout
    res = _amem(arc, "verify", "--sample", "2")
    assert res.returncode == 0 and "checked=2" in res.stdout

    res = _amem(arc, "restore", "--session", MAIN_ID, "--to", str(tmp_path / "out"))
    assert res.returncode == 0 and "files=1" in res.stdout
    assert (tmp_path / "out" / PROJ / f"{MAIN_ID}.jsonl").read_bytes() == srcs["main"].read_bytes()
    res = _amem(arc, "restore", "--session", "no-such-session", "--to", str(tmp_path / "out2"))
    assert res.returncode == 1 and "no archived versions" in res.stderr
    res = _amem(arc, "restore", "--session", MAIN_ID, "--to", str(arc.projects / "x"))
    assert res.returncode == 1 and "refusing to restore" in res.stderr

    for usage in (["restore", "--to", str(tmp_path / "o")],
                  ["restore", "--session", "x", "--to", "o", "--version", "0"],
                  ["restore", "--session", "x", "--to", "o", "--version", "1", "--as-of", "2026-10-01"],
                  ["prune", "--days", "0"], ["verify", "--sample", "x"], []):
        assert _amem(arc, *usage).returncode == 2, usage

    res = _amem(arc, "prune", "--dry-run")
    assert res.returncode == 0 and "dry run" in res.stdout and "versions_removed=0" in res.stdout
    res = _amem(arc, "prune", "--days", "180")
    assert res.returncode == 0 and "versions_kept=6" in res.stdout

    res = _amem(arc, "key-export")
    assert res.returncode == 1 and res.stdout == "" and "not a terminal" in res.stderr
    res = _amem(arc, "key-export", "--allow-non-tty")
    assert res.returncode == 0 and res.stdout.startswith("amem-archive-key:1:k1:")
    recovery = res.stdout.strip()
    res = _amem(arc, "key-import", stdin=recovery + "\n")
    assert res.returncode == 0 and "key k1 already-present" in res.stdout
    res = _amem(arc, "key-import", stdin="amem-archive-key:1:k1:nope:00000000\n")
    assert res.returncode == 1 and "checksum" in res.stderr

    blob = bytearray(object_paths(arc)[0].read_bytes())
    blob[-1] ^= 0x01
    object_paths(arc)[0].write_bytes(bytes(blob))
    res = _amem(arc, "verify")
    assert res.returncode == 1 and res.stdout.startswith("archive verify: PROBLEM") and "bad=1" in res.stdout


def test_cli_refuses_an_archive_dir_in_the_sandbox(arc: Arc) -> None:  # noqa: F811
    make_sources(arc)
    res = _amem(arc, "init", AMEM_ARCHIVE_DIR=str(arc.sandbox / "archive"))
    assert res.returncode == 1 and "sandbox" in res.stderr
    assert not (arc.sandbox / "archive").exists()


def test_keychain_backend_keeps_the_key_out_of_argv(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    exe, store = fake_security(arc.tmp)
    monkeypatch.delenv("AMEM_ARCHIVE_KEY_FILE")
    monkeypatch.setenv("AMEM_ARCHIVE_SECURITY", str(exe))
    make_sources(arc)
    assert isinstance(archive.key_store(), archive.KeychainKeyStore)
    assert archive.init(now=NOW) == "keychain"
    stored = json.loads(store.read_text())
    assert list(stored) == ["amem-archive/k1"]
    calls = [json.loads(x) for x in Path(f"{store}.argv").read_text().splitlines()]
    assert ["-i"] in calls and all(stored["amem-archive/k1"] not in " ".join(c) for c in calls)
    assert archive.run(now=NOW).new_versions == 6 and archive.verify().ok
    kid, key = archive.parse_recovery_string(archive.export_key())
    assert archive._b64url(key) == stored["amem-archive/k1"]
    monkeypatch.setenv("AMEM_ARCHIVE_DIR", str(arc.tmp / "archive2"))
    with pytest.raises(archive.ArchiveError, match="already exists in the keychain"):
        archive.init()
    assert json.loads(store.read_text()) == stored  # never overwritten
    # The same through the CLI: init with the fake, then status sees the key.
    res = _amem(arc, "status", AMEM_ARCHIVE_KEY_FILE="", AMEM_ARCHIVE_SECURITY=str(exe))
    assert res.returncode == 0 and "initialized=yes key=yes versions=6" in res.stdout


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _forbidden(names: set[str]) -> set[str]:
    return {n for n in names if n in FORBIDDEN or n.split(".")[0] in FORBIDDEN}


def test_the_archive_modules_import_no_network_library() -> None:
    for name in ("archive.py", "cli_archive.py"):
        assert _forbidden(_imports(SRC / name)) == set(), name
    code = ("import json, sys\nimport agent_memory.archive, agent_memory.cli_archive\n"
            "print(json.dumps(sorted(sys.modules)))")
    res = subprocess.run([sys.executable, "-c", code], env=base_env(), capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    loaded = set(json.loads(res.stdout))
    assert "agent_memory.archive" in loaded and "cryptography" in loaded
    assert _forbidden(loaded) == set()
