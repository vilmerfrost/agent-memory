#!/usr/bin/env python3
"""Trusted stdio MCP bridge. Generated commands run only in a networkless container.

This process is launched with env -i, never with the client's OAuth environment.
Its immutable configuration is outside both task bind mounts. No provider SDKs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
import uuid

MAX_OUTPUT = 128 * 1024
MAX_REQUEST = 64 * 1024
TOOL_CONTRACT = "rsi-isolated-tools/2"
MAX_INVENTORY_FILES = 64
MAX_INVENTORY_SCAN = 256
ERROR_MESSAGES = {
    "arm_file_not_in_snapshot": "This arm file is absent from the sanitized snapshot. Use the listed arm files; "
                                "do not retry absent references. Use task_shell for workspace writes.",
    "tool_call_limit": "The isolated-container limit is exhausted; no further container operation can run.",
    "tool_request_limit": "The bounded tool-request limit is exhausted.",
    "invalid_read_path": "Use only a canonical path inside the sanitized arm snapshot.",
    "tool_not_allowed": "Only task_shell(command) and task_read_arm(path) are available.",
    "invalid_command": "The task_shell command is empty or exceeds the request bound.",
    "read_result_invalid": "The arm read failed content-integrity validation.",
    "task_deadline": "The task deadline is exhausted.",
    "process_timeout": "The isolated operation reached its timeout.",
    "process_output_limit": "The isolated operation exceeded its output bound.",
    "container_cleanup_failed": "The isolated container could not be confirmed removed.",
}
TOOL_NAME = "task_shell"
TOOL = {
    "name": TOOL_NAME,
    "description": "Run a shell command in the task's isolated, credential-free container. "
                   "Working directory /work is writable; sanitized arm instructions are under "
                   "/arm-home (read-only). No network or host files are available. "
                   "Read workspace files, create or edit files (including heredocs), and run checks using this tool. "
                   "task_read_arm cannot read or write workspace files.",
    "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}},
                    "required": ["command"], "additionalProperties": False},
}

READ_TOOL_NAME = "task_read_arm"
READ_TOOL = {
    "name": READ_TOOL_NAME,
    "description": "Read a complete sanitized arm file (up to 48 KiB) and record verified "
                   "exposure evidence. Use this tool to read experimental instructions; "
                   "shell reads do not establish verified exposure. Paths are ~/relative or "
                   "/home/rsi/relative or /arm-home/relative. Consult the snapshot inventory (partial when marked "
                   "truncated); absent files return arm_file_not_in_snapshot. Use task_shell for /work.",
    "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}},
                    "required": ["path"], "additionalProperties": False},
}
READ_SCRIPT = """import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]); root=pathlib.Path('/arm-home')
if not p.resolve().is_relative_to(root): raise SystemExit(2)
with p.open('rb') as f: data=f.read(49153)
if len(data)>49152: raise SystemExit(3)
print(json.dumps({'content':data.decode('utf-8'),'sha256':hashlib.sha256(data).hexdigest()},ensure_ascii=False))
"""


class ToolError(RuntimeError):
    pass


def bounded_process(argv, *, env, cwd, timeout_s, input_bytes=None, max_output=MAX_OUTPUT):
    """Bound total captured output and wall time, including processes that never exit.

    Discard stderr contents for credentialed clients at the caller. No exception
    includes argv, env, input, stdout or stderr.
    """
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout_s
    pending = memoryview(input_bytes or b"")
    selector = selectors.DefaultSelector()
    for name in ("stdout", "stderr"):
        pipe = getattr(proc, name)
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    if pending:
        os.set_blocking(proc.stdin.fileno(), False)
        selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        proc.stdin.close()
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise ToolError("process_timeout")
            for key, _ in selector.select(min(0.2, max(0, deadline - time.monotonic()))):
                if key.data == "stdin":
                    try:
                        pending = pending[os.write(key.fd, pending[:65536]):]
                    except BrokenPipeError:
                        pending = memoryview(b"")
                    if not pending:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                data = os.read(key.fd, 16384)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(data)
                if sum(len(v) for v in output.values()) > max_output:
                    raise ToolError("process_output_limit")
        code = proc.wait(timeout=max(0.001, deadline - time.monotonic()))
        return code, output["stdout"].decode("utf-8", "replace"), output["stderr"].decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        raise ToolError("process_timeout") from None
    finally:
        selector.close()
        # Always terminate the process group, even if the leader already exited.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # Darwin may reject signalling an already-exited group. Preserve the
            # original limit error only after confirming our child has exited.
            # A denial for a running child still fails closed.
            if proc.poll() is None:
                raise
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream and not stream.closed:
                stream.close()


def _path(raw, *, directory=True):
    p = Path(raw)
    if not p.is_absolute() or any(c in str(p) for c in (",", "\n", "\r", "\x00")):
        raise ToolError("invalid_path")
    if p.is_symlink() or p.resolve() != p or (directory and not p.is_dir()):
        raise ToolError("invalid_path")
    return p


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


class DockerTools:
    def __init__(self, settings, *, process=bounded_process):
        self.settings = dict(settings)
        self.process = process
        self.workspace = _path(settings["workspace"])
        self.arm_home = _path(settings["arm_home"])
        self.control = _path(settings["control_home"])
        if any(_overlap(a, b) for a, b in ((self.workspace, self.control),
                                          (self.arm_home, self.control),
                                          (self.workspace, self.arm_home))):
            raise ToolError("overlapping_mounts")
        self.image = settings["image"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image):
            raise ToolError("immutable_worker_image_required")
        self.docker = _path(settings["docker"], directory=False)
        if not self.docker.is_file():
            raise ToolError("docker_binary_missing")
        self.deadline = float(settings["deadline"])
        self.count = len((self.control / "containers.jsonl").read_text().splitlines()) if (self.control / "containers.jsonl").exists() else 0
        self.max_tool_calls = settings.get("max_tool_calls", 8)
        if isinstance(self.max_tool_calls, bool) or not isinstance(self.max_tool_calls, int) or not 1 <= self.max_tool_calls <= 64:
            raise ToolError("invalid_tool_call_limit")
        self.records = []
        requests = self.control / "tool-requests.jsonl"
        self.request_count = len(requests.read_text().splitlines()) if requests.exists() else 0
        # This is an additional bound for cheap validation requests, not a larger container allowance.
        self.max_requests = min(256, max(8, self.max_tool_calls * 4))
        self.env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                    "HOME": str(self.control), "LANG": "C.UTF-8"}
        host = settings.get("docker_host")
        if host:
            if not isinstance(host, str) or not host.startswith("unix:///") or "\n" in host:
                raise ToolError("local_docker_socket_required")
            self.env["DOCKER_HOST"] = host

    def argv(self, command, name):
        # Every option is controlled by the bridge; model text follows sh -c.
        return [str(self.docker), "run", "--rm", "--pull", "never", "--name", name,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--memory", "1g", "--cpus", "1",
                "--pids-limit", "128", "--ulimit", "fsize=16777216:16777216",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m", "--user", "10001:10001",
                "--init", "--workdir", "/work", "--log-driver", "none",
                "--mount", f"type=bind,source={self.workspace},target=/work",
                "--mount", f"type=bind,source={self.arm_home},target=/arm-home,readonly",
                "--env", "HOME=/tmp", "--env", "PATH=/usr/local/bin:/usr/bin:/bin",
                "--entrypoint", "/usr/bin/env", self.image,
                "-i", "HOME=/tmp", "PATH=/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8",
                "timeout", "--signal=KILL", "60", "/bin/sh", "-c", command]

    def inventory(self):
        """Bounded metadata only; do not read file contents or follow arm symlinks."""
        files, truncated = [], False
        for i, path in enumerate(self.arm_home.rglob("*")):
            if i >= MAX_INVENTORY_SCAN or len(files) >= MAX_INVENTORY_FILES:
                truncated = True
                break
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(self.arm_home):
                continue
            relative = path.relative_to(self.arm_home).as_posix()
            if len(relative) <= 200:
                files.append("~/" + relative)
        return {"files": sorted(files), "truncated": truncated}

    def error_result(self, error):
        """Only fixed public error codes reach the model, never raw exceptions, paths or host stderr."""
        code = str(error) if str(error) in ERROR_MESSAGES else "isolated_tool_failed"
        result = {"exit_code": 1, "stdout": "", "stderr": code, "error": code,
                  "message": ERROR_MESSAGES.get(code, "The isolated operation failed."),
                  "remaining_container_calls": max(0, self.max_tool_calls - self.count),
                  "remaining_tool_requests": max(0, self.max_requests - self.request_count)}
        if code == "arm_file_not_in_snapshot":
            result["arm_snapshot"] = self.inventory()
        return result

    def call(self, name, arguments):
        if self.request_count >= self.max_requests:
            raise ToolError("tool_request_limit")
        self.request_count += 1
        with (self.control / "tool-requests.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(str(self.request_count) + "\n")
        before_records, before_count, started = len(self.records), self.count, time.monotonic()
        try:
            return self._call(name, arguments)
        except ToolError as exc:
            # Record validation refusals too. They consume request capacity but no container capacity.
            if len(self.records) == before_records:
                code = str(exc) if str(exc) in ERROR_MESSAGES else "isolated_tool_failed"
                record = {"request": self.request_count, "call": self.count if self.count > before_count else None,
                          "tool": name if name in (TOOL_NAME, READ_TOOL_NAME) else "unrecognized",
                          "executed": self.count > before_count, "exit_code": None, "error": code,
                          "duration_s": round(time.monotonic() - started, 3),
                          "network": "none", "credentials": "none"}
                self.records.append(record)
                with (self.control / "tool-evidence.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
            raise

    def _call(self, name, arguments):
        read_path = None
        if name == READ_TOOL_NAME and isinstance(arguments, dict) and set(arguments) == {"path"}:
            raw = arguments["path"]
            if not isinstance(raw, str):
                raise ToolError("invalid_read_path")
            relative = None
            for prefix in ("~/", "/home/rsi/", "/arm-home/"):
                if raw.startswith(prefix):
                    relative = raw[len(prefix):]
                    break
            if not relative or ".." in Path(relative).parts or Path(relative).is_absolute():
                raise ToolError("invalid_read_path")
            candidate = self.arm_home / relative
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.arm_home):
                raise ToolError("invalid_read_path")
            if not candidate.is_file():
                # A missing snapshot file is knowable without launching a worker. Do not spend the limited
                # container slots on absent references and starve the later legitimate file-writing tool.
                raise ToolError("arm_file_not_in_snapshot")
            read_path = "~/" + relative
            command = "python3 -I -S -c " + shlex.quote(READ_SCRIPT) + " " + shlex.quote("/arm-home/" + relative)
        elif name == TOOL_NAME and isinstance(arguments, dict) and set(arguments) == {"command"}:
            command = arguments["command"]
        else:
            raise ToolError("tool_not_allowed")
        if not isinstance(command, str) or not command or len(command.encode()) > MAX_REQUEST or "\x00" in command:
            raise ToolError("invalid_command")
        if self.count >= self.max_tool_calls:
            raise ToolError("tool_call_limit")
        remaining = self.deadline - time.time()
        if remaining <= 0:
            raise ToolError("task_deadline")
        self.count += 1
        container = "rsi-subscription-" + uuid.uuid4().hex
        with (self.control / "containers.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write(container + "\n")
        started = time.monotonic()
        code, out, err = None, "", ""
        failure = None
        try:
            code, out, err = self.process(self.argv(command, container), env=self.env,
                                         cwd=str(self.control), timeout_s=min(60, remaining))
        except ToolError as exc:
            failure = str(exc)
        finally:
            # Killing the docker client alone does not stop the container.
            try:
                self.process([str(self.docker), "rm", "-f", container], env=self.env,
                             cwd=str(self.control), timeout_s=10, max_output=4096)
            except (OSError, ToolError):
                failure = "container_cleanup_failed"
        record = {"request": self.request_count, "call": self.count, "tool": name, "executed": True,
                  "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                  "exit_code": code, "duration_s": round(time.monotonic() - started, 3),
                  "network": "none", "credentials": "none", "error": failure,
                  "output_sha256": hashlib.sha256((out + err).encode()).hexdigest()}
        read_result = None
        if read_path is not None and not failure and code == 0:
            try:
                read_result = json.loads(out)
                actual_sha = hashlib.sha256(read_result["content"].encode("utf-8")).hexdigest()
                if actual_sha != read_result["sha256"]:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise ToolError("read_result_invalid") from None
            record["verified_read"] = {"path": read_path, "sha256": actual_sha,
                                       "success": True, "full_read": True, "source": "sandbox-bridge"}
        self.records.append(record)
        evidence = self.control / "tool-evidence.jsonl"
        with evidence.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
        if failure:
            raise ToolError(failure)
        return {"exit_code": code, "stdout": read_result["content"] if read_result else out, "stderr": err}


def serve(settings):
    bridge = DockerTools(settings)
    while True:
        raw = sys.stdin.buffer.readline(MAX_REQUEST + 1)
        if not raw:
            return
        if len(raw) > MAX_REQUEST:
            return
        request_id = None
        try:
            request = json.loads(raw)
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                continue
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "rsi-subscription-tools", "version": "1"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [TOOL, READ_TOOL]}
            elif method == "tools/call":
                params = request.get("params") or {}
                try:
                    value = bridge.call(params.get("name"), params.get("arguments"))
                except ToolError as exc:
                    value = bridge.error_result(exc)
                result = {"content": [{"type": "text", "text": json.dumps(value)}],
                          "isError": value["exit_code"] != 0}
            else:
                raise ToolError("method_not_allowed")
            reply = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (ToolError, ValueError, TypeError, AttributeError, OSError):
            reply = {"jsonrpc": "2.0", "id": request_id,
                     "error": {"code": -32603, "message": "isolated_tool_failed"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    # Configuration is parent-generated trusted data, never a model-controlled path.
    serve(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
