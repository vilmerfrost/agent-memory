"""Commits a session made, for `outcome_evidence.commits` (docs/sessions-live.md, "Commits").

Source: the output of a `git commit` shell call. git prints one summary line per commit:

    [<branch> <sha>] <subject>
    [<branch> (root-commit) <sha>] <subject>
    [detached HEAD <sha>] <subject>

Claude Code: a `Bash` tool_use whose command runs `git ... commit`, and its tool_result text.
Codex: an `item_completed` `CommandExecution` item whose command runs `git ... commit`, and
its `aggregated_output`.

A quiet commit (`git commit -q`) prints nothing. Two fallbacks, only for a call that exited 0:
- `git commit -q ... && git log --oneline -N` in the same command: the first log line
  (`<sha> [(HEAD -> <branch>, ...)] <subject>`) is the new commit.
- else, when the commit is in an `&&`-only chain (so exit 0 means it was made) and has a
  `-m` message: {sha: null, message: the -m first line, branch: null}.
A command in the background (its output is elsewhere) gives only what its result shows.
`git commit` inside a quoted string (a grep pattern, an echo) is not a commit.

Each commit is {sha, message, repo, branch}:
- sha      the short sha as git printed it (null: a quiet commit, see above)
- message  the subject (first line), redacted, at most MESSAGE_MAX chars
- repo     repo_of the `git -C <path>` path, else of a leading `cd <path> &&`, else of the
           command's cwd (the transcript line's cwd; Codex: the item's cwd), else of the
           session cwd. Relative paths resolve against that cwd.
- branch   the branch as printed (`detached HEAD` for a detached checkout), redacted, capped;
           null when not printed

Deduped by sha (a null sha: by repo + message; the first sighting wins), at most MAX_COMMITS
(the latest ones are kept). Nothing else of the transcript is kept.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_memory import codex_sessions, redact

MAX_COMMITS = 50
MESSAGE_MAX = 200
BRANCH_MAX = 100

COMMIT_LINE_RE = re.compile(
    r"^\[(?P<branch>detached HEAD|[^\s\]]+)(?: \(root-commit\))? (?P<sha>[0-9a-f]{7,40})\] ?(?P<subject>.*)$",
    re.M)
# `git [global options] commit`: -C <path>, -c <name=value>, --git-dir=<x>, --no-pager, ...
GIT_COMMIT_RE = re.compile(r"(?:^|[\s;&|(`])git(?:\s+(?:-[Cc]\s+(?:\"[^\"]*\"|'[^']*'|\S+)|--?[\w-]+(?:=\S+)?))*"
                           r"\s+commit\b")
_GIT_C_RE = re.compile(r"(?:^|[\s;&|(`])git\s+(?:(?:-c\s+\S+|--?[\w-]+(?:=\S+)?)\s+)*-C\s+(\"[^\"]*\"|'[^']*'|\S+)"
                       r"(?:\s+(?:-[Cc]\s+\S+|--?[\w-]+(?:=\S+)?))*\s+commit\b")
_CD_RE = re.compile(r"(?:^|(?<=[;&\n]))\s*cd\s+(\"[^\"]*\"|'[^']*'|[^\s;&|]+)\s*(?:&&|;|\n)")


def is_commit_command(command: str) -> bool:
    return bool(command) and bool(GIT_COMMIT_RE.search(_mask(command)))


def _unquote(token: str) -> str:
    try:
        parts = shlex.split(token)
    except ValueError:
        return token.strip("\"'")
    return parts[0] if parts else ""


def _local_path(value: Any) -> str:
    """A cwd as a plain path: Codex items carry `file:///...` URLs."""
    s = str(value or "").strip()
    return s[len("file://"):] if s.startswith("file://") else s


def command_dir(command: str, cwd: str) -> str:
    """Where the commit ran: the `git -C` path, else a leading `cd <path> &&`, else `cwd`."""
    m = _GIT_C_RE.search(command)
    if m is None:   # the last `cd <path> &&` before the commit
        at = GIT_COMMIT_RE.search(_mask(command))
        cds = [c for c in _CD_RE.finditer(command) if at is None or c.end() <= at.start() + 1]
        m = cds[-1] if cds else None
    if not m:
        return cwd
    path = _unquote(m.group(1))
    var = re.match(r"\$\{?(\w+)\}?(.*)$", path)
    if var:   # `W=~/worktrees/x/y; git -C $W commit`: a plain assignment earlier in the command
        value = re.search(rf"(?:^|[\s;&]){var.group(1)}=(\"[^\"]*\"|'[^']*'|[^\s;&|]+)", command)
        path = _unquote(value.group(1)) + var.group(2) if value else ""
    if not path:
        return cwd
    p = Path(path).expanduser()
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    return os.path.normpath(p)


def _repo(directory: str) -> str | None:
    from agent_memory.sessions_ingest import repo_of  # local import: sessions_ingest imports this module

    return repo_of(str(Path(directory))) if directory else None


def _clip(text: str, limit: int) -> str:
    out = " ".join(redact.redact(text).split())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + "…"


def parse_output(output: str) -> list[tuple[str, str, str]]:
    """(branch, sha, subject) for every git commit summary line in `output`."""
    return [(m.group("branch"), m.group("sha"), m.group("subject")) for m in COMMIT_LINE_RE.finditer(output or "")]


_QUOTED_RE = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'[^']*'")
_OP_RE = re.compile(r"&&|\|\||[;|&\n]")
_LOG_RE = re.compile(r"(?:^|[\s;&|(])git\b[^;&|\n]*\blog\b[^;&|\n]*--oneline")
_ONELINE_RE = re.compile(r"^(?P<sha>[0-9a-f]{7,40})(?: \((?P<refs>[^)]*)\))? (?P<subject>.*)$", re.M)


def _mask(command: str) -> str:
    """`command` with the text inside quotes replaced by `x` (same length, so offsets hold):
    a `git commit` or a `;` inside a quoted string is not a command."""
    return _QUOTED_RE.sub(lambda m: m.group(0)[0] + "x" * (len(m.group(0)) - 2) + m.group(0)[-1], command)


def _only_and(text: str) -> bool:
    """True when every shell operator in `text` is `&&`: the whole chain stops at a failure."""
    return all(op == "&&" for op in _OP_RE.findall(text.rstrip()))


def _message_arg(args: str) -> str | None:
    """The first line of the `-m` / `--message` value of a commit's arguments."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        return None
    for i, tok in enumerate(tokens):
        if tok.startswith("--message="):
            value = tok.split("=", 1)[1]
        elif tok == "--message" or (re.fullmatch(r"-[a-zA-Z]*m", tok) is not None):
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
        elif re.fullmatch(r"-m.+", tok):
            value = tok[2:]
        else:
            continue
        lines = [ln.strip() for ln in value.strip().splitlines() if ln.strip()]
        if lines and lines[0].startswith("$(cat <<"):   # -m "$(cat <<'EOF' ... EOF)"
            lines = lines[1:]
        return lines[0] if lines and lines[0] != ")" else None
    return None
    return None


