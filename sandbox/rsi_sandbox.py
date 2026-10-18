#!/usr/bin/env python3
"""rsi sandbox: run agent config experiments in isolated Docker workers.

A CLI and an importable library. Stdlib only. Design and threat model: docs/sandbox.md.
The root is $RSI_SANDBOX_DIR when set, else ~/sandbox (the CLI and `Sandbox()` both honor it).

    rsi_sandbox.py init
    rsi_sandbox.py build
    rsi_sandbox.py net-up | net-down | net-status
    rsi_sandbox.py prepare-arm --experiment E --baseline B --arm A [--artifact H] [--hook-experiment] [--force]
    rsi_sandbox.py record-eval --artifact H --experiment E --arm B --suite S --verdict V [--results FILE] ...
    rsi_sandbox.py run --experiment E --arm A [--timeout S] [--credentials SPEC] -- CMD ...
    rsi_sandbox.py cleanup [--max-age-hours N]
    rsi_sandbox.py recover

A worker container has: a read-only rootfs, tmpfs /tmp, two writable bind mounts
(the arm's home and one run dir), no capabilities, no-new-privileges, cgroup limits,
a wall-clock timeout, and only the `rsi-internal` network. That network is
`internal: true`, so the only way out is the egress proxy (images/egress).
"""
from __future__ import annotations

import argparse
import calendar
import dataclasses
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import tomllib
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGES_DIR = HERE / "images"
LOCK_FILE = IMAGES_DIR / "lock.json"

# The real home from the password database, not $HOME, so a changed HOME cannot move the guard.
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
DEFAULT_ROOT = REAL_HOME / "sandbox"
ROOT_ENV = "RSI_SANDBOX_DIR"          # overrides DEFAULT_ROOT (tests and demos use a temp dir)
DEFAULT_ARCHIVE_DIR = REAL_HOME / "Library" / "Application Support" / "amem-archive"
LAYOUT = ("baselines", "experiments", "artifacts", "stack", "db", "briefs", "run")

IMAGE_TAGS = {"worker": "rsi-worker:m003", "egress": "rsi-egress:m003"}
NET_INTERNAL = "rsi-internal"
NET_EGRESS = "rsi-egress"
PROXY_NAME = "rsi-egress-proxy"
PROXY_PORT = 3128
PROXY_URL = f"http://{PROXY_NAME}:{PROXY_PORT}"
# A worker joins rsi-internal (egress only through the proxy) or no network at all (a stub agent, a check).
RUN_NETWORKS = (NET_INTERNAL, "none")
WORKER_UID = 10001
PROXY_UID = 10002
LABEL = "rsi.sandbox"

# The only variable names the runner ever sets in a worker. Values are fixed by the runner.
ENV_ALLOWLIST = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "HOME", "CLAUDE_CONFIG_DIR",
                 "CODEX_HOME", "EXPERIMENT_ID", "RSI_ARM")
# Named credentials a provider may supply. No OAuth token: production uses a dedicated API key.
CREDENTIAL_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")
# The only Keychain items the runner reads: the dedicated, spend-capped sandbox keys. A mistyped spec can
# then never pull a personal login (for example Claude Code's own Keychain item) into a worker.
KEYCHAIN_ITEMS = (("rsi-sandbox", "anthropic"), ("rsi-sandbox", "openai"))

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ARM_RE = re.compile(r"^[A-Z]$")
RO_TARGET_RE = re.compile(r"^/(mnt|baseline)(/[A-Za-z0-9_.-]+)*$")

# RSI V1 formats (docs/rsi/spec.md). Same canonical JSON as agent-config lib/rsi_artifact.py.
ARTIFACT_FORMAT = "rsi-artifact/1"
BASELINE_FORMAT = "rsi-baseline/1"
EVAL_FORMAT = "rsi-eval/1"
VERDICTS = ("pass", "improves", "no-effect", "worse", "fail", "inconclusive")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SUITE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")

DEFAULT_TIMEOUT_S = 1200
MAX_TIMEOUT_S = 4 * 3600
KILL_GRACE_S = 30


class SandboxError(RuntimeError):
    pass


class MountRefused(ValueError):
    """A bind mount the guard will not allow."""


class CredentialError(ValueError):
    pass


# ------------------------------------------------------------------ path guard

def _key(p: Path | str) -> str:
    # APFS is case-insensitive and normalization-insensitive: compare that way.
    return unicodedata.normalize("NFC", str(p)).casefold().rstrip("/") or "/"


def _within(child: Path | str, parent: Path | str) -> bool:
    c, p = _key(child), _key(parent)
    return c == p or c.startswith(p.rstrip("/") + "/") or p == "/"


def forbidden_paths() -> list[Path]:
    """Host paths that must never be mounted, nor any path above or below them."""
    h = REAL_HOME
    paths = [h / ".claude", h / ".claude.json", h / ".codex", h / ".agents", h / "agent-config",
             h / "Projects", h / "Library", h / ".ssh", h / ".config", h / ".docker", h / ".gnupg",
             h / ".aws", h / ".kube", h / ".netrc", h / ".npmrc", h / ".git-credentials",
             DEFAULT_ARCHIVE_DIR, Path("/var/run/docker.sock"), Path("/run/docker.sock")]
    if os.environ.get("AMEM_ARCHIVE_DIR"):
        paths.append(Path(os.environ["AMEM_ARCHIVE_DIR"]).expanduser())
    out = []
    for p in paths:
        out.append(Path(os.path.abspath(p)))
        out.append(Path(os.path.realpath(p)))
    return out


def check_mount(source: Path | str, allowed_rw: list[Path], allowed_ro: list[Path],
                read_only: bool) -> Path:
    """Return the resolved source if the guard allows this bind mount, else MountRefused."""
    raw = str(source)
    if not raw or any(c in raw for c in ("\x00", "\n", "\r", ",", "=")) or ":" in raw:
        raise MountRefused(f"unsafe characters in mount path: {raw!r}")
    if not os.path.isabs(raw):
        raise MountRefused(f"mount path must be absolute: {raw}")
    absolute = Path(os.path.abspath(raw))
    real = Path(os.path.realpath(raw))
    for cand in (absolute, real):
        if ".env" in _key(cand):
            raise MountRefused(f"path contains .env: {raw}")
        for bad in forbidden_paths():
            if _within(cand, bad) or _within(bad, cand):
                raise MountRefused(f"forbidden host path: {raw}")
    roots = list(allowed_rw) + (list(allowed_ro) if read_only else [])
    if not any(_within(real, Path(os.path.realpath(r))) for r in roots):
        kind = "read-only" if read_only else "writable"
        raise MountRefused(f"{kind} mount outside the allowed sandbox dirs: {raw}")
    if not real.is_dir():
        raise MountRefused(f"mount source must be an existing directory: {raw}")
    return real


