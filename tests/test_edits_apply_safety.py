"""`amem edits run` when `improve apply` fails part-way, or the database connection dies
while improve runs (it can run for an hour).

improve writes the target first, then runs evals, then records the change. A timeout or a
garbage reply after the write must not leave unverified text in the file: the run restores
the original, records the failure, fails the step and holds the rule in a cooldown. A kept
edit must reach its row even when the original connection is gone.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from agent_memory import daily, edits
from test_daily_support import DailyIO, daily_io  # noqa: F401
from test_edits_support import (  # noqa: F401
    SKILL_MD,
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

NEW_TEXT = SKILL_MD.replace("Each brief names the branch.\n",
                            "Each brief names the branch on its first line, before the task text.\n")
NOW = datetime(2026, 10, 15, 9, 20, 0, tzinfo=timezone(timedelta(hours=2)))


@pytest.fixture
def live(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> ScoringDb:  # noqa: F811
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    monkeypatch.setattr(edits, "PERSIST_DELAY", 0.0)
    return sdb


def seed_spawn(url: str, ehome: EHome, **kw: Any) -> list[str]:  # noqa: F811
    return seed(url, "skill:dispatch", "spawn", "4. Spawn", sha=sha_of(ehome.skill), **kw)


def then(monkeypatch: pytest.MonkeyPatch, after: Any) -> None:
    """Run the fake improve as usual, then call `after(argv)` (raise, kill a connection...)."""
    real = edits.run_command

    def wrapped(argv: list[str], timeout: float = edits.IMPROVE_TIMEOUT) -> subprocess.CompletedProcess[str]:
        proc = real(argv, timeout)
        out = after(argv)
        return proc if out is None else out

    monkeypatch.setattr(edits, "run_command", wrapped)


def time_out(argv: list[str]) -> None:
    raise subprocess.TimeoutExpired(argv, edits.IMPROVE_TIMEOUT)


def assert_failed_and_restored(url: str, r: edits.Report, ehome: EHome, match: str) -> None:  # noqa: F811
    (o,) = r.outcomes
    assert o.status == "proposed" and match in o.reason and "target restored" in o.reason, o.line()
    assert r.errors == 1 and r.kept == 0
    assert ehome.skill.read_text() == SKILL_MD                     # the unverified text is gone
    (row,) = edit_rows(url)
    assert row["status"] == "proposed" and row["improve_id"] is None and match in row["meta"]["error"]
    assert row["meta"]["apply_failed"] is True and row["meta"]["restore"] == "restored"


def assert_cooldown_blocks(url: str, ehome: EHome) -> None:  # noqa: F811
    # Same file version: already handled.
    (o,) = edits.run(dry_run=True, db_url=url).outcomes
    assert o.status == "skip" and o.reason == "already handled at this version"
    # Someone edits another part of the file the next day: the rule is still in its cooldown.
    ehome.skill.write_text(SKILL_MD + "\n## 6. Clean\nRemove worktrees.\n")
    seed_spawn(url, ehome, n=2)
    (o,) = edits.run(dry_run=True, db_url=url).outcomes
    assert o.status == "skip" and o.reason.startswith("cooldown until ") and "improve apply failed" in o.reason, o.line()


def test_timeout_after_write_restores_the_target(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    seed_spawn(live.url, ehome, n=3)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-1", write=True))

    def wrote_then_hung(argv: list[str]) -> None:
        assert ehome.skill.read_text() == NEW_TEXT
        time_out(argv)

    then(monkeypatch, wrote_then_hung)
    r = edits.run(db_url=live.url, push=False)
    assert_failed_and_restored(live.url, r, ehome, "timed out after 3600s")
    monkeypatch.setattr(edits, "run_command", subprocess.run)        # nothing more may call improve
    assert_cooldown_blocks(live.url, ehome)


def test_garbage_reply_after_write_restores_the_target(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    seed_spawn(live.url, ehome, n=3)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", {"stdout": "improve apply: written; Traceback\n", "exit": 1, "write": True})
    r = edits.run(db_url=live.url, push=False)
    assert_failed_and_restored(live.url, r, ehome, "no JSON line")
    assert_cooldown_blocks(live.url, ehome)


def test_failed_apply_fails_the_daily_step(sdb: ScoringDb, daily_io: DailyIO, ehome: EHome,  # noqa: F811
                                           ewriter: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    seed_spawn(sdb.url, ehome, n=3)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-1", write=True))
    then(monkeypatch, time_out)
    r = daily.run_daily(push=False, now=NOW)
    assert r.verdict == "FAIL"
    head, fail = daily_io.log_lines()
    assert " edit_errors=1 " in head
    assert "amem-daily-fail edits: 1 error; first: skill:dispatch spawn: improve: improve apply timed out" in fail
    assert "target restored" in fail and ehome.skill.read_text() == SKILL_MD


def test_unknown_text_is_never_overwritten(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                           monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    """Something else changed the file during the apply: leave it, and say so loudly."""
    seed_spawn(live.url, ehome, n=3)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-1", write=True))
    other = SKILL_MD + "\nHand edit during the run.\n"

    def clobber_then_time_out(argv: list[str]) -> None:
        ehome.skill.write_text(other)
        time_out(argv)

    then(monkeypatch, clobber_then_time_out)
    r = edits.run(db_url=live.url, push=False)
    (o,) = r.outcomes
    assert o.status == "proposed" and "target restore_failed" in o.reason and r.errors == 1
    assert ehome.skill.read_text() == other
    (row,) = edit_rows(live.url)
    assert row["meta"]["restore"] == "restore_failed" and row["meta"]["apply_failed"] is True
    assert "neither the original nor the proposal" in row["meta"]["restore_note"]
    assert "was NOT restored" in capsys.readouterr().err


def test_untouched_target_is_reported_unchanged(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    seed_spawn(live.url, ehome, n=3)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", {"stdout": "", "stderr": "boom", "exit": 1})
    (o,) = edits.run(db_url=live.url, push=False).outcomes
    assert o.status == "proposed" and "target unchanged" in o.reason
    assert edit_rows(live.url)[0]["meta"]["restore"] == "unchanged" and ehome.skill.read_text() == SKILL_MD


def _track_connections(monkeypatch: pytest.MonkeyPatch) -> list[psycopg.Connection]:
    made: list[psycopg.Connection] = []
    real = edits.store.connect

    def connect(db_url: str | None = None, **kw: Any) -> psycopg.Connection:
        conn = real(db_url, **kw)
        made.append(conn)
        return conn

    monkeypatch.setattr(edits.store, "connect", connect)
    return made


def _kill(url: str, conn: psycopg.Connection) -> None:
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("select pg_terminate_backend(%s)", (conn.info.backend_pid,))


@pytest.mark.parametrize("status", ["kept", "trial"])
def test_dead_connection_after_improve_still_records_the_edit(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                              monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    seed_spawn(live.url, ehome, n=3)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer(status, "c-42"))
    made = _track_connections(monkeypatch)
    dropped: list[psycopg.Connection] = []

    def drop(argv: list[str]) -> None:                               # the pooler drops the idle connection
        dropped.append(made[-1])
        _kill(live.url, made[-1])

    then(monkeypatch, drop)
    r = edits.run(db_url=live.url, push=False)
    o = next(x for x in r.outcomes if x.rule_id == "spawn")
    assert o.status == status and o.improve_id == "c-42" and o.push == "off", o.line()
    assert r.errors == 0 and made[-1] is not dropped[0]          # a fresh connection wrote it
    (row,) = edit_rows(live.url)
    assert row["status"] == status and row["improve_id"] == "c-42" and row["undo_token_hash"]
    assert row["meta"]["improve"]["revert"] == "improve revert c-42"
    assert row["instruction_sha_after"] == sha_of(ehome.skill)


def test_unreachable_database_after_improve_names_the_revert(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                             monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    seed_spawn(live.url, ehome, n=3)
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-43"))
    made = _track_connections(monkeypatch)

    def kill_and_cut(argv: list[str]) -> None:
        _kill(live.url, made[-1])

        def down(db_url: str | None = None, **kw: Any) -> psycopg.Connection:
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(edits.store, "connect", down)

    then(monkeypatch, kill_and_cut)
    r = edits.run(db_url=live.url, push=False)
    (o,) = r.outcomes
    assert o.status == "error" and o.improve_id == "c-43" and r.errors == 1 and o.push is None
    assert "undo by hand with `improve revert c-43`" in o.reason
    assert "improve revert c-43" in capsys.readouterr().err
    assert ehome.skill.read_text() == NEW_TEXT                      # kept by improve; a human decides
