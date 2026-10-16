"""Encrypted, incremental local archive of Claude Code and Codex transcripts (docs/archive.md).

Why: Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30). The archive
keeps every distinct version of every transcript, encrypted at rest, for 180 days (`prune`).

Read (never written, never deleted):
- Claude Code: `<projects>/<project>/*.jsonl`, `<projects>/<project>/<session>/subagents/agent-*.jsonl`
  and the `agent-*.meta.json` next to them. Projects dir: `--projects-dir`, `AMEM_PROJECTS_DIR`,
  else `<AMEM_CLAUDE_HOME or ~/.claude>/projects`.
- Codex: `<codex home>/sessions/**/rollout-*.jsonl` and `<codex home>/archived_sessions/**/rollout-*.jsonl`.
  Codex home: `--codex-home`, `AMEM_CODEX_HOME`, else `.codex` next to the Claude home.

The archive dir (`AMEM_ARCHIVE_DIR`, default `~/Library/Application Support/amem-archive`, mode 700,
every file mode 600):

    config.json              format version and current key id only (no private data)
    manifest.jsonl           append-only records; every line is encrypted on its own and
                             carries `prev` = sha256 of the previous plaintext line (hash chain)
    index.bin                encrypted: source path -> size, mtime_ns, sha (skips unchanged files)
    objects/<aa>/<sha>.bin   zlib, then AES-256-GCM; <sha> = sha256 of the plaintext (dedupe)
    lock                     flock: one writer at a time

Sealed blob (objects, index, each manifest line):
    magic (4) | format (1) | key id length (1) | key id | nonce (12) | ciphertext + GCM tag
AAD = "amem-archive" | kind | format | key id | ident (the object id; "index"; "manifest").

Privacy: this module imports no network library (tests/test_archive.py checks it, and that is
why it does not import `config` or `sessions_ingest`). Reports carry counts, ids and relative
paths only, never transcript text or key material. Nothing uploads anywhere.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import stat
import subprocess
import sys
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_memory import codex_sessions

FORMAT = 1
SERVICE = "amem-archive"             # Keychain service; the account is the key id
DEFAULT_KEY_ID = "k1"
DEFAULT_DAYS = 180
KEY_BYTES = 32                        # AES-256
NONCE_BYTES = 12                      # 96-bit GCM nonce, fresh and random per seal
MAX_FILE_BYTES = 1 << 30              # larger files are skipped (AESGCM takes < 2**31 bytes)
ZERO_HASH = "0" * 64                  # `prev` of the first manifest line

ARCHIVE_DIR_ENV = "AMEM_ARCHIVE_DIR"
KEY_FILE_ENV = "AMEM_ARCHIVE_KEY_FILE"   # file key backend (tests, non-macOS)
SECURITY_ENV = "AMEM_ARCHIVE_SECURITY"   # path of the `security` executable (tests use a fake)
SANDBOX_ENV = "RSI_SANDBOX_DIR"
DEFAULT_SECURITY = "/usr/bin/security"
SECURITY_NOT_FOUND = 44                  # `security` exit code: errSecItemNotFound

RECOVERY_PREFIX = "amem-archive-key"
KEY_ID_RE = re.compile(r"[a-z0-9]{1,16}")
_B64URL_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_OBJ_NAME_RE = re.compile(r"([0-9a-f]{64})\.bin")
_MAGIC = {"object": b"AMOB", "index": b"AMIX", "manifest": b"AMMF"}
INDEX_IDENT = "index"
MANIFEST_IDENT = "manifest"
CODEX_DIRS = ("sessions", "archived_sessions")


class ArchiveError(Exception):
    """A problem the CLI reports with exit 1. Messages never hold transcript text or key material."""


class KeyMissing(ArchiveError):
    """The key a blob names is not in the key backend."""


class Undecryptable(ArchiveError):
    """A blob with a bad header, a failed GCM tag (wrong key or tampered bytes) or bad content."""


# ---------------------------------------------------------------- paths


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def claude_home() -> Path:
    """AMEM_CLAUDE_HOME (tests), else ~/.claude. Same rule as sessions_ingest, without env files."""
    return _env_path("AMEM_CLAUDE_HOME") or Path.home() / ".claude"


def projects_dir(arg: Path | None = None) -> Path:
    if arg is not None:
        return arg.expanduser()
    return _env_path("AMEM_PROJECTS_DIR") or claude_home() / "projects"


def codex_home(arg: Path | None = None) -> Path:
    if arg is not None:
        return arg.expanduser()
    return _env_path("AMEM_CODEX_HOME") or claude_home().parent / ".codex"


def default_archive_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "amem-archive"


def archive_dir() -> Path:
    return _env_path(ARCHIVE_DIR_ENV) or default_archive_dir()


def sandbox_root() -> Path:
    """The experiment sandbox root: RSI_SANDBOX_DIR, else ~/sandbox."""
    return _env_path(SANDBOX_ENV) or Path.home() / "sandbox"


def key_file() -> Path | None:
    return _env_path(KEY_FILE_ENV)


def _real(path: Path) -> Path:
    return Path(os.path.realpath(os.path.abspath(path.expanduser())))


def _inside(child: Path, parent: Path) -> bool:
    """Both resolved. True when child is parent or below it."""
    return child == parent or parent in child.parents


def _overlaps(a: Path, b: Path) -> bool:
    return _inside(a, b) or _inside(b, a)


def forbidden_mount_roots() -> tuple[Path, ...]:
    """Resolved paths an experiment sandbox must never mount or expose: the archive dir (as
    configured, and the default one) and the key file of the file backend, when set."""
    out: list[Path] = []
    for p in (archive_dir(), default_archive_dir(), key_file()):
        if p is not None and (r := _real(p)) not in out:
            out.append(r)
    return tuple(out)


def source_roots(projects: Path | None = None, codex: Path | None = None) -> list[Path]:
    """The transcript roots in use plus the default ones (~/.claude/projects, ~/.codex), resolved."""
    out: list[Path] = []
    for p in (projects_dir(projects), codex_home(codex),
              Path.home() / ".claude" / "projects", Path.home() / ".codex"):
        if (r := _real(p)) not in out:
            out.append(r)
    return out


def _git_root(path: Path) -> Path | None:
    for d in (path, *path.parents):
        if os.path.lexists(d / ".git"):
            return d
    return None


def check_archive_dir(path: Path, roots: list[Path]) -> None:
    """Refuse an archive dir that overlaps the sandbox root or a transcript source root, sits
    inside a git repo, or would hold the key file. Raises ArchiveError."""
    real = _real(path)
    if _overlaps(real, _real(sandbox_root())):
        raise ArchiveError(f"the archive dir must not be inside the sandbox root ({SANDBOX_ENV} or ~/sandbox) "
                           "or contain it")
    if any(_overlaps(real, r) for r in roots):
        raise ArchiveError("the archive dir must not be inside a transcript source root or contain one")
    if _git_root(real) is not None:
        raise ArchiveError("the archive dir must not be inside a git repository")
    kf = key_file()
    if kf is not None and _inside(_real(kf), real):
        raise ArchiveError(f"{KEY_FILE_ENV} must not be inside the archive dir")


def check_restore_dir(path: Path, archive_root: Path, roots: list[Path]) -> None:
    real = _real(path)
    for r in (*roots, _real(archive_root), _real(default_archive_dir())):
        if _inside(real, r):
            raise ArchiveError("refusing to restore into a transcript source root or the archive dir")


# ---------------------------------------------------------------- keys


def _b64url(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")


def _key_from_b64url(text: str) -> bytes:
    if not _B64URL_RE.fullmatch(text):
        raise ArchiveError("a stored key is malformed")
    key = base64.urlsafe_b64decode(text + "=")
    if len(key) != KEY_BYTES:
        raise ArchiveError("a stored key is malformed")
    return key


def _check_key_id(kid: str) -> str:
    if not KEY_ID_RE.fullmatch(kid):
        raise ArchiveError("a key id is 1 to 16 lowercase letters or digits")
    return kid


class KeyStore(Protocol):
    name: str

    def get(self, kid: str) -> bytes | None: ...

    def add(self, kid: str, key: bytes) -> None: ...


class FileKeyStore:
    """`AMEM_ARCHIVE_KEY_FILE`: JSON `{"format": 1, "keys": {"k1": "<base64url>"}}`, mode 600.
    For tests and machines without a Keychain."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, str]:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return {}
        if st.st_mode & 0o077:
            raise ArchiveError(f"{KEY_FILE_ENV} must be mode 600")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            keys = data["keys"]
            if not isinstance(keys, dict) or not all(isinstance(v, str) for v in keys.values()):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ArchiveError(f"{KEY_FILE_ENV} is not a valid key file") from None
        return keys

    def get(self, kid: str) -> bytes | None:
        value = self._load().get(kid)
        return _key_from_b64url(value) if value is not None else None

    def add(self, kid: str, key: bytes) -> None:
        keys = self._load()
        if kid in keys:
            raise ArchiveError(f"key {kid} already exists in the file backend; refusing to overwrite it")
        keys[kid] = _b64url(key)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_atomic(self.path, json.dumps({"format": FORMAT, "keys": keys}, sort_keys=True).encode())