def _check_id(value: str, what: str) -> str:
    if not ID_RE.match(value or "") or ".." in value:
        raise SandboxError(f"bad {what}: {value!r}")
    return value


def _check_arm(arm: str) -> str:
    if not ARM_RE.match(arm or ""):
        raise SandboxError(f"bad arm: {arm!r} (one capital letter, e.g. A or B)")
    return arm


# ------------------------------------------------------------------ credentials

def load_credentials(spec: str, name: str | None = None, runner=subprocess.run) -> dict[str, str]:
    """Credential provider. spec: none | env-file:<path> | keychain:<service>/<account>.

    Returns {NAME: value}. Values never go to argv, logs or files.
    """
    spec = spec or "none"
    if spec == "none":
        return {}
    if spec.startswith("env-file:"):
        return _env_file_credentials(spec[len("env-file:"):])
    if spec.startswith("keychain:"):
        service, sep, account = spec[len("keychain:"):].partition("/")
        if not sep or not service or not account:
            raise CredentialError("keychain spec is keychain:<service>/<account>")
        if (service, account) not in KEYCHAIN_ITEMS:
            raise CredentialError("keychain item not allowed: use " +
                                  " or ".join(f"keychain:{s}/{a}" for s, a in KEYCHAIN_ITEMS))
        cred_name = name or "ANTHROPIC_API_KEY"
        if cred_name not in CREDENTIAL_NAMES:
            raise CredentialError(f"credential name not allowed: {cred_name}")
        proc = runner(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                      capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise CredentialError(f"keychain item not found: {service}/{account}")
        return {cred_name: proc.stdout.strip()}
    raise CredentialError(f"unknown credential provider: {spec.split(':', 1)[0]}")


def _env_file_credentials(path_s: str) -> dict[str, str]:
    path = Path(path_s).expanduser()
    if path.name.casefold().startswith(".env"):
        raise CredentialError("env-file provider refuses .env* files; use a dedicated dummy file")
    real = Path(os.path.realpath(path))
    for bad in forbidden_paths():
        if _within(real, bad) or _within(Path(os.path.abspath(path)), bad):
            raise CredentialError(f"env-file is inside a forbidden host path: {path}")
    if not real.is_file() or real.stat().st_size > 65536:
        raise CredentialError(f"env-file missing or too large: {path}")
    creds: dict[str, str] = {}
    for line in real.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not sep or k not in CREDENTIAL_NAMES:
            raise CredentialError(f"env-file entry not allowed: {k or '<blank>'}")
        creds[k] = v
    return creds


# ------------------------------------------------------------------ RSI baselines and artifacts

def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _home_rel(key: str) -> str:
    """'~/.claude/CLAUDE.md' -> '.claude/CLAUDE.md'. Anything else (absolute, '..', empty parts) is refused."""
    if not isinstance(key, str) or not key.startswith("~/"):
        raise SandboxError(f"path must start with ~/: {key!r}")
    rel = key[2:]
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts) or "\x00" in rel:
        raise SandboxError(f"unsafe path: {key!r}")
    return rel


def read_baseline(bdir: Path) -> tuple[dict | None, Path]:
    """(manifest, dir to copy). An rsi-baseline/1 dir is verified (id, every copy, no extra files) and its
    files/ subtree is the home. A dir without manifest.json is a raw home snapshot: (None, bdir)."""
    mf = bdir / "manifest.json"
    if not mf.exists():
        return None, bdir
    try:
        m = json.loads(mf.read_text())
    except (OSError, ValueError):
        raise SandboxError(f"baseline {bdir.name}: unreadable manifest") from None
    if not isinstance(m, dict) or m.get("format") != BASELINE_FORMAT or not isinstance(m.get("files"), list):
        raise SandboxError(f"baseline {bdir.name}: not {BASELINE_FORMAT}")
    body = {k: v for k, v in m.items() if k != "baseline_id"}
    if m.get("baseline_id") != bdir.name or bdir.name != "bl-" + _sha(canonical(body))[:12]:
        raise SandboxError(f"baseline {bdir.name}: id does not match its manifest")
    root = bdir / "files"
    listed = set()
    for f in m["files"]:
        rel = _home_rel(f.get("path"))
        copy = root / rel
        if copy.is_symlink() or not copy.is_file() or _sha(copy.read_bytes()) != f.get("sha256"):
            raise SandboxError(f"baseline {bdir.name}: copy of {f.get('path')} is missing or changed")
        listed.add(rel)
    found = {p.relative_to(root).as_posix() for p in root.rglob("*") if not p.is_dir()} if root.is_dir() else set()
    if found != listed:
        raise SandboxError(f"baseline {bdir.name}: unlisted files {sorted(found - listed)[:5]}")
    return m, root


def load_artifact(adir: Path) -> tuple[dict, str, dict[str, bytes]]:
    """(manifest, hash, {path: after bytes}). The dir name, the canonical manifest and every after file
    must verify; otherwise SandboxError."""
    try:
        raw = (adir / "manifest.json").read_bytes()
        m = json.loads(raw)
    except (OSError, ValueError):
        raise SandboxError(f"artifact {adir.name}: unreadable manifest") from None
    if not isinstance(m, dict) or m.get("format") != ARTIFACT_FORMAT or not isinstance(m.get("files"), list):
        raise SandboxError(f"artifact {adir.name}: not {ARTIFACT_FORMAT}")
    if raw.rstrip(b"\n") != canonical(m):
        raise SandboxError(f"artifact {adir.name}: manifest is not canonical JSON")
    h = _sha(canonical(m))
    if adir.name != h:
        raise SandboxError("artifact dir name does not equal its manifest hash")
    after = {}
    for f in m["files"]:
        _home_rel(f.get("path"))
        sha = str(f.get("after_sha256", ""))
        if not HEX64_RE.match(sha):
            raise SandboxError(f"artifact {h[:12]}: bad after_sha256 for {f.get('path')}")
        try:
            b = (adir / "files" / sha).read_bytes()
        except OSError:
            raise SandboxError(f"artifact {h[:12]}: after bytes missing for {f.get('path')}") from None
        if _sha(b) != sha:
            raise SandboxError(f"artifact {h[:12]}: after bytes of {f.get('path')} changed since staging")
        after[f["path"]] = b
    return m, h, after


def _file_mode(mode) -> int:
    try:
        return int(str(mode), 8) & 0o777
    except (TypeError, ValueError):
        return 0o644


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ------------------------------------------------------------------ docker helpers

