"""Security verification for the rsi sandbox.

Every probe in the Docker part runs inside a real worker container started through
`Sandbox.run` (the same path experiments use). Where a probe proves a negative
("cannot connect"), a positive control runs the same probe from a normal container
or from the host, so a broken probe cannot pass by accident.

Run:  python3 -m pytest sandbox/tests/test_isolation.py -s -rA
Needs: Docker, `rsi_sandbox.py build` done (images/lock.json). Without them the
Docker tests skip; the unit tests (mount guard, proxy resolver) still run.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

import egress_proxy as ep
import rsi_sandbox as rs

DUMMY_ANTHROPIC = "sk-ant-api03-DUMMY-NOT-A-KEY"
DUMMY_CODEX = "sk-proj-DUMMY-NOT-A-KEY"
SENSITIVE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|DB_URL|SUPABASE", re.I)

DOCKER_OK = rs.docker_available() and rs.load_lock() is not None
needs_docker = pytest.mark.skipif(not DOCKER_OK, reason="docker or images/lock.json unavailable")

# ---------------------------------------------------------------- probe scripts (run in containers)

TCP = r'''
import json, socket, sys
res = {}
for hp in sys.argv[1:]:
    h, p = hp.rsplit(":", 1)
    try:
        s = socket.create_connection((h.strip("[]"), int(p)), timeout=4); s.close(); res[hp] = "CONNECTED"
    except Exception as e:
        res[hp] = f"{type(e).__name__}: {e}"
print(json.dumps(res))
'''

DNS = r'''
import json, secrets, socket, struct, sys
def udp_query(server, name):
    pkt = struct.pack(">HHHHHH", secrets.randbits(16), 0x0100, 1, 0, 0, 0)
    pkt += b"".join(bytes([len(l)]) + l.encode() for l in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
    try:
        s.sendto(pkt, (server, 53)); data = s.recv(1500)
        flags, _qd, an = struct.unpack(">HHH", data[2:8])
        return {"rcode": flags & 0xF, "answers": an}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
cfg = json.loads(sys.argv[1])
out = {"getaddrinfo": {}, "udp": {}}
for name in cfg["names"]:
    try:
        out["getaddrinfo"][name] = sorted({a[4][0] for a in socket.getaddrinfo(name, 443)})
    except Exception as e:
        out["getaddrinfo"][name] = f"{type(e).__name__}: {e}"
for server, name in cfg["udp"]:
    out["udp"][f"{server}/{name}"] = udp_query(server, name)
print(json.dumps(out))
'''

WALK = r'''
import os
def walk_files():
    for top in sorted(os.listdir("/")):
        if top in ("proc", "sys", "dev"):
            continue
        base = "/" + top
        if os.path.islink(base) or not os.path.isdir(base):
            yield base
            continue
        for dp, _dns, fns in os.walk(base, followlinks=False):
            for f in fns:
                yield os.path.join(dp, f)
'''

PROXY_RAW = r'''
import json, socket, sys
out = {}
for req in json.loads(sys.argv[1]):
    s = socket.create_connection(("rsi-egress-proxy", 3128), timeout=10)
    s.sendall(req.encode())
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
    except Exception as e:
        data += f"<{type(e).__name__}>".encode()
    s.close()
    out[req.split("\r\n")[0]] = data.split(b"\r\n")[0].decode("latin-1") or "<closed>"
print(json.dumps(out))
'''

SNI = r'''
import json, socket, ssl
def tunnel(sni, verify):
    s = socket.create_connection(("rsi-egress-proxy", 3128), timeout=15)
    s.sendall(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com:443\r\n\r\n")
    head = b""
    while b"\r\n\r\n" not in head:
        head += s.recv(1024)
    status = head.split(b"\r\n")[0].decode()
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        t = ctx.wrap_socket(s, server_hostname=sni)
        r = {"connect": status, "tls": t.version(), "verified": verify}
        t.close()
        return r
    except Exception as e:
        return {"connect": status, "tls_error": f"{type(e).__name__}: {e}"}
print(json.dumps({"matching_sni": tunnel("api.anthropic.com", True),
                  "mismatched_sni": tunnel("example.com", False)}))
'''


# ---------------------------------------------------------------- fixtures and helpers

@pytest.fixture(scope="session")
def world(tmp_path_factory):
    if not DOCKER_OK:
        pytest.skip("docker or images/lock.json unavailable")
    sb = rs.Sandbox()
    sb.init()
    st = sb.net_status()
    was_up = bool(st["internal"]) and st["proxy_running"]
    sb.net_up()
    tag = secrets.token_hex(4)
    exp, base = f"t-iso-{tag}", f"t-base-{tag}"
    bdir = sb.baseline_dir(base)
    (bdir / ".claude").mkdir(mode=0o700, parents=True)
    (bdir / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
         "env": {"FOO": "1"}, "model": "sonnet"}))
    sb.prepare_arm(exp, base, "A")
    sb.prepare_arm(exp, base, "B")
    marker = secrets.token_hex(16)
    fake_host = sb.experiment_dir(exp) / "fake-host"
    fake_host.mkdir(mode=0o700)
    (fake_host / f"canary-{tag}.txt").write_text(marker)               # NOT mounted
    (sb.arm_home(exp, "B") / f"canary-b-{tag}.txt").write_text(marker)  # other arm, NOT mounted
    (sb.arm_home(exp, "A") / f"canary-a-{tag}.txt").write_text(marker)  # positive control
    creds = tmp_path_factory.mktemp("creds") / "dummy-creds.txt"
    creds.write_text(f"ANTHROPIC_API_KEY={DUMMY_ANTHROPIC}\nCODEX_API_KEY={DUMMY_CODEX}\n")
    lock = rs.load_lock()
    yield types.SimpleNamespace(sb=sb, exp=exp, tag=tag, marker=marker, creds=f"env-file:{creds}",
                                worker_image=lock["worker"]["id"])
    shutil.rmtree(sb.experiment_dir(exp), ignore_errors=True)
    shutil.rmtree(bdir, ignore_errors=True)
    if not was_up:
        sb.net_down()


def run_py(world, script, *args, timeout=180, **kw):
    return world.sb.run(world.exp, "A", ["python3", "-c", script, *args], timeout_s=timeout, **kw)


def run_json(world, script, *args, **kw) -> dict:
    r = run_py(world, script, *args, **kw)
    assert r.exit_code == 0 and not r.timed_out, f"probe failed: {r.stderr[-2000:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def control_json(world, script, *args, network="bridge", timeout=120) -> dict:
    """Same probe in a normal (non-sandbox-network) container: the positive control."""
    argv = ["docker", "run", "--rm", "--pull", "never", "--label", "rsi.sandbox=1",
            "--label", "rsi.role=test-control", "--network", network, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only", "--user", "10001:10001",
            world.worker_image, "python3", "-c", script, *args]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=rs._docker_cli_env())
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def proxy_log_since(t0: float) -> str:
    p = rs.docker("logs", "--since", str(int(t0)), rs.PROXY_NAME, check=False)
    return p.stdout + p.stderr


def host_lan_ip() -> str | None:
    try:
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        iface = re.search(r"interface:\s*(\S+)", out).group(1)
        ip = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, timeout=5).stdout.strip()
        return ip or None
    except (OSError, AttributeError, subprocess.TimeoutExpired):
        return None


def host_can_connect(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=2).close()
        return True
    except OSError:
        return False


# ================================================================ 7. mount guard (unit, no Docker)

FORBIDDEN_REL = [".claude", ".claude/CLAUDE.md", ".codex", ".codex/auth.json", ".agents", ".agents/skills",
                 "agent-config", "Projects", "Projects/agent-memory", "Library",
                 "Library/Application Support/amem-archive", ".ssh", ".config", ".docker", ".CLAUDE"]


@pytest.mark.parametrize("rel", FORBIDDEN_REL)
def test_mount_guard_refuses_forbidden_host_paths(rel, evidence):
    p = rs.REAL_HOME / rel
    # Even when the caller lists the path itself as an allowed root, the guard refuses it.
    with pytest.raises(rs.MountRefused, match="forbidden"):
        rs.check_mount(p, allowed_rw=[p], allowed_ro=[p], read_only=True)
    evidence(f"refused {p}")


@pytest.mark.parametrize("path", ["/var/run/docker.sock", "/run/docker.sock", str(rs.REAL_HOME), "/",
                                  "/Users"])
def test_mount_guard_refuses_socket_and_parents(path, evidence):
    with pytest.raises(rs.MountRefused):
        rs.check_mount(path, allowed_rw=[Path(path)], allowed_ro=[], read_only=False)
    evidence(f"refused {path}")


def test_mount_guard_refuses_archive_env_dir(monkeypatch, tmp_path):
    archive = tmp_path / "custom-archive"
    archive.mkdir()
    monkeypatch.setenv("AMEM_ARCHIVE_DIR", str(archive))
    with pytest.raises(rs.MountRefused, match="forbidden"):
        rs.check_mount(archive, allowed_rw=[tmp_path], allowed_ro=[], read_only=False)


def test_mount_guard_sandbox_rules(tmp_path, evidence):
    sb = rs.Sandbox(tmp_path / "sandbox")
    sb.init()
    exp = sb.experiment_dir("e1")
    home_a, home_b = sb.arm_home("e1", "A"), sb.arm_home("e1", "B")
    run_dir = exp / "runs" / "r1"
    base = sb.baseline_dir("b1")
    for d in (home_a, home_b, run_dir, base, exp / "dotenv-dir"):
        d.mkdir(parents=True)
    envdir = exp / "x.env.local"
    envdir.mkdir()
    link = exp / "sneaky"
    link.symlink_to(rs.REAL_HOME / ".claude")

    assert rs.check_mount(home_a, [home_a, run_dir], [], read_only=False) == Path(os.path.realpath(home_a))
    assert rs.check_mount(base, [], [exp, sb.baselines], read_only=True)
    refusals = {
        "baseline writable": (base, [home_a, run_dir], [exp, sb.baselines], False),
        "other arm writable": (home_b, [home_a, run_dir], [], False),
        "outside sandbox ro": ("/tmp", [], [exp, sb.baselines], True),
        "outside sandbox rw": ("/opt", [home_a], [], False),
        ".env path": (envdir, [exp], [exp], True),
        "symlink to ~/.claude": (link, [exp], [exp], True),
        "relative path": ("sandbox/experiments", [exp], [exp], True),
        "comma injection": (f"{home_a},target=/x", [exp], [exp], True),
        "colon": (f"{home_a}:/x", [exp], [exp], True),
    }
    for _label, (src, rw, ro, read_only) in refusals.items():
        with pytest.raises(rs.MountRefused):
            rs.check_mount(src, rw, ro, read_only)
    evidence(f"allowed home-A rw and baseline ro; refused: {', '.join(refusals)}")


def test_runner_refuses_forbidden_ro_mount_without_docker(tmp_path):
    sb = rs.Sandbox(tmp_path / "sandbox")
    sb.init()
    sb.arm_home("e1", "A").mkdir(parents=True)
    (sb.experiment_dir("e1") / "runs" / "r1").mkdir(parents=True)
    for src in (rs.REAL_HOME / ".ssh", rs.REAL_HOME / ".claude", Path("/var/run/docker.sock")):
        with pytest.raises(rs.MountRefused):
            sb.build_run_argv(experiment="e1", arm="A", run_id="r1", command=["true"], image="img",
                              timeout_s=10, memory="1g", cpus="1", pids=64, credential_names=[],
                              ro_mounts=[(str(src), "/mnt/x")])
    with pytest.raises(rs.MountRefused, match="target"):
        sb.build_run_argv(experiment="e1", arm="A", run_id="r1", command=["true"], image="img",
                          timeout_s=10, memory="1g", cpus="1", pids=64, credential_names=[],
                          ro_mounts=[(str(sb.experiment_dir("e1")), "/etc")])


# ================================================================ 5. proxy resolver (unit, no Docker)

@pytest.mark.parametrize("addr,public", [
    ("10.0.0.5", False), ("172.16.3.4", False), ("192.168.65.254", False), ("127.0.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("fd00::1", False), ("::ffff:10.0.0.1", False),
    ("64:ff9b::a00:1", False), ("2002:a00:1::", False),
    ("1.1.1.1", True), ("160.79.104.10", True), ("2606:4700::6810:84e5", True),
])
def test_proxy_is_public(addr, public):
    assert ep.is_public(addr) is public


def test_proxy_resolver_refuses_private_answers(evidence):
    def fake(answers):
        return lambda host, port: [(socket.AF_INET6 if ":" in a else socket.AF_INET, (a, port)) for a in answers]
    for answers in (["10.0.0.5"], ["127.0.0.1"], ["192.168.65.254"], ["1.1.1.1", "10.0.0.5"], ["::1"]):
        with pytest.raises(ep.Deny, match="private_address"):
            ep.resolve_public("api.anthropic.com", 443, fake(answers))
    assert ep.resolve_public("api.anthropic.com", 443, fake(["1.1.1.1"]))[0][1][0] == "1.1.1.1"

    def boom(host, port):
        raise socket.gaierror("no such host")
    with pytest.raises(ep.Deny, match="dns_fail"):
        ep.resolve_public("api.anthropic.com", 443, boom)
    evidence("allowlisted name answering 10/8, 127/8, 192.168.65.254, mixed, ::1 -> Deny(private_address)")


@pytest.mark.parametrize("method,target,reason", [
    ("GET", "http://api.anthropic.com/", "method"), ("CONNECT", "1.1.1.1:443", "ip_literal"),
    ("CONNECT", "[2606:4700::1111]:443", "ip_literal"), ("CONNECT", "16843009:443", "ip_literal"),
    ("CONNECT", "0x7f.1:443", "ip_literal"), ("CONNECT", "api.anthropic.com:80", "port"),
    ("CONNECT", "api.anthropic.com:8443", "port"), ("CONNECT", "api.anthropic.com", "bad_target"),
    ("CONNECT", "bad_host!:443", "bad_host"),
])
def test_proxy_parse_target_denies(method, target, reason):
    with pytest.raises(ep.Deny, match=reason):
        ep.parse_target(method, target)


def _client_hello(sni: str) -> bytes:
    import ssl
    ctx = ssl.create_default_context()
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    obj = ctx.wrap_bio(inc, out, server_hostname=sni)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return out.read()


def test_proxy_parses_real_client_hello_sni():
    assert ep.parse_sni(_client_hello("api.anthropic.com")) == "api.anthropic.com"
    with pytest.raises(ValueError):
        ep.parse_sni(b"GET / HTTP/1.1\r\n\r\n")


class _Serve:
    def __init__(self, allowlist, resolver=ep.system_resolver):
        self.log = _Lines()
        self.srv = ep.ProxyServer(("127.0.0.1", 0), frozenset(allowlist), resolver, self.log)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def ask(self, req: bytes) -> bytes:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(req)
        data = s.recv(1024)
        s.close()
        return data

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class _Lines:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines += [x for x in s.splitlines() if x]

    def flush(self):
        pass


def test_proxy_server_denies_allowlisted_name_with_private_answer(evidence):
    s = _Serve({"api.anthropic.com"}, lambda h, p: [(socket.AF_INET, ("10.1.2.3", p))])
    try:
        resp = s.ask(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n")
        assert resp.startswith(b"HTTP/1.1 403")
        time.sleep(0.1)
        assert any("DENY(private_address) api.anthropic.com:443" in l for l in s.log.lines), s.log.lines
        evidence(f"{resp.splitlines()[0].decode()} | log: {s.log.lines[-1]}")
    finally:
        s.close()


def test_proxy_server_real_resolver_refuses_loopback_name(evidence):
    # "localhost" on the allowlist, resolved by the real system resolver -> 127.0.0.1 / ::1.
    s = _Serve({"localhost"})
    try:
        resp = s.ask(b"CONNECT localhost:443 HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert resp.startswith(b"HTTP/1.1 403")
        time.sleep(0.1)
        assert any("DENY(private_address) localhost:443" in l for l in s.log.lines), s.log.lines
        evidence(s.log.lines[-1])
    finally:
        s.close()


def test_proxy_log_never_contains_headers():
    s = _Serve({"api.anthropic.com"}, lambda h, p: [(socket.AF_INET, ("10.1.2.3", p))])
    try:
        s.ask(b"GET http://example.com/?token=SECRETVALUE HTTP/1.1\r\nProxy-Authorization: Basic SECRETVALUE\r\n\r\n")
        s.ask(b"CONNECT example.com:443 HTTP/1.1\r\nX-Api-Key: SECRETVALUE\r\n\r\n")
        time.sleep(0.1)
        joined = "\n".join(s.log.lines)
        assert "SECRETVALUE" not in joined and "Authorization" not in joined
        assert len(s.log.lines) == 2
    finally:
        s.close()


# ================================================================ 1. filesystem

@needs_docker
def test_fs_host_dirs_have_no_host_files(world, evidence):
    script = r'''
import json, os
out = {}
for d in ["/Users", "/Volumes", "/host", "/private", "/mnt"]:
    if not os.path.lexists(d):
        out[d] = "absent"
        continue
    out[d] = "entries=%d" % sum(len(ds) + len(fs) for _, ds, fs in os.walk(d))
print(json.dumps(out))
'''
    out = run_json(world, script)
    assert all(v in ("absent", "entries=0") for v in out.values()), out
    evidence(json.dumps(out))


@needs_docker
def test_fs_unmounted_canaries_are_invisible(world, evidence):
    script = WALK + r'''
import json, sys
tag, marker = sys.argv[1], sys.argv[2]
names, contents = [], []
for path in walk_files():
    if tag in os.path.basename(path):
        names.append(path)
    try:
        if os.path.isfile(path) and not os.path.islink(path) and os.path.getsize(path) <= 1 << 20:
            with open(path, "rb") as f:
                if marker.encode() in f.read():
                    contents.append(path)
    except OSError:
        pass
print(json.dumps({"names": sorted(names), "contents": sorted(contents)}))
'''
    out = run_json(world, script, world.tag, world.marker)
    control = f"/home/rsi/canary-a-{world.tag}.txt"
    # Positive control: the canary planted in this arm's own (mounted) home IS found.
    assert out["names"] == [control] and out["contents"] == [control], out
    evidence(f"only the mounted control canary was found: {out['names']}; "
             f"home-B and fake-host canaries invisible (name and content scan)")


@needs_docker
def test_fs_mount_table_only_allowed_mounts(world, evidence):
    script = r'''
import json
mounts = []
for line in open("/proc/self/mountinfo"):
    pre, _, post = line.partition(" - ")
    f = pre.split()
    mounts.append({"root": f[3], "point": f[4], "opts": f[5], "fstype": post.split()[0]})
print(json.dumps(mounts))
'''
    r = run_py(world, script)
    mounts = json.loads(r.stdout.strip().splitlines()[-1])
    standard = {"/", "/proc", "/dev", "/dev/pts", "/dev/mqueue", "/dev/shm", "/sys", "/sys/fs/cgroup",
                "/tmp", "/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/usr/sbin/docker-init"}
    extra = sorted(m["point"] for m in mounts if m["point"] not in standard
                   and not m["point"].startswith(("/proc/", "/sys/")))
    assert extra == ["/home/rsi", "/work"], extra
    host_shared = {m["point"]: m["root"] for m in mounts
                   if m["fstype"] in ("virtiofs", "fakeowner", "fuse.grpcfuse", "9p", "osxfs")}
    assert set(host_shared) == {"/home/rsi", "/work"}, host_shared
    assert host_shared["/home/rsi"].endswith(f"/sandbox/experiments/{world.exp}/home-A")
    assert host_shared["/work"].endswith(f"/sandbox/experiments/{world.exp}/runs/{r.run_id}")
    tmp = next(m for m in mounts if m["point"] == "/tmp")
    assert tmp["fstype"] == "tmpfs"
    evidence(f"non-standard mounts={extra}; host-shared={host_shared}; /tmp={tmp['fstype']}")


@needs_docker
def test_fs_rootfs_read_only(world, evidence):
    script = r'''
import errno, json, os
root_opts = next(l.split()[5] for l in open("/proc/self/mountinfo") if l.split()[4] == "/")
res = {}
for d in ["/", "/etc", "/usr", "/usr/local/lib", "/opt", "/var", "/var/tmp", "/home", "/tmp", "/home/rsi", "/work"]:
    p = os.path.join(d, ".rsi-write-probe")
    try:
        with open(p, "w") as f:
            f.write("x")
        os.unlink(p)
        res[d] = "WRITABLE"
    except OSError as e:
        res[d] = errno.errorcode.get(e.errno, str(e.errno))
print(json.dumps({"root_opts": root_opts, "writes": res}))
'''
    out = run_json(world, script)
    assert out["root_opts"].split(",")[0] == "ro"
    w = out["writes"]
    assert {w[d] for d in ("/tmp", "/home/rsi", "/work")} == {"WRITABLE"}
    blocked = {d: v for d, v in w.items() if d not in ("/tmp", "/home/rsi", "/work")}
    assert set(blocked.values()) == {"EROFS"}, blocked
    evidence(f"/ mounted {out['root_opts'].split(',')[0]}; EROFS on {sorted(blocked)}; writable only /tmp /home/rsi /work")


@needs_docker
def test_fs_real_claude_md_not_readable(world, evidence):
    real = rs.REAL_HOME / ".claude" / "CLAUDE.md"
    if not real.is_file():
        pytest.skip("no ~/.claude/CLAUDE.md on this host")
    data = real.read_bytes()
    digest, size = hashlib.sha256(data).hexdigest(), len(data)
    del data
    script = WALK + r'''
import hashlib, json, sys
digest, size, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
same_size = same_hash = 0
for p in walk_files():
    try:
        if not os.path.islink(p) and os.path.isfile(p) and os.path.getsize(p) == size:
            same_size += 1
            with open(p, "rb") as f:
                if hashlib.sha256(f.read()).hexdigest() == digest:
                    same_hash += 1
    except OSError:
        pass
print(json.dumps({"same_size": same_size, "same_hash": same_hash, "host_path_exists": os.path.exists(path)}))
'''
    out = run_json(world, script, digest, str(size), str(real))
    assert out["same_hash"] == 0 and out["host_path_exists"] is False, out
    evidence(f"sha256 {digest[:12]}..: 0 matching files in the worker ({out['same_size']} of the same size); "
             f"{real} absent")


# ================================================================ 2. secrets

@needs_docker
def test_secrets_env_is_allowlist_only(world, monkeypatch, evidence):
    # Host canaries: if the host env leaked, these would show up.
    monkeypatch.setenv("RSI_HOST_CANARY_TOKEN", "host-canary-" + secrets.token_hex(4))
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://canary:canary@example.invalid/db")
    # /proc/1/environ is the env Docker gave the container (PID 1 = docker-init), before any
    # process adds its own (Python's PEP 538 adds LC_CTYPE to os.environ, for example).
    script = r'''
import hashlib, json, os, sys
pid1 = [x.split(b"=", 1)[0].decode() for x in open("/proc/1/environ", "rb").read().split(b"\0") if x]
got = hashlib.sha256(os.environ.get("ANTHROPIC_API_KEY", "").encode()).hexdigest()
print(json.dumps({"names": sorted(pid1), "process_names": sorted(os.environ), "anthropic_is_dummy": got == sys.argv[1]}))
'''
    r = run_py(world, script, hashlib.sha256(DUMMY_ANTHROPIC.encode()).hexdigest(), credentials=world.creds)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert set(out["process_names"]) - set(out["names"]) <= {"LC_CTYPE"}, out["process_names"]
    image_env = json.loads(rs.docker("image", "inspect", "--format", "{{json .Config.Env}}",
                                     world.worker_image).stdout)
    image_names = {e.split("=", 1)[0] for e in image_env}
    expected = set(rs.ENV_ALLOWLIST) | {"ANTHROPIC_API_KEY", "CODEX_API_KEY", "HOSTNAME"} | image_names
    names = set(out["names"])
    assert names <= expected, names - expected
    sensitive = {n for n in names | set(out["process_names"]) if SENSITIVE.search(n)}
    assert sensitive == {"ANTHROPIC_API_KEY", "CODEX_API_KEY"}, sensitive
    assert out["anthropic_is_dummy"]
    assert "RSI_HOST_CANARY_TOKEN" not in names and "SUPABASE_DB_URL" not in names
    # The secret value is never in docker's argv: only `-e NAME`.
    assert not any(DUMMY_ANTHROPIC in a or DUMMY_CODEX in a for a in r.argv)
    assert r.argv[r.argv.index("ANTHROPIC_API_KEY") - 1] == "-e"
    evidence(f"env names={sorted(names)}; sensitive={sorted(sensitive)} (the dummies); "
             f"argv has '-e ANTHROPIC_API_KEY' without value")


@needs_docker
def test_secrets_no_docker_socket(world, evidence):
    script = WALK + r'''
import json, stat
socks = []
for p in walk_files():
    try:
        if stat.S_ISSOCK(os.lstat(p).st_mode):
            socks.append(p)
    except OSError:
        pass
fixed = {p: os.path.lexists(p) for p in ["/var/run/docker.sock", "/run/docker.sock", "/run/host-services"]}
print(json.dumps({"sockets": socks, "fixed": fixed}))
'''
    out = run_json(world, script)
    assert out["sockets"] == [] and not any(out["fixed"].values()), out
    evidence(f"no unix sockets on any filesystem; {out['fixed']}")


# ================================================================ 3. privileges

@needs_docker
def test_privileges_non_root_no_caps_no_new_privs(world, evidence):
    script = WALK + r'''
import json
st = dict(l.split(":", 1) for l in open("/proc/self/status"))
st = {k: v.strip() for k, v in st.items() if k in ("Uid", "Gid", "NoNewPrivs", "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb", "Seccomp")}
setuid = []
for p in walk_files():
    try:
        m = os.lstat(p).st_mode
        if m & 0o6000 and not os.path.islink(p):
            setuid.append(p)
    except OSError:
        pass
try:
    os.setuid(0)
    became_root = True
except PermissionError:
    became_root = False
print(json.dumps({"uid": os.getuid(), "status": st, "setuid_files": setuid, "became_root": became_root}))
'''
    out = run_json(world, script)
    st = out["status"]
    assert out["uid"] == rs.WORKER_UID != 0
    assert st["NoNewPrivs"] == "1"
    for cap in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(st[cap], 16) == 0, (cap, st[cap])
    assert st["Seccomp"] == "2"
    assert out["setuid_files"] == [] and out["became_root"] is False
    evidence(f"uid={out['uid']} NoNewPrivs={st['NoNewPrivs']} CapEff={st['CapEff']} CapBnd={st['CapBnd']} "
             f"Seccomp={st['Seccomp']} setuid_files=0 setuid(0)=EPERM")


# ================================================================ 4. resources

@needs_docker
def test_resources_cgroup_limits(world, evidence):
    script = r'''
import json
r = {}
for f in ("memory.max", "memory.swap.max", "pids.max", "cpu.max"):
    r[f] = open("/sys/fs/cgroup/" + f).read().strip()
print(json.dumps(r))
'''
    out = run_json(world, script, memory="1g", pids=256, cpus="1.5")
    assert out == {"memory.max": str(1 << 30), "memory.swap.max": "0", "pids.max": "256",
                   "cpu.max": "150000 100000"}, out
    evidence(json.dumps(out))


@needs_docker
def test_resources_timeout_kills_sleep(world, evidence):
    seen = {}

    def watch():
        for _ in range(50):
            ids = rs.docker("ps", "-q", "--filter", f"label=rsi.experiment={world.exp}", check=False).stdout.split()
            if ids:
                seen.update(json.loads(rs.docker("inspect", ids[0]).stdout)[0])
                return
            time.sleep(0.2)

    t = threading.Thread(target=watch)
    t.start()
    r = world.sb.run(world.exp, "A", ["sleep", "600"], timeout_s=6)
    t.join()
    assert r.timed_out and r.duration_s < 6 + 15, r
    left = rs.docker("ps", "-aq", "--filter", f"name={r.container}").stdout.split()
    assert left == []
    # Docker-level config of the live container.
    hc, labels = seen["HostConfig"], seen["Config"]["Labels"]
    assert hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"] and "no-new-privileges" in hc["SecurityOpt"]
    assert hc["NetworkMode"] == rs.NET_INTERNAL and hc["AutoRemove"] and hc["Privileged"] is False
    assert labels["rsi.sandbox"] == "1" and labels["rsi.experiment"] == world.exp
    assert int(labels["rsi.deadline"]) <= int(time.time()) + 6
    binds = sorted((m["Destination"], m["RW"]) for m in seen["Mounts"])
    assert binds == [("/home/rsi", True), ("/work", True)], binds
    evidence(f"sleep 600 killed after {r.duration_s}s (timeout 6s), container gone; live config: "
             f"ReadonlyRootfs, CapDrop=ALL, no-new-privileges, network={hc['NetworkMode']}, "
             f"labels rsi.sandbox/experiment/deadline, mounts={binds}")


@needs_docker
def test_recover_kills_overdue_and_releases_stale_lock(world, evidence):
    name = f"rsi-test-overdue-{world.tag}"
    rs.docker("run", "-d", "--rm", "--name", name, "--network", "none", "--pull", "never",
              "--label", "rsi.sandbox=1", "--label", "rsi.role=worker",
              "--label", f"rsi.experiment={world.exp}", "--label", f"rsi.deadline={int(time.time()) - 5}",
              world.worker_image, "sleep", "300")
    lock = world.sb.rundir / f"t-stale-{world.tag}.lock"
    lock.write_text(json.dumps({"pid": 999999, "host": socket.gethostname(), "created": time.time(),
                                "expires": time.time() + 3600}))
    out = world.sb.recover()
    assert name in out["killed_containers"] and lock.name in out["released_locks"], out
    assert rs.docker("ps", "-aq", "--filter", f"name={name}").stdout.strip() == ""
    assert not lock.exists()
    evidence(f"recover: {out}")


@needs_docker
def test_cleanup_removes_old_and_exited_workers(world, evidence):
    name = f"rsi-test-old-{world.tag}"
    rs.docker("run", "-d", "--name", name, "--network", "none", "--pull", "never",
              "--label", "rsi.sandbox=1", "--label", "rsi.role=worker",
              "--label", f"rsi.experiment={world.exp}", "--label", f"rsi.deadline={int(time.time()) + 3600}",
              world.worker_image, "sleep", "300")
    time.sleep(1.5)
    kept = world.sb.cleanup(max_age_hours=1)            # young and within its deadline: kept
    assert name not in kept["removed_containers"]
    out = world.sb.cleanup(max_age_hours=0)             # now "older than 0 hours": removed
    assert name in out["removed_containers"], out
    assert rs.docker("ps", "-aq", "--filter", f"name={name}").stdout.strip() == ""
    evidence(f"cleanup(1h) kept it; cleanup(0h): {out}")


@needs_docker
def test_proxy_container_hardened(world, evidence):
    info = json.loads(rs.docker("inspect", rs.PROXY_NAME).stdout)[0]
    hc = info["HostConfig"]
    nets = sorted(info["NetworkSettings"]["Networks"])
    assert hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"] and "no-new-privileges" in hc["SecurityOpt"]
    assert info["Config"]["User"] == f"{rs.PROXY_UID}:{rs.PROXY_UID}" and hc["Privileged"] is False
    assert nets == [rs.NET_EGRESS, rs.NET_INTERNAL] and not hc.get("PortBindings")
    assert info["Image"] == rs.load_lock()["egress"]["id"]
    evidence(f"proxy: user={info['Config']['User']} ro-rootfs cap-drop=ALL no-new-privs networks={nets} "
             f"no published ports, image={info['Image'][:19]}..")


# ================================================================ 5. network

@pytest.fixture(scope="session")
def vm_listener(world):
    """Positive control: a listener inside the Docker VM's own network namespace."""
    port = 47000 + secrets.randbelow(2000)
    name = f"rsi-test-vmlistener-{world.tag}"
    code = ("import socket\ns=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
            f"s.bind(('0.0.0.0',{port}));s.listen(16)\nwhile True:\n c,_=s.accept();c.close()\n")
    rs.docker("run", "-d", "--rm", "--name", name, "--network", "host", "--pull", "never",
              "--label", "rsi.sandbox=1", "--label", "rsi.role=test-control", "--cap-drop", "ALL",
              "--user", "10001:10001", world.worker_image, "python3", "-c", code)
    time.sleep(1.5)
    yield port
    rs.docker("rm", "-f", name, check=False)


