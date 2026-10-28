"""Conservative replay relevance evidence, independent of judge preference.

Case declarations establish intended coverage. Completed, exact-path reads establish
observed exposure, not proof that a model obeyed or understood the contents. A
completed name-only Skill call counts only when the caller passes a staged-skill index
(`skill_index`) in which exactly one staged SKILL.md has that name. Shell snippets,
arbitrary MCP arguments, other name-only skill calls and missing results remain
unknown. No command or target code is executed, and no path is resolved, by this module.
"""
from __future__ import annotations

import json
import hashlib
import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

FORMAT = "rsi-relevance/1"
# Where the replay clients discover skills in the sandbox home: Claude Code reads ~/.claude/skills, Codex and the
# shared skill set live in ~/.agents/skills, older Codex homes use ~/.codex/skills.
SKILL_DIRS = ("~/.claude/skills/", "~/.agents/skills/", "~/.codex/skills/")
SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SKILL_CALL = "completed-unique-staged-skill-call"


def skill_name(path: str) -> str | None:
    """The skill name of `~/<skill dir>/<name>/SKILL.md`, else None."""
    for base in SKILL_DIRS:
        if path.startswith(base) and path.endswith("/SKILL.md"):
            name = path[len(base):-len("/SKILL.md")]
            return name if SKILL_NAME.fullmatch(name) else None
    return None


def skill_index(files: Mapping[str, str], mirrors: Mapping[str, str] | None = None) -> dict[str, str]:
    """name -> the one staged SKILL.md path a name-only Skill call can have loaded.

    files: staged `~/<skill dir>/<name>/SKILL.md` paths -> sha256 (what the arm home holds). mirrors: a copy the
    runner installed so another client discovers the same skill -> its source path. A mirror with the source's
    exact bytes is the same skill; any other second file with the same name makes the name ambiguous, and an
    ambiguous or unknown name maps to nothing.
    """
    mirrors = mirrors or {}
    by_name: dict[str, set[str]] = {}
    for path, sha in files.items():
        name = skill_name(path)
        if name is None:
            continue
        src = mirrors.get(path)
        if src is not None and skill_name(src) == name and files.get(src) == sha:
            path = src
        by_name.setdefault(name, set()).add(path)
    return {name: next(iter(paths)) for name, paths in sorted(by_name.items()) if len(paths) == 1}


def target_path(value: Any) -> str | None:
    """Only unambiguous paths in the sandbox home; never resolve paths on the host."""
    if not isinstance(value, str):
        return None
    if value.startswith("/home/rsi/"):
        value = "~/" + value[len("/home/rsi/"):]
    if not value.startswith("~/"):
        return None
    if any(p in ("", ".", "..") for p in value[2:].split("/")):
        return None
    return value