def _docker_cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env for the docker CLI process. Never the full host env."""
    env = {k: os.environ[k] for k in ("PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                                      "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY") if k in os.environ}
    env["HOME"] = str(REAL_HOME)
    env["PATH"] = env.get("PATH", "") + ":/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
    env["DOCKER_CLI_HINTS"] = "false"
    env.update(extra or {})
    return env


def docker(*args: str, check: bool = True, timeout: float = 120, input_text: str | None = None,
           ) -> subprocess.CompletedProcess:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                          env=_docker_cli_env(), input=input_text,
                          stdin=None if input_text is not None else subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise SandboxError(f"docker {args[0]} failed: {proc.stderr.strip()[-500:]}")
    return proc


def docker_available() -> bool:
    if not shutil.which("docker", path=_docker_cli_env()["PATH"]):
        return False
    try:
        return docker("info", "--format", "{{.ServerVersion}}", check=False, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def load_lock() -> dict | None:
    try:
        return json.loads(LOCK_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def image_id(kind: str) -> str:
    lock = load_lock()
    if not lock or kind not in lock:
        raise SandboxError(f"no {kind} image in {LOCK_FILE}; run `rsi_sandbox.py build`")
    return lock[kind]["id"]


# ------------------------------------------------------------------ results

@dataclasses.dataclass
class RunResult:
    experiment: str
    arm: str
    run_id: str
    container: str
    exit_code: int | None
    timed_out: bool
    duration_s: float
    stdout: str
    stderr: str
    run_dir: str
    log_dir: str
    argv: list[str]          # docker argv; credentials appear as names only

    def to_json(self, max_output: int = 4000) -> dict:
        d = dataclasses.asdict(self)
        d["stdout"] = self.stdout[-max_output:] if max_output > 0 else ""
        d["stderr"] = self.stderr[-max_output:] if max_output > 0 else ""
        return d


# ------------------------------------------------------------------ the sandbox

def default_root() -> Path:
    """$RSI_SANDBOX_DIR when set, else ~/sandbox (the production staging root)."""
    return Path(os.environ.get(ROOT_ENV) or DEFAULT_ROOT).expanduser()


class Sandbox:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(os.path.abspath(Path(root or default_root()).expanduser()))
        for bad in forbidden_paths():
            if _within(self.root, bad) or _within(bad, self.root):
                raise SandboxError(f"sandbox root overlaps a forbidden host path: {self.root}")
        self.baselines = self.root / "baselines"
        self.artifacts = self.root / "artifacts"
        self.experiments = self.root / "experiments"
        self.rundir = self.root / "run"

    # ---- layout

    def init(self) -> list[str]:
        made = []
        for d in (self.root, *(self.root / n for n in LAYOUT), self.rundir / "containers"):
            if not d.exists():
                made.append(str(d))
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        return made

    def experiment_dir(self, experiment: str) -> Path:
        return self.experiments / _check_id(experiment, "experiment id")

    def arm_home(self, experiment: str, arm: str) -> Path:
        return self.experiment_dir(experiment) / f"home-{_check_arm(arm)}"

    def baseline_dir(self, baseline: str) -> Path:
        return self.baselines / _check_id(baseline, "baseline id")

    # ---- locks (in run/)

    def acquire_lock(self, name: str, ttl_s: float) -> Path:
        self.rundir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.rundir / f"{_check_id(name, 'lock name')}.lock"
        body = json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                           "created": time.time(), "expires": time.time() + ttl_s})
        for attempt in (1, 2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if attempt == 1 and self._lock_stale(path):
                    path.unlink(missing_ok=True)
                    continue
                raise SandboxError(f"locked: {path.name} (another run is active)") from None
            with os.fdopen(fd, "w") as f:
                f.write(body)
            return path
        raise SandboxError(f"locked: {path.name}")

    @staticmethod
    def _lock_stale(path: Path) -> bool:
        try:
            info = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return True
        if time.time() > float(info.get("expires", 0)):
            return True
        if info.get("host") == socket.gethostname():
            try:
                os.kill(int(info["pid"]), 0)
            except ProcessLookupError:
                return True
            except (PermissionError, KeyError, ValueError):
                return False
        return False

    # ---- images

    def build(self, log=sys.stderr) -> dict:
        lock: dict = {"schema": 1, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        for kind in ("egress", "worker"):
            ctx = IMAGES_DIR / kind
            proc = subprocess.run(["docker", "build", "--provenance=false", "--sbom=false", "--label", f"{LABEL}=1", "-t", IMAGE_TAGS[kind], str(ctx)],
                                  env=_docker_cli_env(), stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=log, timeout=3600)
            if proc.returncode != 0:
                raise SandboxError(f"docker build {kind} failed")
            iid = docker("image", "inspect", "--format", "{{.Id}}", IMAGE_TAGS[kind]).stdout.strip()
            base = re.search(r"^FROM\s+(\S+)", (ctx / "Dockerfile").read_text(), re.M).group(1)
            lock[kind] = {"tag": IMAGE_TAGS[kind], "id": iid, "base": base}
        lock["egress"]["allowlist_sha256"] = _sha256_file(IMAGES_DIR / "egress" / "allowlist.txt")
        lock["egress"]["proxy_sha256"] = _sha256_file(IMAGES_DIR / "egress" / "egress_proxy.py")
        facts = docker("run", "--rm", "--network", "none", "--pull", "never", lock["worker"]["id"], "bash", "-c",
                       "claude --version; codex --version; node --version; git --version; "
                       "python3 --version; jq --version; curl --version | head -1; "
                       "dpkg-query -W -f='${Package}=${Version}\\n' ca-certificates curl git jq python3; "
                       "echo dpkg_manifest_sha256=$(dpkg-query -W -f='${Package}=${Version}\\n' | sha256sum | cut -d' ' -f1)",
                       timeout=300).stdout.strip().splitlines()
        lock["worker"]["versions"] = facts
        LOCK_FILE.write_text(json.dumps(lock, indent=2) + "\n")
        return lock

    # ---- networks

    def net_status(self) -> dict:
        st = {"internal": None, "egress": None, "proxy_running": False, "proxy_image": None}
        for key, net in (("internal", NET_INTERNAL), ("egress", NET_EGRESS)):
            p = docker("network", "inspect", net, check=False)
            if p.returncode == 0:
                info = json.loads(p.stdout)[0]
                st[key] = {"internal": info.get("Internal"), "id": info.get("Id", "")[:12],
                           "subnets": [c.get("Subnet") for c in (info.get("IPAM") or {}).get("Config") or []],
                           "gateways": [c.get("Gateway") for c in (info.get("IPAM") or {}).get("Config") or []],
                           "options": info.get("Options") or {}}
        p = docker("inspect", PROXY_NAME, check=False)
        if p.returncode == 0:
            info = json.loads(p.stdout)[0]
            st["proxy_running"] = bool(info["State"]["Running"])
            st["proxy_image"] = info.get("Image")
            st["proxy_networks"] = sorted((info["NetworkSettings"].get("Networks") or {}).keys())
        return st

    def net_up(self) -> dict:
        proxy_image = image_id("egress")
        for net, internal in ((NET_INTERNAL, True), (NET_EGRESS, False)):
            p = docker("network", "inspect", net, "--format", "{{.Internal}}", check=False)
            if p.returncode == 0:
                if (p.stdout.strip() == "true") != internal:
                    raise SandboxError(f"network {net} exists with internal={p.stdout.strip()}; "
                                       f"run net-down first")
                continue
            args = ["network", "create", "--driver", "bridge", "--label", f"{LABEL}=1"]
            if internal:
                # No gateway address on the bridge: the worker cannot reach the VM itself.
                args += ["--internal", "-o", "com.docker.network.bridge.inhibit_ipv4=true"]
            docker(*args, net)
        st = self.net_status()
        if st["proxy_running"] and st["proxy_image"] == proxy_image \
                and st.get("proxy_networks") == sorted([NET_EGRESS, NET_INTERNAL]):
            return st
        docker("rm", "-f", PROXY_NAME, check=False)
        docker("run", "-d", "--name", PROXY_NAME, "--network", NET_EGRESS,
               "--label", f"{LABEL}=1", "--label", "rsi.role=egress",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "128",
               "--user", f"{PROXY_UID}:{PROXY_UID}", "--pull", "never", proxy_image)
        docker("network", "connect", NET_INTERNAL, PROXY_NAME)
        return self.net_status()

    def net_down(self) -> dict:
        docker("rm", "-f", PROXY_NAME, check=False)
        out = {}
        for net in (NET_INTERNAL, NET_EGRESS):
            p = docker("network", "rm", net, check=False)
            out[net] = "removed" if p.returncode == 0 else p.stderr.strip()[-200:]
        return out

    def _require_internal_network(self) -> None:
        p = docker("network", "inspect", NET_INTERNAL, "--format", "{{.Internal}}", check=False)
        if p.returncode != 0 or p.stdout.strip() != "true":
            raise SandboxError(f"{NET_INTERNAL} is missing or not internal; run net-up")

    # ---- run

    def build_run_argv(self, *, experiment: str, arm: str, run_id: str, command: list[str],
                       image: str | None, timeout_s: int, memory: str, cpus: str, pids: int,
                       credential_names: list[str], ro_mounts: list[tuple[str, str]] = (),
                       network: str = NET_INTERNAL) -> tuple[list[str], str, int]:
        """The docker argv for one worker run. Pure apart from the mount guard's stat calls.

        network: rsi-internal (default; egress only through the proxy) or none (no interface but loopback).
        Returns (argv, container_name, deadline_epoch).
        """
        _check_id(experiment, "experiment id")
        _check_arm(arm)
        _check_id(run_id, "run id")
        if not command:
            raise SandboxError("empty command")
        if network not in RUN_NETWORKS:
            raise SandboxError(f"network must be one of {', '.join(RUN_NETWORKS)}")
        if network == "none" and credential_names:
            raise CredentialError("a run without network gets no credentials")
        if not (1 <= int(timeout_s) <= MAX_TIMEOUT_S):
            raise SandboxError(f"timeout must be 1..{MAX_TIMEOUT_S}s")
        if not re.match(r"^\d+(\.\d+)?[kmg]?$", str(memory)) or not re.match(r"^\d+(\.\d+)?$", str(cpus)):
            raise SandboxError("bad memory or cpus value")
        if not (16 <= int(pids) <= 4096):
            raise SandboxError("pids must be 16..4096")
        for n in credential_names:
            if n not in CREDENTIAL_NAMES:
                raise CredentialError(f"credential name not allowed: {n}")

        exp_dir = self.experiment_dir(experiment)
        home = self.arm_home(experiment, arm)
        run_dir = exp_dir / "runs" / run_id
        ro_roots = [exp_dir, self.baselines]
        home_src = check_mount(home, [home], [], read_only=False)
        run_src = check_mount(run_dir, [run_dir], [], read_only=False)
        ro_args = []
        for src, target in ro_mounts:
            if not RO_TARGET_RE.match(target):
                raise MountRefused(f"read-only mount target must be under /mnt or /baseline: {target}")
            real = check_mount(src, [], ro_roots, read_only=True)
            ro_args += ["--mount", f"type=bind,source={real},target={target},readonly"]
        image = image or image_id("worker")     # after the mount guard, so a refusal needs no Docker

        name = f"rsi-w-{experiment[:24]}-{arm}-{secrets.token_hex(3)}".replace(".", "-")
        deadline = int(time.time()) + int(timeout_s)
        env = {"HOME": "/home/rsi", "CLAUDE_CONFIG_DIR": "/home/rsi/.claude",
               "CODEX_HOME": "/home/rsi/.codex", "HTTPS_PROXY": PROXY_URL, "HTTP_PROXY": PROXY_URL,
               "NO_PROXY": "localhost,127.0.0.1", "EXPERIMENT_ID": experiment, "RSI_ARM": arm}
        assert set(env) == set(ENV_ALLOWLIST)
        argv = ["docker", "run", "--rm", "--name", name, "--pull", "never",
                "--label", f"{LABEL}=1", "--label", "rsi.role=worker",
                "--label", f"rsi.experiment={experiment}", "--label", f"rsi.arm={arm}",
                "--label", f"rsi.run={run_id}", "--label", f"rsi.deadline={deadline}",
                "--network", network, "--hostname", "rsi-worker",
                "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", str(int(pids)), "--memory", str(memory), "--memory-swap", str(memory),
                "--cpus", str(cpus), "--ulimit", "core=0",
                "--user", f"{WORKER_UID}:{WORKER_UID}", "--init", "--workdir", "/work",
                "--mount", f"type=bind,source={home_src},target=/home/rsi",
                "--mount", f"type=bind,source={run_src},target=/work",
                *ro_args]
        for k in ENV_ALLOWLIST:
            argv += ["-e", f"{k}={env[k]}"]
        for n in credential_names:
            argv += ["-e", n]            # name only: docker reads the value from its own env
        argv += [image, *command]
        return argv, name, deadline

    def run(self, experiment: str, arm: str, command: list[str], *, timeout_s: int = DEFAULT_TIMEOUT_S,
            memory: str = "2g", cpus: str = "2", pids: int = 512, credentials: str = "none",
            credential_name: str | None = None, ro_mounts: list[tuple[str, str]] = (),
            run_id: str | None = None, max_output: int = 1 << 20, network: str = NET_INTERNAL) -> RunResult:
        """Run one command in a fresh worker container for one experiment arm.

        network="none" (a stub agent or a check) needs no rsi-internal network and takes no credentials."""
        _check_id(experiment, "experiment id")
        _check_arm(arm)
        run_id = run_id or f"{arm}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}"
        exp_dir = self.experiment_dir(experiment)
        home = self.arm_home(experiment, arm)
        if not home.is_dir():
            raise SandboxError(f"no arm home {home}; run prepare-arm first")
        run_dir = exp_dir / "runs" / run_id
        log_dir = exp_dir / "logs"
        for d in (exp_dir / "runs", run_dir, log_dir):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)

        try:
            creds = load_credentials(credentials, credential_name)
            argv, name, deadline = self.build_run_argv(
                experiment=experiment, arm=arm, run_id=run_id, command=list(command),
                image=None, timeout_s=timeout_s, memory=memory, cpus=cpus, pids=pids,
                credential_names=sorted(creds), ro_mounts=list(ro_mounts), network=network)
            if network == NET_INTERNAL:
                self._require_internal_network()
        except BaseException:
            run_dir.rmdir()              # refused before start: leave no empty run dir
            raise

        lock = self.acquire_lock(f"{experiment}.{arm}", timeout_s + KILL_GRACE_S + 60)
        record = self.rundir / "containers" / f"{name}.json"
        record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        record.write_text(json.dumps({"container": name, "experiment": experiment, "arm": arm,
                                      "run_id": run_id, "deadline": deadline, "pid": os.getpid()}))
        out_path, err_path = log_dir / f"{run_id}.stdout", log_dir / f"{run_id}.stderr"
        start = time.monotonic()
        timed_out, code = False, None
        try:
            with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out_f, stderr=err_f,
                                        env=_docker_cli_env(creds), start_new_session=True)
                try:
                    code = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    docker("kill", name, check=False, timeout=30)
                    try:
                        proc.wait(timeout=KILL_GRACE_S)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    code = proc.returncode
                except BaseException:
                    docker("kill", name, check=False, timeout=30)
                    proc.kill()
                    raise
        finally:
            docker("rm", "-f", name, check=False, timeout=60)
            record.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
        duration = round(time.monotonic() - start, 2)
        result = RunResult(experiment=experiment, arm=arm, run_id=run_id, container=name,
                           exit_code=code, timed_out=timed_out, duration_s=duration,
                           stdout=_tail(out_path, max_output), stderr=_tail(err_path, max_output),
                           run_dir=str(run_dir), log_dir=str(log_dir), argv=argv)
        meta = result.to_json(max_output=0)
        (log_dir / f"{run_id}.json").write_text(json.dumps(meta, indent=2) + "\n")
        return result

    # ---- prepare-arm

    def prepare_arm(self, experiment: str, baseline: str, arm: str, *, hook_experiment: bool = False,
                    force: bool = False, artifact: str | None = None) -> dict:
        """home-<arm> = the sanitized baseline, or with `artifact` the baseline plus that artifact's after
        bytes (the candidate arm). The report records whether the home holds the artifact's exact bytes."""
        src = self.baseline_dir(baseline)
        if not src.is_dir() or src.is_symlink():
            raise SandboxError(f"baseline not found: {src}")
        bman, broot = read_baseline(src)
        overlay: dict[str, bytes] = {}
        art_m = None
        if artifact is not None:
            if not HEX64_RE.match(artifact):
                raise SandboxError("artifact must be a 64-hex hash")
            if bman is None:
                raise SandboxError(f"--artifact needs an {BASELINE_FORMAT} baseline")
            art_m, _, overlay = load_artifact(self.artifacts / artifact)
            if art_m.get("baseline_id") != baseline:
                raise SandboxError(f"artifact was staged against {art_m.get('baseline_id')}, not {baseline}")
            if art_m.get("experiment_id") != experiment:
                raise SandboxError(f"artifact belongs to experiment {art_m.get('experiment_id')}, not {experiment}")
            base_sha = {f["path"]: f["sha256"] for f in bman["files"]}
            for f in art_m["files"]:
                if f.get("before_sha256") != base_sha.get(f["path"]):
                    raise SandboxError(f"stale artifact: {f['path']} differs from the baseline it claims")
        dest = self.arm_home(experiment, arm)
        exp_dir = self.experiment_dir(experiment)
        if dest.exists():
            if not force:
                raise SandboxError(f"{dest} exists; pass --force to replace it")
            shutil.rmtree(dest)
        exp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (exp_dir / "runs").mkdir(mode=0o700, exist_ok=True)
        report = {"experiment": experiment, "arm": arm, "baseline": baseline,
                  "hook_experiment": hook_experiment,
                  "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "removed": [], "set": []}
        report["baseline_format"] = BASELINE_FORMAT if bman else "raw-home"
        report["artifact_hash"] = artifact
        stage = None
        try:
            copy_from = broot
            if bman is not None:
                # A writable stage: baseline copies are read-only (0444), modes come from the manifests.
                stage = exp_dir / f".stage-{arm}-{secrets.token_hex(4)}"
                shutil.copytree(broot, stage, symlinks=True, copy_function=shutil.copyfile)
                for d in (stage, *(q for q in stage.rglob("*") if q.is_dir() and not q.is_symlink())):
                    os.chmod(d, 0o700)
                modes = {f["path"]: f.get("mode") for f in bman["files"]}
                for key, b in overlay.items():
                    out = stage / _home_rel(key)
                    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    out.write_bytes(b)
                modes.update({f["path"]: f.get("mode") for f in (art_m or {}).get("files", [])})
                for key, mode in modes.items():
                    q = stage / _home_rel(key)
                    if q.is_file() and not q.is_symlink():
                        os.chmod(q, (_file_mode(mode) & 0o755) | 0o600)
                copy_from = stage
            _copy_sanitized(copy_from, dest, report, hook_experiment)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)    # fail closed: no half-sanitized home
            raise
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
        for d in (".claude", ".codex"):            # CLAUDE_CONFIG_DIR / CODEX_HOME must exist
            (dest / d).mkdir(mode=0o700, exist_ok=True)
        if not hook_experiment:
            settings = dest / ".claude" / "settings.json"
            settings.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = json.loads(settings.read_text()) if settings.exists() else {}
            data["disableAllHooks"] = True
            settings.write_text(json.dumps(data, indent=2) + "\n")
            report["set"].append({"path": ".claude/settings.json", "key": "disableAllHooks", "value": True})
        if art_m is not None:
            rows = []
            for f in art_m["files"]:
                p = dest / _home_rel(f["path"])
                got = _sha(p.read_bytes()) if p.is_file() and not p.is_symlink() else None
                rows.append({"path": f["path"], "after_sha256": f["after_sha256"], "home_sha256": got})
            report["overlay"] = rows
            report["exact"] = all(r["after_sha256"] == r["home_sha256"] for r in rows)
        rep_path = exp_dir / "sanitize-report.json"
        allrep = json.loads(rep_path.read_text()) if rep_path.exists() else {}
        allrep[arm] = report
        rep_path.write_text(json.dumps(allrep, indent=2) + "\n")
        return report

    # ---- evaluation records

    def record_eval(self, *, artifact: str, experiment: str, arm: str, suite: str, verdict: str,
                    results: dict | None = None, suite_version: str = "1", model: str = "none", effort: str = "none",
                    started_at: str | None = None, finished_at: str | None = None, control_arm: str | None = "A",
                    replace: bool = False) -> Path:
        """Write artifacts/<H>/evals/<suite>.json (rsi-eval/1). Refused unless home-<arm> was prepared from
        exactly this artifact (exact bytes) and the control arm from the same baseline without an artifact."""
        if not HEX64_RE.match(artifact or ""):
            raise SandboxError("artifact must be a 64-hex hash")
        if not SUITE_RE.match(suite or "") or verdict not in VERDICTS:
            raise SandboxError(f"bad suite or verdict (verdicts: {', '.join(VERDICTS)})")
        adir = self.artifacts / artifact
        m, h, _ = load_artifact(adir)
        if m.get("experiment_id") != experiment:
            raise SandboxError(f"artifact belongs to experiment {m.get('experiment_id')}, not {experiment}")
        try:
            rep = json.loads((self.experiment_dir(experiment) / "sanitize-report.json").read_text())
        except (OSError, ValueError):
            raise SandboxError("no sanitize report for this experiment; run prepare-arm first") from None
        cand = rep.get(_check_arm(arm)) or {}
        if cand.get("artifact_hash") != h or cand.get("baseline") != m.get("baseline_id"):
            raise SandboxError(f"home-{arm} was not prepared from this artifact and its baseline")
        if not cand.get("exact"):
            raise SandboxError(f"home-{arm} does not hold the artifact's exact bytes (the sanitizer changed "
                               f"them); this artifact cannot be evaluated as staged")
        if control_arm is not None:
            ctl = rep.get(_check_arm(control_arm)) or {}
            if ctl.get("baseline") != m.get("baseline_id") or ctl.get("artifact_hash") is not None:
                raise SandboxError(f"control home-{control_arm} is not the plain baseline {m.get('baseline_id')}")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = {"format": EVAL_FORMAT, "artifact_hash": h, "experiment_id": experiment, "baseline_id": m["baseline_id"],
               "suite": suite, "suite_version": str(suite_version), "model": model, "effort": effort,
               "verdict": verdict, "started_at": started_at or now, "finished_at": finished_at or now,
               "runner": "rsi_sandbox", "results": {**(results or {}), "candidate_arm": arm,
                                                    "control_arm": control_arm}}
        out = adir / "evals" / f"{suite}.json"
        if out.exists() and not replace:
            raise SandboxError(f"{out.name} exists; pass --replace to overwrite it")
        out.parent.mkdir(exist_ok=True)
        _atomic_write(out, canonical(rec) + b"\n", 0o644)
        return out

    # ---- cleanup and recovery

    def _workers(self) -> list[dict]:
        ids = docker("ps", "-aq", "--filter", f"label={LABEL}=1", "--filter", "label=rsi.role=worker").stdout.split()
        if not ids:
            return []
        return json.loads(docker("inspect", *ids, check=False).stdout or "[]")

    def cleanup(self, max_age_hours: float = 6.0) -> dict:
        """Remove worker containers past their deadline or older than max_age_hours, plus their records."""
        now = time.time()
        removed = []
        for c in self._workers():
            labels = c["Config"].get("Labels") or {}
            deadline = float(labels.get("rsi.deadline") or 0)
            created = _parse_docker_time(c.get("Created", ""))
            running = c["State"].get("Running")
            if (deadline and now > deadline) or (created and now - created > max_age_hours * 3600) or not running:
                docker("rm", "-f", c["Id"], check=False)
                removed.append(c["Name"].lstrip("/"))
        records = self._prune_records(now, removed)
        temps = []
        for p in self.rundir.glob("*.tmp"):
            if now - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)
                temps.append(p.name)
        return {"removed_containers": removed, "removed_records": records, "removed_temp": temps}

    def recover(self) -> dict:
        """Kill overdue worker containers and release stale locks in run/."""
        now = time.time()
        killed = []
        for c in self._workers():
            deadline = float((c["Config"].get("Labels") or {}).get("rsi.deadline") or 0)
            if deadline and now > deadline:
                docker("kill", c["Id"], check=False)
                docker("rm", "-f", c["Id"], check=False)
                killed.append(c["Name"].lstrip("/"))
        released = []
        for p in sorted(self.rundir.glob("*.lock")):
            if self._lock_stale(p):
                p.unlink(missing_ok=True)
                released.append(p.name)
        return {"killed_containers": killed, "released_locks": released,
                "removed_records": self._prune_records(now, killed)}

    def reap_experiment(self, experiment: str, *, quiet_s: float = 2.0, max_s: float = 20.0) -> list[str]:
        """Remove every worker container of an aborted experiment, running or not. A `docker run` client that is
        killed between create and start (an interrupt during `run`) leaves a container in `Created` state: `--rm`
        never fires for it, and the `rm -f` in run's finally can run before the daemon finished the create. So poll
        until no container of the experiment has appeared for `quiet_s` (or `max_s` passed)."""
        _check_id(experiment, "experiment id")
        removed: list[str] = []
        start = quiet_since = time.monotonic()
        while time.monotonic() - start < max_s:
            ids = docker("ps", "-aq", "--filter", f"label={LABEL}=1", "--filter", "label=rsi.role=worker",
                         "--filter", f"label=rsi.experiment={experiment}", check=False).stdout.split()
            if ids:
                docker("rm", "-f", *ids, check=False, timeout=60)
                removed += [i for i in ids if i not in removed]
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= quiet_s:
                break
            time.sleep(0.25)
        return removed

    def _prune_records(self, now: float, removed: list[str]) -> list[str]:
        out = []
        live = {c["Name"].lstrip("/") for c in self._workers()}
        for p in (self.rundir / "containers").glob("*.json"):
            try:
                rec = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                rec = {}
            name = rec.get("container", p.stem)
            if name in removed or (name not in live and now > float(rec.get("deadline", 0))):
                p.unlink(missing_ok=True)
                out.append(p.name)
        return out


