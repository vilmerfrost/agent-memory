"""The skill refresh as a recorded run and the night's skill_refresh prerequisite (skill_refresh.py, data-use R2).

Recording (`amem ingest skills`, `amem skills scores --refresh`): the run row, its input window and the final
`skills-refresh run=... window_end=...` line. The prerequisite: fresh / stale / failed / missing, report vs enforce,
only skill candidates skipped, the routines.log fallback, the night report field, the routines.log summary field
and the brief's warning. Throwaway Postgres, the night's fakes; nothing hosted, no model.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import rsi_approval as AP, rsi_brief as BR, rsi_night as N, rsi_schedule as S, skill_refresh as SR
from agent_memory import skills
from conftest import run_amem
from rsi_night_support import (KEY_A, RULE_A, FakeEvidence, adapters, clean_db, make_home, record_refresh, rows,
                               seed_rule, strip_agent_env, write_config)
from test_rsi_schedule import GOOD_LOG, NIGHT, NOW, write_log

LINE = re.compile(r"skills-refresh run=(sr-\d{8}T\d{6}Z-[0-9a-f]{6}) window_start=(\S+) window_end=(\S+) "
                  r"status=(ok|failed) scored=(\d+)")
T0 = datetime(2026, 10, 26, 1, 30, tzinfo=UTC)        # a night's window end


def _run(name: str, status: str = "ok", end_ago_h: float | None = 0.5, started_ago_h: float = 0.5) -> SR.RefreshRun:
    end = None if end_ago_h is None else T0 - timedelta(hours=end_ago_h)
    return SR.RefreshRun(name, status, T0 - timedelta(hours=started_ago_h), T0 - timedelta(days=30), end)


# --------------------------------------------------------------------------- the decision (no database)


def test_fresh_stale_failed_missing_and_the_mode() -> None:
    fresh = SR.decide([_run("a")], night_window_end=T0)
    assert (fresh.status, fresh.decision, fresh.reason, fresh.run.run) == ("fresh", "use", None, "a")
    # the 03:30 refresh of the previous morning: 22 h before the night's window end
    old = [_run("b", end_ago_h=22, started_ago_h=22)]
    stale = SR.decide(old, night_window_end=T0)
    assert (stale.status, stale.decision, stale.mode) == ("stale", "warn", "report")
    assert "covers skill calls to 2026-10-25T03:30:00+00:00, 22.0 h before" in stale.reason
    enforced = SR.decide(old, night_window_end=T0, mode="enforce")
    assert (enforced.status, enforced.decision, enforced.skip) == ("stale", "skip_skill_candidates", True)
    # the tolerance edge: exactly max_lag_h before the window end is fresh, one second more is stale
    edge = SR.RefreshRun("e", "ok", T0, None, T0 - timedelta(hours=2))
    assert SR.decide([edge], night_window_end=T0).status == "fresh"
    assert SR.decide([edge], night_window_end=T0 + timedelta(seconds=1)).status == "stale"
    # a failed refresh after a stale success: failed, in report mode too
    failed = SR.decide([_run("f", "failed", None, 1), *old], night_window_end=T0)
    assert (failed.status, failed.decision, failed.run.run) == ("failed", "skip_skill_candidates", "f")
    assert "the latest skill refresh f failed" in failed.reason and "the last successful refresh b covers" in failed.reason
    running = SR.decide([_run("r", "running", None)], night_window_end=T0)
    assert running.status == "failed" and "did not finish" in running.reason and "no successful refresh" in running.reason
    # a later failure does not hide a refresh that is still fresh
    assert SR.decide([_run("f", "failed", None, 0.1), _run("a")], night_window_end=T0).status == "fresh"
    missing = SR.decide([], night_window_end=T0)
    assert (missing.status, missing.decision, missing.run) == ("missing", "skip_skill_candidates", None)
    unknown = SR.decide([_run("u", end_ago_h=None)], night_window_end=T0)
    assert unknown.status == "stale" and "recorded no input window" in unknown.reason


def test_report_field_and_warning() -> None:
    c = SR.decide([_run("b", end_ago_h=22, started_ago_h=22)], night_window_end=T0, mode="enforce")
    c.skipped.update({"skill:pepe", "skill:anatomy"})
    rep = c.as_report()
    assert rep == {"run": "b", "window_start": "2026-09-26T01:30:00+00:00", "window_end": "2026-10-25T03:30:00+00:00",
                   "status": "stale", "run_status": "ok", "mode": "enforce", "decision": "skip_skill_candidates",
                   "night_window_end": "2026-10-26T01:30:00+00:00", "required_end": "2026-10-25T23:30:00+00:00",
                   "max_lag_h": 2.0, "source": "db", "reason": c.reason, "skipped_candidates": 2,
                   "skipped_keys": ["skill:anatomy", "skill:pepe"]}
    assert SR.report_problems(rep) == []
    w = SR.warning(rep)
    assert w.startswith("prerequisite skill_refresh is stale (run b, window to 2026-10-25T03:30:00+00:00): skill "
                        "candidates are skipped tonight; other candidates continue (2 skipped). Why: skill refresh b "
                        "covers skill calls to 2026-10-25T03:30:00+00:00, 22.0 h before") and len(w) <= 300
    assert SR.warning({**rep, "status": "fresh", "decision": "use"}) is None and SR.warning(None) is None
    assert SR.report_problems({**rep, "decision": "use"}) == [
        "prerequisites.skill_refresh: decision use goes with status fresh only"]
    assert SR.report_problems({**rep, "mode": "loud"})[0].startswith("prerequisites.skill_refresh.mode 'loud'")


def test_settings_from_the_night_config_and_the_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SR.MODE_ENV, raising=False)
    assert N.load_config(write_config(tmp_path)).skill_refresh == ("report", 2.0)       # the default
    p = write_config(tmp_path, "e.json")
    d = json.loads(p.read_text())
    d["skill_refresh"] = {"mode": "enforce", "max_lag_h": 3}
    p.write_text(json.dumps(d))
    assert N.load_config(p).skill_refresh == ("enforce", 3.0)
    monkeypatch.setenv(SR.MODE_ENV, "report")
    assert N.load_config(p).skill_refresh == ("report", 3.0)                             # the env wins
    for bad in ({"mode": "strict"}, {"max_lag_h": 0}, {"max_lag_h": True}, {"max_lag_h": 72}):
        monkeypatch.delenv(SR.MODE_ENV, raising=False)
        d["skill_refresh"] = bad
        p.write_text(json.dumps(d))
        with pytest.raises(N.ConfigError, match="skill_refresh"):
            N.load_config(p)


class BrokenConn:
    def execute(self, *_a: Any, **_k: Any) -> Any:
        raise psycopg.OperationalError("server closed the connection")


def test_routines_log_is_the_fallback_when_the_database_read_fails(tmp_path: Path) -> None:
    log = write_log(tmp_path, GOOD_LOG
                    + "2026-10-25T03:30:12+0200 rsi-skills PASS run=sr-20261025T013000Z-aaaaaa "
                      "window_end=2026-10-25T01:29:58+00:00 3s\n"
                    + "2026-10-26T01:10:00+0100 rsi-skills PASS run=sr-20261026T001000Z-bbbbbb "
                      "window_start=2026-04-06T19:34:40+00:00 window_end=2026-10-26T00:09:58+00:00 4s\n")
    c = SR.check(BrokenConn(), night_window_end=T0, log=log)
    assert (c.status, c.source, c.run.run) == ("fresh", "routines.log", "sr-20261026T001000Z-bbbbbb")
    assert c.reason.startswith("database read failed (OperationalError); read routines.log")
    assert c.as_report()["window_start"] == "2026-04-06T19:34:40+00:00"
    # no connection at all; the newest line FAILed after a stale PASS
    log.write_text(log.read_text() + "2026-10-26T01:20:00+0100 rsi-skills FAIL run=sr-20261026T002000Z-cccccc 1s\n")
    c = SR.check(None, night_window_end=T0 + timedelta(hours=5), log=log)
    assert (c.status, c.decision, c.run.run) == ("failed", "skip_skill_candidates", "sr-20261026T002000Z-cccccc")
    # the plan's window=<from>..<to> spelling, and an old line without a window (unknown: stale, not fresh)
    log.write_text("2026-10-26T01:10:00+0100 rsi-skills PASS run=x window=2026-10-01T00:00:00Z..2026-10-26T00:00:00Z\n")
    assert SR.check(None, night_window_end=T0, log=log).status == "fresh"
    log.write_text("2026-10-26T03:30:00+0100 rsi-skills PASS rc=0 2s\n")
    c = SR.check(None, night_window_end=T0, log=log)
    assert c.status == "stale" and c.run.run is None and "recorded no input window" in c.reason
    missing = SR.check(None, night_window_end=T0, log=tmp_path / "nope.log")
    assert (missing.status, missing.source) == ("missing", "none") and "routines.log unreadable" in missing.reason


# --------------------------------------------------------------------------- recording (Postgres)


def _log(path: Path, lines: list[dict]) -> Path:
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def _checkpoint(url: str, job: str, key: str | None = None) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        if key is None:
            return conn.execute("select run_key, status, meta from rsi_checkpoints where job = %s order by started_at",
                                (job,)).fetchall()
        return conn.execute("select run_key, status, meta from rsi_checkpoints where job = %s and run_key = %s",
                            (job, key)).fetchall()


def _scored_window(url: str) -> tuple[datetime, int]:
    with psycopg.connect(url) as conn:
        first, n = conn.execute("select min(c.ts), count(*) from skill_calls c left join sessions s "
                                f"on s.session_id = c.session_id where {skills.CALL_FILTER}").fetchone()
        (rows_,) = conn.execute("select count(*) from skill_scores").fetchone()
    return first, rows_


@pytest.fixture
def rec_env(db_env: dict[str, str], pg_url: str) -> SimpleNamespace:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from rsi_checkpoints where job in ('skills-refresh', 'skills-ingest')")
    return SimpleNamespace(env=db_env, url=pg_url)


def test_refresh_records_its_run_and_input_window_and_prints_the_line(rec_env: SimpleNamespace, tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    calls = [{"t": (now - timedelta(hours=h)).isoformat(), "skill": f"sr-{h}", "by": "me", "agent": "claude",
              "cwd": "/x"} for h in (30, 5, 1)]
    before = datetime.now(UTC)
    ing = run_amem("ingest", "skills", str(_log(tmp_path / "log.jsonl", calls)), env=rec_env.env)
    assert ing.returncode == 0, ing.stderr
    assert ing.stdout.strip().startswith("ingested ")                         # stdout unchanged: one line
    ((key, status, meta),) = _checkpoint(rec_env.url, SR.INGEST_JOB)
    read_at = datetime.fromisoformat(meta["read_at"])
    assert key == "latest" and status == "done" and before - timedelta(seconds=1) <= read_at <= datetime.now(UTC)
    assert meta["path"] == str((tmp_path / "log.jsonl").resolve())

    out = run_amem("skills", "scores", "--refresh", env=rec_env.env)
    assert out.returncode == 0, out.stderr
    last = out.stdout.rstrip("\n").splitlines()[-1]
    m = LINE.fullmatch(last)
    assert m, last
    run, start, end, st, scored = m.groups()
    first, n_scores = _scored_window(rec_env.url)
    assert (st, int(scored)) == ("ok", n_scores) and n_scores > 0
    assert start == first.astimezone(UTC).isoformat(timespec="seconds")
    assert end == read_at.astimezone(UTC).isoformat(timespec="seconds")        # the log read, not the refresh time
    assert "input window" in out.stderr and "(end from: ingest)" in out.stderr
    ((rk, status, meta),) = _checkpoint(rec_env.url, SR.JOB)
    assert (rk, status) == (run, "done")
    assert (meta["window_start"], meta["window_end"], meta["scored"], meta["basis"]) == (start, end, n_scores, "ingest")
    assert meta["calls"] >= 3 and "started_at" in meta and "snapshot_at" in meta
    # the night reads it back as fresh
    with psycopg.connect(rec_env.url) as conn:
        c = SR.check(conn, night_window_end=datetime.now(UTC))
    assert (c.status, c.run.run, c.as_report()["window_end"]) == ("fresh", run, end)


def test_refresh_without_a_recorded_read_dates_the_window_by_the_newest_row(rec_env: SimpleNamespace,
                                                                           tmp_path: Path) -> None:
    calls = [{"t": "2026-10-20T10:00:00+00:00", "skill": "sr-old-read", "by": "me", "agent": "claude", "cwd": "/y"}]
    assert run_amem("ingest", "skills", str(_log(tmp_path / "l.jsonl", calls)), env=rec_env.env).returncode == 0
    with psycopg.connect(rec_env.url) as conn:
        conn.execute("delete from rsi_checkpoints where job = 'skills-ingest'")   # ingested by an older amem
        (newest,) = conn.execute("select max(created_at) from skill_calls").fetchone()
    res = SR.run_refresh(db_url=rec_env.url)
    assert (res.status, res.basis, res.recorded) == ("ok", "latest_insert", True)
    assert res.window_end == newest


def test_a_failed_refresh_is_recorded_and_its_line_says_failed(rec_env: SimpleNamespace,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_conn: Any) -> tuple[int, int]:
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(skills, "rewrite_scores", boom)
    res = SR.run_refresh(db_url=rec_env.url, run_id="sr-20261026T010000Z-abcdef")
    assert (res.status, res.recorded, res.scored, res.window_end) == ("failed", True, 0, None)
    assert res.line() == ("skills-refresh run=sr-20261026T010000Z-abcdef window_start=- window_end=- status=failed "
                          "scored=0")
    ((rk, status, meta),) = _checkpoint(rec_env.url, SR.JOB)
    assert (rk, status) == ("sr-20261026T010000Z-abcdef", "failed") and "QueryCanceled" in meta["error"]
    with psycopg.connect(rec_env.url) as conn:
        c = SR.check(conn, night_window_end=datetime.now(UTC))
    assert (c.status, c.decision) == ("failed", "skip_skill_candidates")


def test_refresh_with_the_database_down_still_prints_its_line(rec_env: SimpleNamespace) -> None:
    env = {**rec_env.env, "AMEM_DB_URL": "postgresql://postgres:postgres@127.0.0.1:1/none?connect_timeout=2"}
    out = run_amem("skills", "scores", "--refresh", env=env)
    assert out.returncode == 1
    m = LINE.fullmatch(out.stdout.strip().splitlines()[-1])
    assert m and m.group(4) == "failed" and m.group(2) == m.group(3) == "-"
    assert "database unavailable" in out.stderr and "could not be recorded" in out.stderr


# --------------------------------------------------------------------------- the night (Postgres, fakes)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    monkeypatch.delenv(SR.MODE_ENV, raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    seed_rule(pg_url, RULE_A, "a")                    # a deviation candidate: never depends on skill scores
    with psycopg.connect(pg_url) as conn:             # a low-scoring skill: a skill candidate (skill:demo)
        conn.execute("insert into skill_scores (skill, bucket, n_calls, n_sessions, n_scored, score) "
                     "values ('demo', '*', 9, 5, 5, 0.3)")
    return SimpleNamespace(url=pg_url, home=home, tmp=tmp_path, sandbox=sandbox,
                           log=write_log(tmp_path, GOOD_LOG), reports=tmp_path / "reports")


def dry_night(env: SimpleNamespace, mode: str = "report") -> dict[str, Any]:
    p = write_config(env.tmp, f"night-{mode}.json")
    d = json.loads(p.read_text())
    d["skill_refresh"] = {"mode": mode}
    p.write_text(json.dumps(d))
    rep = N.run_night(night=NIGHT, config_path=p, dry_run=True, db_url=env.url, adapters=adapters(),
                      install_signals=False, routines_log=env.log)
    assert N.validate_report(rep) == [] and BR.validate_report(rep) == [], N.validate_report(rep)
    return rep


def keys(rep: dict[str, Any]) -> set[str]:
    return {c["key"] for c in rep["candidates"]}


def test_fresh_refresh_keeps_skill_candidates_and_names_the_run(env: SimpleNamespace) -> None:
    run = record_refresh(env.url, "sr-20261026T001000Z-f0f0f0")
    rep = dry_night(env)
    sr = rep["prerequisites"]["skill_refresh"]
    assert (sr["status"], sr["decision"], sr["run"], sr["run_status"], sr["source"]) == ("fresh", "use", run, "ok", "db")
    assert sr["window_end"] and sr["night_window_end"] and sr["skipped_candidates"] == 0
    assert {"skill:demo", KEY_A} <= keys(rep)
    assert BR.skill_refresh_warning(rep) == []


@pytest.mark.parametrize("mode", ["report", "enforce"])
def test_stale_refresh_warns_in_report_mode_and_skips_only_skill_candidates_in_enforce(env: SimpleNamespace,
                                                                                       mode: str) -> None:
    record_refresh(env.url, "sr-20261025T013000Z-0ld0ld", window_end_ago_h=22, started_ago_h=22)
    rep = dry_night(env, mode)
    sr = rep["prerequisites"]["skill_refresh"]
    assert (sr["status"], sr["mode"], sr["run"]) == ("stale", mode, "sr-20261025T013000Z-0ld0ld")
    assert "22.0 h before the night's window end" in sr["reason"]
    if mode == "report":
        assert sr["decision"] == "warn" and {"skill:demo", KEY_A} <= keys(rep) and sr["skipped_candidates"] == 0
    else:
        assert sr["decision"] == "skip_skill_candidates" and sr["skipped_keys"] == ["skill:demo"]
        assert "skill:demo" not in keys(rep) and KEY_A in keys(rep)            # the deviation candidate continues
    (w,) = BR.skill_refresh_warning(rep)
    assert w.startswith("prerequisite skill_refresh is stale (run sr-20261025T013000Z-0ld0ld")


@pytest.mark.parametrize("case", ["failed", "missing"])
def test_failed_or_missing_refresh_skips_skill_candidates_even_in_report_mode(env: SimpleNamespace, case: str) -> None:
    if case == "failed":
        record_refresh(env.url, "sr-20261025T013000Z-0ld0ld", window_end_ago_h=22, started_ago_h=22)
        record_refresh(env.url, "sr-20261026T001000Z-bad000", status="failed", started_ago_h=0.3)
    rep = dry_night(env)
    sr = rep["prerequisites"]["skill_refresh"]
    assert (sr["status"], sr["mode"], sr["decision"]) == (case, "report", "skip_skill_candidates")
    assert sr["run"] == ("sr-20261026T001000Z-bad000" if case == "failed" else None)
    assert sr["run_status"] == ("failed" if case == "failed" else None)
    assert "skill:demo" not in keys(rep) and KEY_A in keys(rep) and sr["skipped_keys"] == ["skill:demo"]
    (w,) = BR.skill_refresh_warning(rep)
    word = "failed (run sr-20261026T001000Z-bad000, window to unknown)" if case == "failed" else "is missing"
    assert w.startswith(f"prerequisite skill_refresh {word}: skill candidates are skipped tonight; other candidates "
                        "continue (1 skipped). Why: "), w


def test_an_earlier_nights_skill_candidate_waits_too(env: SimpleNamespace) -> None:
    (env.home / ".agents" / "skills" / "old").mkdir(parents=True)
    (env.home / ".agents" / "skills" / "old" / "SKILL.md").write_text("---\nname: old\n---\n\n# Old\n\n- Step.\n")
    with psycopg.connect(env.url, autocommit=True) as conn:
        AP.upsert_candidate(conn, dedupe_key="skill:old", source="skill_scores", kind="edit_skill",
                            target="~/.agents/skills/old/SKILL.md", rule_key="skill:old",
                            evidence_refs=["amem:skill/old"], metrics={"score": 0.2, "n_scored": 4, "sessions": 4})
    record_refresh(env.url, "sr-fresh")
    assert {"skill:demo", "skill:old"} <= keys(dry_night(env, "enforce"))      # fresh: both are considered
    with psycopg.connect(env.url) as conn:
        conn.execute("delete from rsi_checkpoints where job = 'skills-refresh'")
    record_refresh(env.url, "sr-x", window_end_ago_h=22, started_ago_h=22)
    rep = dry_night(env, "enforce")
    sr = rep["prerequisites"]["skill_refresh"]
    assert sr["skipped_keys"] == ["skill:demo", "skill:old"] and not keys(rep) & {"skill:demo", "skill:old"}
    assert KEY_A in keys(rep)


def test_the_night_job_logs_the_refresh_and_the_brief_warns(env: SimpleNamespace) -> None:
    record_refresh(env.url, "sr-20261025T013000Z-0ld0ld", window_end_ago_h=22, started_ago_h=22)
    res = S.run_night_job(night=NIGHT, now=NOW, routines_log=env.log, db_url=env.url,
                          adapters=adapters(evidence=FakeEvidence()))
    assert res.status == "PASS" and res.warnings == [], res.items
    assert (res.fields["skill_refresh"], res.fields["skill_refresh_run"]) == ("stale", "sr-20261025T013000Z-0ld0ld")
    assert " skill_refresh=stale skill_refresh_run=sr-20261025T013000Z-0ld0ld " in res.lines[0]
    rep = json.loads(N.report_path(NIGHT).read_text())
    assert N.validate_report(rep) == [] and rep["prerequisites"]["skill_refresh"]["decision"] == "warn"
    assert "prerequisite_warnings" not in rep                     # the gate's list stays the gate's
    b = S.run_brief_job(night=NIGHT, now=NOW + timedelta(hours=6), routines_log=env.log, out_dir=env.reports,
                        db_url=env.url)
    md = b.detail["markdown"]
    assert b.status == "PASS"
    assert "- Warning: prerequisite skill_refresh is stale (run sr-20261025T013000Z-0ld0ld" in md
    assert "skill candidates are still considered (report mode)" in md


def test_the_night_falls_back_to_routines_log_when_the_database_read_fails(env: SimpleNamespace,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    def down(_conn: Any) -> list[SR.RefreshRun]:
        raise psycopg.OperationalError("the rsi_checkpoints read failed")

    monkeypatch.setattr(SR, "db_runs", down)
    end = rows(env.url, "select now() - interval '10 minutes'")[0][0].astimezone(UTC).isoformat(timespec="seconds")
    env.log.write_text(GOOD_LOG + f"2026-10-19T01:05:00+0200 rsi-skills PASS run=sr-log window_end={end} 2s\n")
    rep = dry_night(env)
    sr = rep["prerequisites"]["skill_refresh"]
    assert (sr["status"], sr["source"], sr["run"], sr["window_end"]) == ("fresh", "routines.log", "sr-log", end)
    assert sr["reason"].startswith("database read failed (OperationalError); read routines.log")
    assert "skill:demo" in keys(rep)


def test_jsonb_meta_shape_matches_what_the_reader_expects(env: SimpleNamespace) -> None:
    """A row written by run_refresh and one written by the test helper read back the same way."""
    with psycopg.connect(env.url) as conn:
        conn.execute("insert into rsi_checkpoints (job, run_key, status, finished_at, meta) values "
                     "('skills-refresh', 'sr-shape', 'done', now(), %s)",
                     (Jsonb({"window_start": "2026-10-01T00:00:00+00:00", "window_end": "2026-10-26T00:00:00Z"}),))
        (r,) = SR.db_runs(conn)
    assert (r.run, r.status, r.window_end) == ("sr-shape", "ok", datetime(2026, 10, 26, tzinfo=UTC))
