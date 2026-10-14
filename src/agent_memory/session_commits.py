"""Commits a session made, for `outcome_evidence.commits` (docs/sessions-live.md, "Commits").

Source: the output of a `git commit` shell call. git prints one summary line per commit:

    [<branch> <sha>] <subject>
    [<branch> (root-commit) <sha>] <subject>
    [detached HEAD <sha>] <subject>

Claude Code: a `Bash` tool_use whose command runs `git ... commit`, and its tool_result text.
Codex: an `item_completed` `CommandExecution` item whose command runs `git ... commit`, and
its `aggregated_output`.

A quiet commit (`git commit -q`) prints nothing. Fallbacks, in order:
- the subject is known (`-m`, or a heredoc body for `-F -` / `--file=-`) and a later
  `git log --oneline` (or `--format='%h %s'`) line in the output shows `<sha> <subject>`:
  that line is the new commit, whatever the operators between them.
- only for a call that exited 0: `git commit -q ... && git log --oneline -N` in the same
  command: the first log line (`<sha> [(HEAD -> <branch>, ...)] <subject>`) is the new commit.
- only for a call that exited 0: when the commit is in an `&&`-only chain, or the script runs
  under `set -e` (so exit 0 means it was made) and it has a known subject:
  {sha: null, message: the subject, branch: null}.
Heredoc bodies (`<<'EOF' ... EOF`) are not shell code: they are cut out before the command is
read, so a line break or a `;` in a commit message is not an operator.
A command in the background (its output is elsewhere) gives only what its result shows.
`git commit` inside a quoted string (a grep pattern, an echo) is not a commit.

When the transcript gives no commit but the outcome signals count one (`commit` > 0), the
caller can ask for the git-log fallback (`Fallback`, `git_log_commits`): `git log` in the
dirs where the commit calls ran (the session cwd only when no commit call was seen), limited
to the session's time window,
HEAD only, no merges, and the repo's configured `user.email` as author. Those entries carry
`"source": "git-log"` and branch null. Parallel sessions of the same user on the same branch
of the same checkout can still pick up each other's commits; separate worktrees cannot.

Each commit is {sha, message, repo, branch}:
- sha      the short sha as git printed it (null: a quiet commit, see above)
- message  the subject (first line), redacted, at most MESSAGE_MAX chars
- repo     repo_of the `git -C <path>` path, else of a leading `cd <path> &&`, else of the
           command's cwd (the transcript line's cwd; Codex: the item's cwd), else of the
           session cwd. Relative paths resolve against that cwd. null when the path is a
           command substitution (`$(...)`, backticks) or a `$VAR` not set earlier in the
           command ($HOME is ~). A commit in a `mktemp` dir (`W=$(mktemp -d); cd $W`) is a
           throwaway repo and is left out.
- branch   the branch as printed (`detached HEAD` for a detached checkout), redacted, capped;
           null when not printed

Deduped by sha (a null sha: by repo + message; the first sighting wins), at most MAX_COMMITS
(the latest ones are kept). Nothing else of the transcript is kept.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
_CD_RE = re.compile(r"(?:^|(?<=[;&\n]))\s*cd\s+(\"[^\"]*\"|'[^']*'|`[^`]*`|\$\([^)]*\)|[^\s;&|]+)\s*(?:&&|;|\n)")


_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.-]*))")


def split_heredocs(command: str) -> tuple[str, list[str]]:
    """`command` without its heredoc bodies, and the bodies in order. The `<<'EOF'` marker
    stays; the body lines and the end line go (the line break before them stays). A `<<`
    inside quotes (`-m "$(cat <<'EOF' ...)"`) is not a heredoc of the command itself."""
    out: list[str] = []
    bodies: list[str] = []
    pending: list[tuple[str, bool]] = []   # (delimiter, `<<-`)
    i, n = 0, len(command)
    quote: str | None = None
    while i < n:
        ch = command[i]
        if quote == "'":
            out.append(ch)
            quote = None if ch == "'" else quote
            i += 1
            continue
        if quote == '"':
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(command[i + 1])
                i += 2
                continue
            quote = None if ch == '"' else quote
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if command.startswith("<<", i) and not command.startswith("<<<", i):
            m = _HEREDOC_RE.match(command, i)
            if m is not None:
                pending.append((m.group(2) or m.group(3) or m.group(4), m.group(1) == "-"))
                out.append(m.group(0))
                i = m.end()
                continue
        if ch == "\n" and pending:
            out.append("\n")
            i += 1
            for delim, dash in pending:
                lines: list[str] = []
                while i < n:
                    end = command.find("\n", i)
                    line = command[i:] if end < 0 else command[i:end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if dash else line) == delim:
                        break
                    lines.append(line)
                bodies.append("\n".join(lines))
            pending = []
            continue
        out.append(ch)
        i += 1
    return "".join(out), bodies


def is_commit_command(command: str) -> bool:
    return bool(command) and bool(GIT_COMMIT_RE.search(_mask(split_heredocs(command)[0])))


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


DIR_OK, DIR_UNKNOWN, DIR_TEMP = "ok", "unknown", "temp"
_SHELL_EXPR_RE = re.compile(r"\$|`")


def resolve_dir(command: str, cwd: str) -> tuple[str, str]:
    """(dir, state) of where the commit ran: the `git -C` path, else the last `cd <path>`
    before the commit, else `cwd`. A `$VAR` set by a plain assignment earlier in the command is
    resolved. state: DIR_OK; DIR_UNKNOWN when the path holds a command substitution (`$(...)`,
    backticks) or a `$VAR` it cannot resolve (dir ""); DIR_TEMP when it comes from `mktemp`
    (a throwaway repo, dir "")."""
    command = split_heredocs(command)[0]
    m = _GIT_C_RE.search(command)
    if m is None:   # the last `cd <path> &&` before the commit
        at = GIT_COMMIT_RE.search(_mask(command))
        cds = [c for c in _CD_RE.finditer(command) if at is None or c.end() <= at.start() + 1]
        m = cds[-1] if cds else None
    if not m:
        return cwd, DIR_OK
    raw = re.sub(r"^([\"']?)\$\{?HOME\}?(?=/|[\"']?$)", r"\1~", m.group(1))
    path = _unquote(raw)
    var = re.match(r"\$\{?(\w+)\}?(.*)$", path)
    if var:   # `W=~/worktrees/x/y; git -C $W commit`: a plain assignment earlier in the command
        value = re.search(rf"(?:^|[\s;&]){var.group(1)}=(\"[^\"]*\"|'[^']*'|\$\([^)]*\)|`[^`]*`|[^\s;&|]+)", command)
        if value is None:
            return "", DIR_UNKNOWN
        raw = value.group(1)
        path = _unquote(raw) + var.group(2) if not raw.startswith(("$(", "`")) else raw
    if _SHELL_EXPR_RE.search(path) or _SHELL_EXPR_RE.search(raw):
        return "", DIR_TEMP if "mktemp" in raw else DIR_UNKNOWN
    if not path:
        return cwd, DIR_OK
    p = Path(path).expanduser()
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    return os.path.normpath(p), DIR_OK


def command_dir(command: str, cwd: str) -> str:
    """resolve_dir's dir: "" when it cannot be known."""
    return resolve_dir(command, cwd)[0]


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


