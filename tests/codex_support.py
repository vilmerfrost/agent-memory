"""Synthetic Codex rollouts for tests/test_codex_*.py. Holds no tests.

Every event here is built from the helpers below: the shapes follow the Codex rollout format
(docs/deviations.md, "Codex"), the text is made up. Nothing is copied from a real session.
The Codex home of a test is `<tmp>/home/.codex`, next to the fake Claude home that
`setup_home` makes, so `sessions_ingest.codex_home()` finds it without extra env.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_memory import deviations
from test_deviations_support import PAD, Home, ts

DAY_DIR = "sessions/2026/10/14"


def tid(n: int) -> str:
    return f"01a10000-0000-7000-8000-{n:012d}"


@dataclass
class Rollout:
    """Builds one rollout. Ordinals count up from 0 like the real files."""

    thread: str
    cwd: str
    originator: str = "Codex Desktop"
    source: Any = "vscode"
    thread_source: str = "user"
    repo_url: str | None = None
    agent_path: str | None = None
    fork_start: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        meta: dict[str, Any] = {
            "id": self.thread, "session_id": self.thread, "timestamp": ts(0), "cwd": self.cwd,
            "originator": self.originator, "cli_version": "0.0.0-test", "source": self.source,
            "thread_source": self.thread_source, "model_provider": "openai",
            "base_instructions": {"text": "BASE INSTRUCTIONS NOT FOR THE DIGEST"},
        }
        if self.repo_url:
            meta["git"] = {"branch": "main", "commit_hash": "0" * 40, "repository_url": self.repo_url}
        if self.agent_path:
            meta["agent_path"] = self.agent_path
        if self.fork_start is not None:
            meta["subagent_history_start_ordinal"] = self.fork_start
            meta["forked_from_id"] = "parent"
        self.add("session_meta", meta, 0)

    def add(self, etype: str, payload: dict[str, Any], minute: int = 0) -> Rollout:
        self.events.append({"timestamp": ts(minute), "ordinal": len(self.events), "type": etype,
                            "payload": payload})
        return self

    # -- messages
    def message(self, role: str, text: str, minute: int = 0) -> Rollout:
        kind = "output_text" if role == "assistant" else "input_text"
        return self.add("response_item", {"type": "message", "role": role,
                                          "content": [{"type": kind, "text": text}]}, minute)

    def user(self, text: str, minute: int = 0) -> Rollout:
        return self.message("user", text, minute)

    def assistant(self, text: str, minute: int = 0) -> Rollout:
        return self.message("assistant", text, minute)

    def injected(self, minute: int = 0) -> Rollout:
        """The blocks Codex adds on its own: developer text, AGENTS.md, environment context."""
        self.message("developer", "<permissions>DEVELOPER TEXT NOT FOR THE DIGEST</permissions>", minute)
        self.user(f"# AGENTS.md instructions for {self.cwd}\n\n<INSTRUCTIONS>\nAGENTS TEXT NOT FOR THE "
                  "DIGEST\n</INSTRUCTIONS>", minute)
        self.user(f"<environment_context>\n  <cwd>{self.cwd}</cwd>\n</environment_context>", minute)
        return self.add("turn_context", {"cwd": self.cwd, "approval_policy": "never", "model": "m"}, minute)

    def skill(self, name: str, minute: int = 0) -> Rollout:
        return self.user(f"<skill>\n<name>{name}</name>\n<path>/h/.agents/skills/{name}/SKILL.md</path>\n"
                         "SKILL BODY NOT FOR THE DIGEST\n</skill>", minute)

    def reasoning(self, minute: int = 0) -> Rollout:
        return self.add("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "zzz"}, minute)

    def agent_message(self, author: str, recipient: str, text: str, minute: int = 0) -> Rollout:
        return self.add("response_item", {"type": "agent_message", "author": author, "recipient": recipient,
                                          "content": [{"type": "input_text", "text": text}]}, minute)

    # -- tools
    def item(self, item: dict[str, Any], minute: int = 0) -> Rollout:
        return self.add("event_msg", {"type": "item_completed", "item": item}, minute)

    def command(self, script: str, *, exit_code: int = 0, stderr: str = "", reads: tuple[str, ...] = (),
                status: str | None = None, minute: int = 0) -> Rollout:
        self.add("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "c",
                                   "input": "JS CODE NOT FOR THE DIGEST"}, minute)
        parsed = [{"type": "read", "cmd": "cat", "name": Path(p).name, "path": p} for p in reads] or \
            [{"type": "unknown", "cmd": script}]
        return self.item({"type": "CommandExecution", "command": ["/bin/zsh", "-lc", script], "cwd": self.cwd,
                          "status": status or ("completed" if exit_code == 0 else "failed"),
                          "exit_code": exit_code, "stderr": stderr, "aggregated_output": "OUTPUT",
                          "parsed_cmd": parsed, "source": "unified_exec_startup"}, minute)

    def file_change(self, *paths: str, minute: int = 0) -> Rollout:
        return self.item({"type": "FileChange", "status": "completed",
                          "changes": {p: {"type": "update"} for p in paths}}, minute)

    def mcp(self, server: str, tool: str, *, error: str | None = None, minute: int = 0) -> Rollout:
        return self.item({"type": "McpToolCall", "server": server, "tool": tool, "arguments": {"q": "x"},
                          "status": "failed" if error else "completed",
                          "error": {"message": error} if error else None}, minute)

    def spawn(self, task: str, minute: int = 0) -> Rollout:
        return self.add("response_item", {"type": "function_call", "namespace": "collaboration",
                                          "name": "spawn_agent", "call_id": "s",
                                          "arguments": json.dumps({"task_name": task, "message": "BRIEF"})},
                        minute)

    def aborted(self, minute: int = 0) -> Rollout:
        return self.add("event_msg", {"type": "turn_aborted", "reason": "interrupted"}, minute)

    def busy(self, n: int = 3) -> Rollout:
        for i in range(n):
            self.command(f"ls step{i}", minute=10 + i)
        return self

    def write(self, home: Home, *, pad: bool = True, day_dir: str = DAY_DIR) -> Path:
        path = codex_home(home) / day_dir / f"rollout-2026-10-14T09-00-00-{self.thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e) for e in self.events]
        if pad:  # token counts, like the real files: enough bytes to pass MIN_BYTES
            last = self.events[-1]["timestamp"]
            n = deviations.MIN_BYTES // len(PAD) + 1
            lines += [json.dumps({"timestamp": last, "ordinal": len(self.events) + i, "type": "event_msg",
                                  "payload": {"type": "token_count", "info": {"pad": PAD}}}) for i in range(n)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def codex_home(home: Home) -> Path:
    return home.root / ".codex"


def subagent_source(parent: str, agent_path: str) -> dict[str, Any]:
    return {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1, "agent_path": agent_path,
                                          "agent_nickname": "Tester", "agent_role": None}}}


def make_codex_kinds(home: Home) -> dict[str, str]:
    """One Codex rollout per detectable kind. Returns kind -> session id (codex:<thread id>)."""
    cwd = str(home.repo)
    chat = Rollout(tid(1), cwd).injected().skill("handoff").user("Please tidy the parser module.", 1)
    chat.assistant("On it.", 2).command("cat ~/.agents/skills/handoff/SKILL.md", minute=2,
                                        reads=("/h/.agents/skills/handoff/SKILL.md",))
    chat.busy().write(home)
    parent = Rollout(tid(2), cwd).injected().user("Split this across two agents.", 1).spawn("worker_api", 2)
    parent.skill("dispatch", 1).busy().write(home)
    worker = Rollout(tid(3), cwd, source=subagent_source(tid(2), "/root/worker_api"),
                     thread_source="subagent", agent_path="/root/worker_api")
    worker.agent_message("/root", "/root/worker_api", "## Task\nAdd the endpoint.\n## DoD\nTests pass.", 1)
    worker.busy().agent_message("/root/worker_api", "/root", "Status: DONE", 20).write(home)
    headless = Rollout(tid(4), cwd, originator="codex_exec", source="exec").user("Summarize the report.", 1)
    headless.busy().write(home)
    evals = Rollout(tid(5), "/home/.cache/skill-evals/run-1").user("Run the eval.", 1)
    evals.busy().write(home)
    return {"chat": f"codex:{tid(1)}", "parent": f"codex:{tid(2)}", "worker": f"codex:{tid(3)}",
            "headless": f"codex:{tid(4)}", "eval": f"codex:{tid(5)}"}


def write_git_config(repo: Path, url: str | None) -> None:
    """A minimal `.git/config` (only what `git_remote_url` reads)."""
    git = repo / ".git"
    git.mkdir(parents=True, exist_ok=True)
    body = "[core]\n\tbare = false\n"
    if url:
        body += f'[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    (git / "config").write_text(body, encoding="utf-8")
