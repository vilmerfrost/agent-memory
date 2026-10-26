"""The skill refresh (job rsi-skills) and the evals harvest (job rsi-harvest) write coverage snapshots and
consumption records (data-use plan R4, coverage_daily). A fresh database per test; nothing hosted, no model."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import coverage as CV, coverage_daily as COV, evals_harvest, skill_refresh as SR, skills
from conftest import run_amem
from test_evals_harvest import add_deviation, add_handoff, add_session, drop_handoffs, fixture_data
from test_scoring_support import ScoringDb, sdb  # noqa: F401

RUN = "sr-20261026T001000Z-abc123"
NOW = datetime(2026, 10, 26, 0, 10, tzinfo=UTC)
LINE = re.compile(r"skills-refresh run=(sr-\d{8}T\d{6}Z-[0-9a-f]{6}) window_start=(\S+) window_end=(\S+) "
                  r"status=(ok|failed) scored=(\d+)")


def rows(url: str, table: str, job: str | None = None) -> list[dict[str, Any]]:
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        if job is None:
            cur.execute(f"select * from {table} order by dataset")
        else:
            cur.execute(f"select * from {table} where job = %s order by dataset", (job,))
        return cur.fetchall()


def counts(url: str) -> tuple[int, int]:
    return len(rows(url, "coverage_snapshots")), len(rows(url, "routine_consumption"))


# =========================================================================== skill refresh


def session(url: str, sid: str, outcome: str | None, env: str = "prod") -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, outcome, env, meta) "
                     "values (%s, 'chat', '-x', %s, %s, '{}')", (sid, outcome, env))


def skill_log(url: str, *, unparsed: int = 0) -> None:
    """Five calls: two in a shipped session, one in an active one (not final), one in a sandbox session, one with no
    session at all; then the ingest's read record (with `unparsed` lines that never reached skill_calls)."""
    session(url, "s-ship", "shipped")
    session(url, "s-active", "active")
    session(url, "s-sand", "shipped", env="sandbox")
    lines = [json.dumps({"t": f"2026-10-2{i}T10:00:00+00:00", "skill": sk, "by": "claude", "agent": "claude",
                         "cwd": "/nowhere", **({"session_id": sid} if sid else {})})
             for i, (sk, sid) in enumerate((("a", "s-ship"), ("a", "s-ship"), ("b", "s-active"), ("b", "s-sand"),
                                            ("c", None)))]
    parsed = skills.parse_lines(lines)
    rep = skills.ingest(parsed.calls, db_url=url)
    SR.record_ingest(path=Path("/tmp/log.jsonl"), read_at=datetime(2026, 10, 25, 23, 0, tzinfo=UTC), new=rep.new,
                     present=rep.present, unparsed=unparsed, db_url=url)


def test_refresh_records_match_its_own_counts(sdb: ScoringDb) -> None:  # noqa: F811
    skill_log(sdb.url)
    res = SR.run_refresh(db_url=sdb.url, run_id=RUN, now=NOW)
    assert (res.status, res.calls, res.coverage_notes) == ("ok", 4, [])
    (s,) = rows(sdb.url, "coverage_snapshots")
    assert (s["dataset"], s["status"], s["eligible"], s["collected"], s["linked"], s["unlinked"]) == \
        (COV.SKILL_CALLS, "collected_unlinked", res.calls, res.calls, 3, 1)
    assert (s["window_start"], s["window_end"]) == (res.window_start, res.window_end)
    (u,) = rows(sdb.url, "routine_consumption", COV.SKILLS_JOB)
    assert (u["run_key"], u["status"], u["queried"], u["considered"], u["used"], u["findings"]) == \
        (RUN, "used_no_finding", 5, res.calls, 2, 0)
    assert u["skipped"] == {"env_sandbox": 1, "no_session": 1, "outcome_not_final": 1}
    assert u["detail"] == f"{res.scored} skill_scores row(s) written; scores, not findings"
    assert (u["window_start"], u["window_end"]) == (res.window_start, res.window_end)
    first = counts(sdb.url)
    SR.run_refresh(db_url=sdb.url, run_id=RUN, now=NOW)                 # the same run key: no new rows
    assert counts(sdb.url) == first == (1, 1)


def test_unparsed_log_lines_are_not_collected(sdb: ScoringDb) -> None:  # noqa: F811
    skill_log(sdb.url, unparsed=2)
    SR.run_refresh(db_url=sdb.url, run_id=RUN, now=NOW)
    (s,) = rows(sdb.url, "coverage_snapshots")
    assert (s["status"], s["eligible"], s["collected"]) == ("not_collected", 6, 4)
    assert s["detail"].startswith("2 skill-log line(s) unparsed at the last read: never collected")