def _first_line(value: str) -> str | None:
    lines = [ln.strip() for ln in value.strip().splitlines() if ln.strip()]
    if lines and lines[0].startswith("$(cat <<"):   # -m "$(cat <<'EOF' ... EOF)"
        lines = lines[1:]
    return lines[0] if lines and lines[0] != ")" else None


def _message_arg(args: str, heredoc: str | None = None) -> str | None:
    """The first line of the `-m` / `--message` value of a commit's arguments, else of the
    heredoc body (`heredoc`) for `-F -` / `--file=-` / `--file -`."""
    try:
        tokens = shlex.split(args)
    except ValueError:
        return None
    from_stdin = False
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok.startswith("--message="):
            value = tok.split("=", 1)[1]
        elif tok == "--message" or (re.fullmatch(r"-[a-zA-Z]*m", tok) is not None):
            value = nxt
        elif re.fullmatch(r"-m.+", tok):
            value = tok[2:]
        else:
            if (tok in ("--file=-", "-F-") or (tok in ("--file", "-F") and nxt == "-")
                    or (re.fullmatch(r"-[a-zA-Z]*F", tok) is not None and nxt == "-")):
                from_stdin = True
            continue
        return _first_line(value)
    if from_stdin and heredoc is not None:
        return _first_line(heredoc)
    return None


