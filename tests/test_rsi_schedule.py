"""Mission 005.5: the unattended night and brief jobs (rsi_schedule.py, deploy/launchd/, config/rsi-night.first.json).

Gate on completion checkpoints in routines.log, an overlapping start, missing credentials (presence checks only: a
fake `security` that would fail on -w), the routines.log lines routine-audit reads, the first-night limits, the
paid plan, the plists, and the night -> brief path on the throwaway Postgres with the night's fakes. No model, no
Docker, no hosted database, no real Keychain, nothing installed.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_memory import config, routines_log as RL, rsi_night as N, rsi_schedule as S
from conftest import AMEM, base_env
from rsi_night_support import (KEY_A, RULE_A, FakeEvidence, FakeReplayer, FakeSandboxOps, adapters, clean_db,
                               make_home, rows, seed_rule, strip_agent_env, write_config)

CEST = timezone(timedelta(hours=2))
NIGHT = "2026-10-18"
NOW = datetime(2026, 10, 19, 1, 30, 5, tzinfo=CEST)          # the 01:30 launchd start after night 2026-10-18
FIRST = config.REPO_ROOT / "config" / "rsi-night.first.json"
LAUNCHD = config.REPO_ROOT / "deploy" / "launchd"

GOOD_LOG = """\
2026-10-18T00:08:31+0200 amem-ingest PASS heartbeat 5s
2026-10-18T03:30:18+0200 rsi-harvest PASS rc=0 1s drafted 20 new
2026-10-18T08:45:09+0200 amem-archive PASS heartbeat 5s
2026-10-18T09:20:04+0200 amem-daily PASS scored=16 escalations=14 new=0 push=none tasks=10 deviations=68 707s
2026-10-19T00:09:12+0200 amem-ingest PASS heartbeat 4s
"""
# The real 2026-10-17 amem-daily line: one malformed grade failed the deviations step.
DAILY_FAIL = """\
2026-10-18T09:20:04+0200 amem-daily FAIL scored=16 escalations=14 new=0 push=none tasks=10 deviations=68 deviation_errors=1 707s
2026-10-18T09:20:04+0200 amem-daily-fail deviations: 1 error; first: session b1b2011c/agent-a079: writer output: deviation 0: followed_grade must be a number in 0..1, got None
"""


def write_log(tmp: Path, text: str) -> Path:
    p = tmp / "routines.log"
    p.write_text(text)
    return p


# --------------------------------------------------------------------------- completion checkpoints


def test_gate_passes_only_on_completion_checkpoints(tmp_path: Path) -> None:
    pre = S.DEFAULT_PREREQS
    assert S.check_prerequisites(write_log(tmp_path, GOOD_LOG), NIGHT, NOW, pre) == []
    # amem-daily ran but failed (the real 10-17 line): the night waits for a successful run
    reasons = S.check_prerequisites(write_log(tmp_path, GOOD_LOG.replace(
        GOOD_LOG.splitlines()[3] + "\n", DAILY_FAIL)), NIGHT, NOW, pre)
    assert reasons == ["prerequisite amem-daily: last run FAIL at 2026-10-18T09:20:04+0200"]
    # only yesterday's PASS: completion today is missing (the clock alone would have let it run)
    yday = GOOD_LOG.replace("2026-10-18T09:20:04+0200 amem-daily", "2026-10-17T09:20:04+0200 amem-daily")
    assert S.check_prerequisites(write_log(tmp_path, yday), NIGHT, NOW, pre) == [
        "prerequisite amem-daily: no PASS on 2026-10-18 (last PASS 2026-10-17T09:20:04+0200)"]
    # never ran, ingest failing now, a run logged after `now` does not count
    no_daily = "".join(ln + "\n" for ln in GOOD_LOG.splitlines() if " amem-daily " not in ln)
    ingest_fail = GOOD_LOG + "2026-10-19T00:24:12+0200 amem-ingest FAIL rc=1 0s amem: ingest failed: OperationalError\n"
    future = no_daily + "2026-10-19T09:20:04+0200 amem-daily PASS scored=1 1s\n"
    assert S.check_prerequisites(write_log(tmp_path, no_daily), NIGHT, NOW, pre) == [
        "prerequisite amem-daily: no run in routines.log"]
    assert S.check_prerequisites(write_log(tmp_path, ingest_fail), NIGHT, NOW, pre) == [
        "prerequisite amem-ingest: last run FAIL at 2026-10-19T00:24:12+0200"]
    assert S.check_prerequisites(write_log(tmp_path, future), NIGHT, NOW, pre) == [
        "prerequisite amem-daily: no run in routines.log"]
    stale = GOOD_LOG.replace("2026-10-19T00:09:12+0200 amem-ingest PASS heartbeat 4s\n", "")
    assert S.check_prerequisites(write_log(tmp_path, stale), NIGHT, NOW, pre) == [
        "prerequisite amem-ingest: last PASS 2026-10-18T00:08:31+0200 is older than 25h"]
    assert S.check_prerequisites(tmp_path / "missing.log", NIGHT, NOW, pre)[0].startswith("routines.log: cannot read")


def test_first_config_names_the_prerequisites() -> None:
    sched = S.schedule_section(FIRST)
    assert S.prerequisites(sched) == S.DEFAULT_PREREQS
    assert S.prerequisites({}) == S.DEFAULT_PREREQS
    with pytest.raises(ValueError):
        S.prerequisites({"prerequisites": [{"routine": "../evil"}]})


def test_log_lines_are_what_routine_audit_and_ingest_read() -> None:
    res = S.JobResult(S.NIGHT_ROUTINE, "FAIL", NIGHT, items=["credentials judge (replay.judge_credentials): keychain item "
                                                             "not found",
                                                             "budget max_usd: 0 while paid stages are configured"],
                      fields={"night": NIGHT, "status": "x y", "usd": 0.0234}, report="/Users/x/sandbox/nights/r.json")
    lines = S.log_lines(res, now=NOW, secs=3)
    assert lines[0] == ("2026-10-19T01:30:05+0200 amem-rsi-night FAIL night=2026-10-18 status=x_y usd=0.0234 3s "
                        "report=/Users/x/sandbox/nights/r.json")
    parsed = RL.parse_lines(lines)
    (run,) = parsed.runs
    assert (run.routine, run.status, run.report_path, run.meta["usd"]) == ("amem-rsi-night", "fail",
                                                                           "/Users/x/sandbox/nights/r.json", 0.0234)
    assert run.meta["failures"] == res.items and parsed.unparsed == []
    # routine-audit's item name is the part before ": " (agent-config bin/routine-audit fail_items)
    assert [x.split(": ", 1)[0] for x in run.meta["failures"]] == ["credentials judge (replay.judge_credentials)",
                                                                    "budget max_usd"]


@pytest.mark.parametrize("rep, status, item", [
    ({"status": "ok", "notes": []}, "PASS", None),
    ({"status": "preflight_failed", "notes": ["preflight: lock: locked: rsi-night.lock (another run is active)"]},
     "SKIP", "overlap: another night holds the rsi-night lock"),
    ({"status": "preflight_failed", "night": NIGHT, "notes": ["preflight: night 2026-10-18 is already done"]},
     "SKIP", "already done: night 2026-10-18 finished earlier"),
    ({"status": "preflight_failed", "notes": ["preflight: sandbox unavailable (docker or the worker image)"]},
     "FAIL", "preflight: sandbox unavailable (docker or the worker image)"),
    ({"status": "budget_exhausted", "notes": ["budget exhausted: usd"]}, "FAIL", "budget: usd"),
    ({"status": "aborted", "notes": ["interrupted by SIGTERM"]}, "FAIL", "aborted: interrupted by SIGTERM"),
    ({"status": "partial", "health": {"langfuse": "unavailable", "jev": "live"}, "notes": []}, "FAIL",
     "health langfuse: unavailable"),
    ({"status": "partial", "experiments": [{"experiment_id": "exp-1", "status": "failed",
                                            "verdict_reasons": ["ExperimentFailed: improve stage refused"]}]},
     "FAIL", "experiment exp-1: ExperimentFailed: improve stage refused")])
def test_classify_night_reports(rep: dict[str, Any], status: str, item: str | None) -> None:
    got, items = S.classify(rep)
    assert got == status and (items == [] if item is None else item in items), items


# --------------------------------------------------------------------------- credentials: presence only, no prompt


@pytest.fixture
def fake_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A `security` that logs its argv, exits 44 for unknown items and fails loudly on -w (a secret read)."""
    log, known = tmp_path / "security.log", tmp_path / "known.txt"
    known.write_text("")
    exe = tmp_path / "security"
    exe.write_text(f"""#!/bin/sh
echo "$@" >> '{log}'
case " $* " in *" -w "*) echo "SECRET READ ATTEMPTED" >&2; exit 99;; esac
grep -qx -- "$3/$5" '{known}' && exit 0
exit 44
""")
    exe.chmod(0o755)
    monkeypatch.setenv(S.SECURITY_ENV, str(exe))
    return SimpleNamespace(log=log, known=known)


