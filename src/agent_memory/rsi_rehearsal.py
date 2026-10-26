"""Mission 005.5 Phase D: the deterministic mock rehearsal of the first unattended night ($0, nothing real touched).

    uv run python scripts/m0055_rehearsal.py [--workdir DIR] [--config config/rsi-night.first.json] [--json]
    uv run python scripts/m0055_rehearsal.py --paid-plan        # what a live rehearsal would call and cost; runs nothing

It runs the scheduled path end to end, the way launchd would, in the Mission 005 demo world (rsi_demo):

    0. world     a temp HOME with synthetic instruction targets, a temp RSI_SANDBOX_DIR (never ~/sandbox or
                 ~/sandbox-dev), a throwaway Postgres on 127.0.0.1 seeded with synthetic sessions and deviations,
                 provider stand-ins (ClickHouse / Langfuse / past.dev), the Keychain tripwire, no env file
    1. config    the first-night config with every paid stage replaced by its deterministic stand-in (Jev fixture
                 triage, fixture hypotheses, stub implementer, stub agent, stub judge, Jev fixture) and max_usd 0,
                 so a paid call is impossible; every other limit (1 experiment, 2 hypotheses, 3 A/B pairs, wall clock,
                 sandbox runs) is the first night's own
    2. gate      `rsi_schedule.run_night_job` against a synthetic routines.log without amem-daily: SKIP with the
                 reason; then amem-daily's PASS is appended and the same night runs
    3. night     S0-S9 with the real adapters: triage, evidence pack, strict hypotheses, agent-config's real
                 `improve stage` (scratch copy), the real Docker sandbox replay with the stub agent, the deterministic
                 verdict, a staged proposal for IMPROVES, the night's checkpoint in the throwaway database
    4. brief     `rsi_schedule.run_brief_job`: the morning brief file and its routines.log line
    5. checks    the production fingerprint before and after (rsi_demo.production_fingerprint) must be equal; the
                 real ~/sandbox/nights listing and the real routines.log's night/brief lines unchanged; the hosted
                 database never configured; no container, lock or Keychain read left behind

`--paid-plan` prints `rsi_schedule.paid_plan` for the live first-night config: the exact paid calls, the G2b-based
estimate and the worst case. `--live --approve-max-usd <max_usd>` is the paid rehearsal (activation gate 2): the
same world and checks with the config's live stages (live Jev with the caller's TYPESAFE_API_KEY, the sandbox's
Keychain items, the host `claude -p` hypothesizer). It refuses without the approval flag. Unverified until run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent_memory import rsi_demo as D

FIRST_CONFIG = D.REPO_ROOT / "config" / "rsi-night.first.json"
MOCK_STAGES = {"triage": "fixture", "hypothesizer": "fixture", "implementer": "stub", "replay_agent": "stub",
               "judge": "stub", "replay_jev": "fixture"}
BANNER = ("MOCK REHEARSAL (Mission 005.5): synthetic data, stand-in stages, $0. The production checkpoint, files and "
          "schedules are not touched.")
PROTECTED = (D.REAL_HOME / "sandbox", D.REAL_HOME / "sandbox-dev")


class RehearsalError(RuntimeError):
    pass


def mock_config(first: Path, out: Path) -> Path:
    """The first-night config with each paid stage replaced by its stand-in and max_usd 0 (a paid call is refused)."""
    d = json.loads(first.read_bytes())
    d["_note"] = (f"MOCK rehearsal config derived from {first.name} (sha256 "
                  f"{hashlib.sha256(first.read_bytes()).hexdigest()[:12]}): stand-in stages, max_usd 0.")
    d["stages"] = dict(MOCK_STAGES)
    d["budget"]["max_usd"] = 0.0
    d["budget"]["sandbox_run_estimate_usd"] = 0.0     # stub runs are free; a priced run estimate would refuse them
    rp = dict(d.get("replay") or {})
    rp.update(model=None, credentials="none", credential_name=None, judge_model=None, judge_credentials="none",
              judge_credential_name=None)
    rp.pop("max_budget_usd", None)
    d["replay"] = rp
    d["implementer"] = {**(d.get("implementer") or {}), "credentials": "none"}
    out.write_text(json.dumps(d, indent=1) + "\n")
    return out


def check_workdir(work: Path) -> None:
    D.check_isolated(work)
    w = Path(os.path.realpath(work))
    for bad in PROTECTED:
        b = Path(os.path.realpath(bad))
        if w == b or b in w.parents or w in b.parents:
            raise RehearsalError(f"refusing work dir {w}: it overlaps {b}")


def routines_lines(night: str, now: datetime, *, daily: bool) -> str:
    """A synthetic routines.log for the gate: archive and ingest passed; amem-daily only when `daily`. Archive is
    dated on the night's date (08:45: always before `now`, since a night key is never later than today's morning);
    amem-daily carries the night's key (00:40 the next day, or just before `now` when that is later: never before
    the night's noon); ingest 30 minutes before `now`, so the 25 h rule holds at any time of day."""
    d = datetime.fromisoformat(night).replace(tzinfo=now.tzinfo)
    fmt = "%Y-%m-%dT%H:%M:%S%z"
    lines = [f"{d.replace(hour=8, minute=45, second=9).strftime(fmt)} amem-archive PASS heartbeat 5s",
             f"{(now - timedelta(minutes=30)).strftime(fmt)} amem-ingest PASS heartbeat 5s"]
    if daily:
        at = max(d.replace(hour=12, minute=0, second=4), min(d + timedelta(days=1, minutes=40, seconds=4),
                                                             now - timedelta(minutes=1)))
        lines.append(f"{at.strftime(fmt)} amem-daily PASS scored=16 "
                     "escalations=14 new=0 push=none tasks=10 deviations=8 707s")
    return "".join(x + "\n" for x in lines)


