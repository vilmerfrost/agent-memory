"""amem dispatch start/end/refresh/report: offline, with a fake `vitals` on PATH.

Every test points AMEM_CLAUDE_HOME (and HOME for subprocesses) at a tmp dir, so
nothing reaches the real ~/.claude/parent.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import time
import uuid

import pytest

from agent_memory import dispatch_estimates as D
from conftest import base_env, run_amem

FIXTURES = Path(__file__).parent / "fixtures" / "vitals"
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def write_vitals(directory: Path, *, claude_session: float | None = 25, codex_week: float = 63,
                 session_resets: str = "2026-10-29T13:29:59Z", agents: dict | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    limits = json.loads((FIXTURES / "limits.json").read_text())
    claude, codex = limits["plans"]
    for lim in claude["limits"]:
        if lim["kind"] == "session":
            lim["used_percent"] = claude_session
            lim["resets_at"] = session_resets
    if claude_session is None:
        limits["plans"] = [codex]
    codex["limits"][0]["used_percent"] = codex_week
    (directory / "limits.json").write_text(json.dumps(limits))
    (directory / "agents.json").write_text(json.dumps(agents or json.loads((FIXTURES / "agents.json").read_text())))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "vitals"
    script.write_text('#!/bin/sh\n'
                      'if [ -n "$FAKE_VITALS_SLEEP" ]; then exec /bin/sleep "$FAKE_VITALS_SLEEP"; fi\n'
                      'exec /bin/cat "$FAKE_VITALS_DIR/$1.json"\n')
    script.chmod(0o755)
    claude_home = tmp_path / "claude"
    write_vitals(tmp_path / "vitals")
    for name in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "FAKE_VITALS_SLEEP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AMEM_CLAUDE_HOME", str(claude_home))
    monkeypatch.setenv("FAKE_VITALS_DIR", str(tmp_path / "vitals"))
    monkeypatch.setenv("PATH", str(bin_dir))
    return claude_home


def spool(home: Path, repo: str = "demo") -> list[dict]:
    path = home / "parent" / repo / D.SPOOL
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def add_run(parent: Path, *, tool: str = "claude", workers: int = 2, minutes: float = 20,
            used: tuple[float, float] = (10, 16), others: int = 0, reset: bool = False,
            tokens: int | None = None, day: int = 0, session: str | None = None, repo: str = "demo") -> str:
    run_id = str(uuid.uuid4())
    at = T0 + timedelta(days=day)
    session = session or f"s-{run_id[:8]}"
    key_tool, kind, name = (("claude", "session", "5-hour session") if tool == "claude"
                            else ("codex", "week", "This week"))
    resets = "2026-10-20T12:00:00Z"
    snap = dict(v=1, type="snapshot", run_id=run_id, tool=key_tool, account_hash="ab" * 8,
                limit_kind=kind, limit_name=name, source="vitals")
    conc = {"claude": others if tool == "claude" else 0, "codex": others if tool == "codex" else 0}
    records = [
        dict(v=1, type="start", run_id=run_id, repo=repo, session=session, tool=tool, at=D.iso(at),
             n_workers=workers, concurrent=others, concurrent_by_tool=conc),
        {**snap, "phase": "start", "snapshot_id": str(uuid.uuid4()), "used_percent": used[0],
         "resets_at": resets, "taken_at": D.iso(at)},
        dict(v=1, type="estimate", run_id=run_id, at=D.iso(at), est_minutes=25, est_slow_minutes=40,
             est_usage={f"{key_tool}:{kind}": 5}, basis_runs=3, method_version=D.METHOD),
        dict(v=1, type="end", run_id=run_id, repo=repo, session=session, tool=tool, dispatched_at=D.iso(at),
             at=D.iso(at + timedelta(minutes=minutes)), n_workers=workers, status="done",
             integration_test_ms=None, concurrent=others, concurrent_by_tool=conc),
        {**snap, "phase": "end", "snapshot_id": str(uuid.uuid4()), "used_percent": used[1],
         "resets_at": "2026-10-20T17:00:00Z" if reset else resets,
         "taken_at": D.iso(at + timedelta(minutes=minutes))},
    ]
    if tokens is not None:
        records.append(dict(v=1, type="tokens", run_id=run_id, tokens_fresh=tokens, tokens_cache_read=0))
    D.append_records(D.repo_dir(repo, parent) / D.SPOOL, records)
    return run_id


# ---------------------------------------------------------------- start / end


def test_start_records_snapshots_estimate_and_open_file(home: Path) -> None:
    result = D.start("demo", 2, brief_chars=1800, files_claimed=6, session="parent-session")
    rows = spool(home)
    assert [r["type"] for r in rows][:1] == ["start"] and rows[-1]["type"] == "estimate"
    snaps = [r for r in rows if r["type"] == "snapshot"]
    assert {(s["tool"], s["limit_kind"]) for s in snaps} == {("claude", "week"), ("claude", "session"),
                                                               ("claude", "model"), ("codex", "week")}
    raw = (home / "parent" / "demo" / D.SPOOL).read_text()
    assert "@" not in raw and "example.invalid" not in raw          # emails only as hashes
    assert snaps[0]["account_hash"] == D.account_hash("claude-owner@example.invalid")
    # asked_minutes_ago=1 before measured_at
    assert snaps[0]["taken_at"] == "2026-10-29T10:53:48Z"
    start = rows[0]
    assert start["tool"] == "claude" and start["n_workers"] == 2 and start["brief_chars"] == 1800
    # Own session and idle sessions do not count; the 1.3% Codex process is below the 2% bar.
    assert start["concurrent_by_tool"] == {"claude": 0, "codex": 0}
    opened = json.loads(D.open_path("demo", "parent-session").read_text())
    assert opened["run_id"] == result["run_id"]
    assert result["line"].startswith("Expect: no history yet: ~30 min (default guess), Claude 5h use: "
                                     "no usage history, now 25% used")
    assert (home / "parent" / "demo" / D.SPOOL).stat().st_mode & 0o777 == 0o600


def test_end_without_run_id_finds_the_open_run_by_session_and_is_idempotent(home: Path, tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-session")
    started = D.start("demo", 1)
    write_vitals(tmp_path / "vitals", claude_session=31)
    ended = D.end()
    assert ended["ended"] and ended["run_id"] == started["run_id"]
    assert "Claude 5h +6.0% (clean)" in ended["line"]
    assert not D.open_path("demo", "parent-session").exists()
    before = (home / "parent" / "demo" / D.SPOOL).read_text()
    again = D.end()
    assert again["ended"] is False and "nothing to end" in again["line"]
    assert (home / "parent" / "demo" / D.SPOOL).read_text() == before
    end = [r for r in spool(home) if r["type"] == "end"][0]
    assert end["status"] == "done" and end["dispatched_at"] == spool(home)[0]["at"]
    assert len([r for r in spool(home) if r["type"] == "snapshot" and r["phase"] == "end"]) == 4


def test_codex_parent_uses_the_codex_weekly_limit(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    result = D.start("demo", 3)
    assert result["tool"] == "codex" and result["session"] == "codex:thread-1"
    assert "Codex week use: no usage history, now 63% used" in result["line"]
    assert D.open_path("demo", "codex:thread-1").exists()


def test_vitals_missing_from_path_still_dispatches(home: Path, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = D.start("demo", 2, session="s1")
    assert "Vitals not found on PATH" in result["line"]
    assert [r["type"] for r in spool(home)] == ["start", "estimate"]
    assert spool(home)[0]["concurrent"] is None
    ended = D.end(session="s1")
    assert ended["ended"] and "Claude 5h use not measured" in ended["line"]


def test_slow_vitals_times_out_without_blocking(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VITALS_SLEEP", "5")
    began = time.monotonic()
    result = D.start("demo", 2, session="s1")
    assert time.monotonic() - began < 2
    assert "Vitals limits timed out" in result["line"]


def test_vitals_without_claude_limits_says_so(home: Path, tmp_path: Path) -> None:
    write_vitals(tmp_path / "vitals", claude_session=None)
    result = D.start("demo", 2, session="s1")
    assert "Vitals shows no Claude limits" in result["line"]


def test_limit_reset_between_start_and_end_is_not_clean(home: Path, tmp_path: Path) -> None:
    D.start("demo", 2, session="s1")
    write_vitals(tmp_path / "vitals", claude_session=3, session_resets="2026-10-29T18:29:59Z")
    ended = D.end(session="s1")
    assert "not clean: limit reset during the run" in ended["line"]
    lookup = D.refresh()
    assert "claude:session" not in lookup["usage"]


def test_concurrent_working_sessions_make_usage_unclean(home: Path, tmp_path: Path) -> None:
    agents = json.loads((FIXTURES / "agents.json").read_text())
    agents["sessions"][2]["status"] = "working"             # another Claude session works
    agents["sessions"][0]["cpu_percent_with_helpers"] = 40  # and a busy Codex process
    write_vitals(tmp_path / "vitals", agents=agents)
    D.start("demo", 2, session="parent-session")
    ended = D.end(session="parent-session")
    assert "1 other Claude session(s) working" in ended["line"]
    end = [r for r in spool(home) if r["type"] == "end"][0]
    assert end["concurrent_by_tool"] == {"claude": 1, "codex": 1} and end["concurrent"] == 2


def test_second_start_in_the_same_session_abandons_the_first(home: Path) -> None:
    first = D.start("demo", 1, session="s1")
    second = D.start("demo", 2, session="s1")
    ends = [r for r in spool(home) if r["type"] == "end"]
    assert [(e["run_id"], e["status"], e["at"]) for e in ends] == [(first["run_id"], "abandoned", None)]
    assert json.loads(D.open_path("demo", "s1").read_text())["run_id"] == second["run_id"]


def test_end_by_run_id_across_repos(home: Path) -> None:
    run = D.start("other-repo", 1, session="s9")
    assert D.end(run["run_id"])["ended"]
    assert [r["type"] for r in spool(home, "other-repo")].count("end") == 1


def test_spool_symlink_is_refused(home: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    link = home / "parent" / "demo" / D.SPOOL
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(ValueError):
        D.start("demo", 1, session="s1")
    assert target.read_text() == ""


# ---------------------------------------------------------------- refresh / estimate


def test_refresh_builds_buckets_and_the_expect_line_uses_them(home: Path) -> None:
    parent = home / "parent"
    for i, minutes in enumerate([18, 20, 22, 24, 40]):
        add_run(parent, workers=2, minutes=minutes, used=(10, 15 + i % 2), day=i)
    add_run(parent, workers=2, minutes=30, used=(10, 30), others=2, day=6)      # unclean: counts for time only
    add_run(parent, workers=5, minutes=70, used=(10, 30), day=7)
    lookup = D.refresh()
    b2 = lookup["time"]["claude"]["2"]
    assert b2["n"] == 6 and b2["median"] == pytest.approx(23) and b2["p80"] == pytest.approx(30)
    assert lookup["time"]["claude"]["5+"]["n"] == 1
    usage = lookup["usage"]["claude:session"]["buckets"]
    assert usage["2"]["n"] == 5 and usage["2"]["median"] == pytest.approx(5)
    saved = json.loads((parent / D.LOOKUP).read_text())
    assert saved["time"] == lookup["time"]
    result = D.start("demo", 2, session="new")
    assert result["line"].startswith("Expect: ~23 min (slow case 30), ~5% of Claude 5h, now 25% used "
                                     "— based on 6 past runs")
    est = [r for r in spool(home) if r["type"] == "estimate"][-1]
    assert est["est_minutes"] == 23 and est["est_usage"] == {"claude:session": 5}
    # A bucket with fewer than 3 runs falls back to all of the tool's runs.
    small = D.start("demo", 5, session="new2")["line"]
    assert "based on 7 past runs (claude, all sizes)" in small


def test_few_clean_runs_fall_back_to_the_token_ratio(home: Path) -> None:
    parent = home / "parent"
    add_run(parent, workers=1, minutes=10, used=(10, 14), tokens=2_000_000)        # 2 % per M tokens
    add_run(parent, workers=1, minutes=12, used=(10, 30), tokens=1_000_000, others=1)
    add_run(parent, workers=1, minutes=14, used=(10, 30), tokens=3_000_000, others=1)
    lookup = D.refresh()
    assert lookup["usage"]["claude:session"]["pct_per_mtoken"] == pytest.approx(2)
    est = D.estimate(lookup, "claude", 1)
    assert est["usage"]["claude:session"] == {"pct": 4.0, "slow_pct": None, "method": "token_ratio", "basis": 1}
    assert "~4% of Claude 5h from tokens" in D.expect_line(est, "claude")


def test_refresh_backfills_tasks_rounds_with_median_test_time(home: Path) -> None:
    parent = home / "parent"
    covered = add_run(parent, workers=1, minutes=30, session="sess-a", day=3)
    D.append_records(parent / "demo" / D.SPOOL, [dict(v=1, type="end", run_id=covered, repo="demo",
                     session="sess-a", tool="claude", dispatched_at=D.iso(T0 + timedelta(days=3)),
                     at=D.iso(T0 + timedelta(days=3, minutes=30)), n_workers=1, status="done",
                     integration_test_ms=120_000, concurrent=0, concurrent_by_tool={"claude": 0, "codex": 0})])
    rounds = [
        # real tasks.jsonl shape (trimmed); 13.8 min first spawn -> last handback
        {"backfilled": False, "duration_s": 828, "ended": "2026-10-26T19:45:08.339Z", "final_status": "DONE",
         "id": "t-4baf4d012c", "n_workers": 1, "parent_model": "claude-opus-5-5", "repo": "demo",
         "session": "sess-b", "source": "live", "started": "2026-10-26T19:31:20.144Z", "tokens": 561811},
        # the same work as the measured run: not counted twice
        {"ended": D.iso(T0 + timedelta(days=3, minutes=25)), "n_workers": 1, "repo": "demo",
         "session": "sess-a", "started": D.iso(T0 + timedelta(days=3, minutes=1))},
        {"ended": "2026-10-27T10:00:00Z", "n_workers": 2, "repo": "demo", "session": "codex:t1",
         "started": "2026-10-27T09:00:00Z"},
        {"started": "broken"},
    ]
    (parent / "demo" / "tasks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rounds) + "{not json\n")
    lookup = D.refresh()
    assert lookup["backfill_rounds"] == 2 and lookup["runs"] == 1
    one = lookup["time"]["claude"]["1"]
    assert one["n"] == 2 and one["backfill"] == 1
    # measured 30 min; backfill 13.8 min round + 2 min measured integration test
    assert one["median"] == pytest.approx((30 + 828 / 60 + 2) / 2, abs=0.01)
    assert lookup["time"]["codex"]["2"]["median"] == pytest.approx(62)
    assert lookup["test_ms"]["demo"] == 120_000


def test_report_compares_estimate_and_actual(home: Path) -> None:
    add_run(home / "parent", minutes=30, used=(10, 16))
    row, = D.report()
    assert row["est_minutes"] == 25 and row["actual_minutes"] == 30 and row["time_ratio"] == 1.2
    assert row["est_pct"] == 5 and row["actual_pct"] == 6 and row["clean"] is True


def test_refresh_uses_a_db_source_for_tokens_and_appends_a_tokens_record(home: Path) -> None:
    run_id = add_run(home / "parent", minutes=10, used=(10, 13), session="sess-x")

    class Fake:
        def test_medians(self):
            return {"demo": 90_000.0}

        def tokens(self, run):
            assert run["session"] == "sess-x"
            return 1_500_000, 9_000_000, 2

    lookup = D.refresh(db=Fake())
    assert lookup["test_ms"]["demo"] == 90_000
    tok = [r for r in spool(home) if r["type"] == "tokens"]
    assert tok == [{"v": 1, "type": "tokens", "run_id": run_id, "tokens_fresh": 1_500_000,
                    "tokens_cache_read": 9_000_000, "sessions_counted": 2, "scope": "workers"}]
    assert lookup["usage"]["claude:session"]["pct_per_mtoken"] == pytest.approx(2)
    D.refresh(db=Fake())       # already has tokens: no second record
    assert len([r for r in spool(home) if r["type"] == "tokens"]) == 1
    D.refresh()                # offline keeps the previous test medians
    assert D.load_lookup()["test_ms"]["demo"] == 90_000


# ---------------------------------------------------------------- CLI contract


def cli_env(home: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    return base_env(AMEM_CLAUDE_HOME=str(home), HOME=str(tmp_path / "fake-home"),
                    PATH=str(tmp_path / "bin") + os.pathsep + "/usr/bin:/bin",
                    FAKE_VITALS_DIR=str(tmp_path / "vitals"), **extra)


def test_cli_start_end_refresh_contract(home: Path, tmp_path: Path) -> None:
    env = cli_env(home, tmp_path, CLAUDE_CODE_SESSION_ID="cli-session")
    env.pop("CODEX_THREAD_ID", None)
    env.pop("AMEM_TIMING_SESSION_ID", None)
    started = run_amem("dispatch", "start", "--repo", "demo", "--workers", "2", "--brief-chars", "900", env=env)
    assert started.returncode == 0, started.stderr
    first, second = started.stdout.splitlines()[:2]
    run_id = first.removeprefix("run_id=")
    assert str(uuid.UUID(run_id)) == run_id and second.startswith("Expect: ")
    ended = run_amem("dispatch", "end", "--status", "partial", "--integration-test-ms", "93000", env=env)
    assert ended.returncode == 0, ended.stderr
    assert ended.stdout.splitlines()[0] == f"run_id={run_id}"
    assert ended.stdout.splitlines()[1].startswith("Actual: ")
    again = run_amem("dispatch", "end", env=env)
    assert again.returncode == 0 and "nothing to end" in again.stdout
    refreshed = run_amem("dispatch", "refresh", env=env)
    assert refreshed.returncode == 0 and refreshed.stdout.startswith("Refreshed: 1 measured runs")
    assert (home / "parent" / D.LOOKUP).exists()
    assert not (tmp_path / "fake-home" / ".claude").exists()
    end = [r for r in spool(home) if r["type"] == "end"][0]
    assert end["status"] == "partial" and end["integration_test_ms"] == 93000


def test_cli_start_exits_zero_when_the_spool_cannot_be_written(home: Path, tmp_path: Path) -> None:
    (home / "parent").mkdir(parents=True)
    (home / "parent" / "demo").write_text("a file where the repo dir should be")
    result = run_amem("dispatch", "start", "--repo", "demo", "--workers", "1", "--session", "s",
                      env=cli_env(home, tmp_path))
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["run_id=none",
                                          "Expect: not recorded (FileExistsError: dispatch estimates unavailable)"]