def first_cfg() -> Any:
    return N.load_config(FIRST)


def test_credentials_missing_are_named_and_nothing_is_read(fake_security: SimpleNamespace,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = first_cfg()
    probs = S.credential_problems(cfg)
    assert probs == ["credentials database: AMEM_DB_URL is not set",
                     "credentials jev: live Jev is configured but AMEM_JEV_LIVE is not 1",
                     "credentials jev: TYPESAFE_API_KEY is not set",
                     "credentials replay agent (replay.credentials): keychain item not found",
                     "credentials judge (replay.judge_credentials): keychain item not found"] + \
        (["credentials hypothesizer: the claude CLI is not on PATH"] if not shutil.which("claude") else [])
    calls = fake_security.log.read_text().splitlines()
    assert calls == ["find-generic-password -s rsi-sandbox -a anthropic", "find-generic-password -s rsi-sandbox -a openai"]
    assert not any("-w" in c.split() for c in calls)                     # attributes only: no secret, no dialog
    fake_security.known.write_text("rsi-sandbox/anthropic\nrsi-sandbox/openai\n")
    monkeypatch.setenv("AMEM_DB_URL", "postgresql://u@db.example.invalid/x")
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    monkeypatch.setenv("AMEM_EDITS_CLAUDE", "/bin/echo")
    assert S.credential_problems(cfg) == []
    monkeypatch.setenv("AMEM_DB_URL", "postgresql://u@127.0.0.1/x")            # a local DB under REQUIRE_REMOTE
    assert S.credential_problems(cfg)[0].startswith("credentials database: AMEM_REQUIRE_REMOTE=1 but the database host is local")


def test_paid_stages_with_a_zero_budget_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    fake_security: SimpleNamespace) -> None:
    d = json.loads(FIRST.read_text())
    d["budget"]["max_usd"] = 0.0
    p = tmp_path / "zero.json"
    p.write_text(json.dumps(d))
    assert any(x.startswith("budget max_usd: 0 while paid stages are configured (hypothesizer, judge, replay_agent, "
                            "replay_jev, triage)") for x in S.credential_problems(N.load_config(p)))
    shipped = N.load_config()                                            # the lean default: nothing paid, nothing needed
    fake_security.log.unlink()
    monkeypatch.setenv("AMEM_DB_URL", "postgresql://u@127.0.0.1/x")
    assert S.paid_stages(shipped) == [] and S.credential_problems(shipped) == []
    assert not fake_security.log.exists()