class KeychainKeyStore:
    """macOS Keychain generic password: service `amem-archive`, account = key id, value = the key
    in base64url. Through the `security` CLI. The key never goes into argv (other processes can
    read argv): it is written with `security -i`, which reads the command from stdin, and read
    back with `find-generic-password -w` (the key comes back on stdout, captured, never printed)."""

    name = "keychain"

    def __init__(self, exe: str) -> None:
        self.exe = exe

    def _run(self, args: list[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run([self.exe, *args], input=stdin, capture_output=True, timeout=30, check=False)
        except FileNotFoundError:
            raise ArchiveError("the macOS `security` tool was not found") from None
        except subprocess.TimeoutExpired:
            raise ArchiveError("the macOS `security` tool timed out (is the login keychain locked?)") from None

    def get(self, kid: str) -> bytes | None:
        res = self._run(["find-generic-password", "-s", SERVICE, "-a", kid, "-w"])
        if res.returncode == SECURITY_NOT_FOUND:
            return None
        if res.returncode != 0:
            raise ArchiveError(f"reading the key from the keychain failed (security exit {res.returncode})")
        return _key_from_b64url(res.stdout.decode("ascii", "replace").strip())

    def add(self, kid: str, key: bytes) -> None:
        if self.get(kid) is not None:
            raise ArchiveError(f"key {kid} already exists in the keychain; refusing to overwrite it")
        # No spaces or quotes in any token (base64url, a checked key id), so `security -i` splits it plainly.
        command = f"add-generic-password -s {SERVICE} -a {kid} -l {SERVICE} -w {_b64url(key)}\n"
        res = self._run(["-i"], stdin=command.encode("ascii"))
        if res.returncode != 0:
            raise ArchiveError(f"storing the key in the keychain failed (security exit {res.returncode})")
        if self.get(kid) != key:
            raise ArchiveError("the key stored in the keychain could not be read back")


class MemoryKeyStore:
    """A key held in memory (key-import checks a candidate key with it)."""

    name = "memory"

    def __init__(self, keys: dict[str, bytes] | None = None) -> None:
        self.keys = dict(keys or {})

    def get(self, kid: str) -> bytes | None:
        return self.keys.get(kid)

    def add(self, kid: str, key: bytes) -> None:
        self.keys[kid] = key


def key_store() -> KeyStore:
    """The file backend when AMEM_ARCHIVE_KEY_FILE is set, else the macOS Keychain."""
    kf = key_file()
    if kf is not None:
        return FileKeyStore(kf)
    exe = os.environ.get(SECURITY_ENV, "").strip()
    if exe or sys.platform == "darwin":
        return KeychainKeyStore(exe or DEFAULT_SECURITY)
    raise ArchiveError(f"no key backend: set {KEY_FILE_ENV} (the Keychain backend needs macOS)")


class Keyring:
    """Keys by id, read once per process. `current_id` seals new blobs; any stored id opens old ones."""

    def __init__(self, store: KeyStore, current_id: str) -> None:
        self.store = store
        self.current_id = _check_key_id(current_id)
        self._cache: dict[str, bytes] = {}

    def get(self, kid: str) -> bytes:
        if kid not in self._cache:
            key = self.store.get(kid)
            if key is None:
                raise KeyMissing(f"key {kid} is not in the {self.store.name} key backend")
            self._cache[kid] = key
        return self._cache[kid]


def _recovery_check(kid: str, b64: str) -> str:
    return hashlib.sha256(f"{RECOVERY_PREFIX}:{FORMAT}:{kid}:{b64}".encode()).hexdigest()[:8]


def recovery_string(kid: str, key: bytes) -> str:
    """`amem-archive-key:1:<key id>:<base64url key>:<8 hex checksum>`."""
    b64 = _b64url(key)
    return f"{RECOVERY_PREFIX}:{FORMAT}:{kid}:{b64}:{_recovery_check(kid, b64)}"


def parse_recovery_string(text: str) -> tuple[str, bytes]:
    parts = "".join(text.split()).split(":")
    if len(parts) != 5 or parts[0] != RECOVERY_PREFIX or parts[1] != str(FORMAT):
        raise ArchiveError("not an amem archive recovery string")
    _, _, kid, b64, check = parts
    _check_key_id(kid)
    if not hmac.compare_digest(check, _recovery_check(kid, b64)):
        raise ArchiveError("the recovery string's checksum does not match (a typo?)")
    try:
        key = _key_from_b64url(b64)
    except ArchiveError:
        raise ArchiveError("the recovery string holds a malformed key") from None
    return kid, key


# ---------------------------------------------------------------- sealing


def _aad(kind: str, kid: str, ident: str) -> bytes:
    return b"\x00".join((b"amem-archive", kind.encode(), bytes([FORMAT]), kid.encode(), ident.encode()))


def _seal(kind: str, keyring: Keyring, plaintext: bytes, ident: str) -> bytes:
    kid = keyring.current_id
    nonce = secrets.token_bytes(NONCE_BYTES)
    body = AESGCM(keyring.get(kid)).encrypt(nonce, zlib.compress(plaintext, 6), _aad(kind, kid, ident))
    kid_b = kid.encode("ascii")
    return _MAGIC[kind] + bytes([FORMAT, len(kid_b)]) + kid_b + nonce + body


def _unseal(kind: str, keyring: Keyring, blob: bytes, ident: str) -> bytes:
    if len(blob) < 6 or blob[:4] != _MAGIC[kind] or blob[4] != FORMAT:
        raise Undecryptable("bad header")
    end = 6 + blob[5]
    kid = blob[6:end].decode("ascii", "replace")
    if not KEY_ID_RE.fullmatch(kid):
        raise Undecryptable("bad key id")
    nonce, body = blob[end:end + NONCE_BYTES], blob[end + NONCE_BYTES:]
    if len(nonce) != NONCE_BYTES or len(body) < 16:
        raise Undecryptable("truncated")
    try:
        packed = AESGCM(keyring.get(kid)).decrypt(nonce, body, _aad(kind, kid, ident))
    except InvalidTag:
        raise Undecryptable("authentication failed (wrong key or tampered bytes)") from None
    try:
        return zlib.decompress(packed)
    except zlib.error:
        raise Undecryptable("bad content") from None


def _canon(rec: dict[str, Any]) -> bytes:
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- files


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, data: bytes, *, sync_dir: bool = True) -> None:
    """Write a mode-600 temp file next to `path`, fsync it, rename it over `path`."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)
    if sync_dir:
        _fsync_dir(path.parent)


def _append(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


@dataclass(frozen=True)
class Layout:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.jsonl"

    @property
    def index(self) -> Path:
        return self.root / "index.bin"

    @property
    def objects(self) -> Path:
        return self.root / "objects"

    @property
    def lock(self) -> Path:
        return self.root / "lock"

    def object_path(self, sha: str) -> Path:
        return self.objects / sha[:2] / f"{sha}.bin"


@contextmanager
def _lock(layout: Layout, *, exclusive: bool = True, wait: float = 10.0) -> Iterator[None]:
    """flock on `<archive>/lock`. Readers take a shared lock, and only when the file exists."""
    if not exclusive and not layout.lock.exists():
        yield
        return
    fd = os.open(layout.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ArchiveError("another archive command is running (lock held)") from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """An ISO time; one without an offset is UTC. Raises ValueError."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _ns(dt: datetime) -> int:
    return int(dt.timestamp()) * 1_000_000_000


# ---------------------------------------------------------------- config, manifest, index, objects


def load_config(layout: Layout) -> dict[str, Any] | None:
    try:
        data = json.loads(layout.config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        raise ArchiveError("config.json is not valid JSON") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("key_id"), str):
        raise ArchiveError(f"config.json is not an archive config of format {FORMAT}")
    _check_key_id(data["key_id"])
    return data


def _require(layout: Layout, store: KeyStore | None) -> Keyring:
    """The keyring of an initialized archive, with its current key present (else ArchiveError)."""
    config = load_config(layout)
    if config is None:
        raise ArchiveError("the archive is not initialized; run `amem archive init`")
    keyring = Keyring(store if store is not None else key_store(), config["key_id"])
    keyring.get(keyring.current_id)
    return keyring


def _manifest_lines(layout: Layout) -> list[bytes]:
    try:
        raw = layout.manifest.read_bytes()
    except FileNotFoundError:
        return []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def _open_line(keyring: Keyring, line: bytes) -> tuple[dict[str, Any], str]:
    """(record, sha256 of its plaintext). Raises ArchiveError (Undecryptable, KeyMissing)."""
    try:
        plain = _unseal("manifest", keyring, base64.b64decode(line, validate=True), MANIFEST_IDENT)
        rec = json.loads(plain)
    except (binascii.Error, ValueError):
        raise Undecryptable("not a manifest line") from None
    if not isinstance(rec, dict):
        raise Undecryptable("not a manifest record")
    return rec, _sha(plain)


@dataclass
class ManifestRead:
    records: list[dict[str, Any]]
    hashes: list[str | None]       # sha256 of each line's plaintext, None for a line that did not open
    bad_lines: list[int]           # 1-based: lines that did not decrypt or parse
    chain_breaks: list[int]        # 1-based: wrong seq, wrong prev, or a first line that is not `init`

    @property
    def lines(self) -> int:
        return len(self.hashes)

    @property
    def head(self) -> str:
        return (self.hashes[-1] or "") if self.hashes else ""

    @property
    def ok(self) -> bool:
        return bool(self.hashes) and not self.bad_lines and not self.chain_breaks


def read_manifest(layout: Layout, keyring: Keyring) -> ManifestRead:
    """Decrypt every line and check the hash chain. Never raises for a bad line: it is counted."""
    out = ManifestRead([], [], [], [])
    prev: str | None = ZERO_HASH
    for i, line in enumerate(_manifest_lines(layout)):
        try:
            rec, h = _open_line(keyring, line)
        except ArchiveError:
            out.bad_lines.append(i + 1)
            out.hashes.append(None)
            prev = None  # the next line's `prev` cannot be checked
            continue
        if rec.get("seq") != i or (prev is not None and rec.get("prev") != prev) \
                or (i == 0 and rec.get("type") != "init"):
            out.chain_breaks.append(i + 1)
        out.records.append(rec)
        out.hashes.append(h)
        prev = h
    return out


def _chain(keyring: Keyring, records: list[dict[str, Any]], seq: int, prev: str) -> tuple[bytes, str]:
    """Sealed manifest lines for `records` (their seq / prev are set here) and the new head."""
    out: list[bytes] = []
    for k, rec in enumerate(records):
        plain = _canon({**rec, "seq": seq + k, "prev": prev})
        out.append(base64.b64encode(_seal("manifest", keyring, plain, MANIFEST_IDENT)) + b"\n")
        prev = _sha(plain)
    return b"".join(out), prev


def _load_index(layout: Layout, keyring: Keyring) -> tuple[str, dict[str, Any] | None]:
    """("ok", data), ("missing", None) or ("damaged", None)."""
    try:
        blob = layout.index.read_bytes()
    except FileNotFoundError:
        return "missing", None
    try:
        data = json.loads(_unseal("index", keyring, blob, INDEX_IDENT))
        if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("files"), dict) \
                or not isinstance(data.get("manifest_lines"), int) or not isinstance(data.get("manifest_head"), str):
            raise ValueError
    except (ArchiveError, ValueError):
        return "damaged", None
    return "ok", data