@dataclass
class Shape:
    """What a commit command says about its own output."""

    log_after: bool      # `&& git log --oneline` right after the commit: its first line is the new commit
    certain: bool        # an `&&`-only chain after the commit, or `set -e`: a success exit means it was made
    message: str | None  # the subject: `-m`, or the heredoc body of `-F -`


_ERREXIT_RE = re.compile(r"(?:^|[;&|(\n])\s*set\s+(?:-[a-zA-Z]*e[a-zA-Z]*|-o\s+errexit)\b")


def command_shape(command: str) -> Shape | None:
    stripped, bodies = split_heredocs(command)
    masked = _mask(stripped)
    m = GIT_COMMIT_RE.search(masked)
    if m is None:
        return None
    op = _OP_RE.search(masked, m.end())
    seg_end = op.start() if op else len(masked)
    rest = masked[seg_end:]
    log = _LOG_RE.search(rest)
    log_after = log is not None and _only_and(rest[: log.start() + 1])
    # The heredoc that feeds this commit: the first `<<` marker in its segment.
    heredoc = None
    marker = re.search(r"<<", masked[m.end():seg_end])
    if marker is not None:
        index = len(_HEREDOC_RE.findall(masked[: m.end() + marker.start()]))
        heredoc = bodies[index] if index < len(bodies) else None
    before = _OP_RE.findall(masked[: m.start() + 1])
    after = op.group(0) if op else None
    errexit = (_ERREXIT_RE.search(masked[: m.start() + 1]) is not None
               and (not before or before[-1] not in ("|", "||")) and after not in ("|", "||", "&"))
    return Shape(log_after, _only_and(rest) or errexit, _message_arg(stripped[m.end():seg_end], heredoc))


class Collector:
    """Dedupes by sha (a commit without a known sha: by repo + message) and caps the list."""

    def __init__(self) -> None:
        self.commits: dict[Any, dict[str, Any]] = {}
        self.missed_dirs: list[str] = []   # where commit calls ran that gave no commit (git-log fallback)
        self.calls = 0                     # commit calls seen (with or without a result)

    def _put(self, sha: str | None, message: str, repo: str | None, branch: str | None) -> None:
        key: Any = sha or ("?", repo, message)
        if key not in self.commits:
            self.commits[key] = {"sha": sha, "message": _clip(message, MESSAGE_MAX), "repo": repo,
                                 "branch": _clip(branch, BRANCH_MAX) if branch else None}

    def _put_oneline(self, line: re.Match[str], repo: str | None) -> None:
        head = re.search(r"HEAD -> ([^,\s]+)", line.group("refs") or "")
        self._put(line.group("sha"), line.group("subject"), repo, head.group(1) if head else None)

    def add(self, output: str, command: str, cwd: str, *, ok: bool = True, background: bool = False) -> None:
        """One commit call. `ok`: the call exited 0. `background`: its output is elsewhere."""
        shape = command_shape(command)
        if shape is None:
            return
        self.calls += 1
        directory, state = resolve_dir(command, cwd)
        if state == DIR_TEMP:   # a throwaway `mktemp` repo: not the session's work
            return
        if not self._add(output, shape, _repo(directory), ok=ok, background=background) and directory:
            if directory not in self.missed_dirs:
                self.missed_dirs.append(directory)

    def _add(self, output: str, shape: Shape, repo: str | None, *, ok: bool, background: bool) -> bool:
        found = parse_output(output)
        for branch, sha, subject in found:
            self._put(sha, subject, repo, branch)
        if found or background:
            return bool(found)
        if shape.message:   # a log line with the commit's own subject, after any operators
            want = " ".join(shape.message.split())
            for line in _ONELINE_RE.finditer(output or ""):
                if " ".join(line.group("subject").split()) == want:
                    self._put_oneline(line, repo)
                    return True
        if not ok:
            return False
        if shape.log_after:
            line = _ONELINE_RE.search(output or "")
            if line is not None:
                self._put_oneline(line, repo)
                return True
        if shape.certain and shape.message:   # `git commit -q`: no output, but it exited 0
            self._put(None, shape.message, repo, None)
            return True
        return False

    def result(self) -> list[dict[str, Any]]:
        return list(self.commits.values())[-MAX_COMMITS:]


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