# --------------------------------------------------------------------------- the first-night limits and the paid plan


def test_first_night_config_is_the_smallest_useful_scope() -> None:
    cfg = first_cfg()
    b = cfg.budget
    assert (b.max_experiments, b.max_hypotheses, b.max_ab_pairs, b.model_retries) == (1, 2, 3, 0)
    assert b.max_usd == 1.0 and b.wall_clock_s <= 3600 and b.default_model_calls == 0
    assert cfg.stages == {"triage": "live", "hypothesizer": "claude-cli", "implementer": "stub", "replay_agent": "claude",
                          "judge": "codex", "replay_jev": "live"}
    assert cfg.replay["reps"] * 3 == 3 and cfg.replay["cases"] is None            # 3 cases x 1 rep = 3 A/B pairs
    assert cfg.replay["judge_model"] == "gpt-6-luna" and cfg.replay["max_budget_usd"] == 0.05
    assert dict(b.model_calls) == {"jev-1.14.0": 6, "sonnet": 1, "gpt-6-luna": 3}
    # a replay of the configured size fits; a bigger one is refused before any container (night side)
    rc = N.SandboxReplayer(cfg.stages, cfg.replay).replay_config()
    assert (rc.agent, rc.judge, rc.jev, rc.judge_model, rc.judge_credentials, rc.judge_credential_name) == (
        "claude", "codex", "live", "gpt-6-luna", "keychain:rsi-sandbox/openai", "CODEX_API_KEY")
    assert rc.model == "claude-haiku-5-5" and rc.credentials == "keychain:rsi-sandbox/anthropic" and rc.max_budget_usd == 0.05
    rc.validate()


