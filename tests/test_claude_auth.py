import json
import subprocess

import pytest

from agent_memory import claude_auth as auth, distill


@pytest.fixture
def token_home(tmp_path, monkeypatch):
    home = tmp_path / "automation"
    home.mkdir(mode=0o700)
    secret = home / ".env"
    secret.write_text("CLAUDE_CODE_OAUTH_TOKEN=test-automation-token\n")
    secret.chmod(0o600)
    monkeypatch.setenv("AMEM_CLAUDE_AUTH_HOME", str(home))
    return home


def test_token_is_child_only_and_overrides_personal_login(token_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-personal-token")
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, '{"type":"result","result":"ok"}', "")

    monkeypatch.setattr(distill.subprocess, "run", run)
    assert distill.run_cli(["claude", "-p"], "test", env={"ANTHROPIC_API_KEY": "test-api", "HOME": "/example"})
    assert seen[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "test-automation-token"
    assert seen[0]["CLAUDE_CONFIG_DIR"] == str(token_home / ".claude")
    assert "ANTHROPIC_API_KEY" not in seen[0]
    import os
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "test-personal-token"


@pytest.mark.parametrize("mode", ["missing", "permissions", "symlink", "empty"])
def test_bad_secret_fails_closed_without_running_cli(token_home, monkeypatch, mode):
    path = token_home / ".env"
    if mode == "missing":
        path.unlink()
    elif mode == "permissions":
        path.chmod(0o644)
    elif mode == "symlink":
        target = token_home / "other"
        path.rename(target)
        path.symlink_to(target)
    else:
        path.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n")
    monkeypatch.setattr(distill.subprocess, "run", lambda *a, **k: pytest.fail("CLI must not run"))
    with pytest.raises(distill.WriterAuthError, match="dedicated automation token"):
        distill.run_cli(["claude", "-p"], "test")


@pytest.mark.parametrize("code", [0, 1])
def test_auth_envelope_stops_entire_batch_and_new_batch_can_recover(monkeypatch, code):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, code, json.dumps({"type": "result", "is_error": True,
                                          "result": "Failed to authenticate: OAuth session expired"}), "")
    monkeypatch.setattr(distill.subprocess, "run", run)
    with auth.batch() as batch:
        for _ in range(3):
            with pytest.raises(distill.WriterAuthError):
                distill.run_cli(["claude"], "test", keep_error_envelope=True)
        assert batch.failed
    assert len(calls) == 1
    with auth.batch():
        assert not auth.failed()


def test_successful_model_text_mentioning_auth_is_not_auth_failure(monkeypatch):
    monkeypatch.setattr(distill.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(
        argv, 0, json.dumps({"type": "result", "subtype": "success", "result": "Failed to authenticate example"}), ""))
    assert "example" in distill.run_cli(["claude"], "test")


def test_non_auth_failure_does_not_trip_batch(monkeypatch):
    monkeypatch.setattr(distill.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "overloaded"))
    with auth.batch():
        with pytest.raises(distill.WriterError, match="overloaded"):
            distill.run_cli(["claude"], "test")
        assert not auth.failed()
