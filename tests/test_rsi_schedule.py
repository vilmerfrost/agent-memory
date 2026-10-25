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
DEV_FAIL = ("deviations: 1 error; first: session b1b2011c/agent-a079: writer output: deviation 0: followed_grade must "
            "be a number in 0..1, got None")


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
    assert reasons == [f"prerequisite amem-daily: last run FAIL at 2026-10-18T09:20:04+0200 ({DEV_FAIL})"]
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


DAILY_PASS_LINE = GOOD_LOG.splitlines()[3] + "\n"
SCORE_FAIL = "score dispatch: 1 Jev error; first: run 4f1c: ReadTimeout"
EDITS_FAIL = "edits: 1 error; first: claude rule-7: writer timed out"


def daily_fail(*details: str, ts: str = "2026-10-18T09:20:04+0200") -> str:
    return (f"{ts} amem-daily FAIL scored=16 escalations=14 new=0 push=none tasks=10 707s\n"
            + "".join(f"{ts} amem-daily-fail {d}\n" for d in details))


def test_gate_runs_the_night_when_only_steps_it_does_not_read_failed(tmp_path: Path) -> None:
    pre = S.DEFAULT_PREREQS
    g = S.evaluate_prerequisites(write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail(SCORE_FAIL, EDITS_FAIL))),
                                 NIGHT, NOW, pre)
    assert g.reasons == [] and g.summary() == ""
    assert g.warnings == [
        f"prerequisite amem-daily: step score failed at 2026-10-18T09:20:04+0200; the night does not read it: {SCORE_FAIL}",
        f"prerequisite amem-daily: step edits failed at 2026-10-18T09:20:04+0200; the night does not read it: {EDITS_FAIL}"]
    for step in ("distill: 1 error; first: run 9: x", "escalations: OperationalError: x", "rsi-push: OSError: x",
                 "state: cannot write amem-daily-state.json: OSError: x"):
        assert S.check_prerequisites(write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail(step))),
                                     NIGHT, NOW, pre) == [], step


def test_gate_skips_when_a_step_the_night_reads_failed(tmp_path: Path) -> None:
    pre = S.DEFAULT_PREREQS
    ts = "2026-10-18T09:20:04+0200"
    # a needed step next to a tolerated one: the needed one blocks and names itself, the other is a warning
    g = S.evaluate_prerequisites(write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail(SCORE_FAIL, DEV_FAIL))),
                                 NIGHT, NOW, pre)
    assert g.reasons == [f"prerequisite amem-daily: last run FAIL at {ts} ({DEV_FAIL})"]
    assert g.summary() == f"prerequisite amem-daily failed ({DEV_FAIL})"
    assert [w.split(";")[0] for w in g.warnings] == [f"prerequisite amem-daily: step score failed at {ts}"]
    for needed in ("ingest: FetchError: gh api failed", "tasks: OperationalError: x", DEV_FAIL,
                   "newstep: a step the gate does not know", "Traceback (most recent call last)"):
        got = S.check_prerequisites(write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail(needed))),
                                    NIGHT, NOW, pre)
        assert got == [f"prerequisite amem-daily: last run FAIL at {ts} ({needed})"], needed
    # a FAIL that names no step blocks (fail closed)
    g = S.evaluate_prerequisites(write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail())), NIGHT, NOW, pre)
    assert g.reasons == [f"prerequisite amem-daily: last run FAIL at {ts}"]
    assert g.causes == [f"prerequisite amem-daily failed (last run FAIL at {ts}, no failed step named)"]
    # a tolerated FAIL still has to be the night's own run
    old = daily_fail(SCORE_FAIL, ts="2026-10-17T09:20:04+0200")
    assert S.check_prerequisites(write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, old)), NIGHT, NOW, pre) == [
        "prerequisite amem-daily: no PASS on 2026-10-18 (last completed run 2026-10-17T09:20:04+0200)"]
    # amem-archive and amem-ingest keep the strict rule: any FAIL blocks
    arch = GOOD_LOG.replace("2026-10-18T08:45:09+0200 amem-archive PASS heartbeat 5s\n",
                            "2026-10-18T08:45:09+0200 amem-archive FAIL 5s\n"
                            "2026-10-18T08:45:09+0200 amem-archive-fail score: not a daily step here\n")
    assert S.check_prerequisites(write_log(tmp_path, arch), NIGHT, NOW, pre) == [
        "prerequisite amem-archive: last run FAIL at 2026-10-18T08:45:09+0200 (score: not a daily step here)"]