def test_a_replay_over_the_pair_cap_is_refused_before_any_container() -> None:
    cfg = first_cfg()
    rep = N.SandboxReplayer(cfg.stages, {**cfg.replay, "reps": 2})
    from agent_memory.rsi_budget import Budget
    with pytest.raises(N.ExperimentFailed, match="6 A/B pairs is over the cap of 3"):
        rep.replay(artifact_hash="a" * 64, experiment_id="exp-20261018-abcd1234", baseline_id="bl-1",
                   budget=Budget(cfg.budget))


def test_live_replay_brings_the_network_up_and_down_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import rsi_replay
    from agent_memory.rsi_budget import Budget
    events: list[Any] = []

    class Box:
        def __init__(self, root: Path) -> None:
            pass

        def net_status(self) -> dict[str, Any]:
            events.append("status")
            return {"proxy_running": False}

        def net_down(self) -> dict[str, Any]:
            events.append("down")
            return {}

    monkeypatch.setattr(N, "sandbox_module", lambda: SimpleNamespace(Sandbox=Box))

    def fake_run(h: str, cases: Any, rc: Any, **kw: Any) -> Any:
        events.append(("run", kw["ensure_network"], rc.judge, len(cases)))
        return SimpleNamespace(verdict=SimpleNamespace(verdict="NO_EFFECT", reasons=["checks-equal"]), arms={},
                               artifact_hash=h, baseline_id="bl-1", eval_path=None, budget_stop=None, runs=[],
                               judge=None, jev=None)
    monkeypatch.setattr(rsi_replay, "run_replay", fake_run)
    cfg = first_cfg()
    assert cfg.replay["ensure_network"] is True
    out = N.SandboxReplayer(cfg.stages, cfg.replay).replay(artifact_hash="a" * 64, experiment_id="exp-20261018-ab",
                                                          baseline_id="bl-1", budget=Budget(cfg.budget))
    assert out.verdict == "NO_EFFECT" and events == ["status", ("run", True, "codex", 3), "down"]
    events.clear()
    stub = N.load_config()                                   # stub agent and judge: no network, nothing touched
    N.SandboxReplayer(stub.stages, {**stub.replay, "ensure_network": True}).replay(
        artifact_hash="a" * 64, experiment_id="exp-20261018-ab", baseline_id="bl-1", budget=Budget(stub.budget))
    assert events == [("run", False, "stub", 3)]