# --------------------------------------------------------------------------- git-log fallback

GIT_TIMEOUT = 10.0
SOURCE_GIT_LOG = "git-log"
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class Fallback:
    """When to ask git: the session's `commit` signal count and its time window."""

    commit_signal: int
    since: datetime | None
    until: datetime | None


def _git(run: Runner, directory: str, *args: str) -> str | None:
    """stdout of `git -C <directory> <args>`, or None (not a repo, git missing, timeout)."""
    try:
        got = run(["git", "-C", directory, "--no-pager", *args], capture_output=True, text=True,
                  timeout=GIT_TIMEOUT, check=False, env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    return got.stdout if got.returncode == 0 else None


def git_log_commits(directory: str, since: datetime, until: datetime, *,
                    run: Runner = subprocess.run) -> list[dict[str, Any]]:
    """Commits on HEAD of the local repo at `directory` between `since` and `until` (committer
    date), no merges, by the repo's configured `user.email` when it has one. Oldest first.
    [] when `directory` is not a local git checkout. Never a shell."""
    if not directory or not Path(directory).is_dir():
        return []
    top = _git(run, directory, "rev-parse", "--show-toplevel")
    if not top or not top.strip():
        return []
    author = (_git(run, directory, "config", "user.email") or "").strip()
    args = ["log", "--no-merges", f"--since={(since - timedelta(seconds=1)).isoformat()}",
            f"--until={(until + timedelta(seconds=1)).isoformat()}", f"-n{MAX_COMMITS}", "--format=%h|%s"]
    if author:
        args[1:1] = ["--fixed-strings", f"--author={author}"]
    out = _git(run, directory, *args)
    if out is None:
        return []
    repo = _repo(top.strip())
    commits = []
    for line in reversed(out.splitlines()):
        sha, sep, subject = line.partition("|")
        if sep and re.fullmatch(r"[0-9a-f]{4,40}", sha):
            commits.append({"sha": sha, "message": _clip(subject, MESSAGE_MAX), "repo": repo, "branch": None,
                            "source": SOURCE_GIT_LOG})
    return commits


def with_fallback(found: list[dict[str, Any]], dirs: list[str], cwd: str, fallback: Fallback | None, *,
                  run: Runner = subprocess.run) -> list[dict[str, Any]]:
    """`found`, or when it is empty but the session's signals count a commit: the git-log
    commits of the dirs where the commit calls ran. `cwd`: the session cwd, asked only when no
    commit call was seen at all (callers pass "" otherwise: a commit in an unknown or throwaway
    dir must not pull in the session cwd's commits)."""
    if found or fallback is None or fallback.commit_signal <= 0 or fallback.since is None:
        return found
    until = fallback.until or fallback.since
    out: dict[str, dict[str, Any]] = {}
    for directory in dirs or ([cwd] if cwd else []):
        for c in git_log_commits(directory, fallback.since, until, run=run):
            out.setdefault(c["sha"], c)
    return list(out.values())[-MAX_COMMITS:]


def claude_commits(events: list[dict[str, Any]], *, cwd: str = "", own_session: str | None = None,
                   fallback: Fallback | None = None) -> list[dict[str, Any]]:
    """The commits of a Claude Code transcript. `own_session`: count only lines whose
    `sessionId` is that id or missing (a resumed session's copied lines belong to the earlier
    session). `cwd`: the session cwd, used when a line has none. `fallback`: ask git when the
    transcript shows no commit but the signals count one (see the module doc)."""
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
    for command, call_cwd, _background in calls.values():   # a call with no result line yet
        out.calls += 1
        directory = command_dir(command, call_cwd)
        if directory and directory not in out.missed_dirs:
            out.missed_dirs.append(directory)
    return with_fallback(out.result(), out.missed_dirs, "" if out.calls else cwd, fallback)


def codex_commits(events: list[dict[str, Any]], *, cwd: str = "",
                  fallback: Fallback | None = None) -> list[dict[str, Any]]:
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
    return with_fallback(out.result(), out.missed_dirs, "" if out.calls else _local_path(cwd or turn_cwd), fallback)
