"""The real missing-reference sequence must not starve task writes. No authenticated model/client calls."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tomllib

import pytest

from agent_memory import rsi_subscription as S
from test_rsi_subscription import FakeRpc, setup, tool_setup


def test_absent_arm_references_are_metadata_failures_with_a_separate_request_bound(setup):
    calls = []
    bridge, tool = tool_setup(setup, lambda *a, **k: calls.append(a) or (0, "", ""))
    (setup[2] / "SKILL.md").write_text("# Skill")
    for ref in ("reel", "lessons", "engine", "look", "plan", "review"):
        with pytest.raises(bridge.ToolError, match="arm_file_not_in_snapshot") as error:
            tool.call("task_read_arm", {"path": "~/reference/" + ref + ".md"})
        output = tool.error_result(error.value)
        assert output["remaining_container_calls"] == 8
        assert output["arm_snapshot"] == {"files": ["~/SKILL.md"], "truncated": False}
    assert calls == [] and tool.count == 0 and tool.request_count == 6
    assert all(not r["executed"] and r["error"] == "arm_file_not_in_snapshot" for r in tool.records)
    assert all("verified_read" not in r for r in tool.records)
    for _ in range(tool.max_requests - tool.request_count):
        with pytest.raises(bridge.ToolError, match="arm_file_not_in_snapshot"):
            tool.call("task_read_arm", {"path": "~/absent.md"})
    with pytest.raises(bridge.ToolError, match="tool_request_limit"):
        tool.call("task_read_arm", {"path": "~/absent.md"})
    assert calls == [] and tool.count == 0  # No container budget was raised or consumed.


def test_inventory_and_missing_path_preflight_do_not_follow_escape_symlinks(setup, tmp_path):
    bridge, tool = tool_setup(setup, lambda *a, **k: pytest.fail("container started"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").write_text("not arm data")
    (setup[2] / "escape").symlink_to(outside, target_is_directory=True)
    assert tool.inventory()["files"] == []
    with pytest.raises(bridge.ToolError, match="invalid_read_path"):
        tool.call("task_read_arm", {"path": "~/escape/private.md"})
    assert tool.count == 0 and all("verified_read" not in r for r in tool.records)


def test_inventory_is_bounded_metadata_and_never_reads_content(setup, monkeypatch):
    bridge, tool = tool_setup(setup, lambda *a, **k: pytest.fail("container started"))
    for i in range(80):
        (setup[2] / f"rule-{i:03d}.md").write_text("content must not be dumped")
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read content"))
    inventory = tool.inventory()
    assert len(inventory["files"]) == bridge.MAX_INVENTORY_FILES and inventory["truncated"] is True
    assert "content must not be dumped" not in json.dumps(inventory)


def test_codex_mocked_protocol_preserves_writes_after_the_real_six_absent_reads(setup, monkeypatch):
    """Mock inference and Docker transport; execute the fixed write command in the disposable task workspace."""
    auth, workspace, arm = setup
    source = arm / ".agents" / "skills" / "meltdown" / "SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Meltdown\nRead reference files when available.\n")
    (workspace / "README.md").write_text("Tidepool syncs notes.\n")
    bridge = S._bridge()
    actual_tools = bridge.DockerTools
    docker_argv = []

    def docker_process(argv, **kw):
        docker_argv.append(argv)
        assert not set(kw["env"]) & {"OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
        if argv[1:3] == ["rm", "-f"]:
            return 0, "", ""
        assert argv[argv.index("--network") + 1] == "none" and "--read-only" in argv
        assert argv.count("--mount") == 2 and "no-new-privileges" in argv
        command = argv[-1]
        if command.startswith("python3 -I -S"):
            content = source.read_text()
            return 0, json.dumps({"content": content, "sha256": __import__("hashlib").sha256(content.encode()).hexdigest()}), ""
        process = subprocess.run(["/bin/sh", "-c", command], cwd=workspace,
                                 env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True, check=False)
        return process.returncode, process.stdout, process.stderr

    monkeypatch.setattr(bridge, "DockerTools", lambda settings: actual_tools(settings, process=docker_process))
    monkeypatch.setattr(S, "_bridge", lambda: bridge)
    rpc = FakeRpc()
    requests = [("task_read_arm", {"path": "~/.agents/skills/meltdown/SKILL.md"}),
                ("task_shell", {"command": "cat README.md"})]
    requests += [("task_read_arm", {"path": "~/.agents/skills/meltdown/reference/" + name + ".md"})
                 for name in ("reel", "lessons", "engine", "look", "plan", "review")]
    requests += [("task_shell", {"command": "printf '%s' '{\"style\":\"reel\",\"length_s\":15}' > film.json; "
                                            "printf '%s\\n' 'Tidepool syncs notes.' > script.md"}),
                 ("task_shell", {"command": "test -s film.json && test -s script.md"})]
    rpc.events = [{"id": 100 + i, "method": "item/tool/call", "params": {
        "threadId": "t", "turnId": "u", "callId": f"c{i}", "tool": name, "arguments": args}}
        for i, (name, args) in enumerate(requests)] + rpc.events
    result = S.run("codex", "Write film.json and script.md. Do not change README.md.", model=S.MODELS["codex"],
                   effort="high", auth=auth, workspace=workspace, arm_home=arm, timeout_s=20,
                   _process=lambda *a, **k: (0, S.CODEX_VERSION, ""), _rpc_factory=lambda *a, **k: rpc)
    assert json.loads((workspace / "film.json").read_text()) == {"style": "reel", "length_s": 15}
    assert (workspace / "script.md").read_text() == "Tidepool syncs notes.\n"
    assert (workspace / "README.md").read_text() == "Tidepool syncs notes.\n"
    assert len([x for x in docker_argv if x[1] == "run"]) == 4
    responses = [x for x in rpc.sent if "result" in x]
    assert len(responses) == 10 and responses[-2]["result"]["success"] is True
    missing = json.loads(responses[2]["result"]["contentItems"][0]["text"])
    assert missing["error"] == "arm_file_not_in_snapshot" and missing["remaining_container_calls"] == 6
    assert len(result.evidence["verified_reads"]) == 1
    assert sum(bool(x.get("executed")) for x in result.tool_calls) == 4
    turn = next(params for method, params in rpc.requests if method == "turn/start")
    assert "Sanitized arm snapshot inventory" in turn["input"][0]["text"]
    assert str(auth.home) not in turn["input"][0]["text"]
    thread = next(params for method, params in rpc.requests if method == "thread/start")
    assert {x["name"] for x in thread["dynamicTools"]} == {"task_shell", "task_read_arm"}
    assert all(x["type"] == "function" and x["inputSchema"]["additionalProperties"] is False
               for x in thread["dynamicTools"])
    assert thread["environments"] == [] and thread["runtimeWorkspaceRoots"] == []


def test_codex_stops_with_explicit_resource_error_when_request_bound_is_used(setup):
    bridge, tool = tool_setup(setup, lambda *a, **k: pytest.fail("container started"))
    tool.request_count = tool.max_requests
    rpc = FakeRpc(tool_request={"id": 88, "method": "item/tool/call", "params": {
        "threadId": "t", "turnId": "u", "tool": "task_read_arm", "arguments": {"path": "~/absent"}}})
    with pytest.raises(S.SubscriptionError, match="tool_request_limit") as error:
        S._codex_session(rpc, model=S.MODELS["codex"], effort="high", prompt="test", cwd=setup[1], bridge_tools=tool)
    assert error.value.native_session_id == "codex:t"
    assert json.loads(rpc.sent[-1]["result"]["contentItems"][0]["text"])["error"] == "tool_request_limit"


def test_codex_cli_overrides_are_valid_toml_and_telemetry_is_loopback_private(setup):
    command = S.codex_command(setup[0])
    entries = [command[i + 1] for i, item in enumerate(command[:-1]) if item == "-c"]
    parsed = tomllib.loads("\n".join(entries))
    assert parsed["otel"] == S.CODEX_OTEL
    assert parsed["otel"]["exporter"]["otlp-http"]["endpoint"] == "http://127.0.0.1:43927/v1/logs"
    assert parsed["otel"]["log_user_prompt"] is False
    assert parsed["otel"]["metrics_exporter"] == parsed["otel"]["trace_exporter"] == "none"
    assert parsed["permissions"][S.CODEX_PERMISSION_PROFILE]["filesystem"] == {}
    assert parsed["permissions"][S.CODEX_PERMISSION_PROFILE]["network"]["enabled"] is False


@pytest.mark.parametrize("change", [
    {"log_user_prompt": True}, {"exporter": {"otlp-http": {"endpoint": "https://outside.invalid", "protocol": "json"}}},
    {"metrics_exporter": "statsig"}, {"trace_exporter": "otlp-http"},
    {"exporter": {"otlp-http": {"endpoint": S.CODEX_OTEL_ENDPOINT, "protocol": "json", "headers": {"secret": "no"}}}},
])
def test_codex_effective_telemetry_drift_is_refused_before_generation(change):
    config = FakeRpc().request("config/read", {})["config"]
    config["otel"].update(copy.deepcopy(change))
    with pytest.raises(S.SubscriptionError, match="telemetry_configuration_unverified"):
        S._check_codex_config(config)


def test_rpc_eof_shutdown_allows_flush_without_exceeding_deadline(tmp_path):
    marker = tmp_path / "eof-flushed"
    rpc = S._Rpc([sys.executable, "-I", "-c", "import pathlib,sys; sys.stdin.read(); pathlib.Path(sys.argv[1]).write_text('flushed')", str(marker)],
                 env={}, cwd=tmp_path, timeout_s=3)
    rpc.close()
    assert marker.read_text() == "flushed"
    rpc = S._Rpc([sys.executable, "-I", "-c", "import time; time.sleep(10)"], env={}, cwd=tmp_path, timeout_s=.1)
    started = time.monotonic()
    rpc.close()
    assert time.monotonic() - started < 1
    assert rpc.proc.poll() is not None


def test_real_networkless_bridge_writes_files_after_missing_reference_probes(setup):
    """No model/client: actual pinned Docker worker, only disposable mounts, no network or auth files."""
    docker = shutil.which("docker")
    lock = S.BRIDGE_PATH.parent / "images" / "lock.json"
    if docker is None or not lock.is_file():
        pytest.skip("local pinned worker image metadata unavailable")
    image = json.loads(lock.read_text())["worker"]["id"]
    auth, workspace, arm = setup
    control = auth.home / "real-tools"
    control.mkdir()
    settings = {"workspace": str(workspace), "arm_home": str(arm), "control_home": str(control),
                "image": image, "docker": str(Path(docker).resolve()), "deadline": time.time() + 60}
    if os.environ.get("DOCKER_HOST"):
        settings["docker_host"] = os.environ["DOCKER_HOST"]
    bridge = S._bridge()
    tool = bridge.DockerTools(settings)
    inspected = subprocess.run([str(Path(docker).resolve()), "image", "inspect", image], env=tool.env,
                               cwd=control, capture_output=True, text=True, timeout=10)
    if inspected.returncode:
        pytest.skip("pinned local worker image unavailable to isolated Docker transport")
    source = arm / "SKILL.md"
    source.write_text("# Fixture arm\nCreate the requested workspace files.\n")
    before = source.read_bytes()
    for name in ("reel", "lessons", "engine", "look", "plan", "review"):
        with pytest.raises(bridge.ToolError, match="arm_file_not_in_snapshot"):
            tool.call("task_read_arm", {"path": "~/reference/" + name + ".md"})
    assert tool.count == 0
    read = tool.call("task_read_arm", {"path": "~/SKILL.md"})
    assert read["exit_code"] == 0 and read["stdout"] == before.decode()
    result = tool.call("task_shell", {"command": "printf '%s' '{\"style\":\"reel\"}' > film.json; "
                                                "printf '%s\\n' 'Fixture script.' > script.md; "
                                                "test -s film.json && test -s script.md"})
    assert result["exit_code"] == 0
    assert json.loads((workspace / "film.json").read_text()) == {"style": "reel"}
    assert (workspace / "script.md").read_text() == "Fixture script.\n"
    assert source.read_bytes() == before
    assert tool.count == 2 <= tool.max_tool_calls == 8
    assert all(r["network"] == r["credentials"] == "none" for r in tool.records)
    assert sum("verified_read" in r for r in tool.records) == 1
