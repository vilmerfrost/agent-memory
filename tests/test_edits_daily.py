"""The `amem daily` edits step: off unless AMEM_EDITS_LIVE=1, log fields only when it ran,
one candidate's error fails the step but not the others. Plus the plist and the edge function."""

from __future__ import annotations

import json
import plistlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_memory import daily, distill, edits
from test_daily_support import LOG_LINE_RE, DailyIO, Push, daily_io  # noqa: F401
from test_edits_support import (  # noqa: F401
    SPAWN_NEW,
    EHome,
    apply_answer,
    edit_rows,
    ehome,
    envelope,
    ewriter,
    proposal,
    seed,
    sha_of,
)
from test_scoring_support import ScoringDb, sdb  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 15, 9, 20, 0, tzinfo=timezone(timedelta(hours=2)))
FIELDS = re.compile(r" edits=\d+ edits_kept=\d+ edits_reverted=\d+ edits_suggested=\d+ edit_errors=\d+ ")


def test_off_without_live_flag(sdb: ScoringDb, daily_io: DailyIO, ehome: EHome, ewriter: Any,  # noqa: F811
                               capsys: pytest.CaptureFixture[str]) -> None:
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=sha_of(ehome.skill))
    w = ewriter()
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "PASS" and r.edits is None and w.prompts == []
    (line,) = daily_io.log_lines()
    assert re.fullmatch(LOG_LINE_RE, line) and not FIELDS.search(line)
    assert "edits: off (AMEM_EDITS_LIVE is not 1)" in capsys.readouterr().err
    assert ehome.improve.calls() == []


def test_on_runs_after_deviations_and_logs_fields(sdb: ScoringDb, daily_io: DailyIO, ehome: EHome,  # noqa: F811
                                                  ewriter: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    push = daily_io.use_push(Push(status=200))
    daily_io.notify_env.parent.mkdir(parents=True, exist_ok=True)
    daily_io.notify_env.write_text("NTFY_TOPIC=topic-daily\n")
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=sha_of(ehome.skill))
    seed(sdb.url, "claude-md:demo-repo", "whole", "Demo repo", n=2, cwd=str(ehome.repo), repo="demo-repo")
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-1"))
    ehome.improve.queue("suggest", {"stdout": "logged\n", "exit": 0})
    r = daily.run_daily(now=NOW)
    assert r.verdict == "PASS", r.failures
    (line,) = daily_io.log_lines()
    assert " tasks=0 edits=1 edits_kept=1 edits_reverted=0 edits_suggested=1 edit_errors=0 " in line
    assert [_json(p)["title"] for p in push.posts] == ["Agent edit: 4. Spawn"]
    assert [row["status"] for row in edit_rows(sdb.url)] == ["kept", "suggested"]


def _json(post: dict[str, Any]) -> dict[str, Any]:
    return json.loads(post["body"])


def test_no_push_flag_never_pushes(sdb: ScoringDb, daily_io: DailyIO, ehome: EHome, ewriter: Any,  # noqa: F811
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=sha_of(ehome.skill))
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-1"))
    r = daily.run_daily(push=False, now=NOW)   # daily_io fails the test on any push
    assert r.verdict == "PASS" and r.edits.kept == 1


def test_candidate_error_fails_the_step_not_the_others(sdb: ScoringDb, daily_io: DailyIO, ehome: EHome,  # noqa: F811
                                                       ewriter: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    seed(sdb.url, "claude-md:global", "shell-habits", "Shell Habits", n=4, sha=sha_of(ehome.global_md))
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=sha_of(ehome.skill))
    ewriter(distill.WriterError("exit 1: overloaded"), envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("auto-reverted", "c-2"))
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL"
    head, fail = daily_io.log_lines()
    assert " edits=1 edits_kept=0 edits_reverted=1 edits_suggested=0 edit_errors=1 " in head
    assert fail.endswith("amem-daily-fail edits: 1 error; first: claude-md:global shell-habits: writer: exit 1: overloaded")


def test_require_remote_refuses_the_step(sdb: ScoringDb, daily_io: DailyIO, ehome: EHome,  # noqa: F811
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")
    r = daily.run_daily(push=False, now=NOW)
    assert r.edits is None
    assert "edits: AMEM_REQUIRE_REMOTE=1 refused a local or empty database" in r.failures


def test_plist_sets_edits_live() -> None:
    data = plistlib.loads((REPO / "deploy" / "launchd" / "com.vilmerfrost.amem-daily.plist").read_bytes())
    assert data["EnvironmentVariables"]["AMEM_EDITS_LIVE"] == "1"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_edge_function_handler_under_node() -> None:
    """supabase/functions/amem-undo/handler_test.ts (also runs under `deno test`)."""
    res = subprocess.run(["node", "--test", str(REPO / "supabase" / "functions" / "amem-undo" / "handler_test.ts")],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "# fail 0" in res.stdout or "fail 0" in res.stdout
