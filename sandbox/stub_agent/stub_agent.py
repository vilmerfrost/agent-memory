#!/usr/bin/env python3
"""A deterministic stub agent for RSI sandbox replays (Mission 005). Stdlib only, no network, no model.

It runs inside the real worker container like a live agent would, and stands in for one in tests and demos:

    python3 /mnt/agent/stub_agent.py --case /mnt/case/case.json --fixture /mnt/fixture --work /work

1. Copy the fixture into the workspace (/work).
2. Read the standing instruction files a real agent would load from $HOME (~/.claude/CLAUDE.md,
   ~/.codex/AGENTS.md, ~/.claude/agents/*.md) and the workspace (CLAUDE.md, AGENTS.md). Each read is
   reported as a Read tool call with the file's sha256, so the runner can prove which bytes the arm saw.
3. Recognize a small, fixed vocabulary of rules in those files (RULES below). Unknown text is ignored.
4. Execute the case's scripted plan (`stub` in the case JSON). The recognized rules change how the plan
   is carried out, so an instruction change has a real, reproducible effect on the result.
5. Print Claude Code `stream-json` style events (one JSON object per line) and a final `result` event,
   so the same parser reads a stub run and a live `claude -p --output-format stream-json` run.

Same home + same case -> byte-identical workspace and output. No clock, no randomness.

Plan ops (each a JSON object with "op"):
    read   {path}                       Read a workspace file (tool call only).
    edit   {path, old, new}             Replace `old` with `new` in an existing file.
    write  {path, text}                 Create a file.
    report {path, title, source, pattern}
                                        Write a markdown report: one bullet per line of `source` matching
                                        the regex `pattern` (group 1 is the bullet text).
    run    {argv, timeout_s?}           Run a command in the workspace (its exit code is only reported).
    skill  {name}                       Use a skill: load ~/.claude/skills/<name>/SKILL.md (or
                                        ~/.agents/skills/<name>/SKILL.md) and its rules from now on.
    final  {text}                       The final answer.
    sleep  {s}                          Sleep (tests: a run that times out).
    fail   {code?, message?}            Stop with an error (tests: a run that errors).

Rules (case-insensitive, one per instruction line):
    closing_line      End every report|summary|answer with the line "X".
                      report/summary: append X as the report's last line; answer: append X to the final answer.
    no_edit_existing  Never|do not edit|modify|change existing files.
                      An edit or a write to an existing file goes to a new sibling <stem>_new<ext> instead.
    max_lines         Keep every report|file under N lines.
                      A report is cut to its first N-1 lines.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VERSION = 1
RULES = {
    "closing_line": re.compile(r'end every (report|summary|answer) with the line\s+["“]([^"”\n]+)["”]', re.I),
    "no_edit_existing": re.compile(r"\b(?:never|do not|don't)\s+(?:edit|modify|change)\s+existing\s+files\b", re.I),
    "max_lines": re.compile(r"\bkeep every (?:report|file) under (\d+) lines\b", re.I),
}
HOME_SOURCES = (".claude/CLAUDE.md", ".codex/AGENTS.md")
HOME_GLOBS = (".claude/agents/*.md",)
WORK_SOURCES = ("CLAUDE.md", "AGENTS.md")
MAX_FILE = 256 * 1024


class StubError(Exception):
    pass


class Agent:
    def __init__(self, home: Path, work: Path, out=sys.stdout):
        self.home, self.work, self.out = home, work, out
        self.rules: dict[str, object] = {}
        self.instructions: list[dict] = []
        self.turns = 0
        self.tool_errors = 0

    # ---- events

    def emit(self, obj: dict) -> None:
        self.out.write(json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n")
        self.out.flush()

    def tool(self, name: str, inp: dict, ok: bool = True, note: str = "") -> None:
        self.turns += 1
        uid = f"toolu_stub_{self.turns:03d}"
        self.emit({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": uid, "name": name, "input": inp}]}})
        if not ok:
            self.tool_errors += 1
        self.emit({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": uid, "is_error": not ok, "content": note[:500]}]}})

    # ---- instructions

    def load_instructions(self) -> None:
        paths: list[tuple[str, Path]] = []
        for rel in HOME_SOURCES:
            paths.append((f"~/{rel}", self.home / rel))
        for pattern in HOME_GLOBS:
            for p in sorted(self.home.glob(pattern)):
                paths.append((f"~/{p.relative_to(self.home).as_posix()}", p))
        for rel in WORK_SOURCES:
            paths.append((rel, self.work / rel))
        for label, p in paths:
            self.read_instruction(label, p)

    def read_instruction(self, label: str, p: Path) -> None:
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_FILE:
            return
        data = p.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        self.instructions.append({"path": label, "sha256": sha})
        self.tool("Read", {"file_path": label, "sha256": sha})
        for line in data.decode("utf-8", errors="replace").splitlines():
            for name, rx in RULES.items():
                m = rx.search(line)
                if m:
                    self.rules[name] = m.groups() or True

    # ---- plan

    def ws(self, rel: str) -> Path:
        p = (self.work / rel).resolve()
        if not str(p).startswith(str(self.work.resolve()) + os.sep):
            raise StubError(f"path leaves the workspace: {rel}")
        return p

    def target(self, rel: str) -> str:
        """Where a write to `rel` goes: a new sibling when the file exists and no_edit_existing holds."""
        if "no_edit_existing" in self.rules and self.ws(rel).exists():
            p = Path(rel)
            return str(p.with_name(f"{p.stem}_new{p.suffix}"))
        return rel

    def put(self, rel: str, text: str, tool: str) -> str:
        dest = self.target(rel)
        if dest != rel:
            tool = "Write"
        path = self.ws(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.tool(tool, {"file_path": dest})
        return dest

    def do(self, step: dict) -> str | None:
        op = step.get("op")
        if op == "read":
            p = self.ws(step["path"])
            self.tool("Read", {"file_path": step["path"]}, ok=p.is_file(), note="" if p.is_file() else "not found")
        elif op == "edit":
            p = self.ws(step["path"])
            text = p.read_text(encoding="utf-8") if p.is_file() else None
            if text is None or step["old"] not in text:
                self.tool("Edit", {"file_path": step["path"]}, ok=False, note="old text not found")
            else:
                self.put(step["path"], text.replace(step["old"], step["new"], 1), "Edit")
        elif op == "write":
            self.put(step["path"], step["text"], "Write")
        elif op == "report":
            self.report(step)
        elif op == "run":
            self.run(step)
        elif op == "skill":
            self.skill(step["name"])
        elif op == "final":
            return str(step.get("text", ""))
        elif op == "sleep":
            time.sleep(float(step.get("s", 0)))
        elif op == "fail":
            raise StubError(str(step.get("message", "scripted failure")))
        else:
            raise StubError(f"unknown op {op!r}")
        return None

    def report(self, step: dict) -> None:
        src = self.ws(step["source"])
        self.tool("Read", {"file_path": step["source"]}, ok=src.is_file())
        rx = re.compile(step.get("pattern", r"^(.*\S.*)$"))
        items = [m.group(1).strip() for line in (src.read_text(encoding="utf-8").splitlines() if src.is_file() else [])
                 if (m := rx.match(line))]
        lines = [f"# {step.get('title', 'Report')}", ""] + [f"- {i}" for i in items]
        if "max_lines" in self.rules:
            n = int(self.rules["max_lines"][0])
            lines = lines[:max(n - 1, 1)]
        closing = self.rules.get("closing_line")
        if closing and closing[0].lower() in ("report", "summary"):
            lines += ["", closing[1].strip()]
        self.put(step["path"], "\n".join(lines) + "\n", "Write")

    def run(self, step: dict) -> None:
        argv = [str(a) for a in step["argv"]]
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(self.home),
               "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
        try:
            p = subprocess.run(argv, cwd=self.work, capture_output=True, text=True, env=env,
                               timeout=float(step.get("timeout_s", 60)), stdin=subprocess.DEVNULL)
            ok, note = p.returncode == 0, f"exit {p.returncode}"
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, note = False, type(e).__name__
        self.tool("Bash", {"command": " ".join(argv)}, ok=ok, note=note)

    def skill(self, name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise StubError(f"bad skill name {name!r}")
        self.tool("Skill", {"skill": name})
        for base in (".claude/skills", ".agents/skills"):
            self.read_instruction(f"~/{base}/{name}/SKILL.md", self.home / base / name / "SKILL.md")


def copy_into(src: Path, dst: Path) -> None:
    """Copy src's children into dst (dst itself is a bind mount: never chmod or utime it)."""
    for child in sorted(src.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, dst / child.name, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dst / child.name, follow_symlinks=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--case", required=True, type=Path)
    ap.add_argument("--fixture", type=Path)
    ap.add_argument("--work", type=Path, default=Path("/work"))
    ap.add_argument("--home", type=Path, default=Path(os.environ.get("HOME", "/home/rsi")))
    a = ap.parse_args(argv)
    case = json.loads(a.case.read_text())
    if a.fixture and a.fixture.is_dir():
        copy_into(a.fixture, a.work)
    agent = Agent(a.home, a.work)
    agent.emit({"type": "system", "subtype": "init", "model": "stub-agent", "stub_version": VERSION,
                "case": case.get("id")})
    final, error = "", None
    try:
        agent.load_instructions()
        for step in case.get("stub") or []:
            out = agent.do(step)
            if out is not None:
                final = out
    except (StubError, OSError, KeyError, ValueError) as e:
        error = f"{type(e).__name__}: {e}"
    closing = agent.rules.get("closing_line")
    if final and closing and closing[0].lower() == "answer":
        final = final.rstrip("\n") + "\n\n" + closing[1].strip()
    agent.turns += 1
    agent.emit({"type": "result", "subtype": "error_during_execution" if error else "success",
                "is_error": bool(error), "num_turns": agent.turns, "result": error or final,
                "total_cost_usd": 0.0, "usage": {"input_tokens": 0, "output_tokens": 0},
                "modelUsage": {"stub-agent": {"inputTokens": 0, "outputTokens": 0, "costUSD": 0.0}},
                "stub": {"version": VERSION, "instructions": agent.instructions, "rules": sorted(agent.rules),
                         "tool_errors": agent.tool_errors}})
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