@needs_docker
def test_net_direct_internet_blocked(world, evidence):
    anthropic_ips = sorted({a[4][0] for a in socket.getaddrinfo("api.anthropic.com", 443, socket.AF_INET)})
    targets = ["1.1.1.1:443", "8.8.8.8:53", *(f"{ip}:443" for ip in anthropic_ips[:2])]
    out = run_json(world, TCP, *targets)
    assert not any(v == "CONNECTED" for v in out.values()), out
    udp = run_json(world, DNS, json.dumps({"names": [], "udp": [["8.8.8.8", "example.com"],
                                                                ["1.1.1.1", "example.com"]]}))["udp"]
    assert all("error" in v for v in udp.values()), udp
    ctl = control_json(world, TCP, "1.1.1.1:443", f"{anthropic_ips[0]}:443")
    assert all(v == "CONNECTED" for v in ctl.values()), ctl
    evidence(f"worker: {out}; UDP DNS: {udp}; control container on bridge: {ctl}")


@needs_docker
def test_net_host_vm_and_metadata_blocked(world, vm_listener, evidence):
    st = world.sb.net_status()
    # Finding: a plain `internal: true` network still gives the bridge a gateway IP in the VM,
    # and VM listeners answer on it. net-up sets inhibit_ipv4, so there is no gateway at all.
    assert st["internal"]["internal"] is True
    assert st["internal"]["options"].get("com.docker.network.bridge.inhibit_ipv4") == "true"
    assert st["internal"]["gateways"] in ([None], [""], [])
    names = control_json(world, DNS, json.dumps({"names": ["host.docker.internal", "http.docker.internal",
                                                           "gateway.docker.internal"], "udp": []}))["getaddrinfo"]
    hdi, http_proxy, gw = names["host.docker.internal"][0], names["http.docker.internal"][0], names["gateway.docker.internal"][0]
    egress_gw = st["egress"]["gateways"][0]
    lan = host_lan_ip()
    lan_ports = [p for p in (7000, 5000, 10000) if lan and host_can_connect(lan, p)]
    hdi_ports = [7000, 10000, 80, 443]
    targets = ([f"host.docker.internal:{p}" for p in hdi_ports[:1]] + [f"{hdi}:{p}" for p in hdi_ports]
               + [f"{http_proxy}:3128", f"{gw}:80"]
               + [f"{ip}:{p}" for ip in ("172.17.0.1", egress_gw) for p in (vm_listener, 10000, 2375, 22)]
               + [f"{lan}:{p}" for p in lan_ports] + ["169.254.169.254:80"])
    out = run_json(world, TCP, *targets)
    assert not any(v == "CONNECTED" for v in out.values()), out
    ctl_targets = [f"172.17.0.1:{vm_listener}", f"host.docker.internal:{hdi_ports[0]}", f"{http_proxy}:3128"]
    ctl = control_json(world, TCP, *ctl_targets)
    assert ctl[f"172.17.0.1:{vm_listener}"] == "CONNECTED", ctl
    assert lan is None or lan_ports, "no LAN listener to probe"
    evidence(f"worker blocked on {len(targets)} targets incl. host.docker.internal={hdi}, "
             f"http.docker.internal={http_proxy}:3128, VM listener :{vm_listener} via 172.17.0.1 and {egress_gw}, "
             f"LAN {lan}:{lan_ports} (host reaches them), 169.254.169.254; "
             f"errors={sorted({v.split(':')[0] for v in out.values()})}; control container: {ctl}")


