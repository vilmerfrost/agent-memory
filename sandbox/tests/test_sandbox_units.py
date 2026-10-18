"""Unit tests for the runner: layout, sanitizer, credentials, docker argv, locks. No Docker needed."""
from __future__ import annotations

import json
import os
import socket
import stat
import time
import tomllib
from pathlib import Path

import pytest

import rsi_sandbox as rs

DUMMY = "sk-ant-api03-DUMMY-NOT-A-KEY"


@pytest.fixture
def sb(tmp_path):
    s = rs.Sandbox(tmp_path / "sandbox")
    s.init()
    return s


def test_init_layout_mode_700(sb):
    for name in rs.LAYOUT:
        d = sb.root / name
        assert d.is_dir() and stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE(sb.root.stat().st_mode) == 0o700


def test_sandbox_root_inside_forbidden_path_refused():
    with pytest.raises(rs.SandboxError):
        rs.Sandbox(rs.REAL_HOME / ".claude" / "sandbox")
    with pytest.raises(rs.SandboxError):
        rs.Sandbox(rs.REAL_HOME)


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".hidden", "x" * 80, "a b", "a..b"])
def test_ids_validated(sb, bad):
    with pytest.raises(rs.SandboxError):
        sb.experiment_dir(bad)


# ---------------------------------------------------------------- prepare-arm / sanitizer

CODEX_TOML = '''model = "gpt-5"
notify = [
  "python3",
  "/Users/x/notify.py",
]
approval_policy = "never"
mcp_servers.inline.command = "evil"

[mcp_servers.github]
command = "npx"
args = ["-y", "@x/server-github"]

[mcp_servers.github.env]
GITHUB_TOKEN = "ghp_fake"

[shell_environment_policy]
inherit = "all"
set = { FOO = "bar" }

[profiles.fast]
model = "gpt-5-mini"
'''


def _baseline(sb, name="b1") -> Path:
    b = sb.baseline_dir(name)
    (b / ".claude" / "skills" / "s1").mkdir(parents=True)
    (b / ".claude" / "hooks").mkdir()
    (b / ".claude" / "plugins" / "marketplaces").mkdir(parents=True)
    (b / ".claude" / "projects" / "p").mkdir(parents=True)
    (b / ".codex").mkdir()
    (b / ".agents" / "skills" / "s2").mkdir(parents=True)
    (b / "proj" / ".claude").mkdir(parents=True)
    (b / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus", "hooks": {"Stop": []}, "statusLine": {"type": "command", "command": "x"},
        "mcpServers": {"a": {}}, "enabledPlugins": {"p@m": True}, "extraKnownMarketplaces": {"m": {}},
        "env": {"ANTHROPIC_BASE_URL": "http://evil"}, "apiKeyHelper": "cat /x", "permissions": {"allow": ["Bash"]},
        "disableAllHooks": False}))
    (b / ".claude" / "settings.local.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    (b / ".claude" / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"email": "x"}, "primaryApiKey": "sk-fake", "projects": {"/Users/x": {}},
        "mcpServers": {"b": {}}, "numStartups": 3}))
    (b / ".claude" / ".credentials.json").write_text("{}")
    (b / ".claude" / "CLAUDE.md").write_text("# baseline rules\n")
    (b / ".claude" / "skills" / "s1" / "SKILL.md").write_text("---\nname: s1\n---\n")
    (b / ".claude" / "hooks" / "h.sh").write_text("echo hook\n")
    (b / ".claude" / "plugins" / "marketplaces" / "x.json").write_text("{}")
    (b / ".claude" / "projects" / "p" / "t.jsonl").write_text("{}\n")
    (b / ".codex" / "config.toml").write_text(CODEX_TOML)
    (b / ".codex" / "auth.json").write_text("{}")
    (b / ".codex" / "AGENTS.md").write_text("# agents\n")
    (b / ".agents" / "skills" / "s2" / "SKILL.md").write_text("---\nname: s2\n---\n")
    (b / "proj" / ".mcp.json").write_text("{}")
    (b / "proj" / ".env").write_text("NEVER_READ=1\n")
    (b / "proj" / ".env.local").write_text("NEVER_READ=1\n")
    (b / "proj" / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}, "env": {"X": "1"}}))
    (b / "link-out").symlink_to(rs.REAL_HOME / ".claude")
    return b


def _removed(report, path):
    return {r["item"] for r in report["removed"] if r["path"] == path}


