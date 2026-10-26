"""Official subscription clients separated from credential-free task execution.

Claude uses restricted mode and an explicit MCP-only tool surface. Codex uses
a source-pinned no-environments tool boundary and verifies effective settings
before inference. Authentication is owned by official clients in a dedicated private home;
this module never reads auth files, copies logins or uses private HTTP endpoints.

Official interface references (reviewed 2026-10-25):
https://code.claude.com/docs/en/cli-reference
https://code.claude.com/docs/en/headless
https://learn.chatgpt.com/docs/app-server
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import importlib.util
import json
import os
import pwd
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Callable

MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
EFFORT = "high"
MAX_PROMPT = 256 * 1024
MAX_CLIENT_OUTPUT = 4 * 1024 * 1024
BRIDGE_PATH = Path(__file__).resolve().parents[2] / "sandbox" / "subscription_tools.py"


class SubscriptionError(RuntimeError):
    """Fixed, secret-free error code; never include raw client errors."""


@dataclass(frozen=True)
class SubscriptionAuth:
    # Dedicated directory created by the operator, outside snapshots/arm/workspace.
    # Populate only through official login commands or an approved token provider.
    home: Path
    client: Path
    docker: Path | None = None
    worker_image: str | None = None
    docker_host: str | None = None
    # A caller may obtain a Claude setup-token from its approved secret store.
    # Do not pass API keys here. The token is only placed in the trusted client env.
    claude_token: Callable[[], str] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SubscriptionResult:
    text: str
    usage: dict
    evidence: dict
    tool_calls: list[dict]
    duration_s: float


def _bridge():
    spec = importlib.util.spec_from_file_location("_rsi_subscription_tools", BRIDGE_PATH)
    if spec is None or spec.loader is None:
        raise SubscriptionError("tool_bridge_missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _private_home(home: Path) -> Path:
    home = Path(home)
    if not home.is_absolute() or home.is_symlink() or not home.is_dir() or home.resolve() != home:
        raise SubscriptionError("dedicated_private_auth_home_required")
    info = home.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SubscriptionError("auth_home_must_be_owner_only")
    for name in (".claude", ".codex", "tmp"):
        child = home / name
        if child.is_symlink():
            raise SubscriptionError("auth_home_symlink_refused")
    # An entire everyday home cannot be repurposed as an isolated auth home.
    # Native macOS clients create Library caches inside an otherwise dedicated
    # HOME. Compare the real account home explicitly instead of rejecting that cache.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if home in (Path.home().resolve(), real_home) or any((home / x).exists() for x in
                                                      (".ssh", ".aws", ".config", "Projects")):
        raise SubscriptionError("dedicated_private_auth_home_required")
    return home


def _clean_env(auth: SubscriptionAuth, provider: str) -> dict[str, str]:
    home = _private_home(auth.home)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
           "LANG": "en_US.UTF-8", "TMPDIR": str(home / "tmp")}
    if provider == "claude":
        env.update({"CLAUDE_CONFIG_DIR": str(home / ".claude"),
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})
        if auth.claude_token:
            try:
                token = auth.claude_token()
            except Exception:
                raise SubscriptionError("subscription_auth_unavailable") from None
            if not isinstance(token, str) or not token or "\n" in token:
                raise SubscriptionError("subscription_auth_unavailable")
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    else:
        env["CODEX_HOME"] = str(home / ".codex")
    return env


@contextmanager
def _auth_lock(home: Path, timeout_s: float):
    """Serialize every invocation using this auth store, including token refresh."""
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(home / ".subscription-client.lock", flags, 0o600)
    with os.fdopen(fd, "w") as stream:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SubscriptionError("subscription_auth_busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def claude_command(auth: SubscriptionAuth, *, model: str, effort: str, mcp_config: Path,
                   tools_allowed: bool) -> list[str]:
    settings = {"disableAllHooks": True, "enableAllProjectMcpServers": False,
                # Built-in mods are independent of disableAllHooks/restricted mode.
                # Explicit IDs are documented plugin enable/disable keys. Keep the
                # startup guard: any newly enabled plugin still rejects the run.
                "enabledPlugins": {"cc-plugin-agents-md@builtin": False,
                                   "cc-plugin-plugin-authoring@builtin": False},
                "syncClaudeAiPlugins": False, "autoMemoryEnabled": False,
                "permissions": {"defaultMode": "dontAsk"}}
    argv = [str(auth.client), "--restricted", "--print", "--output-format", "stream-json",
            "--verbose", "--model", model, "--effort", effort,
            "--tools", "", "--strict-mcp-config", "--mcp-config", str(mcp_config),
            "--setting-sources", "", "--settings", json.dumps(settings, separators=(",", ":")),
            "--disable-slash-commands", "--no-session-persistence"]
    if tools_allowed:
        argv += ["--allowedTools", "mcp__rsi__task_shell,mcp__rsi__task_read_arm"]
    else:
        argv += ["--disallowedTools", "mcp__*"]
    return argv


def _parse_claude(raw: str, *, model: str, tools_allowed: bool) -> tuple[str, dict, dict]:
    initialized = False
    native_ids: set[str] = set()
    result = None
    tool_count = 0
    for line in raw.splitlines():
        try:
            msg = json.loads(line)
        except (TypeError, ValueError):
            raise SubscriptionError("client_protocol_invalid") from None
        if not isinstance(msg, dict):
            raise SubscriptionError("client_protocol_invalid")
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            initialized = True
            if isinstance(msg.get("session_id"), str):
                native_ids.add(msg["session_id"])
            if msg.get("model") != model:
                raise SubscriptionError("model_mismatch")
            allowed = {"mcp__rsi__task_shell", "mcp__rsi__task_read_arm", "EndConversation"} if tools_allowed else set()
            if not isinstance(msg.get("tools"), list) or set(msg["tools"]) - allowed:
                raise SubscriptionError("native_tool_surface_not_disabled")
            if msg.get("plugins"):
                raise SubscriptionError("unexpected_client_plugin")
        if msg.get("type") == "assistant":
            message = msg.get("message") or {}
            if message.get("model") and message["model"] != model:
                raise SubscriptionError("model_mismatch")
            for part in message.get("content") or []:
                if part.get("type") == "tool_use":
                    if not tools_allowed or part.get("name") not in {"mcp__rsi__task_shell", "mcp__rsi__task_read_arm", "EndConversation"}:
                        raise SubscriptionError("unexpected_tool_call")
                    tool_count += 1
        if msg.get("type") == "result":
            result = msg
            if isinstance(msg.get("session_id"), str):
                native_ids.add(msg["session_id"])
    if not initialized or result is None:
        raise SubscriptionError("client_completion_missing")
    if result.get("is_error") or result.get("subtype") != "success":
        raise SubscriptionError("client_run_failed")
    if result.get("permission_denials"):
        raise SubscriptionError("client_tool_permission_denied")
    models = result.get("modelUsage")
    if not isinstance(models, dict) or set(models) != {model}:
        raise SubscriptionError("model_usage_unverified")
    value = result.get("result")
    if not isinstance(value, str):
        raise SubscriptionError("client_result_missing")
    # Cost fields are client estimates, not billed API spend. Preserve token counts only.
    names = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    usage = {key: val for key, val in (result.get("usage") or {}).items()
             if key in names and isinstance(val, int) and not isinstance(val, bool) and val >= 0}
    from agent_memory.attempt_links import native_session_id
    native = native_session_id("claude", next(iter(native_ids))) if len(native_ids) == 1 else None
    return value, usage, {"model_verified": True, "reported_model": model,
                          "effort_verification": "explicit_client_option",
                          "tool_use_count": tool_count,
                          "native_session_id": native,
                          "native_identity_source": "client_init_result" if native else
                              "conflicting_client_ids" if len(native_ids) > 1 else "client_id_unavailable"}


def _auth_status(auth: SubscriptionAuth, env: dict, cwd: Path, process) -> dict:
    code, raw, _ = process([str(auth.client), "auth", "status"], env=env, cwd=str(cwd),
                           timeout_s=20, max_output=65536)
    try:
        status = json.loads(raw)
    except (ValueError, TypeError):
        raise SubscriptionError("subscription_auth_unavailable") from None
    if code or not status.get("loggedIn") or status.get("authMethod") not in {"claude.ai", "oauth_token"}:
        raise SubscriptionError("subscription_auth_required")
    return {"auth_method": status["authMethod"], "subscription_type": status.get("subscriptionType")}


def _cleanup_tools(auth: SubscriptionAuth, control: Path, bridge) -> None:
    ledger = control / "containers.jsonl"
    if not ledger.exists():
        return
    env = {"HOME": str(control), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    if auth.docker_host:
        env["DOCKER_HOST"] = auth.docker_host
    for line in ledger.read_text().splitlines()[:64]:
        if not re.fullmatch(r"rsi-subscription-[0-9a-f]{32}", line):
            continue
        try:
            bridge.bounded_process([str(auth.docker), "rm", "-f", line], env=env, cwd=str(control),
                                   timeout_s=10, max_output=4096)
        except (OSError, bridge.ToolError):
            raise SubscriptionError("container_cleanup_failed") from None


def run(provider: str, prompt: str, *, model: str, effort: str, workspace: Path,
        arm_home: Path, timeout_s: int, auth: SubscriptionAuth, tools_allowed: bool = True,
        max_tool_calls: int = 8, _process=None, _rpc_factory=None) -> SubscriptionResult:
    """Run an official client; errors are fixed codes safe to persist as evidence.

    workspace and arm_home must be existing isolated staging directories. The
    caller provides a pinned worker image ID, local Docker socket, absolute client
    binary and dedicated private auth home. The private home must never enter a
    baseline, evidence archive, bind mount, or task prompt. `_process` is a test seam.
    """
    if provider not in MODELS or model != MODELS[provider] or effort != EFFORT:
        raise SubscriptionError("subscription_model_effort_not_approved")
    if provider == "codex" and not CODEX_NATIVE_BOUNDARY_VERIFIED:
        raise SubscriptionError("codex_native_tool_isolation_unverified")
    if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or not 1 <= max_tool_calls <= 64:
        raise SubscriptionError("invalid_tool_call_limit")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > MAX_PROMPT:
        raise SubscriptionError("invalid_prompt")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or not 1 <= timeout_s <= 3600:
        raise SubscriptionError("invalid_timeout")
    home = _private_home(auth.home)
    client = Path(auth.client)
    if not client.is_absolute() or not client.is_file():
        raise SubscriptionError("official_client_missing")
    for raw in (workspace, arm_home):
        path = Path(raw)
        if not path.is_absolute() or not path.is_dir() or path.is_symlink() or path.resolve() != path:
            raise SubscriptionError("isolated_staging_paths_required")
        if _overlap(path, home):
            raise SubscriptionError("auth_store_in_task_mount")
    workspace, arm_home = Path(workspace), Path(arm_home)
    if _overlap(workspace, arm_home):
        raise SubscriptionError("overlapping_task_mounts")
    bridge = _bridge()
    process = _process or bridge.bounded_process
    started = time.monotonic()
    with _auth_lock(home, timeout_s):
        (home / "tmp").mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="subscription-", dir=home / "tmp") as tmp:
            control = Path(tmp).resolve()
            env = _clean_env(auth, provider)
            try:
                status = _auth_status(auth, env, control, process) if provider == "claude" else {}
                mcp = {"mcpServers": {}}
                bridge_tools = None
                if tools_allowed:
                    if auth.docker is None or auth.worker_image is None:
                        raise SubscriptionError("isolated_tool_configuration_missing")
                    settings = {"workspace": str(workspace), "arm_home": str(arm_home),
                                "control_home": str(control), "image": auth.worker_image,
                                "docker": str(Path(auth.docker).resolve()), "docker_host": auth.docker_host,
                                "max_tool_calls": max_tool_calls, "deadline": time.time() + max(0, timeout_s - (time.monotonic() - started))}
                    bridge_tools = bridge.DockerTools(settings)  # Validate before invoking the client.
                    bridge_config = control / "bridge.json"
                    bridge_config.write_text(json.dumps(settings))
                    mcp["mcpServers"]["rsi"] = {
                        "command": "/usr/bin/env",
                        "args": ["-i", f"HOME={control}", "PATH=/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                                 str(Path(sys.executable).resolve()), str(BRIDGE_PATH), str(bridge_config)]}
                mcp_config = control / "mcp.json"
                mcp_config.write_text(json.dumps(mcp))
                # No working directory, hook, settings, or config from the arm is loaded by the client.
                context = ("Your task workspace is /work. The sanitized experimental arm's files are "
                           "at /arm-home. Read and follow its relevant instructions using task_read_arm. "
                           "Use task_shell for task workspace reads, edits and checks.\n\n" if tools_allowed else "")
                remaining = timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    raise SubscriptionError("client_timeout")
                if provider == "claude":
                    code, stdout, _ = process(claude_command(auth, model=model, effort=effort,
                                             mcp_config=mcp_config, tools_allowed=tools_allowed),
                                             env=env, cwd=str(control), timeout_s=remaining,
                                             input_bytes=(context + prompt).encode(), max_output=MAX_CLIENT_OUTPUT)
                    if code:
                        raise SubscriptionError("client_run_failed")
                    value, usage, evidence = _parse_claude(stdout, model=model, tools_allowed=tools_allowed)
                else:
                    code, version, _ = process([str(auth.client), "--version"], env=env, cwd=str(control),
                                               timeout_s=min(10, remaining), max_output=4096)
                    if code or version.strip() != CODEX_VERSION:
                        raise SubscriptionError("codex_client_version_unverified")
                    rpc = (_rpc_factory or _Rpc)(codex_command(auth), env=env, cwd=control,
                                                 timeout_s=max(0.001, timeout_s - (time.monotonic() - started)))
                    try:
                        value, usage, evidence = _codex_session(rpc, model=model, effort=effort,
                                                               prompt=context + prompt, cwd=control,
                                                               bridge_tools=bridge_tools)
                    finally:
                        rpc.close()
                calls_path = control / "tool-evidence.jsonl"
                calls = [json.loads(line) for line in calls_path.read_text().splitlines()] if calls_path.exists() else []
                evidence.update({"provider": provider, "transport": "official-subscription-client",
                                 "billing_source": "subscription", "requested_model": model,
                                 "requested_effort": effort, "native_tools": evidence.get("native_tools", "disabled"),
                                 "generated_execution": "docker-network-none" if tools_allowed else "disabled",
                                 "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                                 "verified_reads": [c["verified_read"] for c in calls if "verified_read" in c], **status})
                return SubscriptionResult(value, usage, evidence, calls, round(time.monotonic() - started, 3))
            except bridge.ToolError:
                raise SubscriptionError("client_timeout_or_output_limit") from None
            except (OSError, ValueError, TypeError):
                raise SubscriptionError("subscription_runtime_failed") from None
            finally:
                env.clear()
                if _process is None:
                    _cleanup_tools(auth, control, bridge)

# Native-tool exclusion is source-pinned, not inferred from approvalPolicy=never.
# openai/codex rust-v0.163.0 core/src/tools/spec_plan.rs gates shell, apply_patch,
# view_image and request_permissions on stable_environment_tools OR an environment.
CODEX_VERSION = "codex-cli 0.163.0"
CODEX_PERMISSION_PROFILE = "rsi-no-host"
# Audited against rust-v0.163.0 spec_plan.rs and ext/{skills,memories,history-notes}.
# No environments/executor roots => no native shell/file/permission handlers.
# Skill tools require cloud skills or executor roots; host discovery is confined
# to the dedicated trusted home/cwd, never the experimental arm's files.
CODEX_NATIVE_BOUNDARY_VERIFIED = True
CODEX_FALSE_FEATURES = (
    "stable_environment_tools", "shell_tool", "shell_snapshot",
    "multi_agent_v2", "apps", "plugins", "image_generation", "tool_suggest",
    "hooks", "memories", "remote_plugin", "browser_use", "computer_use", "goals", "agent_message_board",
)
CODEX_OVERRIDES = {
    **{f"features.{name}": False for name in CODEX_FALSE_FEATURES},
    "features.code_mode.enabled": False,
    "features.token_budget.enabled": False,
    "features.token_budget.use_history_notes_extension": False,
    "memories.use_memories": False,
    "memories.dedicated_tools": False,
    "memories.generate_memories": False,
    "cloud.skills.enabled": False,
    "skills.include_instructions": False, "skills.bundled.enabled": False,
    "orchestrator.mcp.enabled": False,
    "agents.enabled": False, "mcp_servers": {}, "web_search": "disabled",
    "project_doc_max_bytes": 0, "model_provider": "openai", "forced_login_method": "chatgpt",
    "cli_auth_credentials_store": "file", "model_reasoning_effort": "high",
    "default_permissions": CODEX_PERMISSION_PROFILE,
    f"permissions.{CODEX_PERMISSION_PROFILE}.filesystem": {},
    f"permissions.{CODEX_PERMISSION_PROFILE}.network.enabled": False,
}


def codex_command(auth: SubscriptionAuth) -> list[str]:
    argv = [str(auth.client)]
    for key, value in CODEX_OVERRIDES.items():
        # JSON booleans/strings are also valid TOML; empty maps use TOML syntax.
        literal = "{}" if value == {} else json.dumps(value)
        argv += ["-c", f"{key}={literal}"]
    return argv + ["app-server"]


class _Rpc:
    """Bounded JSON-lines app-server transport; stderr is drained then discarded."""
    def __init__(self, argv, *, env, cwd, timeout_s):
        import selectors
        import subprocess
        self.proc = subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.selector = selectors.DefaultSelector()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            os.set_blocking(stream.fileno(), False)
        self.selector.register(self.proc.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.proc.stderr, selectors.EVENT_READ, "stderr")
        self.deadline = time.monotonic() + timeout_s
        self.buffer = bytearray()
        self.output_size = 0
        self.sequence = 0
        self.notifications = []

    def send(self, message):
        import select
        value = memoryview((json.dumps(message) + "\n").encode())
        while value:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SubscriptionError("client_timeout")
            _, ready, _ = select.select([], [self.proc.stdin], [], min(remaining, 0.2))
            if ready:
                value = value[os.write(self.proc.stdin.fileno(), value[:16384]):]

    def receive(self):
        while True:
            if b"\n" in self.buffer:
                raw, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                try:
                    value = json.loads(raw)
                except ValueError:
                    raise SubscriptionError("client_protocol_invalid") from None
                if not isinstance(value, dict):
                    raise SubscriptionError("client_protocol_invalid")
                return value
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise SubscriptionError("client_timeout")
            events = self.selector.select(min(0.2, remaining))
            if not events and self.proc.poll() is not None:
                raise SubscriptionError("client_terminated")
            for key, _ in events:
                value = os.read(key.fd, 16384)
                if not value:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        raise SubscriptionError("client_terminated")
                    continue
                self.output_size += len(value)
                if self.output_size > MAX_CLIENT_OUTPUT:
                    raise SubscriptionError("client_output_limit")
                if key.data == "stdout":
                    self.buffer.extend(value)

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise SubscriptionError("client_request_failed")
                return message.get("result") or {}
            if "id" in message and "method" in message:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "request denied"}})
                raise SubscriptionError("unexpected_server_request")
            self.notifications.append(message)

    def close(self):
        import signal
        self.selector.close()
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


def _check_codex_config(config):
    """Reject config merging or managed overrides that re-enable host capabilities."""
    features = config.get("features") or {}
    for name in CODEX_FALSE_FEATURES:
        if features.get(name) is not False:
            raise SubscriptionError("codex_native_tool_isolation_unverified")
    code_mode = features.get("code_mode") or {}
    if not isinstance(code_mode, dict) or code_mode.get("enabled") is not False:
        raise SubscriptionError("codex_native_tool_isolation_unverified")
    token_budget = features.get("token_budget") or {}
    if (not isinstance(token_budget, dict) or token_budget.get("enabled") is not False
            or token_budget.get("use_history_notes_extension") is not False
            or any((config.get("memories") or {}).get(key) is not False
                   for key in ("use_memories", "dedicated_tools", "generate_memories"))
            or ((config.get("cloud") or {}).get("skills") or {}).get("enabled") is not False):
        raise SubscriptionError("codex_native_tool_isolation_unverified")
    if (config.get("agents") or {}).get("enabled") is not False or config.get("mcp_servers"):
        raise SubscriptionError("codex_native_tool_isolation_unverified")
    if ((config.get("skills") or {}).get("include_instructions") is not False
            or ((config.get("skills") or {}).get("bundled") or {}).get("enabled") is not False
            or ((config.get("orchestrator") or {}).get("mcp") or {}).get("enabled") is not False):
        raise SubscriptionError("codex_native_tool_isolation_unverified")
    if config.get("web_search") != "disabled" or config.get("forced_login_method") != "chatgpt":
        raise SubscriptionError("codex_native_tool_isolation_unverified")
    if config.get("model_provider") != "openai":
        raise SubscriptionError("subscription_auth_required")
    profile = (config.get("permissions") or {}).get(CODEX_PERMISSION_PROFILE) or {}
    filesystem = profile.get("filesystem")
    # config/read adds its optional scan-depth field even to an empty filesystem table.
    grants = ({k: v for k, v in filesystem.items() if not (k == "glob_scan_max_depth" and v is None)}
              if isinstance(filesystem, dict) else None)
    if (config.get("default_permissions") != CODEX_PERMISSION_PROFILE or grants != {}
            or profile.get("extends") or profile.get("workspace_roots")
            or (profile.get("network") or {}).get("enabled") is not False):
        raise SubscriptionError("codex_native_tool_isolation_unverified")


def _codex_session(rpc, *, model, effort, prompt, cwd, bridge_tools):
    rpc.request("initialize", {"clientInfo": {"name": "rsi_subscription", "version": "1"},
                               "capabilities": {"experimentalApi": True}})
    rpc.send({"method": "initialized", "params": {}})
    # Config and account are verified before creating a thread or generating tokens.
    _check_codex_config(rpc.request("config/read", {"includeLayers": False}).get("config") or {})
    profiles = rpc.request("permissionProfile/list", {"cwd": str(cwd), "limit": 100}).get("data") or []
    if not any(p.get("id") == CODEX_PERMISSION_PROFILE and p.get("allowed") is True for p in profiles):
        raise SubscriptionError("codex_permission_profile_unavailable")
    account = rpc.request("account/read", {"refreshToken": True}).get("account") or {}
    if account.get("type") != "chatgpt":
        raise SubscriptionError("subscription_auth_required")
    found = None
    cursor = None
    for _ in range(20):
        params = {"limit": 100, "includeHidden": True}
        if cursor:
            params["cursor"] = cursor
        catalog = rpc.request("model/list", params)
        found = next((entry for entry in catalog.get("data", []) if entry.get("model") == model), None)
        if found or not catalog.get("nextCursor"):
            break
        cursor = catalog["nextCursor"]
    if not found or effort not in {x.get("reasoningEffort") for x in found.get("supportedReasoningEfforts", [])}:
        raise SubscriptionError("subscription_model_effort_unavailable")
    tools = []
    if bridge_tools:
        bridge = _bridge()
        tools = [{"type": "function", **tool} for tool in (bridge.TOOL, bridge.READ_TOOL)]
    started = rpc.request("thread/start", {"model": model, "modelProvider": "openai",
        "cwd": str(cwd), "ephemeral": True, "environments": [], "runtimeWorkspaceRoots": [],
        "approvalPolicy": "never", "approvalsReviewer": "user", "permissions": CODEX_PERMISSION_PROFILE,
        "dynamicTools": tools, "config": {"model_reasoning_effort": effort},
        "baseInstructions": "Complete the supplied task. All task file access and command execution "
                            "must use the explicitly supplied task tools. Do not request permissions.",
        "developerInstructions": "The client has no task filesystem access. Arm files are available "
                                 "only through task_read_arm; task workspace is /work in task_shell."})
    if (started.get("model") != model or started.get("reasoningEffort") != effort
            or started.get("modelProvider") != "openai" or started.get("instructionSources")):
        raise SubscriptionError("model_effort_or_instruction_mismatch")
    thread_id = (started.get("thread") or {}).get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise SubscriptionError("client_protocol_invalid")
    turn = rpc.request("turn/start", {"threadId": thread_id, "model": model, "effort": effort,
        "environments": [], "approvalPolicy": "never", "approvalsReviewer": "user",
        "permissions": CODEX_PERMISSION_PROFILE,
        "input": [{"type": "text", "text": prompt, "text_elements": []}]})
    turn_id = (turn.get("turn") or {}).get("id")
    if not turn_id:
        raise SubscriptionError("client_protocol_invalid")
    text_parts, usage = [], {}
    pending = list(rpc.notifications)
    rpc.notifications.clear()
    while True:
        message = pending.pop(0) if pending else rpc.receive()
        method = message.get("method")
        params = message.get("params") or {}
        if "id" in message:
            if method != "item/tool/call" or bridge_tools is None:
                rpc.send({"id": message["id"], "error": {"code": -32601, "message": "request denied"}})
                raise SubscriptionError("unexpected_server_request")
            if params.get("threadId") != thread_id or params.get("turnId") != turn_id or params.get("namespace"):
                raise SubscriptionError("unexpected_tool_context")
            try:
                value = bridge_tools.call(params.get("tool"), params.get("arguments"))
                success = value["exit_code"] == 0
            except Exception:
                value, success = {"error": "isolated_tool_failed"}, False
            rpc.send({"id": message["id"], "result": {"contentItems": [{"type": "inputText", "text": json.dumps(value)}],
                                                       "success": success}})
        elif method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total") or {}
            usage = {key: total[value] for key, value in (("input_tokens", "inputTokens"),
                     ("output_tokens", "outputTokens"), ("cache_read_input_tokens", "cachedInputTokens"))
                     if isinstance(total.get(value), int) and total[value] >= 0}
            # Codex inputTokens already includes cache reads; normalize to the
            # Claude-shaped usage contract consumed by usage_fields().
            usage["input_tokens"] = max(0, usage.get("input_tokens", 0) - usage.get("cache_read_input_tokens", 0))
        elif method == "item/completed":
            item = params.get("item") or {}
            kind = item.get("type")
            if kind == "agentMessage":
                text_parts.append(item.get("text") or "")
            elif kind in {"commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView", "collabAgentToolCall"}:
                raise SubscriptionError("unexpected_native_tool_execution")
        elif method == "model/rerouted":
            raise SubscriptionError("model_mismatch")
        elif method == "turn/completed":
            completed = params.get("turn") or {}
            if params.get("threadId") != thread_id or completed.get("id") != turn_id or completed.get("status") != "completed":
                raise SubscriptionError("client_run_failed")
            if not text_parts:
                raise SubscriptionError("client_result_missing")
            return "\n".join(text_parts), usage, {"auth_method": "chatgpt", "subscription_type": account.get("planType"),
                "model_verified": True, "reported_model": model, "effort_verification": "thread_start_response",
                "reported_effort": effort, "native_tools": "host_execution_and_file_access_disabled",
                "native_tool_boundary": "codex-0.163.0/no-environments/no-stable-environment-tools",
                "native_session_id": "codex:" + thread_id,
                "native_identity_source": "thread_start_response", "native_turn_id": turn_id}
        elif method == "error":
            raise SubscriptionError("client_run_failed")