# ------------------------------------------------------------------ sanitizer

# Claude settings keys that run commands, load servers or plugins, or set env.
CLAUDE_STRIP = {
    "hooks": "hook", "statusLine": "status_line", "mcpServers": "mcp",
    "enableAllProjectMcpServers": "mcp", "enabledMcpjsonServers": "mcp",
    "enabledPlugins": "plugin", "plugins": "plugin", "extraKnownMarketplaces": "marketplace",
    "env": "env", "apiKeyHelper": "command_helper", "awsAuthRefresh": "command_helper",
    "awsCredentialExport": "command_helper", "otelHeadersHelper": "command_helper",
    "fileSuggestion": "command_helper",
}
# Extra keys in the global state file (.claude.json): account data and per-project state.
CLAUDE_STATE_STRIP = {"oauthAccount": "credential", "primaryApiKey": "credential",
                      "customApiKeyResponses": "credential", "projects": "project_state"}
CODEX_STRIP = {"mcp_servers": "mcp", "notify": "notify", "shell_environment_policy": "env",
               "hooks": "hook"}
# Never copied (never read). Paths relative to the home root.
SKIP_DIRS = {
    ".claude/plugins": "plugin", ".claude/projects": "session_state",
    ".claude/shell-snapshots": "session_state", ".claude/todos": "session_state",
    ".claude/statsig": "session_state", ".claude/file-history": "session_state",
    ".claude/debug": "session_state", ".claude/session-env": "session_state",
    ".claude/ide": "session_state", ".codex/sessions": "session_state",
    ".codex/archived_sessions": "session_state", ".codex/log": "session_state",
}
SKIP_FILES = {".codex/auth.json": "credential", ".codex/history.jsonl": "session_state"}


