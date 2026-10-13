"""Shared helpers for tests/test_edits_*.py. Holds no tests.

`ehome` (autouse where imported) builds a fake home: ~/.claude, ~/.agents (a real skill that
~/.claude/skills links to), ~/agent-config (PARENT.md, agents, improve/criteria.json),
~/.codex and a repo. It points every AMEM_* home override at it, the edit writer at a
missing file, and AMEM_IMPROVE_BIN at a fake `improve` script, so no test can touch the
real setup, run `claude` or run the real improve.

The fake improve answers from a JSON queue (`FakeImprove.queue`) and logs each argv. On
`apply` with `write: true` it copies --new-file over --target (a kept change).
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import deviations, edits, edits_writer, rule_catalog

GLOBAL_MD = """# Global Claude Code Context

## User Profile
Name and focus.

## Explaining Completed Work

### Core rule
Give the smallest clear explanation.

### Response budget
Short answers. Spending money always needs Vilmer's yes.

## Shell Habits
- Quote globs in zsh.
"""

SKILL_MD = """---
name: dispatch
description: parent work mode
---
# Dispatch

Intro text for the skill.

## 4. Spawn
Spawn one worker per task.
Each brief names the branch.

## 5. Track
Read checkpoints.
"""

PARENT_MD = "# Parent role\n\n## Safety guards\nNever read env files.\n\n## Merge\nMerge to integration only.\n"
WORKER_MD = "# Worker\n\n## Command form\nOne simple command per call.\n"
ROUTINE_MD = "# Nightly check\nRun the suite.\n"
AGENTS_MD = "# Codex\n\n## Style\nBe brief.\n"
REPO_MD = "# Demo repo\nRun tests with pytest.\n"

ALLOWED = ["@/parent/PARENT.md", "@/agents/worker.md", "@/lessons.md", "@/hooks/", "~/.agents/skills/",
           "~/.claude/CLAUDE.md"]

FAKE_IMPROVE = '''#!{python}
import json, shutil, sys
from pathlib import Path
base = Path({base!r})
args = sys.argv[1:]
with open(base / "improve.log", "a") as fh:
    fh.write(json.dumps(args) + "\\n")
qfile = base / "improve.json"
queue = json.loads(qfile.read_text()) if qfile.exists() else {{}}
items = queue.get(args[0], [])
ans = items.pop(0) if items else {{"stdout": "", "exit": 0}}
qfile.write_text(json.dumps(queue))
if args[0] == "apply":
    new = args[args.index("--new-file") + 1]
    (base / "last-new-file.txt").write_text(Path(new).read_text())
    if ans.get("write"):
        shutil.copyfile(new, args[args.index("--target") + 1])
sys.stdout.write(ans.get("stdout", ""))
sys.stderr.write(ans.get("stderr", ""))
sys.exit(ans.get("exit", 0))
'''


@dataclass
class FakeImprove:
    base: Path

    def queue(self, sub: str, *answers: dict[str, Any]) -> None:
        q = json.loads((self.base / "improve.json").read_text()) if (self.base / "improve.json").exists() else {}
        q.setdefault(sub, []).extend(answers)
        (self.base / "improve.json").write_text(json.dumps(q))

    def calls(self) -> list[list[str]]:
        log = self.base / "improve.log"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def last_new_file(self) -> str:
        return (self.base / "last-new-file.txt").read_text()


def apply_answer(status: str, cid: str | None = "c-0001", exit: int | None = None, write: bool | None = None,
                 reason: str = "evals ok", noise: bool = True) -> dict[str, Any]:
    line = json.dumps({"id": cid, "status": status, "reason": reason,
                       "revert": f"improve revert {cid}" if cid else None})
    out = ("improve apply: running evals\n" if noise else "") + line + "\n"
    code = exit if exit is not None else (2 if status == "refused" else 0)
    return {"stdout": out, "exit": code, "write": status in ("kept", "trial") if write is None else write}


@dataclass
class EHome:
    root: Path
    claude: Path
    agents: Path
    agent_config: Path
    codex: Path
    repo: Path
    improve: FakeImprove

    @property
    def skill(self) -> Path:
        return self.agents / "skills" / "dispatch" / "SKILL.md"

    @property
    def global_md(self) -> Path:
        return self.claude / "CLAUDE.md"

    def allow(self, targets: list[str]) -> None:
        (self.agent_config / "improve" / "criteria.json").write_text(json.dumps({"allowed_targets": targets}))


@pytest.fixture(autouse=True)
def ehome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EHome:
    root = tmp_path / "home"
    claude, agents, ac, codex = root / ".claude", root / ".agents", root / "agent-config", root / ".codex"
    repo = root / "work" / "demo-repo"
    for d in (claude / "skills", claude / "projects", claude / "scheduled-tasks" / "nightly-check",
              agents / "skills" / "dispatch", ac / "parent", ac / "agents", ac / "improve", codex, repo):
        d.mkdir(parents=True, exist_ok=True)
    (claude / "CLAUDE.md").write_text(GLOBAL_MD)
    (agents / "skills" / "dispatch" / "SKILL.md").write_text(SKILL_MD)
    (claude / "skills" / "dispatch").symlink_to(agents / "skills" / "dispatch", target_is_directory=True)
    (claude / "scheduled-tasks" / "nightly-check" / "SKILL.md").write_text(ROUTINE_MD)
    (ac / "parent" / "PARENT.md").write_text(PARENT_MD)
    (ac / "agents" / "worker.md").write_text(WORKER_MD)
    (codex / "AGENTS.md").write_text(AGENTS_MD)
    (repo / "CLAUDE.md").write_text(REPO_MD)
    imp_dir = tmp_path / "improve-fake"
    imp_dir.mkdir()
    script = imp_dir / "improve"
    script.write_text(FAKE_IMPROVE.format(python=sys.executable, base=str(imp_dir)))
    script.chmod(0o755)
    home = EHome(root, claude, agents, ac, codex, repo, FakeImprove(imp_dir))
    home.allow(ALLOWED)
    monkeypatch.setenv("AMEM_CLAUDE_HOME", str(claude))
    monkeypatch.setenv("AMEM_PROJECTS_DIR", str(claude / "projects"))
    monkeypatch.setenv("AMEM_AGENT_CONFIG_DIR", str(ac))
    monkeypatch.setenv("AMEM_AGENTS_HOME", str(agents))
    monkeypatch.setenv("AMEM_CODEX_HOME", str(codex))
    monkeypatch.setenv(edits.IMPROVE_ENV, str(script))
    monkeypatch.setenv(edits_writer.CLAUDE_ENV, str(tmp_path / "no-claude-here"))
    monkeypatch.setenv(deviations.CLAUDE_ENV, str(tmp_path / "no-claude-here"))
    return home


def home_env(h: EHome) -> dict[str, str]:
    """The ehome overrides for a CLI subprocess."""
    return {"AMEM_CLAUDE_HOME": str(h.claude), "AMEM_PROJECTS_DIR": str(h.claude / "projects"),
            "AMEM_AGENT_CONFIG_DIR": str(h.agent_config), "AMEM_AGENTS_HOME": str(h.agents),
            "AMEM_CODEX_HOME": str(h.codex), edits.IMPROVE_ENV: str(h.improve.base / "improve"),
            edits_writer.CLAUDE_ENV: str(h.root / "no-claude-here")}


# --------------------------------------------------------------------------- writer stand-in


def envelope(reply: Any) -> str:
    body = reply if isinstance(reply, str) else json.dumps(reply)
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": body})


def proposal(new_section: str, topic: str = "worker spawn brief", why: str = "Agents skipped the branch name; "
             "a concrete line makes it hard to miss.", summary: str = "Spawn rule now names the branch line "
             "explicitly.", kind: str = "contract",
             kind_reason: str = "The brief must now carry the branch as a checkable first line.") -> dict[str, str]:
    return {"new_section": new_section, "topic": topic, "why": why, "summary": summary, "kind": kind,
            "kind_reason": kind_reason}


SPAWN_NEW = ("## 4. Spawn\nSpawn one worker per task.\nEach brief names the branch on its first line, "
             "before the task text.\n")


@dataclass
class FakeWriter:
    replies: list[Any] = field(default_factory=list)
    default: Any = None
    prompts: list[str] = field(default_factory=list)

    def __call__(self, prompt: str, *, timeout: float = 300.0) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        assert isinstance(reply, str), "FakeWriter has no reply left"
        return reply


@pytest.fixture
def ewriter(monkeypatch: pytest.MonkeyPatch) -> Any:
    def use(*replies: Any, default: Any = None) -> FakeWriter:
        fake = FakeWriter(list(replies), default)
        monkeypatch.setattr(edits_writer, "call_writer", fake)
        return fake
    return use


# --------------------------------------------------------------------------- seeding


def sha_of(path: Path) -> str:
    return rule_catalog.sha([path.read_text()])


def seed(url: str, source: str, rule_id: str, rule_title: str | None, *, n: int = 3, verdict: str = "edit_instruction",
         confidence: float = 0.8, sha: str | None = None, started: datetime | None = None, prefix: str | None = None,
         kind: str = "chat", repo: str | None = None, cwd: str | None = None) -> list[str]:
    """`n` scanned sessions, each with one deviation on source + rule. Returns deviation ids."""
    started = started or datetime.now(UTC) - timedelta(days=1)
    prefix = prefix or f"s-{uuid.uuid4().hex[:6]}"
    ids = []
    with psycopg.connect(url) as conn:
        for i in range(n):
            sid = f"{prefix}-{i}"
            meta: dict[str, Any] = {"cwd": cwd} if cwd else {}
            conn.execute(
                "insert into sessions (session_id, kind, repo, project_dir, started_at, transcript_bytes, skills, "
                "instructions, meta, scanned_at) values (%s, %s, %s, %s, %s, 30000, %s, %s, %s, now())",
                (sid, kind, repo, "-work-demo", started + timedelta(minutes=i), Jsonb(["dispatch"]),
                 Jsonb([source]), Jsonb(meta)))
            row = conn.execute(
                "insert into deviations (source_key, session_id, instruction_source, instruction_ref, turn, what, why, "
                "how, followed_grade, result_grade, jev_verdict, jev_confidence, jev_mode, rule_id, rule_title, "
                "instruction_sha) values (%s, %s, %s, %s, 1, %s, %s, %s, 0.3, 0.7, %s, %s, 'fixture', %s, %s, %s) "
                "returning id",
                (f"dev:{sid}:{rule_id}", sid, source, rule_id,
                 "The agent spawned workers without naming the branch in each brief.",
                 "The rule is easy to skim past in a long step.",
                 "It showed in the first spawn and cost one retry.",
                 verdict, confidence, rule_id, rule_title, sha)).fetchone()
            ids.append(str(row[0]))
    return ids


def edit_rows(url: str) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from edits order by created_at, id")
        return cur.fetchall()