def test_mock_rehearsal_config_keeps_the_limits_and_drops_every_paid_stage(tmp_path: Path) -> None:
    from agent_memory import rsi_rehearsal as R
    mock = N.load_config(R.mock_config(FIRST, tmp_path / "mock.json"))
    first = first_cfg()
    assert S.paid_stages(mock) == [] and mock.budget.max_usd == 0.0 and S.paid_plan(mock)["calls"] == []
    for k in ("max_experiments", "max_hypotheses", "max_ab_pairs", "max_sandbox_runs", "wall_clock_s", "model_retries"):
        assert getattr(mock.budget, k) == getattr(first.budget, k), k
    assert mock.replay["credentials"] == mock.replay["judge_credentials"] == "none" and mock.replay["reps"] == 1
    assert S.prerequisites(S.schedule_section(mock.path)) == S.DEFAULT_PREREQS
    now = datetime(2026, 10, 18, 22, 40, tzinfo=CEST)
    log = write_log(tmp_path, R.routines_lines(NIGHT, now, daily=False))
    assert S.check_prerequisites(log, NIGHT, now, S.DEFAULT_PREREQS) == ["prerequisite amem-daily: no run in routines.log"]
    log.write_text(R.routines_lines(NIGHT, now, daily=True))
    assert S.check_prerequisites(log, NIGHT, now, S.DEFAULT_PREREQS) == []
    for bad in (R.D.REAL_HOME / "sandbox-dev" / "x", R.D.REAL_HOME / "sandbox" / "x"):
        with pytest.raises((R.RehearsalError, R.D.DemoError)):
            R.check_workdir(bad)


def test_paid_plan_lists_every_paid_call_and_fits_the_cap() -> None:
    plan = S.paid_plan(first_cfg())
    got = {(r["stage"], r["model"]): r["count"] for r in plan["calls"]}
    assert got == {("triage", "jev-1.14.0"): 5, ("hypothesize", "sonnet"): 1, ("replay-agent", "claude-haiku-5-5"): 6,
                   ("replay-judge", "gpt-6-luna"): 3, ("replay-jev", "jev-1.14.0"): 1}
    assert plan["ab_pairs_per_experiment"] == 3 and plan["within_cap"] and plan["worst_case_usd"] == 0.91
    assert plan["expected_usd"] == pytest.approx(0.0734, abs=1e-4) and plan["expected_incomplete"]   # Sonnet unmeasured
    text = S.render_plan(plan)
    assert "worst case $0.9100, hard cap max_usd $1.00 (the worst case fits)" in text
    lean = S.paid_plan(N.load_config())
    assert lean["calls"] == [] and lean["expected_usd"] == 0 and "No paid call" in S.render_plan(lean)