def _copy_sanitized(src: Path, dest: Path, report: dict, hook_experiment: bool) -> None:
    removed = report["removed"]
    dest.mkdir(mode=0o700, parents=True)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel_dir = Path(dirpath).relative_to(src)
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix()
            full = Path(dirpath) / d
            if full.is_symlink():
                removed.append({"path": rel, "item": "symlink", "kind": "symlink"})
            elif rel in SKIP_DIRS:
                removed.append({"path": rel + "/", "item": "directory", "kind": SKIP_DIRS[rel]})
            elif rel == ".claude/hooks" and not hook_experiment:
                removed.append({"path": rel + "/", "item": "directory", "kind": "hook"})
            elif d.casefold().startswith(".env"):
                removed.append({"path": rel + "/", "item": "directory", "kind": "env_file"})
            else:
                keep.append(d)
                (dest / rel_dir / d).mkdir(mode=0o700, exist_ok=True)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = (rel_dir / f).as_posix()
            full = Path(dirpath) / f
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                removed.append({"path": rel, "item": "symlink", "kind": "symlink"})
                continue
            if not stat.S_ISREG(st.st_mode):
                removed.append({"path": rel, "item": "special_file", "kind": "special_file"})
                continue
            if f.casefold().startswith(".env"):
                removed.append({"path": rel, "item": "file", "kind": "env_file"})
                continue
            if f == ".credentials.json" or rel in SKIP_FILES:
                removed.append({"path": rel, "item": "file", "kind": SKIP_FILES.get(rel, "credential")})
                continue
            if f == ".mcp.json":
                removed.append({"path": rel, "item": "file", "kind": "mcp"})
                continue
            out = dest / rel
            parent = Path(dirpath).name
            if parent == ".claude" and f in ("settings.json", "settings.local.json"):
                out.write_text(_sanitize_claude_json(full, rel, CLAUDE_STRIP, removed, hook_experiment))
            elif f == ".claude.json":
                out.write_text(_sanitize_claude_json(full, rel, {**CLAUDE_STRIP, **CLAUDE_STATE_STRIP},
                                                     removed, hook_experiment))
            elif parent == ".codex" and f == "config.toml":
                out.write_text(sanitize_codex_toml(full.read_text(), rel, removed, hook_experiment))
            else:
                shutil.copyfile(full, out, follow_symlinks=False)
            os.chmod(out, (st.st_mode & 0o755) | 0o600)