@dataclass
class Shape:
    """What a commit command says about its own output."""

    log_after: bool      # `&& git log --oneline` right after the commit: its first line is the new commit
    certain: bool        # the commit is in an `&&`-only chain: a success exit means it was made
    message: str | None  # the `-m` subject


def command_shape(command: str) -> Shape | None:
    masked = _mask(command)
    m = GIT_COMMIT_RE.search(masked)
    if m is None:
        return None
    op = _OP_RE.search(masked, m.end())
    seg_end = op.start() if op else len(masked)
    rest = masked[seg_end:]
    log = _LOG_RE.search(rest)
    log_after = log is not None and _only_and(rest[: log.start() + 1])
    return Shape(log_after, _only_and(rest), _message_arg(command[m.end():seg_end]))


class Collector:
    """Dedupes by sha (a commit without a known sha: by repo + message) and caps the list."""

    def __init__(self) -> None:
        self.commits: dict[Any, dict[str, Any]] = {}

    def _put(self, sha: str | None, message: str, repo: str | None, branch: str | None) -> None:
        key: Any = sha or ("?", repo, message)
        if key not in self.commits:
            self.commits[key] = {"sha": sha, "message": _clip(message, MESSAGE_MAX), "repo": repo,
                                 "branch": _clip(branch, BRANCH_MAX) if branch else None}

    def add(self, output: str, command: str, cwd: str, *, ok: bool = True, background: bool = False) -> None:
        """One commit call. `ok`: the call exited 0. `background`: its output is elsewhere."""
        shape = command_shape(command)
        if shape is None:
            return
        repo = _repo(command_dir(command, cwd))
        found = parse_output(output)
        for branch, sha, subject in found:
            self._put(sha, subject, repo, branch)
        if found or background or not ok:
            return
        if shape.log_after:
            line = _ONELINE_RE.search(output or "")
            if line is not None:
                refs = line.group("refs") or ""
                head = re.search(r"HEAD -> ([^,\s]+)", refs)
                self._put(line.group("sha"), line.group("subject"), repo, head.group(1) if head else None)
                return
        if shape.certain and shape.message:   # `git commit -q`: no output, but it exited 0
            self._put(None, shape.message, repo, None)

    def result(self) -> list[dict[str, Any]]:
        return list(self.commits.values())[-MAX_COMMITS:]


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def claude_commits(events: list[dict[str, Any]], *, cwd: str = "", own_session: str | None = None) -> list[dict[str, Any]]:
    """The commits of a Claude Code transcript. `own_session`: count only lines whose
    `sessionId` is that id or missing (a resumed session's copied lines belong to the earlier
    session). `cwd`: the session cwd, used when a line has none."""
    calls: dict[str, tuple[str, str, bool]] = {}
    out = Collector()
    for e in events:
        sid = e.get("sessionId")
        if own_session is not None and isinstance(sid, str) and sid and sid != own_session:
            continue
        m = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = m.get("content")
        if not isinstance(content, list):
            continue
        line_cwd = e.get("cwd") if isinstance(e.get("cwd"), str) and e.get("cwd") else cwd
        if e.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    command = inp.get("command") if isinstance(inp.get("command"), str) else ""
                    if is_commit_command(command) and b.get("id"):
                        calls[str(b["id"])] = (command, line_cwd, inp.get("run_in_background") is True)
        elif e.get("type") == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result" and str(b.get("tool_use_id")) in calls:
                    command, call_cwd, background = calls.pop(str(b.get("tool_use_id")))
                    ok = not b.get("is_error") and not e.get("toolDenialKind")
                    out.add(_result_text(b.get("content")), command, call_cwd, ok=ok, background=background)
    return out.result()


def codex_commits(events: list[dict[str, Any]], *, cwd: str = "") -> list[dict[str, Any]]:
    """The commits of a Codex rollout (its own events; a fork's copied history is left out)."""
    out = Collector()
    turn_cwd = cwd
    for e in codex_sessions.own_events(events):
        p = codex_sessions.payload(e)
        if e.get("type") == "turn_context" and isinstance(p.get("cwd"), str) and p["cwd"]:
            turn_cwd = p["cwd"]
        elif e.get("type") == "event_msg" and p.get("type") == "item_completed":
            item = p.get("item") if isinstance(p.get("item"), dict) else {}
            if item.get("type") != "CommandExecution":
                continue
            command = codex_sessions.command_text(item.get("command"))
            output = item.get("aggregated_output")
            if isinstance(output, str):
                code = item.get("exit_code")
                ok = item.get("status") == "completed" and code == 0 and not isinstance(code, bool)
                out.add(output, command, _local_path(item.get("cwd")) or turn_cwd, ok=ok)
    return out.result()