def test_prepare_arm_strips_everything(sb):
    _baseline(sb)
    rep = sb.prepare_arm("e1", "b1", "A")
    home = sb.arm_home("e1", "A")
    s = json.loads((home / ".claude" / "settings.json").read_text())
    assert s == {"model": "opus", "permissions": {"allow": ["Bash"]}, "disableAllHooks": True}
    assert _removed(rep, ".claude/settings.json") >= {"hooks", "statusLine", "mcpServers", "enabledPlugins",
                                                       "extraKnownMarketplaces", "env", "apiKeyHelper"}
    assert json.loads((home / ".claude" / "settings.local.json").read_text()) == {}
    state = json.loads((home / ".claude" / ".claude.json").read_text())
    assert state == {"numStartups": 3}
    assert json.loads((home / "proj" / ".claude" / "settings.json").read_text()) == {}
    # Never copied.
    for rel in (".claude/.credentials.json", ".codex/auth.json", "proj/.mcp.json", "proj/.env",
                "proj/.env.local", ".claude/hooks", ".claude/plugins", ".claude/projects", "link-out"):
        assert not os.path.lexists(home / rel), rel
    # Kept.
    for rel in (".claude/CLAUDE.md", ".claude/skills/s1/SKILL.md", ".codex/AGENTS.md", ".agents/skills/s2/SKILL.md"):
        assert (home / rel).is_file(), rel
    kinds = {(r["path"], r["kind"]) for r in rep["removed"]}
    assert {("proj/.env", "env_file"), ("proj/.env.local", "env_file"), ("link-out", "symlink"),
            (".codex/auth.json", "credential"), (".claude/.credentials.json", "credential"),
            (".claude/plugins/", "plugin"), (".claude/hooks/", "hook"), ("proj/.mcp.json", "mcp")} <= kinds
    report = json.loads((sb.experiment_dir("e1") / "sanitize-report.json").read_text())
    assert report["A"]["removed"] == rep["removed"]
    assert {"path": ".claude/settings.json", "key": "disableAllHooks", "value": True} in rep["set"]


def test_prepare_arm_codex_toml(sb):
    _baseline(sb)
    rep = sb.prepare_arm("e1", "b1", "A")
    text = (sb.arm_home("e1", "A") / ".codex" / "config.toml").read_text()
    data = tomllib.loads(text)
    assert data == {"model": "gpt-5", "approval_policy": "never", "profiles": {"fast": {"model": "gpt-5-mini"}}}
    assert _removed(rep, ".codex/config.toml") == {"notify", "mcp_servers.inline.command", "[mcp_servers.github]",
                                                   "[mcp_servers.github.env]", "[shell_environment_policy]"}
    assert "ghp_fake" not in text