@needs_docker
def test_net_external_dns_fails(world, evidence):
    names = ["example.com", "api.anthropic.com", f"rsi-test-{world.tag}.supabase.co", "host.docker.internal"]
    out = run_json(world, DNS, json.dumps({"names": names + ["rsi-egress-proxy"],
                                           "udp": [["127.0.0.11", "example.com"]]}))
    g = out["getaddrinfo"]
    assert all(isinstance(g[n], str) and "gaierror" in g[n] for n in names), g
    assert isinstance(g["rsi-egress-proxy"], list) and g["rsi-egress-proxy"], g   # internal names still work
    udp = out["udp"]["127.0.0.11/example.com"]
    assert udp.get("answers", 0) == 0, udp
    evidence(f"external names -> gaierror; embedded DNS for example.com -> {udp}; "
             f"rsi-egress-proxy -> {g['rsi-egress-proxy']}")


@needs_docker
def test_net_curl_noproxy_fails(world, evidence):
    codes = {}
    for url in ("https://example.com", "https://api.anthropic.com/v1/models"):
        r = world.sb.run(world.exp, "A", ["curl", "-sS", "--noproxy", "*", "--max-time", "8", "-o", "/dev/null",
                                          url], timeout_s=60)
        codes[url] = (r.exit_code, r.stderr.strip().splitlines()[-1][:90] if r.stderr.strip() else "")
        assert r.exit_code not in (0, None), codes
    evidence(f"curl --noproxy '*': {codes}")