def test_refresh_with_nothing_to_score_is_no_eligible_data(sdb: ScoringDb) -> None:  # noqa: F811
    res = SR.run_refresh(db_url=sdb.url, run_id=RUN, now=NOW)
    assert (res.status, res.calls, res.basis) == ("ok", 0, "none")
    (s,) = rows(sdb.url, "coverage_snapshots")
    (u,) = rows(sdb.url, "routine_consumption")
    assert (s["status"], s["window_start"], s["window_end"]) == ("no_eligible_data", NOW, NOW)
    assert (u["status"], u["queried"]) == ("analysis_not_run", 0)
    assert "no complete skill-log read is recorded" in s["detail"]


def test_a_failed_refresh_is_analysis_failed(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    def boom(_conn: Any) -> tuple[int, int]:
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(skills, "rewrite_scores", boom)
    res = SR.run_refresh(db_url=sdb.url, run_id=RUN, now=NOW)
    assert res.status == "failed"
    assert rows(sdb.url, "coverage_snapshots") == []
    (u,) = rows(sdb.url, "routine_consumption")
    assert (u["status"], u["run_key"], u["window_start"]) == ("analysis_failed", RUN, None)
    assert u["detail"].startswith("QueryCanceled: canceling statement")


def test_refresh_switch_off_writes_nothing(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    skill_log(sdb.url)
    monkeypatch.setenv(CV.WRITE_SWITCH, "off")
    assert SR.run_refresh(db_url=sdb.url, run_id=RUN, now=NOW).status == "ok"
    assert counts(sdb.url) == (0, 0)


def test_refresh_cli_keeps_its_last_line_and_exit_code_when_coverage_fails(sdb: ScoringDb) -> None:  # noqa: F811
    skill_log(sdb.url)
    with psycopg.connect(sdb.url) as conn:
        conn.execute("drop table routine_consumption")                 # every consumption write fails
    out = run_amem("skills", "scores", "--refresh", env=sdb.env)
    assert out.returncode == 0, out.stderr
    last = out.stdout.rstrip("\n").splitlines()[-1]
    m = LINE.fullmatch(last)
    assert m and m.group(4) == "ok", last
    assert "amem: skills scores: coverage skills:skill_calls: UndefinedTable" in out.stderr
    assert len(rows(sdb.url, "coverage_snapshots")) == 1                # the other record still landed


def test_refresh_cli_writes_records_and_prints_the_line_last(sdb: ScoringDb) -> None:  # noqa: F811
    skill_log(sdb.url)
    out = run_amem("skills", "scores", "--refresh", env=sdb.env)
    assert out.returncode == 0, out.stderr
    m = LINE.fullmatch(out.stdout.rstrip("\n").splitlines()[-1])
    assert m and "coverage" not in out.stderr
    (u,) = rows(sdb.url, "routine_consumption")
    assert (u["job"], u["run_key"]) == (COV.SKILLS_JOB, m.group(1))


# =========================================================================== evals harvest


@pytest.fixture
def db(sdb: ScoringDb) -> str:  # noqa: F811
    return sdb.url


def harvest(db: str, out: Path, now: datetime, **kw: Any) -> tuple[evals_harvest.Report, list[str]]:
    rep = evals_harvest.harvest(out, days=7, db_url=db, now=now, **kw)
    notes = COV.write(COV.harvest_records(rep, now=now, days=7, limit=kw.get("limit", 20)), db_url=db)
    return rep, notes


def test_harvest_records_match_its_line(db: str, tmp_path: Path) -> None:
    ids = fixture_data(db)
    add_handoff(db, "c1")
    add_handoff(db, "c2", path="/home/x/.claude/handoffs/h2.md", reason="auto")
    add_handoff(db, "", path="", reason="manual")
    now = datetime.now(UTC)
    rep, notes = harvest(db, tmp_path, now)
    assert notes == []
    assert rep.line() == "drafted 4 new, 0 already drafted, 2 skipped (incomplete-handoff=1, team-repo=1)"
    u = {r["dataset"]: r for r in rows(db, "routine_consumption", COV.HARVEST_JOB)}
    dev, ho = u[COV.DEVIATIONS], u[COV.HANDOFFS]
    assert (dev["status"], dev["queried"], dev["considered"], dev["used"], dev["findings"]) == ("findings", 3, 3, 2, 2)
    assert dev["skipped"] == {"team-repo": 1}
    assert sorted(dev["evidence_refs"]) == sorted(f"amem:deviation/{ids[k]}" for k in ("edit", "incident"))
    assert (ho["status"], ho["queried"], ho["used"], ho["findings"], ho["skipped"]) == \
        ("findings", 3, 2, 2, {"incomplete-handoff": 1})
    assert sorted(ho["evidence_refs"]) == ["amem:session/c1", "amem:session/c2"]
    assert dev["run_key"] == COV.harvest_run_key(now) and dev["window_end"] == now
    assert dev["used"] + ho["used"] == len(rep.drafted)
    (s,) = rows(db, "coverage_snapshots")
    assert (s["dataset"], s["status"], s["eligible"], s["linked"], s["unlinked"]) == \
        (COV.HANDOFFS, "collected_unlinked", 3, 2, 1)

    first = counts(db)
    rep2, _ = harvest(db, tmp_path, now)                               # the same run key: updated, no new rows
    assert counts(db) == first
    assert rep2.line() == "drafted 0 new, 4 already drafted, 2 skipped (incomplete-handoff=1, team-repo=1)"
    u = {r["dataset"]: r for r in rows(db, "routine_consumption", COV.HARVEST_JOB)}
    assert (u[COV.DEVIATIONS]["status"], u[COV.DEVIATIONS]["skipped"]) == \
        ("read_excluded", {"already_drafted": 2, "team-repo": 1})        # read, nothing new: never used_no_finding
    assert u[COV.DEVIATIONS]["evidence_refs"] == []


def test_harvest_limit_is_a_skip_reason(db: str, tmp_path: Path) -> None:
    add_session(db, "p1")
    for _ in range(3):
        add_deviation(db, "p1")
    rep, _ = harvest(db, tmp_path, datetime.now(UTC), limit=1)
    (u,) = [r for r in rows(db, "routine_consumption") if r["dataset"] == COV.DEVIATIONS]
    assert (u["used"], u["skipped"], len(u["evidence_refs"])) == (1, {"limit": 2}, 1)
    assert rep.skipped == {"limit": 2}


def test_harvest_empty_window_is_no_eligible_data(db: str, tmp_path: Path) -> None:
    harvest(db, tmp_path, datetime.now(UTC))
    (s,) = rows(db, "coverage_snapshots")
    assert (s["dataset"], s["status"]) == (COV.HANDOFFS, "no_eligible_data")
    assert {r["status"] for r in rows(db, "routine_consumption")} == {"analysis_not_run"}


def test_a_missing_handoffs_table_is_not_collected(db: str, tmp_path: Path) -> None:
    fixture_data(db)
    drop_handoffs(db)
    now = datetime.now(UTC)
    harvest(db, tmp_path, now)
    assert rows(db, "coverage_snapshots") == []                         # nothing measured: no snapshot claimed
    u = {r["dataset"]: r for r in rows(db, "routine_consumption")}
    assert (u[COV.HANDOFFS]["status"], u[COV.HANDOFFS]["detail"]) == \
        ("analysis_not_run", "no public.handoffs table: handoffs are not collected")
    with psycopg.connect(db) as conn:
        v = CV.verdict(conn, job=COV.HARVEST_JOB, run_key=COV.harvest_run_key(now), required=[COV.HANDOFFS])
    assert not v.all_clear and v.gaps[0].distinction == "not_collected"


def _cov_env(sdb: ScoringDb, **extra: str) -> dict[str, str]:  # noqa: F811
    return {**sdb.env, **extra}


def test_harvest_cli_failure_is_analysis_failed(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    fixture_data(sdb.url)
    out = tmp_path / "locked"
    out.mkdir()
    os.chmod(out, 0o500)                                                # drafts cannot be written
    try:
        res = run_amem("evals", "harvest", "--out", str(out), env=sdb.env)
    finally:
        os.chmod(out, 0o700)
    assert res.returncode == 1
    u = rows(sdb.url, "routine_consumption", COV.HARVEST_JOB)
    assert {r["dataset"]: r["status"] for r in u} == {COV.DEVIATIONS: "analysis_failed", COV.HANDOFFS: "analysis_failed"}
    assert all(r["detail"].startswith("evals harvest: PermissionError") and r["run_id"] for r in u)


def test_harvest_cli_switch_off_and_write_failure(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    fixture_data(sdb.url)
    off = run_amem("evals", "harvest", "--out", str(tmp_path / "a"), env=_cov_env(sdb, AMEM_COVERAGE_WRITE="0"))
    assert off.returncode == 0 and counts(sdb.url) == (0, 0)
    with psycopg.connect(sdb.url) as conn:
        conn.execute("drop table coverage_snapshots")
    res = run_amem("evals", "harvest", "--out", str(tmp_path / "b"), env=sdb.env)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().splitlines()[-1] == "drafted 2 new, 0 already drafted, 1 skipped (team-repo=1)"
    assert "amem: evals harvest: coverage coordination:handoffs: UndefinedTable" in res.stderr
    assert len(rows(sdb.url, "routine_consumption", COV.HARVEST_JOB)) == 2    # the consumption records landed
    with psycopg.connect(sdb.url) as conn:                                  # the run row still logs the line
        (summary,) = conn.execute("select summary from runs where routine = 'evals-harvest' order by created_at desc "
                                  "limit 1").fetchone()
    assert summary == "drafted 2 new, 0 already drafted, 1 skipped (team-repo=1)"


def test_record_builders_validate_against_the_contract() -> None:
    rep = evals_harvest.Report()
    for r in COV.harvest_records(rep, now=NOW, days=7, limit=20) + COV.harvest_records(
            rep, now=NOW, days=7, limit=20, error="boom"):
        r.validate()
    assert COV.harvest_run_key(NOW) == "harvest-20261026T001000Z"
    assert COV.harvest_records(rep, now=NOW, days=7, limit=20)[0].window_start == NOW - timedelta(days=7)