def test_prepare_arm_never_reads_env_files(sb, monkeypatch):
    _baseline(sb)
    opened = []
    real_open = open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)
    monkeypatch.setattr("builtins.open", spy)
    real_read_text = Path.read_text

    def spy_read(self, *a, **k):
        opened.append(str(self))
        return real_read_text(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", spy_read)
    sb.prepare_arm("e1", "b1", "A")
    assert not [p for p in opened if ".env" in p]


def test_prepare_arm_hook_experiment_keeps_hooks(sb):
    _baseline(sb)
    rep = sb.prepare_arm("e1", "b1", "B", hook_experiment=True)
    home = sb.arm_home("e1", "B")
    s = json.loads((home / ".claude" / "settings.json").read_text())
    assert s["hooks"] == {"Stop": []} and s.get("disableAllHooks") in (None, False)
    assert (home / ".claude" / "hooks" / "h.sh").is_file()
    assert "env" not in s and "mcpServers" not in s
    assert rep["hook_experiment"] is True and rep["set"] == []


def test_prepare_arm_refuses_existing_home_and_bad_json(sb):
    b = _baseline(sb)
    sb.prepare_arm("e1", "b1", "A")
    with pytest.raises(rs.SandboxError, match="exists"):
        sb.prepare_arm("e1", "b1", "A")
    sb.prepare_arm("e1", "b1", "A", force=True)
    (b / ".claude" / "settings.json").write_text("{not json")
    with pytest.raises(rs.SandboxError, match="not valid JSON"):
        sb.prepare_arm("e2", "b1", "A")


def test_codex_sanitizer_fails_closed_on_leftovers():
    # A key the line stripper cannot see (inside an inline table of another table) is caught by the verifier.
    text = 'profiles = { p = { notify = ["x"] } }\n'
    with pytest.raises(rs.SandboxError, match="forbidden keys"):
        rs.sanitize_codex_toml(text, "config.toml", [])
    with pytest.raises(rs.SandboxError, match="not valid TOML"):
        rs.sanitize_codex_toml("[[[", "config.toml", [])


# ---------------------------------------------------------------- credentials

def test_credentials_none():
    assert rs.load_credentials("none") == {}


def test_credentials_env_file(tmp_path):
    f = tmp_path / "dummy-creds.txt"
    f.write_text(f"# test\nANTHROPIC_API_KEY={DUMMY}\nCODEX_API_KEY='sk-proj-DUMMY'\n")
    assert rs.load_credentials(f"env-file:{f}") == {"ANTHROPIC_API_KEY": DUMMY, "CODEX_API_KEY": "sk-proj-DUMMY"}


@pytest.mark.parametrize("content", ["CLAUDE_CODE_OAUTH_TOKEN=x\n", "SUPABASE_DB_URL=x\n", "garbage\n"])
def test_credentials_env_file_rejects_other_names(tmp_path, content):
    f = tmp_path / "creds.txt"
    f.write_text(content)
    with pytest.raises(rs.CredentialError):
        rs.load_credentials(f"env-file:{f}")


def test_credentials_env_file_refuses_dotenv_and_forbidden(tmp_path):
    with pytest.raises(rs.CredentialError, match=".env"):
        rs.load_credentials(f"env-file:{tmp_path / '.env'}")
    with pytest.raises(rs.CredentialError, match="forbidden"):
        rs.load_credentials(f"env-file:{rs.REAL_HOME / '.codex' / 'auth.json'}")


def test_credentials_keychain_builds_security_call_without_calling_keychain():
    calls = []

    class P:
        returncode, stdout = 0, f"{DUMMY}\n"

    def fake_runner(argv, **kw):
        calls.append(argv)
        return P()
    out = rs.load_credentials("keychain:rsi-sandbox/anthropic", runner=fake_runner)
    assert out == {"ANTHROPIC_API_KEY": DUMMY}
    assert calls == [["/usr/bin/security", "find-generic-password", "-s", "rsi-sandbox", "-a", "anthropic", "-w"]]
    with pytest.raises(rs.CredentialError):
        rs.load_credentials("keychain:rsi-sandbox/x", name="CLAUDE_CODE_OAUTH_TOKEN", runner=fake_runner)
    with pytest.raises(rs.CredentialError):
        rs.load_credentials("keychain:nosep", runner=fake_runner)
    with pytest.raises(rs.CredentialError):
        rs.load_credentials("vault:x")


def test_credentials_keychain_reads_only_the_sandbox_items():
    """A wrong spec must never read a personal Keychain item: refused before `security` runs."""
    calls = []

    class P:
        returncode, stdout = 0, f"{DUMMY}\n"

    def fake_runner(argv, **kw):
        calls.append(argv)
        return P()
    for spec in ("keychain:Claude Code-credentials/vilmerfrost", "keychain:amem-admin-db/postgres",
                 "keychain:amem-archive/k1", "keychain:Supabase CLI/supabase", "keychain:rsi-sandbox/x",
                 "keychain:RSI-SANDBOX/anthropic"):
        with pytest.raises(rs.CredentialError, match="not allowed"):
            rs.load_credentials(spec, runner=fake_runner)
    assert calls == []
    assert rs.load_credentials("keychain:rsi-sandbox/openai", name="CODEX_API_KEY", runner=fake_runner) == \
        {"CODEX_API_KEY": DUMMY}


# ---------------------------------------------------------------- docker argv

def test_run_argv_flags_and_env_allowlist(sb):
    sb.arm_home("e1", "A").mkdir(parents=True)
    (sb.experiment_dir("e1") / "runs" / "r1").mkdir(parents=True)
    argv, name, deadline = sb.build_run_argv(
        experiment="e1", arm="A", run_id="r1", command=["claude", "-p", "x"], image="sha256:abc",
        timeout_s=60, memory="2g", cpus="2", pids=256, credential_names=["ANTHROPIC_API_KEY"])
    joined = " ".join(argv)
    for flag in ("--rm", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--pids-limit 256", "--memory 2g", "--memory-swap 2g", "--cpus 2",
                 f"--network {rs.NET_INTERNAL}", "--user 10001:10001", "--label rsi.sandbox=1",
                 "--label rsi.experiment=e1", f"--label rsi.deadline={deadline}", "--pull never"):
        assert flag in joined, flag
    assert "--tmpfs /tmp:" in joined and "docker.sock" not in joined and "--privileged" not in joined
    env = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    names = [e.split("=", 1)[0] for e in env]
    assert sorted(names) == sorted([*rs.ENV_ALLOWLIST, "ANTHROPIC_API_KEY"])
    assert "ANTHROPIC_API_KEY" in env                    # name only, no value
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert len(mounts) == 2 and all(m.startswith("type=bind,source=") for m in mounts)
    assert argv[-4:] == ["sha256:abc", "claude", "-p", "x"]
    assert name.startswith("rsi-w-e1-A-") and deadline - time.time() <= 61


def test_run_argv_rejects_bad_limits_and_names(sb):
    sb.arm_home("e1", "A").mkdir(parents=True)
    (sb.experiment_dir("e1") / "runs" / "r1").mkdir(parents=True)
    base = dict(experiment="e1", arm="A", run_id="r1", command=["true"], image="img", timeout_s=60,
                memory="1g", cpus="1", pids=64, credential_names=[])
    for bad in ({"timeout_s": 0}, {"timeout_s": rs.MAX_TIMEOUT_S + 1}, {"memory": "1g --privileged"},
                {"cpus": "all"}, {"pids": 1}, {"command": []}, {"arm": "a"}):
        with pytest.raises((rs.SandboxError, ValueError)):
            sb.build_run_argv(**{**base, **bad})
    with pytest.raises(rs.CredentialError):
        sb.build_run_argv(**{**base, "credential_names": ["SUPABASE_DB_URL"]})


def test_docker_cli_env_is_minimal(monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-real-key-would-be-here")
    env = rs._docker_cli_env({"ANTHROPIC_API_KEY": DUMMY})
    assert set(env) <= {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
                        "DOCKER_TLS_VERIFY", "DOCKER_CLI_HINTS", "ANTHROPIC_API_KEY"}
    assert env["ANTHROPIC_API_KEY"] == DUMMY
    assert "ANTHROPIC_API_KEY" not in rs._docker_cli_env()


# ---------------------------------------------------------------- locks

def test_lock_exclusive_and_stale_release(sb):
    p = sb.acquire_lock("e1.A", ttl_s=60)
    with pytest.raises(rs.SandboxError, match="locked"):
        sb.acquire_lock("e1.A", ttl_s=60)
    p.unlink()
    p.write_text(json.dumps({"pid": 999999, "host": socket.gethostname(), "created": 0, "expires": time.time() + 60}))
    assert sb._lock_stale(p)
    sb.acquire_lock("e1.A", ttl_s=60)           # a stale lock (dead pid) is taken over
    p.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "created": 0, "expires": 1}))
    assert sb._lock_stale(p)                     # expired