def test_cli_plan_runs_nothing(tmp_path: Path) -> None:
    e = base_env(HOME=str(tmp_path), RSI_SANDBOX_DIR=str(tmp_path / "sandbox"))
    r = subprocess.run([AMEM, "rsi", "night-job", "--plan", "--config", str(FIRST), "--json"], env=e,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["worst_case_usd"] == 0.91
    assert not (tmp_path / "sandbox").exists() and not (tmp_path / ".claude").exists()


# --------------------------------------------------------------------------- the launchd plists (files only)


def load_plist(name: str) -> dict[str, Any]:
    return plistlib.loads((LAUNCHD / name).read_bytes())


@pytest.mark.parametrize("name, sub", [("com.vilmerfrost.amem-rsi-night.plist", "night-job"),
                                       ("com.vilmerfrost.amem-rsi-brief.plist", "brief-job")])
def test_plists_are_stage_only_hooks_off_and_lint_clean(name: str, sub: str) -> None:
    if shutil.which("plutil"):
        r = subprocess.run(["plutil", "-lint", str(LAUNCHD / name)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.strip().endswith(": OK"), r.stdout + r.stderr
    p = load_plist(name)
    assert p["Label"] == name.removesuffix(".plist") and p["RunAtLoad"] is False and "KeepAlive" not in p
    args = p["ProgramArguments"]
    assert args[:6] == ["/opt/homebrew/bin/uv", "run", "--directory", "/Users/vilmerfrost/Projects/agent-memory",
                        "--quiet", "amem"] and args[6:8] == ["rsi", sub]
    assert not {"apply", "promote", "decide", "decide-poll", "push", "--push", "undo"} & set(args)
    env = p["EnvironmentVariables"]
    assert env["AMEM_HOOK_OFF"] == "1" and env["AMEM_REQUIRE_REMOTE"] == "1"
    assert env["RSI_SANDBOX_DIR"] == "/Users/vilmerfrost/sandbox"                     # the staging root, not a live path
    assert "/usr/local/bin" in env["PATH"].split(":") and "/Users/vilmerfrost/.local/bin" in env["PATH"].split(":")
    for k in env:                                     # no secret, no store URL, no write mode, no phone push
        assert not any(s in k for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "DB_URL")), k
        assert k not in ("AMEM_EDITS_MODE", "AMEM_EDITS_LIVE", "AMEM_RSI_BRIEF_PUSH", "AMEM_DEVIATIONS_LIVE"), k
    live = ("/.claude/CLAUDE.md", "/.codex/", "/agent-config", "/.agents/skills", "/Library/LaunchAgents")
    for v in [*args, *env.values(), p["StandardOutPath"], p["StandardErrorPath"]]:
        assert not any(x in v for x in live), v
    assert p["StandardInPath"] == "/dev/null"                                      # nothing can wait on a prompt
    when = p["StartCalendarInterval"]
    assert set(when) == {"Hour", "Minute"}


def test_night_plist_uses_the_first_night_config_and_brief_waits_for_the_night() -> None:
    night, brief = load_plist("com.vilmerfrost.amem-rsi-night.plist"), load_plist("com.vilmerfrost.amem-rsi-brief.plist")
    a = night["ProgramArguments"]
    assert a[a.index("--config") + 1] == "/Users/vilmerfrost/Projects/agent-memory/config/rsi-night.first.json"
    assert night["EnvironmentVariables"]["AMEM_JEV_LIVE"] == "1"
    b = brief["ProgramArguments"]
    assert float(b[b.index("--wait-s") + 1]) >= first_cfg().budget.wall_clock_s + S.HARD_GRACE_S
    assert "AMEM_JEV_LIVE" not in brief["EnvironmentVariables"]                     # the brief calls no model
    # the night starts after amem-daily's day and finishes before the brief (wall clock + watchdog)
    start = night["StartCalendarInterval"]["Hour"] * 60 + night["StartCalendarInterval"]["Minute"]
    end = start + (first_cfg().budget.wall_clock_s + S.HARD_GRACE_S + S.KILL_GRACE_S) / 60
    assert end < brief["StartCalendarInterval"]["Hour"] * 60 + brief["StartCalendarInterval"]["Minute"]


# --------------------------------------------------------------------------- the watchdog backstop


def test_watchdog_terms_then_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[Any] = []
    old = signal.signal(signal.SIGTERM, lambda s, f: got.append("SIGTERM"))
    try:
        exited: list[int] = []
        with S.Watchdog(0.05, 0.05, lambda: got.append("on_kill"), exit=exited.append) as w:
            deadline = time.monotonic() + 5
            while not exited and time.monotonic() < deadline:
                time.sleep(0.01)
        assert w.fired == "kill" and exited == [S.EXIT_HARD_TIMEOUT] and got == ["SIGTERM", "on_kill"]
        with S.Watchdog(5, 5, lambda: got.append("never"), exit=exited.append) as w2:
            pass
        assert w2.fired is None
    finally:
        signal.signal(signal.SIGTERM, old)


# --------------------------------------------------------------------------- the jobs on the throwaway Postgres


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home, sandbox=sandbox, tmp=tmp_path, log=write_log(tmp_path, GOOD_LOG),
                           reports=tmp_path / "reports")


def night_job(env: SimpleNamespace, **kw: Any) -> S.JobResult:
    kw.setdefault("adapters", adapters(evidence=FakeEvidence()))
    return S.run_night_job(night=NIGHT, now=NOW, routines_log=env.log, db_url=env.url, **kw)


def log_runs(env: SimpleNamespace, routine: str) -> list[RL.ParsedRun]:
    return [r for r in RL.parse_file(env.log).runs if r.routine == routine]


def test_night_skips_with_a_recorded_reason_until_amem_daily_completed(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    env.log.write_text(GOOD_LOG.replace(GOOD_LOG.splitlines()[3] + "\n", DAILY_FAIL))
    ev = FakeEvidence()
    res = night_job(env, adapters=adapters(evidence=ev))
    assert res.status == "SKIP" and res.exit_code == 0 and ev.since_calls == []          # the night never started
    (run,) = log_runs(env, "amem-rsi-night")
    assert run.status == "skip" and run.meta["skipped"] == "prerequisites"
    assert run.meta["failures"] == ["prerequisite amem-daily: last run FAIL at 2026-10-18T09:20:04+0200"]
    rep = json.loads(N.report_path(NIGHT).read_text())                                   # the brief will say why
    assert N.validate_report(rep) == [] and rep["status"] == "preflight_failed"
    assert rep["notes"] == ["skipped by amem-rsi-night: prerequisite amem-daily: last run FAIL at 2026-10-18T09:20:04+0200"]
    assert run.report_path == str(N.report_path(NIGHT))
    for t in ("rsi_checkpoints", "rsi_candidates", "rsi_experiments"):
        assert rows(env.url, f"select count(*) from {t}") == [(0,)], t
    # once amem-daily has passed, the same night runs
    with env.log.open("a") as fh:
        fh.write("2026-10-18T11:02:10+0200 amem-daily PASS scored=3 1s\n")
    res2 = night_job(env)
    assert res2.status == "PASS" and res2.fields["status"] == "ok" and res2.fields["staged"] == 1
    assert json.loads(N.report_path(NIGHT).read_text())["status"] == "ok"                 # the real report replaced it


def test_overlapping_start_is_refused_and_logged_as_skip(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    box = FakeSandboxOps()
    held = box.lock(60)                                                                  # another night is running
    try:
        res = night_job(env, adapters=adapters(sandbox=box))
        assert res.status == "SKIP" and res.items == ["overlap: another night holds the rsi-night lock"]
        assert held.exists() and not N.report_path(NIGHT).exists()                        # its lock and report untouched
        assert rows(env.url, "select count(*) from rsi_checkpoints") == [(0,)]
        (run,) = log_runs(env, "amem-rsi-night")
        assert run.status == "skip" and run.report_path is None
    finally:
        box.release(held)


def test_missing_credential_skips_cleanly_without_a_prompt(env: SimpleNamespace, fake_security: SimpleNamespace,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    seed_rule(env.url, RULE_A, "a")
    d = json.loads(N.CONFIG_PATH.read_text())
    d["stages"].update(judge="codex")                                                  # one live stage, its key missing
    d["budget"].update(max_usd=0.5, model_calls={"gpt-6-luna": 3})
    d["replay"].update(judge_model="gpt-6-luna", judge_credentials="keychain:rsi-sandbox/openai",
                       judge_credential_name="CODEX_API_KEY")
    cfgp = env.tmp / "live-judge.json"
    cfgp.write_text(json.dumps(d))
    rep_before = FakeReplayer()
    res = night_job(env, config_path=cfgp, adapters=adapters(replayer=rep_before))
    assert res.status == "FAIL" and res.exit_code == 1 and res.fields["skipped"] == "credentials"
    assert res.items == ["credentials judge (replay.judge_credentials): keychain item not found"]
    assert rep_before.calls == [] and rows(env.url, "select count(*) from rsi_checkpoints") == [(0,)]
    calls = fake_security.log.read_text().splitlines()
    assert calls == ["find-generic-password -s rsi-sandbox -a openai"]                    # presence only, never -w
    (run,) = log_runs(env, "amem-rsi-night")
    assert run.status == "fail" and run.meta["failures"] == res.items                     # routine-audit raises it
    assert json.loads(N.report_path(NIGHT).read_text())["notes"][0].startswith("skipped by amem-rsi-night: credentials")


def test_cli_night_job_gates_without_running_anything(env: SimpleNamespace) -> None:
    env.log.write_text("")                                                                # nothing has run today
    e = base_env(AMEM_DB_URL=env.url, HOME=str(env.home), RSI_SANDBOX_DIR=str(env.sandbox))
    r = subprocess.run([AMEM, "rsi", "night-job", "--night", NIGHT, "--now", NOW.isoformat(), "--routines-log",
                        str(env.log), "--json"], env=e, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "SKIP" and len(out["items"]) == 3 and out["lines"][0].startswith(
        "2026-10-19T01:30:05+0200 amem-rsi-night SKIP night=2026-10-18 config=")
    assert env.log.read_text().splitlines() == out["lines"]


def test_night_then_brief_job_writes_the_brief_and_never_offers_apply_for_stand_ins(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    res = night_job(env)
    assert res.status == "PASS" and res.fields["checkpoint"] == "advanced" and res.report == str(N.report_path(NIGHT))
    assert res.fields["usd"] == 0.0 and res.fields["reported_usd"] == 0.0
    b = S.run_brief_job(night=NIGHT, now=NOW + timedelta(hours=6), routines_log=env.log, out_dir=env.reports,
                        db_url=env.url)
    assert b.status == "PASS", b.items
    md = (env.reports / f"rsi-brief-{NIGHT}.md").read_text()
    assert md.startswith(f"# RSI morning brief: night {NIGHT}") and (env.reports / f"rsi-brief-{NIGHT}.json").is_file()
    assert " apply --hash " not in md and "- Apply: not offered: " in md                 # the fake replay: stand-ins
    assert b.fields["ready"] == 0 and b.fields["staged_not_ready"] == 1 and b.fields["apply"] == 0
    (run,) = log_runs(env, "amem-rsi-brief")
    assert run.status == "pass" and run.report_path == str(env.reports / f"rsi-brief-{NIGHT}.md")
    assert [r.status for r in log_runs(env, "amem-rsi-night")] == ["pass"]


def test_brief_job_waits_for_a_running_night_then_reports(env: SimpleNamespace) -> None:
    box = FakeSandboxOps()
    held = box.lock(600)
    slept: list[float] = []

    def sleep(s: float) -> None:                 # the night finishes while the brief waits
        slept.append(s)
        box.release(held)
        N._write_json(N.report_path(NIGHT), S.skip_report(NIGHT, ["prerequisite amem-daily: no run"], now=NOW))
    b = S.run_brief_job(night=NIGHT, now=NOW, routines_log=env.log, out_dir=env.reports, db_url=env.url, wait_s=600,
                        poll_s=60, sleep=sleep)
    assert slept == [60] and b.status == "PASS" and b.fields["night_status"] == "preflight_failed"
    assert "skipped by amem-rsi-night: prerequisite amem-daily: no run" in b.detail["markdown"]


def test_brief_job_fails_visibly_while_the_night_runs_or_without_a_report(env: SimpleNamespace) -> None:
    b = S.run_brief_job(night=NIGHT, now=NOW, routines_log=env.log, out_dir=env.reports, db_url=env.url)
    assert b.status == "FAIL" and b.items[0].startswith("report: no report for night 2026-10-18")
    box = FakeSandboxOps()
    held = box.lock(600)
    try:
        b = S.run_brief_job(night=NIGHT, now=NOW, routines_log=env.log, out_dir=env.reports, db_url=env.url)
        assert b.status == "FAIL" and b.items == ["night running: the rsi-night lock is held; no brief for 2026-10-18 yet"]
    finally:
        box.release(held)
    assert [r.status for r in log_runs(env, "amem-rsi-brief")] == ["fail", "fail"]
    assert not env.reports.exists()