@needs_docker
def test_proxy_allows_anthropic_tls(world, evidence):
    t0 = time.time() - 1
    r = world.sb.run(world.exp, "A", [
        "curl", "-sS", "--max-time", "20", "-o", "/dev/null",
        "-w", "%{http_connect} %{http_code} %{ssl_verify_result}",
        "-H", f"x-api-key: {DUMMY_ANTHROPIC}", "-H", "anthropic-version: 2023-06-01",
        "https://api.anthropic.com/v1/models"], timeout_s=60)
    connect, code, verify = r.stdout.split()
    assert connect == "200" and 400 <= int(code) < 500 and verify == "0", (r.stdout, r.stderr)
    time.sleep(0.5)
    log = proxy_log_since(t0)
    assert "ALLOW api.anthropic.com:443" in log
    assert "DUMMY" not in log and "x-api-key" not in log.lower()
    evidence(f"CONNECT={connect} HTTP={code} ssl_verify_result={verify}; proxy log: ALLOW api.anthropic.com:443")


@needs_docker
def test_proxy_denies_everything_else(world, evidence):
    t0 = time.time() - 1
    supa = f"rsi-test-{world.tag}.supabase.co"
    reqs = ["CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
            f"CONNECT {supa}:443 HTTP/1.1\r\nHost: {supa}:443\r\n\r\n",
            "CONNECT 1.1.1.1:443 HTTP/1.1\r\n\r\n",
            "CONNECT [2606:4700:4700::1111]:443 HTTP/1.1\r\n\r\n",
            "CONNECT 16843009:443 HTTP/1.1\r\n\r\n",
            "CONNECT api.anthropic.com:80 HTTP/1.1\r\n\r\n",
            "CONNECT api.anthropic.com:8443 HTTP/1.1\r\n\r\n",
            "GET http://api.anthropic.com/ HTTP/1.1\r\nHost: api.anthropic.com\r\n\r\n",
            "GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"]
    out = run_json(world, PROXY_RAW, json.dumps(reqs))
    assert all(v.startswith("HTTP/1.1 403") for v in out.values()), out
    r = world.sb.run(world.exp, "A", ["curl", "-sS", "--max-time", "10", "-o", "/dev/null", "-w",
                                      "%{http_connect}", "https://example.com"], timeout_s=60)
    assert r.stdout.strip() == "403" and r.exit_code != 0
    time.sleep(0.5)
    log = proxy_log_since(t0)
    for line in ("DENY(not_allowlisted) example.com:443", f"DENY(not_allowlisted) {supa}:443",
                 "DENY(ip_literal) 1.1.1.1:443", "DENY(ip_literal) [2606:4700:4700::1111]:443",
                 "DENY(ip_literal) 16843009:443", "DENY(port) api.anthropic.com:80",
                 "DENY(port) api.anthropic.com:8443", "DENY(method) api.anthropic.com:80",
                 "DENY(method) example.com:80"):
        assert line in log, (line, log)
    evidence("all 9 raw requests -> 403; curl example.com CONNECT=403; log has "
             "not_allowlisted/ip_literal/port/method lines")