def case_targets(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("targets must be a list of exact path, kind and behavior declarations")
    out = []
    for item in raw:
        if not isinstance(item, Mapping) or target_path(item.get("path")) != item.get("path"):
            raise ValueError("target path must be an exact ~/ path without traversal")
        if not all(isinstance(item.get(k), str) and item[k].strip() for k in ("kind", "behavior")):
            raise ValueError("target kind and behavior must be nonempty strings")
        row = {k: item[k] for k in ("path", "kind", "behavior")}
        if "sections" in item:
            sections = item["sections"]
            if not isinstance(sections, list) or not sections or not all(
                    isinstance(x, str) and re.fullmatch(r"[a-z0-9][a-z0-9/_-]{0,119}", x) for x in sections):
                raise ValueError("sections must list explicit instruction section IDs")
            if len(set(sections)) != len(sections):
                raise ValueError("duplicate instruction section declaration")
            row["sections"] = list(sections)
        out.append(row)
    if len({(x["path"], x["kind"]) for x in out}) != len(out):
        raise ValueError("duplicate target declaration")
    return tuple(out)


def section_rule_id(path: str, section: str) -> str:
    """Stable file+section identity; equal heading names in different files never share a pin."""
    return "rsi-section-" + hashlib.sha256((path + "\0" + section).encode()).hexdigest()[:20]


def select(manifest: Mapping[str, Any], cases: Sequence[Any]) -> dict[str, Any]:
    targets = {f["path"]: f["kind"] for f in manifest.get("files", [])}
    eligible = {}
    for case in cases:
        matching = []
        for t in case.targets:
            if targets.get(t["path"]) != t["kind"]:
                continue
            file = next(f for f in manifest.get("files", []) if f["path"] == t["path"])
            rule = file.get("rule_id", "")
            if isinstance(rule, str) and rule.startswith("rsi-section-"):
                if not any(rule == section_rule_id(t["path"], sid) for sid in t.get("sections", [])):
                    continue
            matching.append(t["path"])
        if matching:
            eligible[case.id] = sorted(set(matching))
    covered = {p for paths in eligible.values() for p in paths}
    return {"format": FORMAT, "targets": targets, "eligible_cases": eligible,
            "regression_cases": [c.id for c in cases if c.id not in eligible],
            "uncovered_targets": sorted(set(targets) - covered), "pairs": [],
            "limitation": "Exact-path read evidence proves exposure, not that the model applied the changed rule."}


def _read_command(command: Any) -> list[str]:
    """Recognize only a full-file cat command, optionally wrapped by a CLI shell.

    Fail closed for substitutions, redirection, pipelines, compound commands and
    scripts. A successful shell exit is otherwise not evidence that cat ran.
    """
    if not isinstance(command, str) or len(command) > 32768:
        return []
    try:
        argv = shlex.split(command)
        if len(argv) == 3 and argv[0] in ("sh", "bash", "zsh", "/bin/sh", "/bin/bash", "/bin/zsh") \
                and argv[1] in ("-c", "-lc"):
            command = argv[2]
            argv = shlex.split(command)
    except ValueError:
        return []
    if any(c in command for c in ("$", "`", "|", ";", "&", ">", "<", "\n")):
        return []
    if not argv or argv[0] not in ("cat", "/bin/cat", "/usr/bin/cat"):
        return []
    paths = argv[1:]
    if paths[:1] == ["--"]:
        paths = paths[1:]
    if not paths or any(target_path(p) is None for p in paths):
        return []
    return [target_path(p) for p in paths]


def parse_calls(events: Sequence[Mapping[str, Any]], agent: str,
                skills: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Keep bounded arguments from raw events; LF's telemetry parser drops them.

    Only a matching successful tool_result / completed command yields evidence.
    Unknown schemas and incomplete events retain diagnostics but cannot pass.
    skills: the arm's `skill_index`; without it a name-only Skill call stays unknown.
    """
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, bool] = {}
    for ev in events:
        if agent != "codex":
            message = ev.get("message")
            content = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if ev.get("type") == "assistant" and item.get("type") == "tool_use":
                    inp = item.get("input")
                    args = inp if isinstance(inp, dict) else {}
                    call = {"name": str(item.get("name", "")), "input": json.dumps(inp)[:300],
                            "status": "unknown", "target_reads": []}
                    # Do not persist arbitrary arguments. Only known file-read path fields matter here.
                    if call["name"] == "Read":
                        p = target_path(args.get("file_path") or args.get("path"))
                        # Offset/limit reads may omit the actual change. Require the whole file.
                        if p and not any(k in args for k in ("offset", "limit")):
                            call["target_reads"] = [p]
                    elif call["name"] == "Bash":
                        call["target_reads"] = _read_command(args.get("command"))
                    elif call["name"] == "Skill" and skills and set(args) <= {"skill", "args"}:
                        # Claude Code's Skill tool loads the whole SKILL.md of the named skill. Only a name that
                        # exactly one staged skill carries maps to a path; plugin:name and unknown names do not.
                        name = args.get("skill")
                        if isinstance(name, str) and name in skills:
                            call["target_reads"], call["read_kind"] = [skills[name]], SKILL_CALL
                    calls[str(item.get("id") or f"unknown-{len(calls)}")] = call
                elif ev.get("type") == "user" and item.get("type") == "tool_result":
                    results[str(item.get("tool_use_id"))] = item.get("is_error", False) is False
        elif ev.get("type") in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item")
            if not isinstance(item, dict) or item.get("type") not in (
                    "command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search"):
                continue
            iid = str(item.get("id") or f"unknown-{len(calls)}")
            previous = calls.get(iid, {})
            done = ev.get("type") == "item.completed" and item.get("status") == "completed"
            call = {"name": str(item["type"]), "input": str(item.get("status", "unknown")),
                    "status": "completed" if done else "unknown", "target_reads": []}
            if item["type"] == "command_execution":
                command = item.get("command", previous.get("command"))
                # Kept only while assembling the event stream; removed before returning.
                call["command"] = command
                if done and item.get("exit_code") == 0:
                    call["target_reads"] = _read_command(command)
            # An MCP tool named Read is not necessarily a filesystem reader. No inference.
            calls[iid] = call
    for iid, call in calls.items():
        if agent != "codex":
            call["status"] = "completed" if results.get(iid) else "unknown"
            if not results.get(iid):
                call["target_reads"] = []
        call.pop("command", None)
    return list(calls.values())


def evidence(run: Any, paths: Sequence[str], files: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    def matches(path: str, value: Any) -> bool:
        expected = files[path].get("before_sha256" if run.arm == "A" else "after_sha256")
        return isinstance(expected, str) and len(expected) == 64 and value == expected

    seen = {}
    # The runtime owns this field. parse_agent intentionally never accepts it from stdout.
    for item in getattr(run, "verified_reads", []):
        path = target_path(item.get("path"))
        if (path in files and item.get("source") == "sandbox-bridge" and
                item.get("success") is True and item.get("full_read") is True and
                matches(path, item.get("sha256"))):
            seen[path] = "bridge-full-read-matching-sha256"
    if run.agent == "stub":
        for item in run.instructions:
            path = item.get("path")
            if path in files and matches(path, item.get("sha256")):
                seen[path] = "stub-read-matching-sha256"
    else:
        for call in run.tool_calls:
            if call.get("status") == "completed":
                for path in call.get("target_reads", []):
                    kind = SKILL_CALL if call.get("read_kind") == SKILL_CALL else "completed-exact-path-read"
                    if seen.get(path) != "completed-exact-path-read":
                        seen[path] = kind
    return {"observed": {p: seen[p] for p in paths if p in seen},
            "missing": [p for p in paths if p not in seen]}
