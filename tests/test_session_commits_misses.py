"""Commit capture misses found in real top-level sessions (2026-10-14), each as a synthetic
case, and the git-log fallback (docs/sessions-live.md, "Commits").

Miss causes (the example session in brackets):
- heredoc message: `git commit -q -F - <<'EOF' ... EOF`, then `git log --oneline` after a line
  break or `;` [038b12ef, 3a742025, 2a4d669b, f78910e9]
- `set -e` script: `;` and line breaks between the commands, `git commit -q -m "<multi-line>"`
  [13dae1cd]
- background commit: `cd <worktree> && git commit -q -m "<multi-line>" && ...` with
  run_in_background, the output went to a task file [01763625]
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_memory import session_commits
from test_deviations_support import assistant, result, tool


def commit(cwd: str, tid_: str, output: str, command: str, *, background: bool = False) -> list[dict[str, Any]]:
    inp: dict[str, Any] = {"command": command}
    if background:
        inp["run_in_background"] = True
    return [assistant(tool("Bash", tid_, **inp), minute=5, cwd=cwd), result(tid_, output, 5, cwd=cwd)]


HEREDOC = """git add infra/a.bicep && git commit -q -F - <<'EOF'
feat(infra): connect PR environments to the service

It's on per environment; staging stays off && prod too.
git commit inside a message body is text, not a command.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
git log --oneline -1; git status --short"""


# --------------------------------------------------------------------------- heredoc messages


def test_heredoc_message_with_log_after_a_line_break() -> None:
    events = commit("/work/demo-repo", "h1", "f8f9261 feat(infra): connect PR environments to the service\n M x\n",
                    HEREDOC)
    assert session_commits.claude_commits(events) == [
        {"sha": "f8f9261", "message": "feat(infra): connect PR environments to the service", "repo": "demo-repo",
         "branch": None}]


def test_heredoc_log_line_is_matched_by_subject_not_position() -> None:
    # The log shows an older commit first (a pull, `log -3`): the subject picks the right line.
    out = "378b679 feat(infra): seed baseline texts\n315948f feat(infra): connect PR environments to the service\n"
    events = commit("/work/demo-repo", "h2", out, HEREDOC)
    assert [c["sha"] for c in session_commits.claude_commits(events)] == ["315948f"]


def test_heredoc_with_a_variable_dir_and_a_decorated_log() -> None:
    command = ("I=~/worktrees/pipeline-poc/_integration; git -C $I add -A && git -C $I commit -q -F - <<'EOF'\n"
               "fix(datasheets): match the header logo\n\nbody\nEOF\ngit -C $I log --oneline --decorate -1")
    events = commit("/work/demo-repo", "h3", "4330b44 (HEAD -> integration) fix(datasheets): match the header logo\n",
                    command)
    assert session_commits.claude_commits(events) == [
        {"sha": "4330b44", "message": "fix(datasheets): match the header logo", "repo": "pipeline-poc",
         "branch": "integration"}]


def test_quiet_heredoc_commit_at_the_end_is_certain() -> None:
    command = "git add -A && git commit -q -F- <<-EOF\n\tSubject of a quiet commit\n\tEOF\n"
    events = commit("/work/demo-repo", "h4", "", command)
    assert session_commits.claude_commits(events) == [
        {"sha": None, "message": "Subject of a quiet commit", "repo": "demo-repo", "branch": None}]


def test_heredoc_body_is_not_shell_code() -> None:
    stripped, bodies = session_commits.split_heredocs(HEREDOC)
    assert "inside a message body" not in stripped and stripped.endswith("git log --oneline -1; git status --short")
    assert bodies[0].startswith("feat(infra): connect")
    # Text that only mentions `git commit` inside a heredoc is no commit call.
    assert not session_commits.is_commit_command("cat > notes.md <<'EOF'\nthen run git commit -m x\nEOF")
    # A `<<` inside quotes is not the command's own heredoc.
    assert session_commits.split_heredocs('git commit -m "$(cat <<\'EOF\'\nS\nEOF\n)"')[1] == []


# --------------------------------------------------------------------------- set -e scripts


def test_set_e_script_with_semicolons_and_a_multiline_message() -> None:
    command = ("set -e; cd /tmp/scratch/agent-skills\ngit init -q -b main; git config user.name x\n"
               'git add -A; git commit -q -m "Add agent skills (as is)\n\nBody line one.\nBody; line two."\n'
               "gh repo create agent-skills --private --source . --push")
    events = commit("/work/demo-repo", "s1", "https://github.com/x/agent-skills\n", command)
    assert session_commits.claude_commits(events) == [
        {"sha": None, "message": "Add agent skills (as is)", "repo": "agent-skills", "branch": None}]


def test_semicolon_chain_without_set_e_stays_unknown() -> None:
    events = commit("/work/demo-repo", "s2", "", 'git add -A; git commit -q -m "Maybe"; echo done')
    assert session_commits.claude_commits(events) == []
    # `set -e` does not cover a commit inside `||` or a pipe.
    events = commit("/work/demo-repo", "s3", "", 'set -e; git commit -q -m "Maybe" || true')
    assert session_commits.claude_commits(events) == []


# --------------------------------------------------------------------------- dirs from shell syntax


MKTEMP = ("set -e; W=$(mktemp -d /tmp/audit-routines/repro.XXXX); cp -R /tmp/fx/. $W/; cd $W; git init -q && "
          "git add -A && git -c user.email=b@b -c user.name=b commit -qm base; python3 -m unittest -q 2>&1 | tail -1")


def test_mktemp_repo_commit_is_dropped() -> None:
    # [b1b2011c/agent-a5dfc06f3febc7edf] stored {repo: "$(mktemp", message: "base"}
    events = commit("/work/demo-repo", "t1", "OK\n", MKTEMP)
    assert session_commits.resolve_dir(MKTEMP, "/work/demo-repo") == ("", session_commits.DIR_TEMP)
    assert session_commits.claude_commits(events) == []
    direct = 'cd "$(mktemp -d)" && git init -q && git commit -q --allow-empty -m tmp'
    assert session_commits.claude_commits(commit("/work/demo-repo", "t2", "", direct)) == []


def test_mktemp_commit_never_falls_back_to_the_session_cwd(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    events = commit(str(tmp_path), "t3", "OK\n", MKTEMP)
    fb = session_commits.Fallback(2, T, T + timedelta(hours=1))
    assert session_commits.with_fallback([], [], "", fb, run=run) == [] and calls == []
    assert session_commits.claude_commits(events, cwd=str(tmp_path), fallback=fb) == []


@pytest.mark.parametrize("command", [
    'cd "$(git rev-parse --show-toplevel)" && git commit -q -m "Subshell dir"',
    "cd `pwd -P` && git commit -q -m \"Subshell dir\"",
    'cd $UNSET_DIR && git commit -q -m "Subshell dir"',
    'git -C "$(dirname $X)" commit -q -m "Subshell dir"',
])
def test_unresolvable_dir_gives_a_null_repo(command: str) -> None:
    got = session_commits.claude_commits(commit("/work/demo-repo", "u1", "", command))
    assert got == [{"sha": None, "message": "Subshell dir", "repo": None, "branch": None}]


def test_home_variable_and_plain_assignment_still_resolve() -> None:
    assert session_commits.command_dir('cd "$HOME/worktrees/x/w1" && git commit -m a', "/c") == \
        str(Path("~/worktrees/x/w1").expanduser())
    assert session_commits.command_dir("W=/src/proj; git -C $W commit -m a", "/c") == "/src/proj"


# --------------------------------------------------------------------------- git-log fallback


def _git(repo: Path, *args: str, when: datetime | None = None, email: str = "me@example.com") -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": email}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


T = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    r = tmp_path / "demo-repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "me@example.com")
    _git(r, "commit", "-q", "--allow-empty", "-m", "Before the session", when=T - timedelta(hours=2))
    _git(r, "commit", "-q", "--allow-empty", "-m", "Mine one", when=T + timedelta(minutes=5))
    _git(r, "commit", "-q", "--allow-empty", "-m", "A colleague's", when=T + timedelta(minutes=6),
         email="other@example.com")
    _git(r, "commit", "-q", "--allow-empty", "-m", "Mine | two", when=T + timedelta(minutes=7))
    _git(r, "commit", "-q", "--allow-empty", "-m", "After the session", when=T + timedelta(hours=3))
    return r


def test_git_log_fallback_window_author_and_source(repo: Path) -> None:
    got = session_commits.git_log_commits(str(repo), T, T + timedelta(hours=1))
    assert [c["message"] for c in got] == ["Mine one", "Mine | two"]           # oldest first, own author only
    assert all(c["source"] == "git-log" and c["branch"] is None and c["repo"] == "demo-repo" for c in got)
    assert all(len(c["sha"]) >= 7 for c in got)
    # A subdirectory of the checkout is the same repo.
    (repo / "sub").mkdir()
    assert len(session_commits.git_log_commits(str(repo / "sub"), T, T + timedelta(hours=1))) == 2


def test_git_log_fallback_without_a_configured_author_takes_every_author(repo: Path) -> None:
    _git(repo, "config", "--unset", "user.email")
    got = session_commits.git_log_commits(str(repo), T, T + timedelta(hours=1))
    assert [c["message"] for c in got] == ["Mine one", "A colleague's", "Mine | two"]


def test_git_log_fallback_skips_dirs_that_are_no_local_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert session_commits.git_log_commits(str(plain), T, T + timedelta(hours=1)) == []
    assert session_commits.git_log_commits(str(tmp_path / "missing"), T, T + timedelta(hours=1)) == []
    assert session_commits.git_log_commits("", T, T + timedelta(hours=1)) == []


def test_fallback_only_when_nothing_parsed_and_a_confirmed_call_ran_in_the_dir(repo: Path) -> None:
    window = session_commits.Fallback(1, T, T + timedelta(hours=1))
    parsed = [{"sha": "abc1234", "message": "m", "repo": "r", "branch": "main"}]
    assert session_commits.with_fallback(parsed, [str(repo)], str(repo), window) == parsed
    assert session_commits.with_fallback([], [str(repo)], str(repo), None) == []
    # The session cwd and the signal count alone never ask git (s-fix3): only a confirmed call's dir does.
    assert session_commits.with_fallback([], [], str(repo), window) == []
    assert [c["message"] for c in session_commits.with_fallback([], [str(repo)], "", window)] == ["Mine one",
                                                                                                 "Mine | two"]


def test_background_commit_uses_the_dir_it_ran_in(repo: Path, tmp_path: Path) -> None:
    # [01763625] the session cwd is another folder; the commit ran in the `cd` dir, in the background.
    other = tmp_path / "agent-config"
    other.mkdir()
    command = (f'cd {repo} && git add -A && git commit -q -m "Mine one\n\nbody line" && uv run pytest -q 2>&1 | tail -2')
    events = commit(str(other), "b1", "Command running in background with ID: x. Output is being written to: /tmp/x",
                    command, background=True)
    assert session_commits.claude_commits(events, cwd=str(other)) == []          # no fallback asked
    got = session_commits.claude_commits(events, cwd=str(other),
                                         fallback=session_commits.Fallback(2, T, T + timedelta(hours=1)))
    assert [c["message"] for c in got] == ["Mine one", "Mine | two"] and got[0]["source"] == "git-log"


def test_git_runs_without_a_shell_and_with_a_timeout(repo: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kw))
        return subprocess.run(argv, **kw)

    session_commits.git_log_commits(str(repo), T, T + timedelta(hours=1), run=run)
    assert calls and all(argv[0] == "git" and "shell" not in kw and kw["timeout"] == session_commits.GIT_TIMEOUT
                         for argv, kw in calls)
    log = next(argv for argv, _ in calls if "log" in argv)
    assert "--no-merges" in log and "--author=me@example.com" in log and "--fixed-strings" in log

    def timeout(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    assert session_commits.git_log_commits(str(repo), T, T + timedelta(hours=1), run=timeout) == []
