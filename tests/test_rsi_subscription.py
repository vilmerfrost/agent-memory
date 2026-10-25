"""Offline protocol and isolation-contract tests. No real clients or credentials."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from agent_memory import rsi_subscription as S


@pytest.fixture
def setup(tmp_path):
    home, workspace, arm = (tmp_path / name for name in ("auth", "workspace", "arm"))
    for p in (home, workspace, arm):
        p.mkdir(mode=0o700)
    auth = S.SubscriptionAuth(home=home.resolve(), client=Path(sys.executable).resolve(),
                              docker=Path(sys.executable).resolve(), worker_image="sha256:" + "1" * 64)
    return auth, workspace.resolve(), arm.resolve()


def response(*, model=S.MODELS["claude"], tools=(), permission_denials=(), extra_usage=None):
    return "\n".join(json.dumps(x) for x in [
        {"type": "system", "subtype": "init", "model": model, "tools": list(tools)},
        {"type": "result", "subtype": "success", "is_error": False, "result": "answer",
         "modelUsage": {model: {}}, "usage": {"input_tokens": 10, "output_tokens": 4, **(extra_usage or {})},
         "permission_denials": list(permission_denials)},
    ])


def fake_client(calls, raw=None, auth_method="claude.ai"):
    def process(argv, **kwargs):
        calls.append((argv, {**kwargs, "env": dict(kwargs["env"])}))
        if argv[1:] == ["auth", "status"]:
            return 0, json.dumps({"loggedIn": True, "authMethod": auth_method,
                                 "subscriptionType": "max", "email": "never-record@example.invalid"}), ""
        return 0, raw or response(), "never persist raw client stderr"
    return process


def invoke(setup, process, **kwargs):
    auth, workspace, arm = setup
    return S.run("claude", "Do the task", model=S.MODELS["claude"], effort="high", workspace=workspace,
                 arm_home=arm, timeout_s=30, auth=auth, _process=process, **kwargs)


def test_subscription_run_does_not_forward_parent_secrets_or_config(setup, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-forward")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "do-not-inherit")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/real/host/config")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/host/evil.js")
    calls = []
    result = invoke(setup, fake_client(calls), tools_allowed=False)
    assert result.text == "answer"
    assert result.usage == {"input_tokens": 10, "output_tokens": 4}
    assert result.evidence["billing_source"] == "subscription"
    assert "email" not in result.evidence
    argv, options = calls[-1]
    assert not set(options["env"]) & {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "NODE_OPTIONS", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert argv[argv.index("--tools") + 1] == ""
    assert "--restricted" in argv and "--bare" not in argv
    assert "--fallback-model" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert options["cwd"] != str(setup[1])
    assert str(setup[0].home).encode() not in options["input_bytes"]
    assert result.evidence["generated_execution"] == "disabled"


def test_api_auth_rejected_before_inference(setup):
    calls = []
    with pytest.raises(S.SubscriptionError, match="subscription_auth_required"):
        invoke(setup, fake_client(calls, auth_method="api_key"), tools_allowed=False)
    assert len(calls) == 1


@pytest.mark.parametrize("model,effort", [("claude-sonnet-5-5", "high"), ("claude-opus-5-5", "medium")])
def test_unapproved_model_effort_fail_before_process(setup, model, effort):
    auth, workspace, arm = setup
    with pytest.raises(S.SubscriptionError, match="not_approved"):
        S.run("claude", "test", model=model, effort=effort, auth=auth, workspace=workspace,
              arm_home=arm, timeout_s=10, _process=lambda *a, **k: pytest.fail("started"))


def test_codex_rejects_unverified_client_version_before_inference(setup, monkeypatch):
    monkeypatch.setattr(S, "CODEX_NATIVE_BOUNDARY_VERIFIED", True)
    auth, workspace, arm = setup
    with pytest.raises(S.SubscriptionError, match="codex_client_version_unverified"):
        S.run("codex", "test", model=S.MODELS["codex"], effort="high", auth=auth, workspace=workspace,
              arm_home=arm, timeout_s=10, _process=lambda *a, **k: (0, "codex-cli 0.162.0", ""))


def test_auth_store_never_mounted(setup):
    auth, _, arm = setup
    workspace = auth.home / "workspace"
    workspace.mkdir()
    with pytest.raises(S.SubscriptionError, match="auth_store_in_task_mount"):
        S.run("claude", "test", model=S.MODELS["claude"], effort="high", auth=auth,
              workspace=workspace, arm_home=arm, timeout_s=10, _process=lambda *a, **k: pytest.fail("started"))


def test_native_tool_and_model_drift_fail_closed():
    for raw, error in [(response(tools=["Bash"]), "native_tool_surface_not_disabled"),
                       (response(model="claude-sonnet-5-5"), "model_mismatch"),
                       (response(permission_denials=[{"tool_name": "x"}]), "client_tool_permission_denied")]:
        with pytest.raises(S.SubscriptionError, match=error):
            S._parse_claude(raw, model=S.MODELS["claude"], tools_allowed=False)


def test_model_report_missing_fails_closed():
    raw = response().replace('"modelUsage": {"claude-opus-5-5": {}}', '"modelUsage": {}')
    with pytest.raises(S.SubscriptionError, match="model_usage_unverified"):
        S._parse_claude(raw, model=S.MODELS["claude"], tools_allowed=False)


def test_client_cost_is_not_recorded_as_api_spend():
    _, usage, _ = S._parse_claude(response(extra_usage={"total_cost_usd": 3.2}),
                                 model=S.MODELS["claude"], tools_allowed=False)
    assert "total_cost_usd" not in usage


def tool_setup(setup, process):
    auth, workspace, arm = setup
    control = auth.home / "control"
    control.mkdir()
    bridge = S._bridge()
    return bridge, bridge.DockerTools({"workspace": str(workspace), "arm_home": str(arm),
                                       "control_home": str(control), "image": auth.worker_image,
                                       "docker": str(auth.docker), "deadline": 9999999999}, process=process)


def test_docker_command_has_no_credentials_network_or_socket(setup):
    bridge, tool = tool_setup(setup, lambda *a, **k: (0, "ok", ""))
    argv = tool.argv("echo hello", "test")
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--entrypoint") + 1] == "/usr/bin/env"
    assert "--read-only" in argv and "no-new-privileges" in argv
    assert "10001:10001" in argv and "ALL" in argv
    assert "--pids-limit" in argv and "--memory" in argv and "--cpus" in argv
    assert argv.count("--mount") == 2
    assert not any("docker.sock" in arg or "auth" in arg for arg in argv)
    assert argv[-1] == "echo hello"
    assert "-i" in argv and "timeout" in argv
    assert not set(tool.env) & {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}


def test_only_fixed_bridge_schema_accepted(setup):
    bridge, tool = tool_setup(setup, lambda *a, **k: pytest.fail("process started"))
    for name, args in [("exec", {"command": "id"}), ("task_shell", {"command": "id", "env": {}}),
                       ("task_read_arm", {"path": "~/../../secret"}), ("task_read_arm", {"path": "/etc/passwd"})]:
        with pytest.raises(bridge.ToolError):
            tool.call(name, args)


def test_verified_read_requires_fixed_read_tool_success_and_matching_bytes(setup):
    content = "Follow these experimental instructions.\n"
    expected = hashlib.sha256(content.encode()).hexdigest()
    calls = []
    def process(argv, **kwargs):
        calls.append(argv)
        return (0, json.dumps({"content": content, "sha256": expected}), "") if "run" in argv else (0, "", "")
    bridge, tool = tool_setup(setup, process)
    result = tool.call("task_read_arm", {"path": "~/.claude/CLAUDE.md"})
    assert result["stdout"] == content
    read = tool.records[0]["verified_read"]
    assert read == {"path": "~/.claude/CLAUDE.md", "sha256": expected, "full_read": True,
                    "success": True, "source": "sandbox-bridge"}
    assert "python3 -I -S" in calls[0][-1]
    assert calls[-1][1:3] == ["rm", "-f"]


def test_shell_cannot_forge_verified_read(setup):
    forged = json.dumps({"verified_read": {"path": "~/.claude/CLAUDE.md", "full_read": True}})
    bridge, tool = tool_setup(setup, lambda *a, **k: (0, forged, ""))
    tool.call("task_shell", {"command": "echo forged"})
    assert "verified_read" not in tool.records[0]


def test_tool_timeout_still_cleans_container(setup):
    calls = []
    bridge = S._bridge()
    def process(argv, **kwargs):
        calls.append(argv)
        if "run" in argv:
            raise RuntimeError("mock timeout")
        return 0, "", ""
    bridge, tool = tool_setup(setup, process)
    with pytest.raises(RuntimeError, match="mock timeout"):
        tool.call("task_shell", {"command": "sleep 1000"})
    assert calls[-1][1:3] == ["rm", "-f"]


def test_tool_mcp_process_starts_with_empty_environment(setup):
    calls = []
    seen = {}
    process = fake_client(calls, response(tools=["mcp__rsi__task_shell", "mcp__rsi__task_read_arm"]))
    def inspect(argv, **kwargs):
        if "--mcp-config" in argv:
            config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
            seen.update(config["mcpServers"]["rsi"])
        return process(argv, **kwargs)
    invoke(setup, inspect)
    assert seen["command"] == "/usr/bin/env" and seen["args"][0] == "-i"
    assert not any("TOKEN" in arg or "API_KEY" in arg for arg in seen["args"])


class FakeRpc:
    def __init__(self, *, auth_type="chatgpt", model="gpt-6.1-sol", effort="high", feature_override=None,
                 tool_request=None):
        self.notifications = []
        self.requests = []
        self.sent = []
        self.auth_type, self.model, self.effort = auth_type, model, effort
        self.feature_override = feature_override or {}
        self.closed = False
        self.events = ([tool_request] if tool_request else []) + [
            {"method": "item/completed", "params": {"threadId": "t", "item": {"type": "agentMessage", "text": "answer"}}},
            {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {"inputTokens": 10, "outputTokens": 4}}}},
            {"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "u", "status": "completed"}}},
        ]

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "initialize":
            return {}
        if method == "config/read":
            return {"config": {"features": {**{x: False for x in S.CODEX_FALSE_FEATURES},
                                            "code_mode": {"enabled": False},
                                            "token_budget": {"enabled": False, "use_history_notes_extension": False},
                                            **self.feature_override},
                               "memories": {"use_memories": False, "dedicated_tools": False, "generate_memories": False},
                               "cloud": {"skills": {"enabled": False}},
                               "skills": {"include_instructions": False, "bundled": {"enabled": False}},
                               "orchestrator": {"mcp": {"enabled": False}},
                               "agents": {"enabled": False}, "mcp_servers": {}, "web_search": "disabled",
                               "forced_login_method": "chatgpt", "model_provider": "openai"}}
        if method == "account/read":
            return {"account": {"type": self.auth_type, "planType": "pro"}}
        if method == "model/list":
            return {"data": [{"model": self.model, "supportedReasoningEfforts": [{"reasoningEffort": self.effort}]}]}
        if method == "thread/start":
            return {"thread": {"id": "t"}, "model": self.model, "reasoningEffort": self.effort,
                    "modelProvider": "openai", "instructionSources": []}
        if method == "turn/start":
            return {"turn": {"id": "u"}}
        raise AssertionError(method)

    def send(self, value):
        self.sent.append(value)

    def receive(self):
        return self.events.pop(0)

    def close(self):
        self.closed = True


def test_codex_no_native_environment_and_exact_model_effort(setup, monkeypatch):
    monkeypatch.setattr(S, "CODEX_NATIVE_BOUNDARY_VERIFIED", True)
    auth, workspace, arm = setup
    rpc = FakeRpc()
    result = S.run("codex", "Test", model="gpt-6.1-sol", effort="high", workspace=workspace,
                   arm_home=arm, timeout_s=20, auth=auth, tools_allowed=False,
                   _process=lambda *a, **k: (0, S.CODEX_VERSION, ""), _rpc_factory=lambda *a, **k: rpc)
    assert result.text == "answer" and result.usage == {"input_tokens": 10, "output_tokens": 4}
    assert result.evidence["reported_effort"] == "high"
    assert rpc.closed
    thread = next(p for m, p in rpc.requests if m == "thread/start")
    turn = next(p for m, p in rpc.requests if m == "turn/start")
    assert thread["environments"] == [] and turn["environments"] == []
    assert thread["dynamicTools"] == []
    assert turn["sandboxPolicy"]["access"]["readableRoots"] == []
    assert thread["approvalPolicy"] == turn["approvalPolicy"] == "never"


@pytest.mark.parametrize("kwargs,error", [
    ({"auth_type": "apiKey"}, "subscription_auth_required"),
    ({"feature_override": {"stable_environment_tools": True}}, "codex_native_tool_isolation_unverified"),
    ({"feature_override": {"multi_agent_v2": True}}, "codex_native_tool_isolation_unverified"),
    ({"model": "gpt-6-luna"}, "subscription_model_effort_unavailable"),
    ({"effort": "medium"}, "subscription_model_effort_unavailable"),
])
def test_codex_preflight_blocks_unsafe_or_unavailable_configuration(kwargs, error, tmp_path):
    rpc = FakeRpc(**kwargs)
    with pytest.raises(S.SubscriptionError, match=error):
        S._codex_session(rpc, model="gpt-6.1-sol", effort="high", prompt="test", cwd=tmp_path, bridge_tools=None)
    assert "turn/start" not in [m for m, _ in rpc.requests]


def test_codex_refuses_server_approval_requests(tmp_path):
    rpc = FakeRpc(tool_request={"id": 99, "method": "item/commandExecution/requestApproval", "params": {}})
    with pytest.raises(S.SubscriptionError, match="unexpected_server_request"):
        S._codex_session(rpc, model="gpt-6.1-sol", effort="high", prompt="test", cwd=tmp_path, bridge_tools=None)
    assert rpc.sent[-1]["id"] == 99 and "error" in rpc.sent[-1]


def test_codex_unknown_dynamic_tool_cannot_execute_host_commands(tmp_path):
    class FakeBridge:
        def call(self, name, arguments):
            assert name == "not-an-allowed-tool"
            raise RuntimeError("rejected")
    rpc = FakeRpc(tool_request={"id": 99, "method": "item/tool/call", "params": {
        "threadId": "t", "turnId": "u", "tool": "not-an-allowed-tool", "arguments": {}}})
    S._codex_session(rpc, model="gpt-6.1-sol", effort="high", prompt="test", cwd=tmp_path, bridge_tools=FakeBridge())
    assert rpc.sent[-1]["result"]["success"] is False


def test_bounded_transport_enforces_output_and_wall_clock_inside_test_container(tmp_path):
    bridge = S._bridge()
    with pytest.raises(bridge.ToolError, match="process_output_limit"):
        bridge.bounded_process([sys.executable, "-I", "-c", "print('x' * 10000)"],
                               env={}, cwd=str(tmp_path), timeout_s=2, max_output=100)
    with pytest.raises(bridge.ToolError, match="process_timeout"):
        bridge.bounded_process([sys.executable, "-I", "-c", "import time; time.sleep(5)"],
                               env={}, cwd=str(tmp_path), timeout_s=0.05)


def test_auth_child_symlink_refused(setup, tmp_path):
    auth, _, _ = setup
    (auth.home / ".claude").symlink_to(tmp_path / "host-login")
    with pytest.raises(S.SubscriptionError, match="auth_home_symlink_refused"):
        S._clean_env(auth, "claude")


def test_read_hash_mismatch_cannot_become_verified_evidence(setup):
    bridge, tool = tool_setup(setup, lambda *a, **k: (0, json.dumps({"content": "x", "sha256": "0" * 64}), ""))
    with pytest.raises(bridge.ToolError, match="read_result_invalid"):
        tool.call("task_read_arm", {"path": "~/CLAUDE.md"})
    assert not tool.records



def test_codex_live_boundary_can_be_revoked_before_client_start(setup, monkeypatch):
    monkeypatch.setattr(S, "CODEX_NATIVE_BOUNDARY_VERIFIED", False)
    auth, workspace, arm = setup
    with pytest.raises(S.SubscriptionError, match="codex_native_tool_isolation_unverified"):
        S.run("codex", "test", model="gpt-6.1-sol", effort="high", workspace=workspace,
              arm_home=arm, timeout_s=30, auth=auth, _process=lambda *a, **k: pytest.fail("client started"))


def test_tool_call_budget_is_enforced_before_starting_another_container(setup):
    calls = []
    def process(argv, **kwargs):
        calls.append(argv)
        return 0, "ok", ""
    bridge, tool = tool_setup(setup, process)
    tool.max_tool_calls = 1
    tool.call("task_shell", {"command": "printf ok"})
    with pytest.raises(bridge.ToolError, match="tool_call_limit"):
        tool.call("task_shell", {"command": "printf again"})
    assert len(calls) == 2
