"""Commit evidence (docs/sessions-live.md, "Outcome" and "Commits"): a `git commit` that is only
text (a grep / rg pattern, an echo, a heredoc body) or a commit call that failed never counts as a
commit, never sets the outcome to committed / shipped, and never runs the git-log fallback. A
commit counts only on proof from the call's result. Synthetic lines only."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_memory import amem_digest, codex_sessions, session_commits, session_tokens, sessions_ingest
from codex_support import Rollout, tid
from test_deviations_support import assistant, result, text, tool

CWD = "/work/demo-repo"
LATER = datetime(2026, 10, 30, tzinfo=UTC)


def call(tid_: str, command: str, output: str, *, error: bool = False, background: bool = False,
         cwd: str = CWD, **extra: Any) -> list[dict[str, Any]]:
    inp: dict[str, Any] = {"command": command}
    if background:
        inp["run_in_background"] = True
    return [assistant(tool("Bash", tid_, **inp), minute=5, cwd=cwd),
            result(tid_, output, 5, error=error, cwd=cwd, **extra)]


def signals(events: list[dict[str, Any]]) -> dict[str, int]:
    return amem_digest.outcome_signals(events)["signals"]


# --------------------------------------------------------------------------- not a commit

GREP_OUT = "src/x.py:12: if 'git commit' in line:\n"
NOT_COMMITS = [
    ('grep -n "foo\\|git commit" src/x.py', GREP_OUT, False),              # the 2026-10-14 bug: `\|` in quotes
    ("grep -n foo\\|git\\ commit src/x.py", GREP_OUT, False),              # escaped, no quotes
    ("rg 'git commit' src", GREP_OUT, False),
    ("rg -n 'git commit|git push' src | head -5", GREP_OUT, False),
    ('echo "git commit"', "git commit\n", False),
    ('echo "next: git commit -m x && git push origin main"', "next\n", False),
    ("cat > notes.md <<'EOF'\nthen run git commit -m x\nand git push origin main\nEOF", "", False),
    ('git commit -m "x"', "On branch main\nnothing to commit, working tree clean\n", True),   # failed: exit 1
    ('git add -A && git commit -m "x"', "Exit code 1\nOn branch main\nnothing to commit\n", True),
    ('git commit -m "x"; echo done', "nothing to commit, working tree clean\ndone\n", False),  # `;` hid the exit
    ('git commit -m "x" || true', "nothing to commit, working tree clean\n", False),
]


@pytest.mark.parametrize("command,output,error", NOT_COMMITS)
def test_no_commit_signal_outcome_or_commits(command: str, output: str, error: bool, tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    events = call("t1", command, output, error=error, cwd=str(repo)) + [assistant(text("done"), minute=6)]
    got = signals(events)
    assert "commit" not in got and "push_main" not in got and "push" not in got
    name, evidence = session_tokens.claude_summary(events).outcome(LATER)
    assert name not in ("committed", "pushed", "shipped") and "commit" not in evidence
    # No commits entry, and no git-log fallback: the repo HAS commits in the window.
    window = session_commits.Fallback(5, T, T + timedelta(hours=1))
    assert session_commits.claude_commits(events, cwd=str(repo), fallback=window) == []


def test_denied_commit_is_no_commit() -> None:
    events = call("t1", 'git commit -m "x"', "The user doesn't want to proceed", error=True,
                  toolDenialKind="user")
    assert "commit" not in signals(events)
    assert session_commits.claude_commits(events) == []


def test_quote_aware_segments() -> None:
    seg = amem_digest.segments
    assert [s for s, _ in seg('grep -n "a\\|git commit" f')] == ['grep -n "a\\|git commit" f']
    assert [op for _, op in seg("a && b || c; d | e & f\ng")] == ["&&", "||", ";", "|", "&", "\n", None]
    assert [s.strip() for s, _ in seg("make test 2>&1 | tail -3")] == ["make test 2>&1", "tail -3"]
    assert [s.strip() for s, _ in seg('x=$(git log -1; echo) && git commit -m "a;b"')] == \
        ["x=$(git log -1; echo)", 'git commit -m "a;b"']
    assert [s.strip() for s, _ in seg("git add -A &&\n  git commit -q -m x")] == ["git add -A", "git commit -q -m x"]
    cs = amem_digest.command_signals
    assert cs("grep 'git push origin main' f") == [] and cs('echo "x | git push origin main"') == []
    assert cs('git commit -m "x | y" && git push origin main') == ["commit", "push_main"]


# --------------------------------------------------------------------------- a commit

def test_real_commit_with_its_commit_line() -> None:
    events = call("t1", 'git commit -m "x"', "[main abc1234] x\n 1 file changed, 1 insertion(+)\n")
    assert signals(events) == {"commit": 1}
    assert session_tokens.claude_summary(events).outcome(LATER)[0] == "committed"
    assert session_commits.claude_commits(events) == [
        {"sha": "abc1234", "message": "x", "repo": "demo-repo", "branch": "main"}]


def test_commit_line_counts_even_when_a_later_step_failed() -> None:
    events = call("t1", 'git commit -m "x" && npm test', "[main abc1234] x\n...\n1 failing\n", error=True)
    assert signals(events) == {"commit": 1}


def test_quiet_commit_in_a_certain_chain_that_succeeded() -> None:
    events = call("t1", 'git commit -q -m "x" && git push', "")
    assert signals(events) == {"commit": 1, "push": 1}
    assert session_tokens.claude_summary(events).outcome(LATER)[0] == "pushed"
    assert session_commits.claude_commits(events) == [{"sha": None, "message": "x", "repo": "demo-repo", "branch": None}]
    # The same quiet commit that failed, or after `;`: no proof.
    assert "commit" not in signals(call("t2", 'git commit -q -m "x" && git push', "Exit code 1", error=True))
    assert "commit" not in signals(call("t3", 'git commit -q -m "x"; git push', ""))


def test_set_e_makes_a_quiet_commit_certain() -> None:
    assert amem_digest.commit_call('set -e; git add -A; git commit -q -m "x"; echo ok') == amem_digest.COMMIT_CERTAIN
    assert amem_digest.commit_call('git commit -q -m "x"; echo ok') == amem_digest.COMMIT_UNSURE
    assert amem_digest.commit_call('git commit -q -m "x" &') == amem_digest.COMMIT_BACKGROUND
    # Pipes between later commands keep it certain; a pipe on the commit itself or a later `||` / `;` do not.
    certain = 'git add -A && git commit -q -m "x" && git push 2>&1 | tail -1'
    assert amem_digest.commit_call(certain) == amem_digest.COMMIT_CERTAIN
    for unsure in ('git commit -m "x" | tail -1', 'git commit -q -m "x" && a || b', 'a || git commit -q -m "x"',
                   'git commit -q -m "x" && a; b'):
        assert amem_digest.commit_call(unsure) == amem_digest.COMMIT_UNSURE, unsure
    assert amem_digest.commit_call("rg 'git commit'") is None


# --------------------------------------------------------------------------- tracker state

def test_old_saved_tracker_state_loads() -> None:
    old = {"signals": {"commit": 2, "edit": 1, "bad": "x"}, "end": "tool", "pending": ["t1"]}   # script_version 4
    t = amem_digest.SignalTracker(old)
    assert t.signals == {"commit": 2, "edit": 1} and t.pending == ["t1"] and t.commit_calls == {}
    for e in call("t1", 'git commit -m "x"', "[main abc1234] x")[1:]:   # its result: counted at the call already
        t.feed(e)
    assert t.signals["commit"] == 2 and t.end == "result"
    assert amem_digest.SignalTracker(None).state() == {"signals": {}, "end": None, "pending": [], "commit_calls": {},
                                                               "operation_calls": {}, "method_version": 2}


def test_commit_waits_in_the_state_until_its_result() -> None:
    first, res = call("t1", 'git commit -q -m "x" && git push', "")
    t = amem_digest.SignalTracker()
    t.feed(first)
    saved = json.loads(json.dumps(t.state()))   # a hook ping stops here
    assert saved["commit_calls"] == {"t1": "c"} and "commit" not in saved["signals"]
    t2 = amem_digest.SignalTracker(saved)
    t2.feed(res)
    assert t2.signals == {"commit": 1, "push": 1} and t2.commit_calls == {}
    junk = amem_digest.SignalTracker({"commit_calls": {"a": "z", "b": "u", "c": 3}})
    assert junk.commit_calls == {"b": "u"}


def test_interrupt_drops_waiting_commits() -> None:
    t = amem_digest.SignalTracker()
    t.feed(call("t1", 'git commit -q -m "x"', "")[0])
    t.feed({"type": "user", "message": {"role": "user", "content": "[Request interrupted by user]"}})
    assert t.commit_calls == {} and "commit" not in t.signals


# --------------------------------------------------------------------------- background + git log

T = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def _git(repo: Path, *args: str, when: datetime | None = None) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "me@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "me@example.com"}
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env)


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "demo-repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "me@example.com")
    _git(r, "commit", "-q", "--allow-empty", "-m", "Another session's commit", when=T + timedelta(minutes=5))
    return r


def test_background_commit_counts_only_when_git_log_confirms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    repo = _repo(tmp_path)
    events = call("b1", f'cd {repo} && git commit -q -m "Mine" && uv run pytest -q',
                  "Command running in background with ID: x. Output is being written to: /tmp/x",
                  background=True, cwd=str(repo))
    assert "commit" not in signals(events)                       # the command text alone never counts
    got = session_commits.claude_commits(events, cwd=str(repo),
                                         fallback=session_commits.Fallback(0, T, T + timedelta(hours=1)))
    assert [c["source"] for c in got] == ["git-log"]               # a confirmed call: git is asked
    row = SimpleNamespace(signals={})
    sessions_ingest.confirm_commits(row, got)                      # type: ignore[arg-type]
    assert row.signals == {"commit": 1}
    assert sessions_ingest.stored_outcome({"end": "reply", "commits": got}, T, LATER, "chat") == "committed"
    assert sessions_ingest.stored_outcome({"end": "reply"}, T, LATER, "chat") == "no-change"


def test_no_fallback_without_a_confirmed_commit_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    repo = _repo(tmp_path)
    window = session_commits.Fallback(3, T, T + timedelta(hours=1))   # an old, inflated commit count
    assert session_commits.with_fallback([], [], str(repo), window) == []
    no_result = [assistant(tool("Bash", "n1", command='git commit -q -m "x"'), minute=5, cwd=str(repo))]
    assert session_commits.claude_commits(no_result, cwd=str(repo), fallback=window) == []
    stats: dict[str, int] = {}
    session_commits.claude_commits(call("g1", 'grep -n "a\\|git commit" f', GREP_OUT, cwd=str(repo)),
                                   cwd=str(repo), fallback=window, stats=stats)
    assert stats == {"confirmed": 0}


# --------------------------------------------------------------------------- Codex

def test_codex_commit_needs_proof() -> None:
    def sig(script: str, code: int = 0) -> dict[str, int]:
        ro = Rollout(tid(8), CWD).injected().user("go", 1).command(script, exit_code=code, minute=2)
        return codex_sessions.outcome_signals(ro.events)[0]

    assert sig('grep -n "a\\|git commit" f') == {}
    assert sig("rg 'git commit'") == {}
    assert sig('git commit -m "x"', code=1) == {}                  # nothing to commit
    assert sig('git commit -m "x"; echo done') == {}               # no commit line, `;` hid the exit
    assert sig('git commit -m "x" && git push origin main') == {"commit": 1, "push_main": 1}


def test_codex_function_call_commit_waits_for_its_output() -> None:
    def ro_with(output: str) -> dict[str, int]:
        ro = Rollout(tid(9), CWD).injected().user("go", 1)
        ro.add("response_item", {"type": "function_call", "name": "shell", "call_id": "f1",
                                 "arguments": json.dumps({"command": ["bash", "-lc", 'git commit -q -m "x"']})}, 2)
        ro.add("response_item", {"type": "function_call_output", "call_id": "f1", "output": output}, 3)
        return codex_sessions.outcome_signals(ro.events)[0]

    assert ro_with('{"output":"","metadata":{"exit_code":0}}') == {"commit": 1}
    assert ro_with('{"output":"nothing to commit","metadata":{"exit_code":1}}') == {}
    assert ro_with("Exit code: 0\nOutput:\n[main abc1234] x\n") == {"commit": 1}