def _sanitize_claude_json(path: Path, rel: str, strip: dict, removed: list, hook_experiment: bool) -> str:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SandboxError(f"{rel} is not valid JSON ({e.msg}); refusing to copy it") from None
    if not isinstance(data, dict):
        raise SandboxError(f"{rel} is not a JSON object")
    for key, kind in strip.items():
        if key == "hooks" and hook_experiment:
            continue
        if key in data:
            del data[key]
            removed.append({"path": rel, "item": key, "kind": kind})
    if not hook_experiment and data.get("disableAllHooks") is False:
        del data["disableAllHooks"]
        removed.append({"path": rel, "item": "disableAllHooks=false", "kind": "hook"})
    return json.dumps(data, indent=2) + "\n"


_TABLE_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
_KEY_RE = re.compile(r"""^\s*("[^"]*"|'[^']*'|[A-Za-z0-9_-]+)(\s*\.\s*("[^"]*"|'[^']*'|[A-Za-z0-9_-]+))*\s*=""")


def _first_segment(dotted: str) -> str:
    seg = re.match(r"""\s*("([^"]*)"|'([^']*)'|[A-Za-z0-9_-]+)""", dotted)
    if not seg:
        return dotted.strip()
    return seg.group(2) if seg.group(2) is not None else (seg.group(3) if seg.group(3) is not None else seg.group(1))


