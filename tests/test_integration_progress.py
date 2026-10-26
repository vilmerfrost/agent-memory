"""Exercise the progress runner with real pytest, including failing and parallel runs."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import threading

import pytest

from conftest import base_env

RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "integration_tests.py"


def command(tmp_path: Path, source: str, workers: int = 0) -> list[str]:
    test = tmp_path / "test_sample.py"
    test.write_text(source)
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    return [sys.executable, str(RUNNER), "--workers", str(workers), "--interval", "0.1",
            "--log-dir", str(tmp_path / "logs"), "--", "-c", str(config), str(test)]


@pytest.mark.parametrize("workers", [0, 2])
def test_failed_run_keeps_exit_code_counts_and_full_output(tmp_path: Path, workers: int) -> None:
    cmd = command(tmp_path, '''
import pytest
def test_ok(): pass
def test_skipped(): pytest.skip("deliberate skip")
def test_bad():
    print("captured failure details")
    assert False, "failure marker"
@pytest.fixture
def broken_setup(): raise RuntimeError("setup marker")
def test_setup(broken_setup): pass
@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown marker")
def test_teardown(broken_teardown): pass
''', workers)
    result = subprocess.run(cmd, cwd=tmp_path, env=base_env(), capture_output=True, text=True, timeout=45)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "5/5 complete" in result.stdout
    assert "passed=1 failed=1 errors=2 skipped=1" in result.stdout
    assert "FAIL [call]" in result.stdout and "FAIL [setup]" in result.stdout
    assert "FAIL [teardown]" in result.stdout and "pytest exit 1" in result.stdout
    log = (tmp_path / "logs" / "latest.log").read_text()
    for text in ("captured failure details", "failure marker", "setup marker", "teardown marker",
                 "test_ok", "test_skipped", "test_bad", "test_setup", "test_teardown"):
        assert text in log
    assert "captured failure details" not in result.stdout
    assert "Starting pytest" not in log  # progress stays separate from captured test output


@pytest.mark.parametrize("capture", ["capsys", "capfd"])
def test_progress_arrives_during_a_test_without_contaminating_capture(tmp_path: Path, capture: str) -> None:
    started, release = tmp_path / "started", tmp_path / "release"
    cmd = command(tmp_path, f'''
from pathlib import Path
import time
def test_wait({capture}):
    print("only the test's output")
    Path({str(started)!r}).touch()
    deadline = time.monotonic() + 20
    while not Path({str(release)!r}).exists():
        assert time.monotonic() < deadline, "parent never saw live progress"
        time.sleep(0.01)
    assert {capture}.readouterr().out == "only the test's output\\n"
''')
    lines: list[str] = []
    live = threading.Event()
    with subprocess.Popen(cmd, cwd=tmp_path, env=base_env(), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True) as process:
        def read() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                if started.exists() and "0/1 complete" in line:
                    live.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            assert live.wait(15), "".join(lines)
        finally:
            release.touch()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            reader.join(timeout=5)
        assert process.returncode == 0, "".join(lines)
    assert "1/1 complete" in "".join(lines)
    assert "PASS (pytest exit 0)" in "".join(lines)


@pytest.mark.parametrize("source,code,detail", [("def broken(:\n", 2, "SyntaxError"),
                                               ("# no tests\n", 5, "no tests ran")])
def test_collection_errors_and_empty_runs_cannot_look_like_a_pass(
    tmp_path: Path, source: str, code: int, detail: str,
) -> None:
    result = subprocess.run(command(tmp_path, source), cwd=tmp_path, env=base_env(),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == code, result.stdout + result.stderr
    assert f"FAIL (pytest exit {code})" in result.stdout
    assert "PASS (pytest exit 0)" not in result.stdout
    assert detail in (tmp_path / "logs" / "latest.log").read_text()
