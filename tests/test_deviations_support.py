"""Shared helpers for tests/test_sessions_*.py and tests/test_deviations_*.py. Holds no tests.

Every transcript here is synthetic: built from the small event helpers below, never copied
from a real session. `setup_home` (autouse where imported) points the Claude home, the
projects folder and the agent-config folder at tmp dirs, and the deviation writer at a
missing file, so no test can read the real setup or run `claude`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_memory import deviations, jev

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def cli_days() -> str:
    """`--days` for CLI calls: the CLI uses the real clock, the fixtures are dated around NOW."""
    return str(max(7, (datetime.now(UTC) - NOW).days + 7))
PAD = "x" * 400  # makes a transcript big enough to pass MIN_BYTES with a few turns


def ts(minutes: int) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def user(text: str, minute: int = 0, *, cwd: str = "/work/demo-repo", entrypoint: str = "claude-desktop",
         **extra: Any) -> dict[str, Any]:
    return {"type": "user", "timestamp": ts(minute), "cwd": cwd, "entrypoint": entrypoint,
            "message": {"role": "user", "content": text}, **extra}


def assistant(*blocks: dict[str, Any], minute: int = 0, cwd: str = "/work/demo-repo",
              entrypoint: str = "claude-desktop") -> dict[str, Any]:
    return {"type": "assistant", "timestamp": ts(minute), "cwd": cwd, "entrypoint": entrypoint,
            "message": {"role": "assistant", "content": list(blocks)}}


def text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


def tool(name: str, tid: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid: str, body: str, minute: int = 0, *, error: bool = False, cwd: str = "/work/demo-repo",
           **extra: Any) -> dict[str, Any]:
    return {"type": "user", "timestamp": ts(minute), "cwd": cwd,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                     "content": body, "is_error": error}]}, **extra}


def write_transcript(path: Path, events: list[dict[str, Any]], *, pad: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in events]
    if pad:  # filler assistant turns so the file passes MIN_BYTES
        n = deviations.MIN_BYTES // len(PAD) + 1
        last = events[-1].get("timestamp", ts(0))
        lines += [json.dumps({"type": "progress", "timestamp": last, "data": PAD}) for _ in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass
class Home:
    root: Path
    claude: Path       # fake ~/.claude
    projects: Path     # fake ~/.claude/projects
    agent_config: Path  # fake ~/agent-config
    repo: Path         # a fake repo checkout with CLAUDE.md files

    def project(self, name: str = "-work-demo-repo") -> Path:
        p = self.projects / name
        p.mkdir(parents=True, exist_ok=True)
        return p


@pytest.fixture(autouse=True)
def setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Home:
    root = tmp_path / "home"
    claude = root / ".claude"
    projects = claude / "projects"
    agent_config = root / "agent-config"
    repo = root / "work" / "demo-repo"
    for d in (projects, agent_config / "parent", repo, claude / "skills" / "dispatch",
              claude / "skills" / "systematic-debugging", claude / "agents",
              claude / "scheduled-tasks" / "nightly-check"):
        d.mkdir(parents=True, exist_ok=True)
    (claude / "CLAUDE.md").write_text("# Global\n## Explaining\nKeep answers short.\n", encoding="utf-8")
    (claude / "skills" / "dispatch" / "SKILL.md").write_text("# Dispatch\n## Brief\nEvery brief has a DoD.\n")
    (claude / "skills" / "systematic-debugging" / "SKILL.md").write_text("# Debug\nReproduce first.\n")
    (claude / "agents" / "worker.md").write_text("# Worker\n## Command form\nOne simple command per call.\n")
    (claude / "scheduled-tasks" / "nightly-check" / "SKILL.md").write_text("# Nightly check\nRun the suite.\n")
    (agent_config / "parent" / "PARENT.md").write_text("# Parent\n## Merge\nMerge to integration only.\n")
    (repo / "CLAUDE.md").write_text("# Demo repo\nRun tests with pytest.\n")
    (repo / "CLAUDE.local.md").write_text("# Local\nNever touch the hosted DB.\n")
    (repo / ".env").write_text("SECRET_TOKEN=do-not-read-me-0123456789\n")
    monkeypatch.setenv("AMEM_CLAUDE_HOME", str(claude))
    monkeypatch.setenv("AMEM_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("AMEM_AGENT_CONFIG_DIR", str(agent_config))
    monkeypatch.setenv(deviations.CLAUDE_ENV, str(tmp_path / "no-claude-here"))
    monkeypatch.setenv("AMEM_SUMMARY_CLAUDE", str(tmp_path / "no-claude-here"))
    return Home(root, claude, projects, agent_config, repo)


# --------------------------------------------------------------- one transcript per kind


def chat_events(cwd: str) -> list[dict[str, Any]]:
    return [
        user("Please fix the flaky test in the parser.", 0, cwd=cwd),
        assistant(text("Looking at it."), tool("Skill", "s1", skill="systematic-debugging"), minute=1, cwd=cwd),
        result("s1", "loaded", 1, cwd=cwd),
        assistant(tool("Bash", "b1", command="pytest -q"), minute=2, cwd=cwd),
        result("b1", "1 failed", 3, error=True, cwd=cwd),
        user("<command-name>/compact</command-name>", 4, cwd=cwd),
        user("<command-name>/handoff</command-name>", 5, cwd=cwd),
        assistant(text("Fixed and verified."), minute=6, cwd=cwd),
    ]


def parent_events(cwd: str) -> list[dict[str, Any]]:
    return [
        user("we've talked enough, let's go to work", 0, cwd=cwd),
        assistant(tool("Skill", "s1", skill="dispatch"), minute=1, cwd=cwd),
        result("s1", "loaded", 1, cwd=cwd),
        assistant(tool("Bash", "b1", command="parent-lock acquire demo"), minute=2, cwd=cwd),
        result("b1", "ok", 2, cwd=cwd),
        assistant(tool("Agent", "a1", subagent_type="worker", description="worker-api",
                       prompt="## Task\nBuild it."), minute=3, cwd=cwd),
        result("a1", "spawned", 3, cwd=cwd),
    ]


def worker_events(cwd: str) -> list[dict[str, Any]]:
    return [
        user("## Task\nAdd the endpoint.\n## DoD\nTests pass.", 0, cwd=cwd),
        assistant(tool("Bash", "b1", command="cd x && npm test"), minute=1, cwd=cwd),
        result("b1", "Refusing to run", 1, error=True, cwd=cwd, toolDenialKind="permission-rule"),
        assistant(tool("SubagentHandback", "h1", message="Status: DONE"), minute=2, cwd=cwd),
    ]


def headless_events(cwd: str) -> list[dict[str, Any]]:
    return [
        user("Summarize the report.", 0, cwd=cwd, entrypoint="sdk-cli"),
        assistant(text("Done."), minute=1, cwd=cwd, entrypoint="sdk-cli"),
    ]


def routine_events(cwd: str) -> list[dict[str, Any]]:
    return [
        user('<scheduled-task name="nightly-check" file="/x/SKILL.md">\nRun the suite.\n</scheduled-task>', 0,
             cwd=cwd),
        assistant(tool("Bash", "b1", command="pytest -q"), minute=1, cwd=cwd),
        result("b1", "ok", 2, cwd=cwd),
    ]


def busy(cwd: str, n: int = 3) -> list[dict[str, Any]]:
    """`n` Read calls with results: enough activity (deviations.MIN_ACTIVITY) to be scanned."""
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append(assistant(tool("Read", f"r{i}", file_path="notes.txt"), minute=10 + i, cwd=cwd))
        out.append(result(f"r{i}", "ok", 10 + i, cwd=cwd))
    return out


def make_all_kinds(home: Home) -> dict[str, str]:
    """One transcript of every kind. Returns kind -> session id."""
    cwd = str(home.repo)
    proj = home.project("-work-demo-repo")
    write_transcript(proj / "chat-1.jsonl", chat_events(cwd) + busy(cwd))
    write_transcript(proj / "parent-1.jsonl", parent_events(cwd) + busy(cwd))
    sub = proj / "parent-1" / "subagents"
    write_transcript(sub / "agent-w1.jsonl", worker_events(cwd) + busy(cwd))
    (sub / "agent-w1.meta.json").write_text(json.dumps({"agentType": "worker", "toolUseId": "a1"}))
    write_transcript(proj / "headless-1.jsonl", headless_events(cwd) + busy(cwd))
    write_transcript(proj / "routine-1.jsonl", routine_events(cwd) + busy(cwd))
    ev = home.project("-home--cache-agent-evals-eval-jo-a")
    write_transcript(ev / "eval-1.jsonl", chat_events("/home/.cache/agent-evals/eval-jo-a"))
    return {"chat": "chat-1", "parent": "parent-1", "worker": "parent-1/agent-w1", "headless": "headless-1",
            "routine": "routine-1", "eval": "eval-1"}


# --------------------------------------------------------------- writer and Jev stand-ins


def envelope(model_reply: Any) -> str:
    body = model_reply if isinstance(model_reply, str) else json.dumps(model_reply)
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": body})


def finding(source: str = "claude-md:global", ref: str = "Explaining", turn: int | None = 1,
            followed: float = 0.3, result_: float = 0.8, **over: Any) -> dict[str, Any]:
    body = {"instruction_source": source, "instruction_ref": ref, "turn": turn,
            "what": "The agent wrote a long recap instead of a short answer.",
            "why": "The rule competes with a habit of summarising every step.", "why_category": "conflict",
            "how": "It showed at the end of the session and made the answer harder to read.",
            "followed_grade": followed, "result_grade": result_}
    body.update(over)
    return body


@dataclass
class FakeWriter:
    """Stands in for deviations.call_writer. Replies: envelope str, Exception, or callable(prompt)."""

    replies: list[Any] = field(default_factory=list)
    default: Any = None
    prompts: list[str] = field(default_factory=list)

    def __call__(self, prompt: str, *, timeout: float = deviations.DEFAULT_TIMEOUT) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        assert isinstance(reply, str), "FakeWriter has no reply left"
        return reply


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWriter]:
    def use(*replies: Any, default: Any = None) -> FakeWriter:
        fake = FakeWriter(list(replies), default)
        monkeypatch.setattr(deviations, "call_writer", fake)
        return fake
    return use


@dataclass
class FakeJev:
    """Stands in for jev.ask: records every state, answers one verdict."""

    verdict: str = "edit_instruction"
    confidence: float = 0.8
    states: list[str] = field(default_factory=list)
    error: Exception | None = None

    def __call__(self, state: str, qs: list[jev.Question]) -> dict[str, jev.JevResult]:
        self.states.append(state)
        if self.error is not None:
            raise self.error
        return {
            "verdict": jev.JevResult("verdict", "choice", self.verdict, self.confidence,
                                     {self.verdict: self.confidence}),
            "urgency": jev.JevResult("urgency", "score", 1.2, 0.7, {"0": 0.2, "1": 0.4, "2": 0.4}),
            "systemic": jev.JevResult("systemic", "noul", 0.6, None, {}),
        }


@pytest.fixture
def fake_jev(monkeypatch: pytest.MonkeyPatch) -> FakeJev:
    fake = FakeJev()
    monkeypatch.setattr(jev, "ask", fake)
    return fake