def real_job_lines() -> int:
    """How many amem-rsi-night / amem-rsi-brief lines the real routines.log holds (the rehearsal adds none)."""
    p = D.REAL_HOME / ".claude" / "parent" / "routines.log"
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return 0
    return sum(1 for ln in text.splitlines() if " amem-rsi-night" in ln or " amem-rsi-brief" in ln)


def checkpoint_rows(db_url: str) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select job, run_key, status, meta->>'next_checkpoint' as next_checkpoint from rsi_checkpoints "
                    "where job = 'rsi-night' order by run_key")
        return [dict(r) for r in cur.fetchall()]


LIVE_ENV_PASS = ("TYPESAFE_API_KEY",)        # the only caller env a live rehearsal passes in (Jev's key)


def live_config(first: Path, out: Path) -> Path:
    """The first-night config as is (paid stages): the paid rehearsal runs exactly what the first night would."""
    d = json.loads(first.read_bytes())
    d["_note"] = (f"PAID rehearsal config: {first.name} unchanged (sha256 "
                  f"{hashlib.sha256(first.read_bytes()).hexdigest()[:12]}), on the synthetic world.")
    out.write_text(json.dumps(d, indent=1) + "\n")
    return out


def run_rehearsal(work: Path, *, first: Path = FIRST_CONFIG, db_url: str | None = None, keep: bool = False,
                  agent_config: Path | None = None, live: bool = False) -> dict[str, Any]:
    """live=False: the $0 mock. live=True: the first-night config's paid stages on the same synthetic world (only
    with Vilmer's approval; main() requires --approve-max-usd). Unverified until the paid rehearsal runs."""
    from agent_memory import rsi_approval as AP, rsi_artifact as RA, rsi_night as N, rsi_schedule as S
    work = Path(work).expanduser().resolve()
    check_workdir(work)
    passed = {k: os.environ[k] for k in LIVE_ENV_PASS if live and os.environ.get(k)}
    fp_before = D.production_fingerprint()
    nights_before = D.listing(D.REAL_HOME / "sandbox" / "nights")
    real_lines_before = real_job_lines()
    t0 = time.monotonic()
    pg: D.DisposablePostgres | None = None
    out: dict[str, Any] = {"work": str(work)}
    try:
        if db_url is None:
            pg = D.DisposablePostgres(keep=keep).__enter__()
            admin = pg.url
        else:
            from agent_memory import synth
            synth.check_local(db_url)
            admin = db_url
        world = D.build_world(work / "world", agent_config=agent_config)
        world.db_url = D.fresh_database(admin)
        cfgp = (live_config if live else mock_config)(first, work / "rsi-night.rehearsal.json")
        log = work / "routines.log"
        reports = work / "reports"
        env = D.demo_env(world, hypotheses=D.BENCH / "hypotheses" / "success")
        if live:     # live Jev, and the sandbox items' presence check on the real Keychain (attributes only)
            env.update(passed, AMEM_JEV_LIVE="1", AMEM_RSI_SECURITY="/usr/bin/security",
                       AMEM_RSI_KEYCHAIN=str(D.REAL_HOME / "Library" / "Keychains" / "login.keychain-db"),
                       AMEM_RSI_MODEL_HOME=str(D.REAL_HOME))
        now = datetime.now().astimezone()
        night = AP.night_key(now)
        with D.environ(env):
            seeded = D.seed(world, env)
            ad = D.adapters(world.db_url, providers="synthetic", config_path=cfgp)
            log.write_text(routines_lines(night, now, daily=False))
            gate = S.run_night_job(config_path=cfgp, night=night, now=now, routines_log=log, db_url=world.db_url,
                                   adapters=ad)
            with log.open("a") as fh:
                fh.write(routines_lines(night, now, daily=True).splitlines()[-1] + "\n")
            job = S.run_night_job(config_path=cfgp, night=night, now=now, routines_log=log, db_url=world.db_url,
                                  adapters=ad)
            brief = S.run_brief_job(root=world.sandbox, night=night, now=now, routines_log=log, out_dir=reports,
                                    db_url=world.db_url)
            report_file = N.report_path(night)
        report = job.detail.get("report") or {}
        exp = (report.get("experiments") or [{}])[0]
        problems = None
        if exp.get("artifact_hash"):
            ep = D.agent_config_eval_problems()
            adir = world.sandbox / "artifacts" / exp["artifact_hash"]
            if ep is not None and adir.is_dir():
                m, _, _ = RA.load_artifact(adir)
                problems = ep(adir, m, ["static", "sandbox-replay"])
        checks = D.checks(world, report, fp_before)
        out.update(
            night=night, config=str(cfgp), seed={"rsi_demo": (seeded or {}).get("rsi_demo")},
            gate={"status": gate.status, "items": gate.items, "lines": gate.lines},
            night_job={"status": job.status, "items": job.items, "fields": job.fields, "lines": job.lines,
                       "report": str(report_file)},
            brief_job={"status": brief.status, "items": brief.items, "fields": brief.fields, "lines": brief.lines,
                       "path": brief.report},
            brief_markdown=brief.detail.get("markdown", ""),
            report={k: report.get(k) for k in ("status", "checkpoint", "delta", "candidates", "health", "costs",
                                               "pending_decisions", "notes")},
            experiment=exp, eval_problems=problems, checks=checks,
            max_usd=(json.loads(Path(cfgp).read_bytes()).get("budget") or {}).get("max_usd"),
            rehearsal_checkpoint=checkpoint_rows(world.db_url), routines_log=log.read_text().splitlines())
    finally:
        if pg is not None:
            pg.__exit__(None, None, None)
    fp_after = D.production_fingerprint()
    out["fingerprint"] = {"before": fp_before["sha256"], "after": fp_after["sha256"],
                          "diff": D.fingerprint_diff(fp_before, fp_after)}
    out["production_checkpoint"] = {
        "hosted_database": "never configured: AMEM_NO_DOTENV=1, the only database URL in the run's env was the "
                           f"throwaway one on {checks_db_host(out)}, and the night's checkpoint row went there",
        "real_sandbox_nights": {"before": nights_before, "after": D.listing(D.REAL_HOME / "sandbox" / "nights")},
        "real_routines_log_job_lines": {"before": real_lines_before, "after": real_job_lines()}}
    out["seconds"] = round(time.monotonic() - t0, 1)
    out["live"] = live
    out["problems"] = problems_of(out, live=live)
    out["ok"] = out["problems"] == []
    return out