def _value_continues(text: str) -> bool:
    """True when a TOML value that starts in text runs onto later lines."""
    if text.count('"""') % 2 == 1 or text.count("'''") % 2 == 1:
        return True
    bare = re.sub(r'"(\\.|[^"\\])*"|\'[^\']*\'', "", text)          # drop strings
    bare = "\n".join(line.split("#", 1)[0] for line in bare.splitlines())  # drop comments
    return bare.count("[") + bare.count("{") > bare.count("]") + bare.count("}")


def sanitize_codex_toml(text: str, rel: str, removed: list, hook_experiment: bool = False) -> str:
    """Strip [mcp_servers], notify, [shell_environment_policy] and hooks from a Codex config.toml.

    Line-based (stdlib has no TOML writer), then verified with tomllib: fails closed.
    """
    strip = {k: v for k, v in CODEX_STRIP.items() if not (k == "hooks" and hook_experiment)}
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SandboxError(f"{rel} is not valid TOML ({e}); refusing to copy it") from None
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping_table = False
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _TABLE_RE.match(line)
        if m:
            name = m.group(1)
            root = _first_segment(name)
            skipping_table = root in strip
            if skipping_table:
                removed.append({"path": rel, "item": f"[{name}]", "kind": strip[root]})
            else:
                out.append(line)
            i += 1
            continue
        if skipping_table:
            i += 1
            continue
        km = _KEY_RE.match(line)
        if km and _first_segment(line) in strip:
            key = line.split("=", 1)[0].strip()
            removed.append({"path": rel, "item": key, "kind": strip[_first_segment(line)]})
            value = line.split("=", 1)[1]
            i += 1
            while _value_continues(value) and i < len(lines):
                value += lines[i]
                i += 1
            continue
        out.append(line)
        i += 1
    new = "".join(out)
    try:
        parsed = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise SandboxError(f"sanitizing {rel} broke the TOML ({e}); refusing to copy it") from None
    left = _find_keys(parsed, set(strip))
    if left:
        raise SandboxError(f"sanitizing {rel} left forbidden keys {sorted(left)}; refusing to copy it")
    return new