def test_nonblocking_steps_are_configurable_and_backward_compatible() -> None:
    old_shape = {"prerequisites": [{"routine": "amem-daily", "same_night": True}, {"routine": "amem-archive"}]}
    daily_p, arch = S.prerequisites(old_shape)
    assert daily_p.nonblocking_steps == S.DAILY_NONBLOCKING_STEPS and arch.nonblocking_steps == ()
    strict = S.prerequisites({"prerequisites": [{"routine": "amem-daily", "nonblocking_steps": []}]})
    assert strict == (S.Prereq("amem-daily", True, None, ()),)
    custom = S.prerequisites({"prerequisites": [{"routine": "amem-archive", "nonblocking_steps": ["verify"]}]})
    assert custom[0].nonblocking_steps == ("verify",)
    for bad in ("score", ["Score"], [1], ["../x"]):
        with pytest.raises(ValueError):
            S.prerequisites({"prerequisites": [{"routine": "amem-daily", "nonblocking_steps": bad}]})


def test_strict_config_restores_the_old_gate(tmp_path: Path) -> None:
    strict = S.prerequisites({"prerequisites": [{"routine": "amem-daily", "nonblocking_steps": []}]})
    log = write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail(SCORE_FAIL)))
    assert S.check_prerequisites(log, NIGHT, NOW, strict) == [
        f"prerequisite amem-daily: last run FAIL at 2026-10-18T09:20:04+0200 ({SCORE_FAIL})"]


def test_skip_reports_say_why_and_stay_valid_for_every_reader() -> None:
    from agent_memory import rsi_brief as BR
    pre = S.skip_report(NIGHT, ["prerequisite amem-daily: last run FAIL at x (deviations: 2 errors)"], now=NOW,
                        summary="prerequisite amem-daily failed (deviations: 2 errors)", warnings=["w1"])
    creds = S.skip_report(NIGHT, ["credentials jev: TYPESAFE_API_KEY is not set", "budget max_usd: 0"], now=NOW,
                          kind="credentials")
    for rep in (pre, creds):
        assert N.validate_report(rep) == [] and BR.validate_report(rep) == [] and rep["status"] == "preflight_failed"
    assert pre["skipped"]["reason"] == "prerequisites" and pre["prerequisite_warnings"] == ["w1"]
    assert creds["skipped"] == {"by": "amem-rsi-night", "reason": "credentials",
                                "summary": "the credentials and budget check failed (credentials jev: "
                                           "TYPESAFE_API_KEY is not set; and 1 more)",
                                "items": ["credentials jev: TYPESAFE_API_KEY is not set", "budget max_usd: 0"]}
    assert "prerequisite_warnings" not in creds
    bad = {**creds, "skipped": {**creds["skipped"], "reason": "tired"}}
    assert any("skipped.reason" in e for e in N.validate_report(bad)) and BR.validate_report(bad)
    assert any("only valid with status preflight_failed" in e for e in N.validate_report({**creds, "status": "ok"}))
    with pytest.raises(ValueError):
        S.skip_report(NIGHT, ["x"], now=NOW, kind="tired")


def render_skip_brief(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rep: dict[str, Any]) -> str:
    from agent_memory import rsi_brief as BR
    root = tmp_path / "sandbox"
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(root))
    monkeypatch.setenv("AMEM_AGENT_CONFIG_DIR", str(tmp_path / "no-agent-config"))
    N._write_json(N.report_path(NIGHT), rep)
    return BR.render_markdown(BR.make_brief(root=root, night=NIGHT, db_url="postgresql://postgres:postgres@127.0.0.1:1/x"))


