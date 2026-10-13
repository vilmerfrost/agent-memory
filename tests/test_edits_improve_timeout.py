"""`edits.run_command` on a timeout: improve runs in its own session, and the whole process
group (improve and its eval children) is gone afterwards: SIGTERM first, SIGKILL after the grace."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from agent_memory import edits

FAKE = '''#!{python}
import signal, subprocess, sys, time
from pathlib import Path
if {ignore_term!r}:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import signal, time\\n"
                          + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n" if {ignore_term!r} else "")
                          + "time.sleep(300)"])
Path({pids!r}).write_text(f"{{__import__('os').getpid()}} {{child.pid}}")
print("improve apply: running evals", flush=True)
time.sleep(300)
'''


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone(pids: list[int], within: float = 5.0) -> bool:
    end = time.monotonic() + within        # an orphan is reaped by launchd/init a moment later
    while time.monotonic() < end:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("ignore_term", [False, True])
def test_timeout_kills_improve_and_its_children(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                ignore_term: bool) -> None:
    pids_file = tmp_path / "pids"
    script = tmp_path / "improve"
    script.write_text(FAKE.format(python=sys.executable, pids=str(pids_file), ignore_term=ignore_term))
    script.chmod(0o755)
    monkeypatch.setenv(edits.IMPROVE_ENV, str(script))
    monkeypatch.setattr(edits, "KILL_GRACE", 0.5)
    start = time.monotonic()
    with pytest.raises(edits.ImproveError, match=r"improve apply timed out after 1.5s"):
        edits._improve(["apply", "--json"], timeout=1.5)
    assert time.monotonic() - start < 15
    pids = [int(x) for x in pids_file.read_text().split()]
    assert len(pids) == 2 and _gone(pids), pids


def test_normal_exit_is_unchanged(tmp_path: Path) -> None:
    proc = edits.run_command([sys.executable, "-c", "import sys; print('{\"a\": 1}'); "
                              "print('err', file=sys.stderr); sys.exit(2)"], timeout=30)
    assert proc.returncode == 2 and proc.stdout == '{"a": 1}\n' and proc.stderr == "err\n"