def _write_index(layout: Layout, keyring: Keyring, files: dict[str, dict[str, Any]], lines: int, head: str,
                 last_run: str | None) -> None:
    data = {"format": FORMAT, "manifest_lines": lines, "manifest_head": head, "last_run": last_run, "files": files}
    _write_atomic(layout.index, _seal("index", keyring, _canon(data), INDEX_IDENT))


def _key_of(rec: dict[str, Any]) -> str:
    return f"{rec['source']}/{rec['path']}"


def _latest_versions(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.get("type") == "version":
            key = _key_of(rec)
            if key not in latest or rec["version"] >= latest[key]["version"]:
                latest[key] = rec
    return latest


def _files_from_manifest(records: list[dict[str, Any]], old: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Rebuild the index's `files` from the manifest. Facts of a stale index (newer mtimes,
    last_seen, pruned markers) are kept where they still agree."""
    old_files: dict[str, dict[str, Any]] = (old or {}).get("files") or {}
    old_last_run = (old or {}).get("last_run")
    files: dict[str, dict[str, Any]] = {}
    for key, rec in _latest_versions(records).items():
        entry = {"sha": rec["sha256"], "size": rec["size"], "mtime_ns": rec["mtime_ns"], "version": rec["version"],
                 "session_id": rec["session_id"], "last_seen": rec["first_seen"]}
        prior = old_files.get(key)
        if prior and prior.get("sha") == entry["sha"] and not prior.get("pruned"):
            entry.update(size=prior["size"], mtime_ns=prior["mtime_ns"], last_seen=prior.get("last_seen"))
        files[key] = entry
    for key, prior in old_files.items():
        if key not in files and old_last_run and prior.get("last_seen") == old_last_run:
            files[key] = {**prior, "pruned": True}
    return files


def _last_run_of(records: list[dict[str, Any]]) -> str | None:
    return next((r["at"] for r in reversed(records) if r.get("type") == "run"), None)


@dataclass
class _State:
    files: dict[str, dict[str, Any]]
    lines: int
    head: str
    last_run: str | None
    index: str                       # loaded | rebuilt
    manifest: ManifestRead | None = None


def _load_state(layout: Layout, keyring: Keyring) -> _State:
    """The index, checked against the manifest. Cheap when they agree (only the last line is
    opened). A stale index (a crash between the manifest write and the index write) is rebuilt
    in memory from the manifest. Truncation or a mismatch is refused. Read only."""
    lines = _manifest_lines(layout)
    if not lines:
        raise ArchiveError("the manifest is missing or empty; run `amem archive verify`")
    try:
        last, last_head = _open_line(keyring, lines[-1])
    except KeyMissing:
        raise
    except ArchiveError:
        raise ArchiveError("the key does not open this archive, or the manifest is damaged; "
                           "run `amem archive verify`") from None
    if last.get("seq") != len(lines) - 1:
        raise ArchiveError("the manifest is damaged (sequence); run `amem archive verify`")
    status, idx = _load_index(layout, keyring)
    if status == "ok" and idx is not None:
        n, head = idx["manifest_lines"], idx["manifest_head"]
        if n == len(lines):
            if head != last_head:
                raise ArchiveError("the manifest does not match the index; run `amem archive verify`")
            return _State(idx["files"], n, head, idx.get("last_run"), "loaded")
        if n > len(lines):
            cf = last.get("compacted_from") if last.get("type") == "prune" else None
            if not (isinstance(cf, dict) and cf.get("lines") == n and cf.get("head") == head):
                raise ArchiveError("the manifest has fewer lines than the index records (lines removed?); "
                                   "run `amem archive verify`")
    m = read_manifest(layout, keyring)
    if not m.ok:
        raise ArchiveError("the manifest is damaged; run `amem archive verify`")
    if status == "ok" and idx is not None and 1 <= idx["manifest_lines"] < m.lines \
            and m.hashes[idx["manifest_lines"] - 1] != idx["manifest_head"]:
        raise ArchiveError("the manifest does not match the index; run `amem archive verify`")
    files = _files_from_manifest(m.records, idx)
    last_run = (idx or {}).get("last_run") or _last_run_of(m.records)
    return _State(files, m.lines, m.head, last_run, "rebuilt", m)


def _write_object(layout: Layout, keyring: Keyring, sha: str, data: bytes) -> int:
    path = layout.object_path(sha)
    _mkdir_private(path.parent)
    blob = _seal("object", keyring, data, sha)
    _write_atomic(path, blob, sync_dir=False)
    return len(blob)


def read_object(layout: Layout, keyring: Keyring, sha: str) -> bytes:
    """The plaintext of one object; Undecryptable when it does not open or its sha256 differs."""
    try:
        blob = layout.object_path(sha).read_bytes()
    except FileNotFoundError:
        raise ArchiveError(f"object {sha} is missing") from None
    plain = _unseal("object", keyring, blob, sha)
    if _sha(plain) != sha:
        raise Undecryptable("content hash mismatch")
    return plain


def _object_files(layout: Layout) -> Iterator[tuple[str | None, Path]]:
    """(object id or None for a stray file, path) for every file below objects/."""
    if not layout.objects.is_dir():
        return
    for dirpath, _dirs, names in os.walk(layout.objects):
        for name in sorted(names):
            m = _OBJ_NAME_RE.fullmatch(name)
            sha = m.group(1) if m and Path(dirpath).name == m.group(1)[:2] else None
            yield sha, Path(dirpath) / name


# ---------------------------------------------------------------- sources


@dataclass(frozen=True)
class Source:
    source: str                 # claude | codex
    path: Path                  # absolute
    rel: str                    # relative to its root, posix
    role: str                   # main | subagent | subagent-meta | codex
    session_id: str | None      # None for Codex: read from the first line

    @property
    def key(self) -> str:
        return f"{self.source}/{self.rel}"


def _in_root(path: Path, real_root: Path) -> bool:
    return not path.is_symlink() and _inside(_real(path), real_root)


def iter_sources(projects: Path, codex: Path) -> Iterator[Source]:
    """Every transcript file below the two roots, in a stable order. Symlinks are skipped."""
    if projects.is_dir():
        real = _real(projects)
        for proj in sorted(projects.iterdir()):
            if proj.is_symlink() or not proj.is_dir():
                continue
            found: list[tuple[Path, str, str]] = [(f, "main", f.stem) for f in proj.glob("*.jsonl")]
            for f in proj.glob("*/subagents/agent-*.jsonl"):
                found.append((f, "subagent", f"{f.parent.parent.name}/{f.stem}"))
            for f in proj.glob("*/subagents/agent-*.meta.json"):
                found.append((f, "subagent-meta", f"{f.parent.parent.name}/{f.name[:-len('.meta.json')]}"))
            for f, role, sid in sorted(found):
                if _in_root(f, real):
                    yield Source("claude", f, f.relative_to(projects).as_posix(), role, sid)
    for sub in CODEX_DIRS:
        base = codex / sub
        if not base.is_dir():
            continue
        real = _real(codex)
        for f in sorted(base.rglob("rollout-*.jsonl")):
            if _in_root(f, real):
                yield Source("codex", f, f.relative_to(codex).as_posix(), "codex", None)


def codex_session_id(data: bytes, stem: str) -> str:
    """`codex:<thread id>`: the first line's `session_meta` id, else the id in the file name,
    else the file name (the `sessions` table convention, sessions_ingest.codex_row)."""
    meta: dict[str, Any] = {}
    try:
        first = json.loads(data.split(b"\n", 1)[0])
        if isinstance(first, dict) and first.get("type") == "session_meta":
            meta = codex_sessions.payload(first)
    except ValueError:
        pass
    return codex_sessions.PREFIX + (codex_sessions.thread_id_of(meta, stem) or stem)


# ---------------------------------------------------------------- init and keys


def _create_layout(layout: Layout, keyring: Keyring, now: datetime) -> None:
    layout.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(layout.root, 0o700)
    _mkdir_private(layout.objects)
    os.close(os.open(layout.lock, os.O_RDWR | os.O_CREAT, 0o600))
    stamp = _iso(now)
    blob, head = _chain(keyring, [{"type": "init", "at": stamp, "format": FORMAT}], 0, ZERO_HASH)
    _write_atomic(layout.manifest, blob)
    _write_index(layout, keyring, {}, 1, head, None)
    config = {"format": FORMAT, "key_id": keyring.current_id, "created": stamp}
    _write_atomic(layout.config, json.dumps(config, sort_keys=True, indent=1).encode())


def init(*, root: Path | None = None, key_id: str = DEFAULT_KEY_ID, reuse_key: bool = False,
         store: KeyStore | None = None, now: datetime | None = None) -> str:
    """Create the key and the archive dir. Refuses an existing archive, a non-empty dir, and an
    existing key (unless `reuse_key`). Returns the key backend name."""
    layout = Layout(root or archive_dir())
    check_archive_dir(layout.root, source_roots())
    _check_key_id(key_id)
    if layout.config.exists():
        raise ArchiveError("the archive is already initialized")
    if layout.root.exists() and any(layout.root.iterdir()):
        raise ArchiveError("the archive dir is not empty")
    store = store if store is not None else key_store()
    if store.get(key_id) is None:
        store.add(key_id, secrets.token_bytes(KEY_BYTES))
    elif not reuse_key:
        raise ArchiveError(f"key {key_id} already exists in the {store.name} backend; refusing to overwrite it "
                           "(--reuse-key starts a new archive with it)")
    _create_layout(layout, Keyring(store, key_id), now or _now())
    return store.name


def export_key(*, root: Path | None = None, store: KeyStore | None = None, key_id: str | None = None) -> str:
    """The recovery string of the archive's current key (or `key_id`)."""
    layout = Layout(root or archive_dir())
    config = load_config(layout)
    kid = key_id or (config or {}).get("key_id") or DEFAULT_KEY_ID
    keyring = Keyring(store if store is not None else key_store(), kid)
    return recovery_string(kid, keyring.get(kid))


def import_key(text: str, *, root: Path | None = None, store: KeyStore | None = None,
               now: datetime | None = None) -> tuple[str, str]:
    """Store the key of a recovery string. Checks it opens the archive when the archive uses that
    key id; creates the archive dir when there is none. Returns (key id, what happened)."""
    kid, key = parse_recovery_string(text)
    layout = Layout(root or archive_dir())
    check_archive_dir(layout.root, source_roots())
    store = store if store is not None else key_store()
    config = load_config(layout)
    if config is not None:
        candidate = Keyring(MemoryKeyStore({kid: key}), config["key_id"])
        lines = _manifest_lines(layout)
        line_kid = _line_key_id(lines[-1]) if lines else None
        if line_kid == kid:
            try:
                _open_line(candidate, lines[-1])
            except ArchiveError:
                raise ArchiveError("this key does not open the archive; nothing was stored") from None
    if config is None and layout.root.exists() and any(layout.root.iterdir()):
        raise ArchiveError("the archive dir is not empty and has no config.json; nothing was stored")
    existing = store.get(kid)
    if existing is not None and not hmac.compare_digest(existing, key):
        raise ArchiveError(f"a different key {kid} already exists in the {store.name} backend; "
                           "refusing to overwrite it")
    action = "already-present" if existing is not None else "stored"
    if existing is None:
        store.add(kid, key)
    if config is None:
        _create_layout(layout, Keyring(store, kid), now or _now())
        action += "+archive-created"
    return kid, action


def _line_key_id(line: bytes) -> str | None:
    try:
        blob = base64.b64decode(line, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(blob) < 6:
        return None
    kid = blob[6:6 + blob[5]].decode("ascii", "replace")
    return kid if KEY_ID_RE.fullmatch(kid) else None


# ---------------------------------------------------------------- run


@dataclass
class RunReport:
    files: int = 0          # regular transcript files seen
    new_versions: int = 0   # files whose content is new for their path
    unchanged: int = 0
    new_objects: int = 0    # distinct new contents (a resumed copy of stored bytes adds none)
    bytes_seen: int = 0     # size of every file seen
    bytes_new: int = 0      # plaintext size of the new versions
    bytes_stored: int = 0   # encrypted size of the new objects
    skipped: int = 0        # not a regular file, or over MAX_FILE_BYTES
    vanished: int = 0       # deleted between the listing and the read
    errors: int = 0         # could not be read
    index: str = "none"     # loaded | rebuilt | none (dry run without a readable index)
    dry_run: bool = False

    def line(self) -> str:
        head = "archive run (dry run, nothing written)" if self.dry_run else "archive run"
        parts = [f"files={self.files}", f"new_versions={self.new_versions}", f"unchanged={self.unchanged}",
                 f"new_objects={self.new_objects}", f"bytes_seen={self.bytes_seen}", f"bytes_new={self.bytes_new}"]
        if not self.dry_run:
            parts.append(f"bytes_stored={self.bytes_stored}")
        parts += [f"skipped={self.skipped}", f"vanished={self.vanished}", f"errors={self.errors}",
                  f"index={self.index}"]
        return f"{head}: " + " ".join(parts)


def _read_file(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def run(*, root: Path | None = None, projects: Path | None = None, codex: Path | None = None,
        dry_run: bool = False, now: datetime | None = None, store: KeyStore | None = None) -> RunReport:
    """Archive every new version of every transcript. `dry_run` writes nothing, needs no key, and
    still counts what would be archived (against the index when the key can open it)."""
    layout = Layout(root or archive_dir())
    proj, cdx = projects_dir(projects), codex_home(codex)
    check_archive_dir(layout.root, source_roots(proj, cdx))
    stamp = _iso(now or _now())
    rep = RunReport(dry_run=dry_run)
    if dry_run:
        files = _dry_run_files(layout, store)
        rep.index = "none" if files is None else "loaded"
        _scan(rep, layout, None, files or {}, proj, cdx, stamp)
        return rep
    keyring = _require(layout, store)
    with _lock(layout):
        state = _load_state(layout, keyring)
        rep.index = state.index
        _mkdir_private(layout.objects)
        records = _scan(rep, layout, keyring, state.files, proj, cdx, stamp)
        lines, head = state.lines, state.head
        if records:
            records.append({"type": "run", "at": stamp, "files": rep.files, "new_versions": rep.new_versions,
                            "new_objects": rep.new_objects, "bytes_new": rep.bytes_new})
            blob, head = _chain(keyring, records, lines, head)
            _append(layout.manifest, blob)
            lines += len(records)
        _write_index(layout, keyring, state.files, lines, head, stamp)
    return rep


def _dry_run_files(layout: Layout, store: KeyStore | None) -> dict[str, dict[str, Any]] | None:
    """A copy of the index's files when the archive exists and the key opens it, else None."""
    if not layout.config.exists():
        return None
    try:
        state = _load_state(layout, _require(layout, store))
    except ArchiveError:
        return None
    return json.loads(json.dumps(state.files))


def _scan(rep: RunReport, layout: Layout, keyring: Keyring | None, files: dict[str, dict[str, Any]],
          projects: Path, codex: Path, stamp: str) -> list[dict[str, Any]]:
    """Count (and, with a keyring, write) every new version. Returns the new version records."""
    write = keyring is not None
    records: list[dict[str, Any]] = []
    seen_objects: set[str] = set()
    touched: set[Path] = set()
    for src in iter_sources(projects, codex):
        try:
            st = os.lstat(src.path)
        except FileNotFoundError:
            rep.vanished += 1
            continue
        except OSError:
            rep.errors += 1
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            rep.skipped += 1
            continue
        rep.files += 1
        rep.bytes_seen += st.st_size
        entry = files.get(src.key)
        if entry and entry["size"] == st.st_size and entry["mtime_ns"] == st.st_mtime_ns:
            rep.unchanged += 1
            if write:
                entry["last_seen"] = stamp
            continue
        try:
            data = _read_file(src.path)
        except FileNotFoundError:
            rep.vanished += 1
            continue
        except OSError:
            rep.errors += 1
            continue
        sha = _sha(data)
        if entry and entry["sha"] == sha:
            rep.unchanged += 1  # touched, or the stat changed back and forth: same content
            if write:
                entry.update(size=len(data), mtime_ns=st.st_mtime_ns, last_seen=stamp)
            continue
        rep.new_versions += 1
        rep.bytes_new += len(data)
        if sha not in seen_objects and not layout.object_path(sha).exists():
            rep.new_objects += 1
            if write:
                rep.bytes_stored += _write_object(layout, keyring, sha, data)  # type: ignore[arg-type]
                touched.add(layout.object_path(sha).parent)
        seen_objects.add(sha)
        if not write:
            continue
        sid = src.session_id or codex_session_id(data, src.path.stem)
        version = (entry["version"] if entry else 0) + 1
        rec: dict[str, Any] = {
            "type": "version", "source": src.source, "path": src.rel, "role": src.role, "session_id": sid,
            "sha256": sha, "size": len(data), "mtime_ns": st.st_mtime_ns,
            "birthtime": getattr(st, "st_birthtime", None), "first_seen": stamp, "version": version,
        }
        if entry and not entry.get("pruned"):
            rec["prev_version_last_seen"] = entry.get("last_seen")
        records.append(rec)
        files[src.key] = {"sha": sha, "size": len(data), "mtime_ns": st.st_mtime_ns, "version": version,
                          "session_id": sid, "last_seen": stamp}
    for d in touched:
        _fsync_dir(d)
    if touched:
        _fsync_dir(layout.objects)
    return records


# ---------------------------------------------------------------- verify


@dataclass
class VerifyReport:
    manifest_lines: int = 0
    versions: int = 0
    objects: int = 0              # distinct objects the manifest references
    checked: int = 0              # objects decrypted and hashed
    missing: int = 0
    bad: int = 0                  # did not decrypt, or the sha256 differs
    manifest_bad: int = 0         # lines that did not decrypt or parse
    chain_breaks: int = 0
    index_problems: int = 0
    orphans: int = 0              # files below objects/ that nothing references (info only)
    problem_ids: list[str] = field(default_factory=list)
    problem_lines: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.bad or self.manifest_bad or self.chain_breaks or self.index_problems)

    def lines(self) -> list[str]:
        out = [f"archive verify: {'ok' if self.ok else 'PROBLEM'} manifest_lines={self.manifest_lines} "
               f"versions={self.versions} objects={self.objects} checked={self.checked} missing={self.missing} "
               f"bad={self.bad} manifest_bad={self.manifest_bad} chain_breaks={self.chain_breaks} "
               f"index_problems={self.index_problems} orphans={self.orphans}"]
        if self.problem_lines:
            out.append("manifest lines: " + ",".join(str(n) for n in self.problem_lines[:50]))
        if self.problem_ids:
            out.append("objects: " + " ".join(self.problem_ids[:50]))
        return out


def verify(*, root: Path | None = None, sample: int | None = None, store: KeyStore | None = None,
           rng: random.Random | None = None) -> VerifyReport:
    """Decrypt every (or `sample` random) referenced object and check its sha256; check the
    manifest chain; check the index agrees with the manifest."""
    layout = Layout(root or archive_dir())
    check_archive_dir(layout.root, source_roots())
    keyring = _require(layout, store)
    rep = VerifyReport()
    with _lock(layout, exclusive=False):
        m = read_manifest(layout, keyring)
        rep.manifest_lines = m.lines
        rep.manifest_bad = len(m.bad_lines)
        rep.chain_breaks = len(m.chain_breaks) + (0 if m.lines else 1)
        rep.problem_lines = sorted(set(m.bad_lines) | set(m.chain_breaks))
        versions = [r for r in m.records if r.get("type") == "version"]
        rep.versions = len(versions)
        rep.index_problems = _index_problems(layout, keyring, m)
        referenced = sorted({r["sha256"] for r in versions})
        rep.objects = len(referenced)
        present = []
        for sha in referenced:
            if layout.object_path(sha).is_file():
                present.append(sha)
            else:
                rep.missing += 1
                rep.problem_ids.append(sha)
        if sample is not None and sample < len(present):
            present = (rng or random.SystemRandom()).sample(present, sample)
        for sha in present:
            rep.checked += 1
            try:
                read_object(layout, keyring, sha)
            except ArchiveError:
                rep.bad += 1
                rep.problem_ids.append(sha)
        live = set(referenced)
        rep.orphans = sum(1 for sha, _p in _object_files(layout) if sha not in live)
    return rep


def _index_problems(layout: Layout, keyring: Keyring, m: ManifestRead) -> int:
    status, idx = _load_index(layout, keyring)
    if status != "ok" or idx is None:
        return 1
    problems = 0
    if idx["manifest_lines"] != m.lines or idx["manifest_head"] != m.head:
        problems += 1
    latest = _latest_versions(m.records)
    files: dict[str, dict[str, Any]] = idx["files"]
    for key, rec in latest.items():
        entry = files.get(key)
        if entry is None or entry.get("pruned") or entry.get("sha") != rec["sha256"] \
                or entry.get("version") != rec["version"]:
            problems += 1
    for key, entry in files.items():
        if key not in latest and not entry.get("pruned"):
            problems += 1
    return problems


# ---------------------------------------------------------------- restore


@dataclass
class RestoreReport:
    files: list[str]       # written, relative to the target dir
    skipped: int           # paths of the session with no version matching the selection


def _safe_rel(rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        raise ArchiveError("refusing an unsafe relative path in the manifest")
    return p


def restore(session_id: str, to: Path, *, version: int | str = "latest", as_of: datetime | None = None,
            root: Path | None = None, store: KeyStore | None = None) -> RestoreReport:
    """Write the selected version of every file of a session below `to` (at its path relative to
    its source root), mode 600, with the recorded mtime. Never overwrites a file."""
    layout = Layout(root or archive_dir())
    roots = source_roots()
    check_archive_dir(layout.root, roots)
    check_restore_dir(to, layout.root, roots)
    keyring = _require(layout, store)
    with _lock(layout, exclusive=False):
        m = read_manifest(layout, keyring)
        if not m.ok:
            raise ArchiveError("the manifest is damaged; run `amem archive verify`")
        groups: dict[str, list[dict[str, Any]]] = {}
        for rec in m.records:
            if rec.get("type") == "version" and rec.get("session_id") == session_id:
                groups.setdefault(_key_of(rec), []).append(rec)
        if not groups:
            raise ArchiveError("no archived versions for that session id")
        picked: list[dict[str, Any]] = []
        for key in sorted(groups):
            vs = sorted(groups[key], key=lambda r: r["version"])
            if as_of is not None:
                vs = [r for r in vs if parse_iso(r["first_seen"]) <= as_of]
            elif version != "latest":
                vs = [r for r in vs if r["version"] == version]
            if vs:
                picked.append(vs[-1])
        if not picked:
            raise ArchiveError("no archived version of that session matches --version / --as-of")
        real_to = _real(to)
        forbidden = [*roots, _real(layout.root), _real(default_archive_dir())]
        plan: list[tuple[dict[str, Any], Path, str]] = []
        for rec in picked:
            rel = _safe_rel(rec["path"])
            target = real_to / rel
            real_target = _real(target)
            if not _inside(real_target, real_to) or any(_inside(real_target, f) for f in forbidden):
                raise ArchiveError("refusing a restore target outside the target dir or inside a source root")
            if os.path.lexists(target):
                raise ArchiveError(f"refusing to overwrite {rel.as_posix()}")
            plan.append((rec, target, rel.as_posix()))
        payloads = [(rec, target, rel, read_object(layout, keyring, rec["sha256"])) for rec, target, rel in plan]
    to.mkdir(parents=True, exist_ok=True, mode=0o700)
    written: list[str] = []
    for rec, target, rel, data in payloads:
        parent = target.parent
        missing = []
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for d in reversed(missing):
            d.mkdir(mode=0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.utime(target, ns=(rec["mtime_ns"], rec["mtime_ns"]))
        written.append(rel)
    return RestoreReport(written, len(groups) - len(picked))


# ---------------------------------------------------------------- prune


@dataclass
class PruneReport:
    cutoff: str
    paths_removed: int = 0       # source paths whose whole history expired
    versions_removed: int = 0
    versions_kept: int = 0
    events_removed: int = 0      # run / prune records older than the cutoff
    objects_removed: int = 0
    bytes_freed: int = 0
    compacted: bool = False
    dry_run: bool = False

    def line(self) -> str:
        head = "archive prune (dry run, nothing written)" if self.dry_run else "archive prune"
        return (f"{head}: cutoff={self.cutoff} paths_removed={self.paths_removed} "
                f"versions_removed={self.versions_removed} versions_kept={self.versions_kept} "
                f"events_removed={self.events_removed} objects_removed={self.objects_removed} "
                f"bytes_freed={self.bytes_freed} compacted={'yes' if self.compacted else 'no'}")


def prune(*, days: int = DEFAULT_DAYS, dry_run: bool = False, now: datetime | None = None,
          root: Path | None = None, store: KeyStore | None = None) -> PruneReport:
    """Retention by the source's last modification: a source path whose newest mtime (over its
    versions and the index) is older than `days` loses its whole history. A compacted manifest
    (re-chained, with a `prune` record) replaces the old one atomically, then unreferenced
    objects are deleted. Nothing to remove: only a `prune` record is appended."""
    if days < 1:
        raise ArchiveError("--days must be at least 1")
    layout = Layout(root or archive_dir())
    check_archive_dir(layout.root, source_roots())
    keyring = _require(layout, store)
    now = now or _now()
    cutoff = now - timedelta(days=days)
    cutoff_ns = _ns(cutoff)
    rep = PruneReport(cutoff=_iso(cutoff), dry_run=dry_run)
    with _lock(layout, exclusive=not dry_run):
        state = _load_state(layout, keyring)
        m = state.manifest or read_manifest(layout, keyring)
        if not m.ok:
            raise ArchiveError("the manifest is damaged; run `amem archive verify`")
        last_mod: dict[str, int] = {}
        for rec in m.records:
            if rec.get("type") == "version":
                key = _key_of(rec)
                last_mod[key] = max(last_mod.get(key, 0), rec["mtime_ns"])
        for key, entry in state.files.items():
            if key in last_mod and not entry.get("pruned"):
                last_mod[key] = max(last_mod[key], entry["mtime_ns"])
        expired = {key for key, ns in last_mod.items() if ns < cutoff_ns}
        keep: list[dict[str, Any]] = []
        for rec in m.records:
            kind = rec.get("type")
            if kind == "version" and _key_of(rec) in expired:
                rep.versions_removed += 1
            elif kind in ("run", "prune") and parse_iso(rec["at"]) < cutoff:
                rep.events_removed += 1
            else:
                keep.append({k: v for k, v in rec.items() if k not in ("seq", "prev")})
                if kind == "version":
                    rep.versions_kept += 1
        rep.paths_removed = len(expired)
        live = {r["sha256"] for r in keep if r.get("type") == "version"}
        doomed = [p for sha, p in _object_files(layout) if sha not in live]
        rep.objects_removed = len(doomed)
        rep.bytes_freed = sum(p.stat().st_size for p in doomed)
        rep.compacted = bool(rep.versions_removed or rep.events_removed)
        if dry_run:
            return rep
        event: dict[str, Any] = {"type": "prune", "at": _iso(now), "days": days, "cutoff": rep.cutoff,
                                 "removed_versions": rep.versions_removed, "removed_objects": rep.objects_removed}
        if rep.compacted:
            event["compacted_from"] = {"lines": m.lines, "head": m.head}
            blob, head = _chain(keyring, [*keep, event], 0, ZERO_HASH)
            _write_atomic(layout.manifest, blob)
            lines = len(keep) + 1
        else:
            blob, head = _chain(keyring, [event], m.lines, m.head)
            _append(layout.manifest, blob)
            lines = m.lines + 1
        files = state.files
        for key in list(files):
            entry = files[key]
            still_at_source = bool(state.last_run) and entry.get("last_seen") == state.last_run
            if key in expired or entry.get("pruned"):
                if still_at_source:
                    files[key] = {**entry, "pruned": True}  # unchanged at the source: not archived again
                else:
                    del files[key]
        _write_index(layout, keyring, files, lines, head, state.last_run)
        for p in doomed:
            p.unlink(missing_ok=True)
        for sub in layout.objects.iterdir() if layout.objects.is_dir() else []:
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
    return rep


# ---------------------------------------------------------------- status


@dataclass
class StatusReport:
    initialized: bool
    key: bool
    objects: int = 0
    bytes_stored: int = 0
    versions: int | None = None
    sessions: int | None = None
    paths: int | None = None
    gone: int | None = None          # paths the last run no longer saw at the source
    last_run: str | None = None
    manifest_ok: bool | None = None

    def line(self) -> str:
        def v(x: object) -> str:
            return "?" if x is None else str(x)
        out = (f"archive status: initialized={'yes' if self.initialized else 'no'} "
               f"key={'yes' if self.key else 'no'} versions={v(self.versions)} sessions={v(self.sessions)} "
               f"paths={v(self.paths)} gone_at_source={v(self.gone)} objects={self.objects} "
               f"bytes_stored={self.bytes_stored} last_run={self.last_run or ('never' if self.key else '?')}")
        if self.manifest_ok is False:
            out += " manifest=damaged"
        return out


def status(*, root: Path | None = None, store: KeyStore | None = None) -> StatusReport:
    """Counts, size, last run and whether the key is present. Without the key: what the
    filesystem shows (object count and size) only."""
    layout = Layout(root or archive_dir())
    check_archive_dir(layout.root, source_roots())
    config = load_config(layout)
    objs = [p for _sha, p in _object_files(layout)]
    rep = StatusReport(initialized=config is not None, key=False, objects=len(objs),
                       bytes_stored=sum(p.stat().st_size for p in objs)
                       + sum(p.stat().st_size for p in (layout.manifest, layout.index) if p.exists()))
    if config is None:
        return rep
    try:
        keyring = Keyring(store if store is not None else key_store(), config["key_id"])
        keyring.get(keyring.current_id)
    except ArchiveError:
        return rep
    rep.key = True
    with _lock(layout, exclusive=False):
        m = read_manifest(layout, keyring)
        _st, idx = _load_index(layout, keyring)
    rep.manifest_ok = m.ok
    versions = [r for r in m.records if r.get("type") == "version"]
    rep.versions = len(versions)
    rep.sessions = len({r["session_id"] for r in versions})
    rep.paths = len({_key_of(r) for r in versions})
    rep.last_run = (idx or {}).get("last_run") or _last_run_of(m.records)
    if idx is not None and rep.last_run:
        rep.gone = sum(1 for key, e in idx["files"].items()
                       if not e.get("pruned") and e.get("last_seen") != rep.last_run)
    return rep