def test_prerequisite_skip_brief_names_the_cause_and_claims_nothing(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    g = S.evaluate_prerequisites(write_log(tmp_path, GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail(SCORE_FAIL, DEV_FAIL))),
                                 NIGHT, NOW, S.DEFAULT_PREREQS)
    md = render_skip_brief(tmp_path, monkeypatch, S.skip_report(NIGHT, g.reasons, now=NOW, summary=g.summary(),
                                                                warnings=g.warnings))
    assert f"The night did not run: prerequisite amem-daily failed ({DEV_FAIL}). " in md
    assert "Checkpoint not read or advanced" in md and "- Warning: prerequisite amem-daily: step score failed" in md
    assert "- Sessions, deviations, eval failures and routine failures: not analyzed: the night did not run." in md
    assert "- Not analyzed: the night did not run." in md and "None considered: the night did not run." in md
    assert "- Services: not checked: the night did not run." in md
    assert "- Cost basis: none needed: the night did not run, so it made no model or provider call." in md
    for claim in ("No new sessions", "0 deviations", "see Service health", "- clickhouse: skipped", "- None found.",
                  "not recorded in this report", "stays at start"):
        assert claim not in md, claim


def test_credentials_skip_brief_names_the_missing_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import rsi_brief as BR
    rep = S.skip_report(NIGHT, ["credentials judge (replay.judge_credentials): keychain item not found"], now=NOW,
                        kind="credentials")
    md = render_skip_brief(tmp_path, monkeypatch, rep)
    assert ("The night did not run: the credentials and budget check failed (credentials judge "
            "(replay.judge_credentials): keychain item not found).") in md
    assert "No new sessions" not in md and "see Service health" not in md and "not analyzed" in md
    title, body = BR.notification(BR.make_brief(root=tmp_path / "sandbox", night=NIGHT,
                                                db_url="postgresql://postgres:postgres@127.0.0.1:1/x"))
    assert "Not run: skipped by the night job (credentials); nothing analyzed." in body and "Sessions 0" not in body


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
    generic = ["docs-typo", "fix-add", "report-todos"]       # an explicit case list is replayed as given
    rep = N.SandboxReplayer(cfg.stages, {**cfg.replay, "cases": generic, "reps": 2})
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
        events.append(("run", kw.get("ensure_network", False), rc.judge, len(cases)))
        return SimpleNamespace(verdict=SimpleNamespace(verdict="NO_EFFECT", reasons=["checks-equal"]), arms={},
                               artifact_hash=h, baseline_id="bl-1", eval_path=None, budget_stop=None, runs=[],
                               judge=None, jev=None)
    monkeypatch.setattr(rsi_replay, "run_replay", fake_run)
    cfg = first_cfg()
    assert cfg.replay["ensure_network"] is True
    generic = ["docs-typo", "fix-add", "report-todos"]       # the artifact is not staged: an explicit list
    out = N.SandboxReplayer(cfg.stages, {**cfg.replay, "cases": generic}).replay(
        artifact_hash="a" * 64, experiment_id="exp-20261018-ab", baseline_id="bl-1", budget=Budget(cfg.budget))
    assert out.verdict == "NO_EFFECT" and events == ["status", ("run", True, "codex", 3), "down"]
    events.clear()
    stub = N.load_config()                                   # stub agent and judge: no network, nothing touched
    N.SandboxReplayer(stub.stages, {**stub.replay, "cases": generic, "ensure_network": True}).replay(
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
    from agent_memory import rsi_approval as AP
    for now in (datetime(2026, 10, 18, 12, 5, tzinfo=CEST), datetime(2026, 10, 18, 22, 40, tzinfo=CEST),
                datetime(2026, 10, 19, 0, 30, tzinfo=CEST), datetime(2026, 10, 19, 8, 0, tzinfo=CEST),
                datetime(2026, 10, 19, 11, 59, tzinfo=CEST)):     # the rehearsal works at any time of day
        night = AP.night_key(now)
        assert night == NIGHT
        log = write_log(tmp_path, R.routines_lines(night, now, daily=False))
        assert S.check_prerequisites(log, night, now, S.DEFAULT_PREREQS) == [
            "prerequisite amem-daily: no run in routines.log"], now
        log.write_text(R.routines_lines(night, now, daily=True))
        assert S.check_prerequisites(log, night, now, S.DEFAULT_PREREQS) == [], now
    for bad in (R.D.REAL_HOME / "sandbox-dev" / "x", R.D.REAL_HOME / "sandbox" / "x"):
        with pytest.raises((R.RehearsalError, R.D.DemoError)):
            R.check_workdir(bad)


def test_paid_rehearsal_refuses_without_the_approved_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    from agent_memory import rsi_rehearsal as R
    ran: list[Any] = []
    monkeypatch.setattr(R, "run_rehearsal", lambda *a, **k: ran.append((a, k)))
    for argv in (["--live"], ["--live", "--approve-max-usd", "5"], ["--live", "--approve-max-usd", "0.5"]):
        assert R.main([*argv, "--workdir", str(tmp_path / "w")]) == 2
        err = capsys.readouterr().err
        assert "needs Vilmer's approval" in err and "--approve-max-usd 1" in err and "Nothing ran." in err
    assert ran == [] and not (tmp_path / "w").exists()
    assert R.main(["--paid-plan"]) == 0 and "worst case $0.9100" in capsys.readouterr().out and ran == []


def test_live_jev_triage_is_booked_as_an_estimate_not_as_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import jev
    from agent_memory.rsi_budget import Budget
    ans = {"action": SimpleNamespace(value="experiment", probabilities={"experiment": 0.8}),
           "impact": SimpleNamespace(value=2), "risk": SimpleNamespace(value=0), "testability": SimpleNamespace(value=1)}
    monkeypatch.setattr(jev, "mode", lambda: "live")
    monkeypatch.setattr(jev, "ask", lambda state, qs: ans)
    t = N.JevTriage("live")
    monkeypatch.setattr(t, "_questions", lambda: SimpleNamespace(jev_questions=lambda: []))
    b = Budget(first_cfg().budget)
    got = t.assess({"dedupe_key": "k"}, state="s", budget=b)
    assert got is not None and got.mode == "live" and got.action == "experiment"
    c = b.costs()
    assert c["model_bases"] == {"jev-1.14.0": {"call": 0.01}} and c["reported_usd"] == 0.0 and c["estimated_usd"] == 0.01


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
    assert a[a.index("--config") + 1] == "/Users/vilmerfrost/Projects/agent-memory/config/rsi-night.subscription.json"
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


def test_watchdog_always_reaches_its_fail_line_even_outside_the_nights_handler() -> None:
    """SIGTERM at its default disposition (the night's handler is gone: unwinding, final DB writes) would kill the
    job before the watchdog's FAIL line. term_guard keeps the process alive so the kill stage runs."""
    old = signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        got: list[str] = []
        exited: list[int] = []
        with S.term_guard() as terms, S.Watchdog(0.05, 0.1, lambda: got.append("FAIL line"), exit=exited.append):
            deadline = time.monotonic() + 5
            while not exited and time.monotonic() < deadline:     # "blocked" outside Night._signals
                time.sleep(0.01)
        assert terms["term"] == 1 and got == ["FAIL line"] and exited == [S.EXIT_HARD_TIMEOUT]
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL                      # restored afterwards
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
    reason = f"prerequisite amem-daily: last run FAIL at 2026-10-18T09:20:04+0200 ({DEV_FAIL})"
    assert run.meta["failures"] == [reason]
    rep = json.loads(N.report_path(NIGHT).read_text())                                   # the brief will say why
    assert N.validate_report(rep) == [] and rep["status"] == "preflight_failed"
    assert rep["notes"] == [f"skipped by amem-rsi-night: {reason}"]
    assert rep["skipped"] == {"by": "amem-rsi-night", "reason": "prerequisites", "items": [reason],
                              "summary": f"prerequisite amem-daily failed ({DEV_FAIL})"}
    assert run.report_path == str(N.report_path(NIGHT))
    for t in ("rsi_checkpoints", "rsi_candidates", "rsi_experiments"):
        assert rows(env.url, f"select count(*) from {t}") == [(0,)], t
    # once amem-daily has passed, the same night runs
    with env.log.open("a") as fh:
        fh.write("2026-10-18T11:02:10+0200 amem-daily PASS scored=3 1s\n")
    res2 = night_job(env)
    assert res2.status == "PASS" and res2.fields["status"] == "ok" and res2.fields["staged"] == 1
    assert json.loads(N.report_path(NIGHT).read_text())["status"] == "ok"                 # the real report replaced it


def test_night_runs_with_a_warning_when_only_a_step_it_does_not_read_failed(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    env.log.write_text(GOOD_LOG.replace(DAILY_PASS_LINE, daily_fail(SCORE_FAIL)))
    res = night_job(env)
    assert res.status == "PASS" and res.fields["status"] == "ok" and res.fields["staged"] == 1, res.items
    warning = (f"prerequisite amem-daily: step score failed at 2026-10-18T09:20:04+0200; the night does not read it: "
               f"{SCORE_FAIL}")
    assert res.warnings == [warning]
    (run,) = log_runs(env, "amem-rsi-night")
    assert run.status == "pass" and run.meta["warnings"] == [warning] and run.meta["failures"] == []
    rep = json.loads(N.report_path(NIGHT).read_text())
    assert N.validate_report(rep) == [] and rep["prerequisite_warnings"] == [warning] and "skipped" not in rep
    b = S.run_brief_job(night=NIGHT, now=NOW + timedelta(hours=6), routines_log=env.log, out_dir=env.reports,
                        db_url=env.url)
    md = b.detail["markdown"]
    assert b.status == "PASS" and md.split("\n")[2].startswith("The night finished.")
    assert f"- Warning: {warning}" in md and "not analyzed" not in md


def test_overlapping_start_is_refused_and_logged_as_skip(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    box = FakeSandboxOps()
    held = box.lock(60)                                                                  # another night is running
    try:
        res = night_job(env, adapters=adapters(sandbox=box))
        assert res.status == "SKIP" and res.items == []                               # nothing went wrong: no -fail
        assert res.warnings == ["overlap: another night holds the rsi-night lock"]
        assert not any("-fail" in ln for ln in res.lines) and any("amem-rsi-night-warn overlap" in ln for ln in res.lines)
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
    rep = json.loads(N.report_path(NIGHT).read_text())
    assert rep["notes"][0].startswith("skipped by amem-rsi-night: credentials")
    assert rep["skipped"]["reason"] == "credentials" and N.validate_report(rep) == []


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


def test_keychain_present_searches_the_named_keychain_file(monkeypatch, tmp_path) -> None:
    """Rehearsal only: AMEM_RSI_KEYCHAIN is appended so a temp HOME still sees the login keychain."""
    seen: list[list[str]] = []
    monkeypatch.setenv("AMEM_RSI_SECURITY", "/usr/bin/security")
    monkeypatch.setenv("AMEM_RSI_KEYCHAIN", str(tmp_path / "login.keychain-db"))
    assert S.keychain_present("keychain:rsi-sandbox/openai",
                              runner=lambda argv, **k: seen.append(argv) or SimpleNamespace(returncode=0)) is None
    assert seen[-1] == ["/usr/bin/security", "find-generic-password", "-s", "rsi-sandbox", "-a", "openai",
                        str(tmp_path / "login.keychain-db")]
    monkeypatch.delenv("AMEM_RSI_KEYCHAIN")
    S.keychain_present("keychain:rsi-sandbox/openai", runner=lambda argv, **k: seen.append(argv) or SimpleNamespace(returncode=0))
    assert seen[-1][-1] == "openai"


def test_health_reason_reaches_the_fail_line() -> None:
    rep = {"status": "partial", "night": NIGHT, "notes": [], "experiments": [],
           "health": {"supabase": "ok", "clickhouse": "error"},
           "health_reasons": {"clickhouse": ["ch.routine_streaks: error (timeout after 8s)"]}}
    assert S.classify(rep) == ("FAIL", ["health clickhouse: error (ch.routine_streaks: error (timeout after 8s))"])


def test_already_done_night_is_a_quiet_skip(env: SimpleNamespace) -> None:
    # 01:30 on 10-24 has night_key 10-23; a night that a manual run already finished must not run (or spend) again,
    # and the skip is not a problem: -warn, no -fail, so routine-audit stays green.
    seed_rule(env.url, RULE_A, "a")
    first = night_job(env)
    assert first.status in ("PASS", "FAIL") and N.report_path(NIGHT).exists()
    again = night_job(env)
    assert again.status == "SKIP" and again.items == []
    assert again.warnings and "already done" in again.warnings[0]
    assert not any("amem-rsi-night-fail" in ln for ln in again.lines)