def checks_db_host(out: Mapping[str, Any]) -> str:
    return str((out.get("checks") or {}).get("db_host"))


def problems_of(r: Mapping[str, Any], *, live: bool = False) -> list[str]:
    """What makes the rehearsal fail. The mock must stage IMPROVES with $0 and no Apply (stand-ins); a live
    rehearsal may end with any verdict, but every safety check below applies to both."""
    p: list[str] = []
    if (r.get("gate") or {}).get("status") != "SKIP":
        p.append(f"the gate did not skip without amem-daily: {r.get('gate')}")
    nj = r.get("night_job") or {}
    if not live and (nj.get("status") != "PASS" or (nj.get("fields") or {}).get("status") != "ok"):
        p.append(f"night job {nj.get('status')} {nj.get('items')}")
    if live and (nj.get("status") != "PASS" or (nj.get("fields") or {}).get("status") != "ok"):
        p.append(f"the live night did not run to the end: {nj.get('status')} {nj.get('items')}")
    exp = r.get("experiment") or {}
    if not live and (exp.get("verdict") != "IMPROVES" or exp.get("status") != "staged" or not exp.get("proposal_id")):
        p.append(f"experiment {exp.get('status')} {exp.get('verdict')} {exp.get('verdict_reasons')}")
    if live and exp.get("verdict") not in ("IMPROVES", "NO_EFFECT", "WORSE", "INCONCLUSIVE"):
        p.append(f"the live night ran no experiment to a verdict: {exp.get('status')} {exp.get('verdict')}")
    if live:
        spent = float(((r.get("report") or {}).get("costs") or {}).get("total_usd") or 0.0)
        cap = r.get("max_usd")
        if cap is not None and spent > float(cap):
            p.append(f"the live night spent ${spent:.4f}, over the approved ${float(cap):.2f}")
    if exp.get("status") == "staged" and r.get("eval_problems") not in ([], None):
        p.append(f"agent-config eval_problems: {r.get('eval_problems')}")
    bj = r.get("brief_job") or {}
    if bj.get("status") != "PASS":
        p.append(f"brief job {bj.get('status')} {bj.get('items')}")
    md = r.get("brief_markdown") or ""
    if not live and " apply --hash " in md:
        p.append("the brief offered Apply for a stand-in evaluation")
    if not live and "- Apply: not offered: " not in md:
        p.append("the brief does not say why Apply is not offered")
    fp = r.get("fingerprint") or {}
    if fp.get("before") != fp.get("after") or fp.get("diff"):
        p.append(f"production fingerprint changed: {fp.get('diff')}")
    pc = r.get("production_checkpoint") or {}
    for k in ("real_sandbox_nights", "real_routines_log_job_lines"):
        v = pc.get(k) or {}
        if v.get("before") != v.get("after"):
            p.append(f"{k} changed: {v}")
    if not live and (r.get("report") or {}).get("costs", {}).get("total_usd") not in (0, 0.0):
        p.append("the mock night booked a cost")
    if not live and not (r.get("rehearsal_checkpoint") or [{}])[0].get("status") == "done":
        p.append(f"the night's own checkpoint did not advance in the throwaway database: {r.get('rehearsal_checkpoint')}")
    c = r.get("checks") or {}
    for k in ("synthetic_production_unchanged", "scratch_agent_config_unchanged"):
        if c.get(k) is not True:
            p.append(f"{k} is {c.get(k)}")
    if c.get("keychain_tripwire_hits"):
        p.append("the Keychain tripwire was hit")
    if set(c.get("improve_calls") or []) - {"stage"}:
        p.append(f"improve was called with {c.get('improve_calls')}")
    if c.get("containers_left") or c.get("locks_left"):
        p.append(f"left behind: containers {c.get('containers_left')} locks {c.get('locks_left')}")
    if c.get("db_host") not in ("127.0.0.1", "localhost", "::1"):
        p.append(f"database host {c.get('db_host')} is not local")
    return p