def test_credentials_keychain_file_override(tmp_path, monkeypatch):
    """The paid rehearsal runs under a temp HOME, which hides the login keychain from `security` (exit 44):
    AMEM_RSI_KEYCHAIN names the keychain file to search. Unset (production), the argv is unchanged."""
    calls = []

    class P:
        returncode, stdout = 0, f"{DUMMY}\n"

    def fake_runner(argv, **kw):
        calls.append(argv)
        return P()
    kc = tmp_path / "login.keychain-db"
    kc.write_bytes(b"")
    monkeypatch.setenv(rs.KEYCHAIN_FILE_ENV, str(kc))
    assert rs.load_credentials("keychain:rsi-sandbox/openai", name="OPENAI_API_KEY", runner=fake_runner) == {"OPENAI_API_KEY": DUMMY}
    assert calls[-1] == ["/usr/bin/security", "find-generic-password", "-s", "rsi-sandbox", "-a", "openai", "-w", str(kc)]
    monkeypatch.setenv(rs.KEYCHAIN_FILE_ENV, str(tmp_path / "missing.keychain-db"))
    with pytest.raises(rs.CredentialError, match="not a keychain file"):
        rs.load_credentials("keychain:rsi-sandbox/openai", name="OPENAI_API_KEY", runner=fake_runner)
    monkeypatch.delenv(rs.KEYCHAIN_FILE_ENV)
    rs.load_credentials("keychain:rsi-sandbox/openai", name="OPENAI_API_KEY", runner=fake_runner)
    assert calls[-1][-1] == "-w"
