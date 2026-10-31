"""Guarded T3 session folders in the real shape, with synthetic content (tests/test_guarded_roots*.py).

Shape seen in T3 test runs, 2026-10-31: `<control>/native/<sha256 of the thread id>/home` holds
`projects/<flattened worktree path>/<uuid>.jsonl` for a Claude thread and
`sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl` for a Codex thread, next to `Library/`,
`skills/` and `backups/`. `owner-admissions/<hash>/home` and `transitions/<hash>/home` have the same
layout. A Claude transcript starts with a `queue-operation` line, and its lines carry `entrypoint`
`sdk-ts`, `promptSource`, `turnOrigin`, `turnAttributionKey`, `version`. No real prompt or path."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from test_deviations_support import NOW, ts

T3_SENTENCE = ("<runtime_info>In case you're asked: you are running in T3 Code through the Claude Code harness. "
               "No need to mention this otherwise.</runtime_info>")
CONTROL_FLAT = "-private-tmp-run-control-worktrees-sample-t3-e683"


def thread_hash(name: str) -> str:
    """The folder of a thread: sha256 of its id, as ManagedGuardRuntime names it."""
    return hashlib.sha256(name.encode()).hexdigest()


def session_id(n: int) -> str:
    return f"{n:08x}-1111-4222-8333-444455556666"


def guarded_home(control: Path, family: str, thread: str) -> Path:
    """`<control>/<family>/<hash>/home` with the neighbours a real one has."""
    home = control / family / thread_hash(thread) / "home"
    for sub in ("Library/Caches", "skills", "backups"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def line(sid: str, n: int, kind: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "uuid": f"{sid[:8]}-{n:04d}", "sessionId": sid, "timestamp": ts(n), "cwd": "/work/demo-repo",
            "entrypoint": "sdk-ts", "userType": "external", "version": "2.1.312", "isSidechain": False,
            "gitBranch": "main", "parentUuid": None, **extra}


def sdk_events(sid: str, *, system: list[str], tools: list[str], blocks: tuple[dict[str, Any], ...] = ()) -> list[dict[str, Any]]:
    """The lines of one SDK turn. `system` and `tools` are what the request carried."""
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    return [
        {"type": "queue-operation", "operation": "enqueue", "sessionId": sid, "timestamp": ts(0), "content": "say hello"},
        line(sid, 1, "user", promptSource="sdk", turnOrigin="sdk", permissionMode="bypassPermissions", promptId=sid,
             turnAttributionKey=sid, turnPosition={"promptIndex": 0, "turnIndex": 0},
             message={"role": "user", "content": "say hello"}),
        line(sid, 2, "attachment", attachment={"type": "prompt_snapshot", "systemPrompt": system, "reminderFold": True,
                                               "echoWireToolInputs": False, "contextRendering": "structured"}),
        {"type": "api-request-shape", "sessionId": sid, "timestamp": ts(3), "version": "2.1.312", "shapeHash": "a" * 64,
         "shape": {"system": [{"type": "text", "text": s} for s in system],
                   "tools": [{"name": t, "description": "d", "input_schema": {"type": "object"}} for t in tools]}},
        line(sid, 4, "assistant", requestRef=sid, perTurnEffort=None,
             message={"role": "assistant", "model": "claude-haiku-4-5", "id": "msg_1", "stop_reason": "end_turn",
                      "usage": usage, "content": [{"type": "text", "text": "hello"}, *blocks]}),
        {"type": "tools-used", "sessionId": sid, "tools": [b.get("name") for b in blocks]},
        {"type": "last-prompt", "sessionId": sid, "leafUuid": sid, "lastPrompt": "say hello"},
    ]


def t3_events(sid: str, blocks: tuple[dict[str, Any], ...] = ()) -> list[dict[str, Any]]:
    return sdk_events(sid, system=["You are Claude Code.", T3_SENTENCE],
                      tools=["Bash", "Read", "mcp__t3-code__delegate_task", "mcp__t3-code__t3_thread_list"], blocks=blocks)


def plain_sdk_events(sid: str, blocks: tuple[dict[str, Any], ...] = ()) -> list[dict[str, Any]]:
    """`claude -p` or another SDK run: the same line shape, with no sign of T3."""
    return sdk_events(sid, system=["You are Claude Code."], tools=["Bash", "Read", "mcp__other-server__lookup"],
                      blocks=blocks)


def write_jsonl(path: Path, events: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def claude_session(home: Path, sid: str, events: list[dict[str, Any]] | None = None, *, flat: str = CONTROL_FLAT) -> Path:
    return write_jsonl(home / "projects" / flat / f"{sid}.jsonl", t3_events(sid) if events is None else events)


def rollout_name(thread: str) -> str:
    return f"rollout-2026-10-14T09-00-00-{thread}.jsonl"


def rollout_events(thread: str, *, originator: str = "T3 Code", source: str = "vscode", text: str = "say hello") -> list[dict[str, Any]]:
    meta = {"id": thread, "session_id": thread, "timestamp": ts(0), "cwd": "/work/demo-repo",
            "runtime_workspace_roots": ["/work/demo-repo"], "originator": originator, "cli_version": "0.166.0",
            "source": source, "model_provider": "openai", "base_instructions": {"text": "base"},
            "history_mode": "full", "context_window": 272000}
    return [
        {"timestamp": ts(0), "ordinal": 0, "type": "session_meta", "payload": meta},
        {"timestamp": ts(1), "ordinal": 1, "type": "turn_context", "payload": {"cwd": "/work/demo-repo"}},
        {"timestamp": ts(1), "ordinal": 2, "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}},
        {"timestamp": ts(2), "ordinal": 3, "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hello"}]}},
    ]


def codex_session(home: Path, thread: str, *, folder: str = "sessions/2026/10/14", **kw: Any) -> Path:
    return write_jsonl(home / folder / rollout_name(thread), rollout_events(thread, **kw))


def set_mtime(path: Path, minutes: int) -> None:
    stamp = (NOW + timedelta(minutes=minutes)).timestamp()
    os.utime(path, (stamp, stamp))


def write_roots(claude_home: Path, *lines: str) -> Path:
    path = claude_home / "parent" / "claude-roots.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{x}\n" for x in lines), encoding="utf-8")
    return path