def render(r: Mapping[str, Any]) -> str:
    bar = "=" * min(len(BANNER), 110)
    e = r.get("experiment") or {}
    arms = e.get("arms") or {}
    nj, bj, rep = r["night_job"], r["brief_job"], r["report"]
    sd = (r.get("seed") or {}).get("rsi_demo") or {}
    pc = r["production_checkpoint"]
    banner = BANNER if not r.get("live") else ("PAID REHEARSAL (Mission 005.5): synthetic data, the first-night "
                                              "config's live stages. Production is not touched.")
    out = [bar, banner, bar, "",
           f"night {r['night']}, work dir {r['work']}, {r['seconds']} s",
           f" 0. world      seeded {sd.get('dataset')}: {(sd.get('counts') or {}).get('sessions')} sessions, "
           f"{(sd.get('counts') or {}).get('deviations')} deviations; db host {r['checks'].get('db_host')}",
           f" 1. config     {r['config']} " + ("(the first-night config, live stages)" if r.get("live") else
                                                "(first-night limits, stand-in stages, max_usd 0)"),
           f"               costs: total ${float((rep.get('costs') or {}).get('total_usd') or 0):.4f}, provider-reported "
           f"${float((rep.get('costs') or {}).get('reported_usd') or 0):.4f}, estimated "
           f"${float((rep.get('costs') or {}).get('estimated_usd') or 0):.4f}",
           f" 2. gate       without amem-daily: {r['gate']['status']} ({'; '.join(r['gate']['items'])})",
           f"               with amem-daily PASS: {nj['status']}",
           f" 3. night      status {rep.get('status')}; candidates "
           + "; ".join(f"{c['key']} rank {c['rank']} {'SELECTED' if c['selected'] else 'deferred'}"
                       for c in rep.get("candidates") or []),
           f"               hypothesis {(e.get('hypothesis') or {}).get('what')!r} -> {e.get('target')}",
           f"               A/B: A check pass {(arms.get('A') or {}).get('check_pass_rate')} | B "
           f"{(arms.get('B') or {}).get('check_pass_rate')}; verdict {e.get('verdict')}: "
           f"{'; '.join(e.get('verdict_reasons') or [])}",
           f"               judge {e.get('judge')}; Jev {e.get('jev')}",
           f"               staged {e.get('status')}, proposal {e.get('proposal_id')}, artifact {e.get('artifact_hash')}",
           f"               checkpoint (throwaway DB): {r['rehearsal_checkpoint']}",
           f" 4. brief      {bj['status']} -> {bj['path']}",
           f" 5. checks     fingerprint before {r['fingerprint']['before']}",
           f"                           after  {r['fingerprint']['after']} (diff {r['fingerprint']['diff']})",
           f"               real ~/sandbox/nights listing unchanged "
           f"{pc['real_sandbox_nights']['before'] == pc['real_sandbox_nights']['after']}; real routines.log "
           f"night/brief lines {pc['real_routines_log_job_lines']['before']} -> {pc['real_routines_log_job_lines']['after']}",
           f"               hosted database: {pc['hosted_database']}",
           f"               Keychain tripwire hits {r['checks']['keychain_tripwire_hits']}; containers left "
           f"{len(r['checks']['containers_left'])}; locks left {r['checks']['locks_left']}; improve "
           f"{r['checks']['improve_calls']}",
           "", "routines.log (rehearsal copy):", *("  " + ln for ln in r["routines_log"]),
           "", "---- morning brief ----", r["brief_markdown"].rstrip(), "---- end of brief ----", "",
           ("REHEARSAL OK" if r["ok"] else "REHEARSAL FAILED: " + "; ".join(r["problems"]))]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    os.environ["AMEM_NO_DOTENV"] = "1"
    ap = argparse.ArgumentParser(prog="scripts/m0055_rehearsal.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--workdir", type=Path, default=None, help="default: a new temp dir (kept for inspection)")
    ap.add_argument("--config", type=Path, default=FIRST_CONFIG, help="the first-night config the rehearsal mirrors")
    ap.add_argument("--db-url", default=None, help="a local Postgres (default: a throwaway container)")
    ap.add_argument("--keep", action="store_true", help="keep the throwaway Postgres container")
    ap.add_argument("--paid-plan", action="store_true", help="print the live night's paid calls and estimate; run nothing")
    ap.add_argument("--live", action="store_true",
                    help="PAID: run the config's live stages on the synthetic world (needs --approve-max-usd)")
    ap.add_argument("--approve-max-usd", type=float, default=None,
                    help="the approved hard cap; must equal the config's budget.max_usd")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    from agent_memory import rsi_night as N, rsi_schedule as S
    if args.paid_plan or args.live:
        plan = S.paid_plan(N.load_config(args.config))
        print(json.dumps(plan, indent=1, sort_keys=True, default=str) if args.json else S.render_plan(plan), flush=True)
        if args.paid_plan:
            return 0
        if args.approve_max_usd is None or abs(args.approve_max_usd - plan["max_usd"]) > 1e-9:
            print(f"m0055 rehearsal: --live makes paid calls; it needs Vilmer's approval of this plan and "
                  f"--approve-max-usd {plan['max_usd']:g} (the config's max_usd). Nothing ran.", file=sys.stderr)
            return 2
    work = (args.workdir or Path(tempfile.mkdtemp(prefix="amem-m0055-rehearsal-"))).expanduser().resolve()
    try:
        res = run_rehearsal(work, first=args.config.expanduser().resolve(), db_url=args.db_url, keep=args.keep,
                            live=args.live)
    except (RehearsalError, D.DemoError) as exc:
        print(f"m0055 rehearsal: {exc}", file=sys.stderr)
        return 2
    (work / "rehearsal.json").write_text(json.dumps(res, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(res, indent=1, sort_keys=True, default=str) if args.json else render(res), flush=True)
    print(f"work dir: {work}", file=sys.stderr)
    return 0 if res["ok"] else 1
