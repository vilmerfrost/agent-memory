"""The two sandbox changes Mission 005 needs: $RSI_SANDBOX_DIR as the default root (library and CLI), and
`network="none"` runs (stub agent, checks) that take no credentials and need no rsi-internal network."""

from __future__ import annotations

import json

import pytest
from rsi_replay_support import rs


def test_sandbox_default_root_honors_rsi_sandbox_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path / "sb"))
    assert rs.Sandbox().root == tmp_path / "sb"
    assert rs.default_root() == tmp_path / "sb"
    assert rs.main(["init"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["root"] == str(tmp_path / "sb") and (tmp_path / "sb" / "experiments").is_dir()
    assert rs.Sandbox(tmp_path / "explicit").root == tmp_path / "explicit"      # an explicit root still wins
    monkeypatch.delenv("RSI_SANDBOX_DIR")
    assert rs.Sandbox().root == rs.DEFAULT_ROOT


def test_sandbox_root_env_still_refuses_forbidden_paths(monkeypatch):
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(rs.REAL_HOME / ".claude" / "sandbox"))
    with pytest.raises(rs.SandboxError, match="forbidden"):
        rs.Sandbox()


def _argv_kw(sb, **kw):
    base = dict(experiment="exp-20261017-01", arm="A", run_id="r1", command=["true"], image="sha256:x",
                timeout_s=10, memory="1g", cpus="1", pids=64, credential_names=[])
    return {**base, **kw}


def test_network_none_runs(tmp_path):
    sb = rs.Sandbox(tmp_path / "sb")
    sb.init()
    home = sb.arm_home("exp-20261017-01", "A")
    home.mkdir(parents=True)
    (sb.experiment_dir("exp-20261017-01") / "runs" / "r1").mkdir(parents=True)
    argv, _, _ = sb.build_run_argv(**_argv_kw(sb, network="none"))
    assert argv[argv.index("--network") + 1] == "none"
    argv, _, _ = sb.build_run_argv(**_argv_kw(sb))
    assert argv[argv.index("--network") + 1] == rs.NET_INTERNAL                # default unchanged
    with pytest.raises(rs.SandboxError, match="network must be"):
        sb.build_run_argv(**_argv_kw(sb, network="host"))
    with pytest.raises(rs.SandboxError, match="network must be"):
        sb.build_run_argv(**_argv_kw(sb, network="bridge"))
    with pytest.raises(rs.CredentialError, match="no credentials"):
        sb.build_run_argv(**_argv_kw(sb, network="none", credential_names=["ANTHROPIC_API_KEY"]))


def test_cli_run_accepts_only_known_networks(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path / "sb"))
    with pytest.raises(SystemExit):
        rs.main(["run", "--experiment", "exp-20261017-01", "--arm", "A", "--network", "host", "--", "true"])
    assert "invalid choice" in capsys.readouterr().err