@needs_docker
def test_proxy_denies_sni_mismatch(world, evidence):
    t0 = time.time() - 1
    out = run_json(world, SNI)
    assert out["matching_sni"].get("tls", "").startswith("TLSv1"), out
    assert out["mismatched_sni"]["connect"].startswith("HTTP/1.1 200") and "tls_error" in out["mismatched_sni"], out
    time.sleep(0.5)
    assert "DENY(sni_mismatch) api.anthropic.com:443" in proxy_log_since(t0)
    evidence(f"SNI api.anthropic.com -> {out['matching_sni']['tls']} (verified); "
             f"SNI example.com in an api.anthropic.com tunnel -> {out['mismatched_sni']['tls_error'][:60]}")


# ================================================================ 6. headless CLIs with dummy keys

@needs_docker
def test_claude_headless_dummy_key_terminates(world, evidence):
    t0 = time.time() - 1
    r = world.sb.run(world.exp, "A", [
        "claude", "-p", "say ok", "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--max-turns", "1", "--output-format", "json", "--settings", '{"disableAllHooks":true}'],
        timeout_s=120, credentials=world.creds)
    assert not r.timed_out, r
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["is_error"] is True and (res.get("api_error_status") == 401 or "API key" in res.get("result", "")), res
    time.sleep(0.5)
    log = proxy_log_since(t0)
    assert "ALLOW api.anthropic.com:443" in log
    evidence(f"exit={r.exit_code} in {r.duration_s}s, not timed out; is_error={res['is_error']} "
             f"api_error_status={res.get('api_error_status')} result={res.get('result')!r}; egress via proxy")


@needs_docker
def test_codex_exec_dummy_key_terminates(world, evidence):
    t0 = time.time() - 1
    r = world.sb.run(world.exp, "A", [
        "codex", "-a", "never", "exec", "-s", "workspace-write", "--json", "--ephemeral",
        "--ignore-user-config", "--skip-git-repo-check", "say ok"], timeout_s=150, credentials=world.creds)
    assert not r.timed_out, r
    assert r.exit_code != 0 and "401" in r.stdout and "invalid_api_key" in r.stdout, r.stdout[-1500:]
    last = json.loads(r.stdout.strip().splitlines()[-1])
    time.sleep(0.5)
    log = proxy_log_since(t0)
    assert "ALLOW api.openai.com:443" in log
    denied = sorted({l.split()[-1] for l in log.splitlines() if "DENY" in l})
    evidence(f"exit={r.exit_code} in {r.duration_s}s, not timed out; last event type={last.get('type')} "
             f"(401 invalid_api_key); proxy ALLOW api.openai.com:443, denied={denied}")