def _find_keys(obj, names: set[str], path: str = "") -> set[str]:
    found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in names:
                found.add(p)
            found |= _find_keys(v, names, p)
    elif isinstance(obj, list):
        for v in obj:
            found |= _find_keys(v, names, path)
    return found


# ------------------------------------------------------------------ small helpers

def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tail(path: Path, limit: int) -> str:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > limit:
            f.seek(size - limit)
        return f.read().decode("utf-8", errors="replace")


def _parse_docker_time(s: str) -> float | None:
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", s or "")
    if not m:
        return None
    return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")))   # Docker times are UTC


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rsi_sandbox.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help=f"create the sandbox layout (mode 700) in ${ROOT_ENV} or ~/sandbox")
    sub.add_parser("build", help="build both images and write images/lock.json")
    sub.add_parser("net-up", help="create the networks and start the egress proxy")
    sub.add_parser("net-down", help="stop the proxy and remove the networks")
    sub.add_parser("net-status", help="show network and proxy state")
    p = sub.add_parser("prepare-arm", help="copy a baseline into home-<arm> and sanitize it")
    p.add_argument("--experiment", required=True)
    p.add_argument("--baseline", required=True)
    p.add_argument("--arm", required=True)
    p.add_argument("--hook-experiment", action="store_true", help="keep hooks (arm tests a hook)")
    p.add_argument("--force", action="store_true", help="replace an existing home-<arm>")
    p.add_argument("--artifact", default=None, help="overlay this staged artifact (the candidate arm)")
    e = sub.add_parser("record-eval", help="write an rsi-eval/1 record bound to the tested artifact")
    e.add_argument("--artifact", required=True)
    e.add_argument("--experiment", required=True)
    e.add_argument("--arm", default="B", help="the candidate arm prepared with --artifact")
    e.add_argument("--control-arm", default="A")
    e.add_argument("--suite", required=True)
    e.add_argument("--suite-version", default="1")
    e.add_argument("--model", default="none")
    e.add_argument("--effort", default="none")
    e.add_argument("--verdict", required=True, choices=VERDICTS)
    e.add_argument("--results", default=None, help="JSON file with the suite's results")
    e.add_argument("--replace", action="store_true")
    r = sub.add_parser("run", help="run one command in a fresh worker container")
    r.add_argument("--experiment", required=True)
    r.add_argument("--arm", required=True)
    r.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    r.add_argument("--memory", default="2g")
    r.add_argument("--cpus", default="2")
    r.add_argument("--pids", type=int, default=512)
    r.add_argument("--credentials", default="none",
                   help="none | env-file:<path> | keychain:<service>/<account>")
    r.add_argument("--credential-name", default=None, help="env name for a keychain credential")
    r.add_argument("--ro-mount", action="append", default=[], metavar="SRC:TARGET",
                   help="read-only mount from the experiment or a baseline, target under /mnt or /baseline")
    r.add_argument("--network", default=NET_INTERNAL, choices=RUN_NETWORKS,
                   help="rsi-internal (egress through the proxy) or none (no network, no credentials)")
    r.add_argument("command", nargs=argparse.REMAINDER)
    c = sub.add_parser("cleanup", help="remove overdue or old worker containers and temp files")
    c.add_argument("--max-age-hours", type=float, default=6.0)
    sub.add_parser("recover", help="kill overdue workers and release stale locks")
    args = ap.parse_args(argv)

    sb = Sandbox()
    try:
        if args.cmd == "init":
            out = {"created": sb.init(), "root": str(sb.root)}
        elif args.cmd == "build":
            out = sb.build()
        elif args.cmd == "net-up":
            out = sb.net_up()
        elif args.cmd == "net-down":
            out = sb.net_down()
        elif args.cmd == "net-status":
            out = sb.net_status()
        elif args.cmd == "prepare-arm":
            out = sb.prepare_arm(args.experiment, args.baseline, args.arm,
                                 hook_experiment=args.hook_experiment, force=args.force, artifact=args.artifact)
        elif args.cmd == "record-eval":
            results = json.loads(Path(args.results).read_text()) if args.results else {}
            out = {"eval": str(sb.record_eval(
                artifact=args.artifact, experiment=args.experiment, arm=args.arm, suite=args.suite,
                verdict=args.verdict, results=results, suite_version=args.suite_version, model=args.model,
                effort=args.effort, control_arm=args.control_arm or None, replace=args.replace))}
        elif args.cmd == "run":
            cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
            ro = []
            for spec in args.ro_mount:
                src, sep, target = spec.rpartition(":")
                if not sep:
                    raise SandboxError("--ro-mount is SRC:TARGET")
                ro.append((src, target))
            res = sb.run(args.experiment, args.arm, cmd, timeout_s=args.timeout, memory=args.memory,
                         cpus=args.cpus, pids=args.pids, credentials=args.credentials,
                         credential_name=args.credential_name, ro_mounts=ro, network=args.network)
            print(json.dumps(res.to_json(), indent=2))
            return 124 if res.timed_out else (res.exit_code or 0)
        elif args.cmd == "cleanup":
            out = sb.cleanup(args.max_age_hours)
        elif args.cmd == "recover":
            out = sb.recover()
        else:  # pragma: no cover
            ap.error("unknown command")
    except (SandboxError, MountRefused, CredentialError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
