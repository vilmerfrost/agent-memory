"""Mission 005.5: the unattended nightly RSI jobs (docs/rsi/night-ops.md). Prepared, never installed by code.

    amem rsi night-job   launchd com.vilmerfrost.amem-rsi-night (deploy/launchd/): gate, then `amem rsi night`
    amem rsi brief-job   launchd com.vilmerfrost.amem-rsi-brief: the morning brief to a report file
    amem rsi night-job --plan   the paid calls and the cost estimate the configured night would make (no run)

Night job, in order:
1. Completion checkpoints, not the clock: every prerequisite routine must have a PASS line in routines.log
   (`schedule.prerequisites` in the night config, default DEFAULT_PREREQS): amem-daily and amem-archive passed on
   the night's own date (the night key: 00:00-11:59 counts as the previous day), amem-ingest's latest line is a PASS
   younger than 25 h. Otherwise the night is skipped (`SKIP`, one `-fail` line per reason).
2. Credentials, checked for presence only, never read and never prompted for: the database URL (remote under
   AMEM_REQUIRE_REMOTE=1), live Jev (AMEM_JEV_LIVE=1 and TYPESAFE_API_KEY), the sandbox Keychain items the live
   stages name (`security find-generic-password` without -w reads attributes only: no dialog), the `claude` CLI
   for a live hypothesizer, and max_usd > 0 when any stage is paid. Missing => the night does not run: `FAIL` with
   a `credentials:` item, so routine-audit raises it.
3. `rsi_night.run_night` (its own lock refuses an overlapping night; its SIGALRM enforces wall_clock_s). A
   watchdog thread is the backstop: SIGTERM at wall_clock_s + HARD_GRACE_S (the night aborts cleanly, its
   experiment is marked interrupted and its hypotheses stay pending for the next night), then a FAIL line and
   exit 124 if the process is still alive KILL_GRACE_S later.
4. One routines.log summary line `amem-rsi-night PASS|FAIL|SKIP night=... status=... report=...` plus one
   `amem-rsi-night-fail <item>: <detail>` line per problem: the format routine-audit reads.

A skipped night writes a `preflight_failed` rsi-night/1 report (unless that night already has one), so the morning
brief says why nothing ran. Nothing here writes outside routines.log, the RSI sandbox and the brief's report dir.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_memory import config, redact, routines_log as RL, rsi_approval as AP, rsi_artifact as RA

NIGHT_ROUTINE = "amem-rsi-night"
BRIEF_ROUTINE = "amem-rsi-brief"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
HARD_GRACE_S = 600          # the watchdog's SIGTERM comes this long after the night's own wall clock
KILL_GRACE_S = 120          # then a hard exit if the night still has not finished
EXIT_HARD_TIMEOUT = 124
MAX_ITEMS = 8
SECURITY_ENV = "AMEM_ADMIN_SECURITY"        # credentials.SECURITY_ENV: the `security` executable (tests: a tripwire)
DEFAULT_REPORTS_DIR = Path.home() / ".claude" / "parent" / "reports"
PAID_STAGE_MODES = {"triage": ("live",), "hypothesizer": ("claude-cli",), "implementer": ("claude", "codex"),
                    "replay_agent": ("claude", "codex"), "judge": ("codex", "claude"), "replay_jev": ("live",)}

# Gate G2b (2026-10-17, ~/sandbox-dev/sandbox/replays/g2b-result.json): measured booked cost per call or run.
G2B_MEASURED_USD = {"claude-haiku-5-5": round(0.008122 / 6, 6),     # 6 agent runs, CLI-reported
                    "gpt-6-luna": round(0.005275 / 3, 6),           # 3 Codex judge calls, tokens x price table
                    "jev-1.14.0": 0.01}                             # 1 Jev call, flat table price


@dataclass(frozen=True)
class Prereq:
    routine: str
    same_night: bool = True             # a PASS dated on the night key's date
    max_age_h: float | None = None      # a PASS younger than this many hours


DEFAULT_PREREQS = (Prereq("amem-daily", same_night=True), Prereq("amem-archive", same_night=True),
                   Prereq("amem-ingest", same_night=False, max_age_h=25))


@dataclass
class JobResult:
    routine: str
    status: str                          # PASS | FAIL | SKIP
    night: str
    items: list[str] = field(default_factory=list)
    fields: dict[str, Any] = field(default_factory=dict)
    report: str | None = None
    lines: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "FAIL" else 0


# =========================================================================== config, routines.log


def routines_log_path(arg: Path | None = None) -> Path:
    """--routines-log, else AMEM_ROUTINES_LOG, else ~/.claude/parent/routines.log (as amem daily)."""
    from agent_memory import daily
    return daily.routines_log_path(arg)


def schedule_section(config_path: str | Path) -> dict[str, Any]:
    """The night config's optional `schedule` object (the night itself ignores it)."""
    try:
        d = json.loads(Path(config_path).read_bytes())
    except (OSError, ValueError):
        return {}
    s = d.get("schedule") if isinstance(d, dict) else None
    return {k: v for k, v in s.items() if not k.startswith("_")} if isinstance(s, dict) else {}


def prerequisites(sched: Mapping[str, Any]) -> tuple[Prereq, ...]:
    raw = sched.get("prerequisites")
    if raw is None:
        return DEFAULT_PREREQS
    out = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, dict) and isinstance(p.get("routine"), str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}",
                                                                                      p["routine"]):
            age = p.get("max_age_h")
            out.append(Prereq(p["routine"], bool(p.get("same_night", True)),
                              float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None))
        else:
            raise ValueError(f"schedule.prerequisites: bad entry {p!r}")
    return tuple(out)


def _night_date(night: str) -> date:
    return date.fromisoformat(night)


def check_prerequisites(log: Path, night: str, now: datetime, prereqs: Sequence[Prereq]) -> list[str]:
    """Reasons the night must not run yet (empty: every prerequisite completed). Reads routines.log only."""
    try:
        runs = RL.parse_file(log).runs
    except OSError as exc:
        return [f"routines.log: cannot read {log.name} ({type(exc).__name__})"]
    reasons = []
    for p in prereqs:
        mine = [r for r in runs if r.routine == p.routine and r.started_at <= now]
        if not mine:
            reasons.append(f"prerequisite {p.routine}: no run in routines.log")
            continue
        last = max(mine, key=lambda r: (r.started_at, r.line_no))
        ts = last.started_at.strftime(TS_FORMAT)
        if last.status != "pass":
            reasons.append(f"prerequisite {p.routine}: last run {last.meta.get('raw_status') or last.status.upper()} "
                           f"at {ts}")
            continue
        if p.same_night and last.started_at.date() != _night_date(night):
            reasons.append(f"prerequisite {p.routine}: no PASS on {night} (last PASS {ts})")
        if p.max_age_h is not None and now - last.started_at > timedelta(hours=p.max_age_h):
            reasons.append(f"prerequisite {p.routine}: last PASS {ts} is older than {p.max_age_h:g}h")
    return reasons


_WS = re.compile(r"\s+")


def _one_line(s: str, cap: int = 300) -> str:
    s = _WS.sub(" ", redact.redact(str(s))).strip()
    return s if len(s) <= cap else s[: cap - 3] + "..."


def _kv(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    return re.sub(r"[^A-Za-z0-9._:/+@-]", "_", str(v))[:80] or "-"


def log_lines(res: JobResult, *, now: datetime, secs: int) -> list[str]:
    """The house format (routines_log.py): one summary line, one `<routine>-fail <item>: <detail>` per item."""
    ts = now.strftime(TS_FORMAT)
    head = f"{ts} {res.routine} {res.status}"
    for k, v in res.fields.items():
        head += f" {k}={_kv(v)}"
    head += f" {secs}s"
    if res.report:
        head += f" report={res.report.replace(' ', '%20')}"
    return [head, *(f"{ts} {res.routine}-fail {_one_line(i)}" for i in res.items[:MAX_ITEMS])]


def append_log(path: Path, lines: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("".join(line + "\n" for line in lines))


# =========================================================================== credentials (presence only)


def keychain_present(ref: str, *, runner: Callable[..., Any] | None = None) -> str | None:
    """None if the reference resolves, else why not. keychain:<service>/<account>: attributes only (no -w, so
    no secret is read and no access dialog appears); env-file:<path>: the file exists."""
    if ref.startswith("env-file:"):
        return None if Path(ref[len("env-file:"):]).expanduser().is_file() else "env file missing"
    if not ref.startswith("keychain:"):
        return f"not a credential reference ({ref.split(':', 1)[0] or 'empty'})"
    service, sep, account = ref[len("keychain:"):].partition("/")
    if not sep or not service or not account:
        return "keychain reference is not keychain:<service>/<account>"
    exe = os.environ.get(SECURITY_ENV, "").strip() or "/usr/bin/security"
    run = runner or subprocess.run
    try:
        proc = run([exe, "find-generic-password", "-s", service, "-a", account], capture_output=True, timeout=10,
                   check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"keychain lookup failed ({type(exc).__name__})"
    if proc.returncode == 0:
        return None
    return "keychain item not found" if proc.returncode == 44 else f"keychain lookup exit {proc.returncode}"


def paid_stages(cfg: Any) -> list[str]:
    return sorted(k for k, v in cfg.stages.items() if v in PAID_STAGE_MODES.get(k, ()))


def credential_problems(cfg: Any, *, runner: Callable[..., Any] | None = None) -> list[str]:
    """`credentials: ...` items for everything the configured night needs and does not have. Presence only."""
    out = []
    if not os.environ.get("AMEM_DB_URL", "").strip():
        out.append("credentials database: AMEM_DB_URL is not set")
    else:
        err = config.remote_write_error(for_backend="postgres")
        if err:
            out.append("credentials database: " + err.replace("amem: ", ""))
    st = cfg.stages
    if "live" in (st.get("triage"), st.get("replay_jev")):
        if not config.jev_live_flag():
            out.append("credentials jev: live Jev is configured but AMEM_JEV_LIVE is not 1")
        if not config.typesafe_api_key():
            out.append("credentials jev: TYPESAFE_API_KEY is not set")
    refs = []                    # item names avoid "<name>_credentials: " (redact.py would mask the reason)
    if st.get("replay_agent") in ("claude", "codex"):
        refs.append(("replay agent (replay.credentials)", cfg.replay.get("credentials") or "none"))
    if st.get("judge") in ("codex", "claude"):
        refs.append(("judge (replay.judge_credentials)", cfg.replay.get("judge_credentials") or "none"))
    if st.get("implementer") in ("claude", "codex"):
        refs.append(("implementer (implementer.credentials)", cfg.implementer.get("credentials") or "none"))
    for name, ref in refs:
        why = keychain_present(str(ref), runner=runner) if ref != "none" else "none configured for a live stage"
        if why:
            out.append(f"credentials {name}: {why}")
    if st.get("hypothesizer") == "claude-cli":
        exe = os.environ.get("AMEM_EDITS_CLAUDE", "").strip() or "claude"
        if not shutil.which(exe):
            out.append("credentials hypothesizer: the claude CLI is not on PATH")
    paid = paid_stages(cfg)
    if paid and cfg.budget.max_usd <= 0:
        out.append(f"budget max_usd: 0 while paid stages are configured ({', '.join(paid)})")
    try:
        from agent_memory import rsi_budget
        rsi_budget.load_prices()
    except Exception as exc:  # noqa: BLE001 - PriceConfigError and friends: a paid night needs the table
        out.append(f"budget prices: {_one_line(exc, 120)}")
    return out


# =========================================================================== the paid plan (no call is made)


def paid_plan(cfg: Any, *, cases: int | None = None) -> dict[str, Any]:
    """Every paid call a night with `cfg` can make, with the G2b-measured expected cost and the worst case the
    budget allows (pre-call estimates and per-run caps). Nothing runs."""
    from agent_memory import rsi_budget, rsi_replay
    prices = rsi_budget.load_prices()
    b = cfg.budget
    st, rp = cfg.stages, cfg.replay
    n_cases = cases if cases is not None else len(rsi_replay.load_cases(rp.get("cases")))
    reps = int(rp.get("reps") or 1)
    pairs = n_cases * reps
    exps = b.max_experiments
    rows: list[dict[str, Any]] = []

    def per_call_cap(model: str) -> float:
        est = b.estimate(model)
        return est if est > 0 else prices.estimate(model)

    def add(stage: str, model: str, count: int, *, cap: float | None = None, note: str = "") -> None:
        if count <= 0:
            return
        call_cap = b.call_cap(model) if stage not in ("replay-agent",) else None
        if call_cap is not None:
            count = min(count, call_cap)
        expected = G2B_MEASURED_USD.get(model)
        worst = cap if cap is not None else per_call_cap(model)
        rows.append({"stage": stage, "model": model, "count": count, "budget_call_cap": call_cap,
                     "expected_usd_each": expected, "worst_usd_each": round(worst, 6),
                     "expected_basis": "G2b measured" if expected is not None else "no measurement (worst case only)",
                     "expected_usd": round(count * expected, 6) if expected is not None else None,
                     "worst_usd": round(count * worst, 6), "note": note})

    jev_model = "jev-1.14.0"
    if st["triage"] == "live":
        add("triage", jev_model, int(cfg.candidates["max_considered"]), note="one Jev call per candidate considered")
    if st["hypothesizer"] == "claude-cli":
        from agent_memory import edits_writer
        add("hypothesize", str(cfg.hypothesizer.get("model") or edits_writer.writer_model()), exps,
            note="host `claude -p`, no tools, hooks off; pending hypotheses from an earlier night need no call")
    if st["implementer"] in ("claude", "codex"):
        add("implement", str(cfg.implementer.get("model") or st["implementer"]), exps, note="sandbox arm I")
    if st["replay_agent"] in ("claude", "codex"):
        model = str(rp.get("model") or st["replay_agent"])
        cap = float(rp.get("max_budget_usd")) if st["replay_agent"] == "claude" and rp.get("max_budget_usd") else None
        add("replay-agent", model, 2 * pairs * exps, cap=cap,
            note=f"{pairs} A/B pair(s) x 2 arms per experiment; checks run free in their own containers")
    if st["judge"] in ("codex", "claude"):
        add("replay-judge", str(rp.get("judge_model") or st["judge"]), pairs * exps, note="one blind judge call per pair")
    if st["replay_jev"] == "live":
        add("replay-jev", jev_model, exps, note="one Jev call per replay")
    expected = [r["expected_usd"] for r in rows]
    exp_total = round(sum(x for x in expected if x is not None), 6)
    worst_total = round(sum(r["worst_usd"] for r in rows), 6)
    return {"config_sha256": cfg.sha256, "paid_stages": paid_stages(cfg), "cases": n_cases, "reps": reps,
            "ab_pairs_per_experiment": pairs, "max_ab_pairs": b.max_ab_pairs, "max_experiments": exps,
            "max_hypotheses": min(b.max_hypotheses, rsi_budget.HARD_MAX_HYPOTHESES), "model_retries": b.model_retries,
            "wall_clock_s": b.wall_clock_s, "max_usd": b.max_usd, "calls": rows,
            "expected_usd": exp_total, "expected_incomplete": any(x is None for x in expected),
            "worst_case_usd": worst_total, "budget_stops_at_usd": b.max_usd,
            "within_cap": worst_total <= b.max_usd + 1e-9, "prices": prices.describe(),
            "basis_note": "expected = G2b measured per call (Haiku CLI-reported, Codex tokens x price table, Jev flat); "
                          "worst = the budget's pre-call estimate or the per-run CLI cap. The budget refuses a call "
                          "whose estimate would pass max_usd and stops after any call that crossed it, so a night can "
                          "pass max_usd by at most one call's real cost (Claude runs are capped by the CLI's "
                          "--max-budget-usd; the Codex judge has no per-call cap: its sandbox key is spend-capped)."}


def render_plan(plan: Mapping[str, Any]) -> str:
    out = [f"Paid plan for config {plan['config_sha256'][:12]} (prices {plan['prices']['version']}): "
           f"{plan['max_experiments']} experiment(s), <= {plan['max_hypotheses']} hypotheses, "
           f"{plan['ab_pairs_per_experiment']} A/B pair(s) ({plan['cases']} case(s) x {plan['reps']} rep(s), cap "
           f"{plan['max_ab_pairs']}), retries {plan['model_retries']}, wall clock {plan['wall_clock_s']:g}s."]
    if not plan["calls"]:
        out.append("No paid call: every stage is a stub or a fixture. Expected $0.")
    for r in plan["calls"]:
        exp = f"${r['expected_usd']:.4f}" if r["expected_usd"] is not None else "unmeasured"
        out.append(f"- {r['stage']}: {r['count']} x {r['model']} -> expected {exp} ({r['expected_basis']}), "
                   f"worst ${r['worst_usd']:.4f}. {r['note']}")
    out.append(f"Total: expected ${plan['expected_usd']:.4f}" + (" + unmeasured calls" if plan["expected_incomplete"] else "")
               + f", worst case ${plan['worst_case_usd']:.4f}, hard cap max_usd ${plan['max_usd']:.2f} "
               + ("(the worst case fits)" if plan["within_cap"] else "(the budget stops the night at the cap)") + ".")
    out.append(plan["basis_note"])
    return "\n".join(out)


# =========================================================================== the night job


def skip_report(night: str, reasons: Sequence[str], *, now: datetime) -> dict[str, Any]:
    """A valid rsi-night/1 report for a night that did not run: status preflight_failed, the reasons as notes."""
    from agent_memory import rsi_night as N
    iso = now.isoformat(timespec="seconds")
    return {"format": N.FORMAT, "night": night, "started_at": iso, "finished_at": iso, "status": "preflight_failed",
            "checkpoint": {"before": None, "after": None, "advanced": False},
            "delta": {"since": None, "sessions": 0, "deviations": 0, "eval_failures": 0, "routine_failures": 0},
            "patterns": [], "candidates": [], "experiments": [],
            "costs": {"by_model": {}, "sandbox_runs": 0, "providers": {}, "total_usd": 0.0},
            "health": {k: "skipped" for k in N.HEALTH_KEYS}, "pending_decisions": [],
            "notes": [f"skipped by {NIGHT_ROUTINE}: {_one_line(r, 260)}" for r in reasons][:30]}


def _write_skip_report(night: str, reasons: Sequence[str], now: datetime) -> str | None:
    from agent_memory import rsi_night as N
    path = N.report_path(night)
    if path.exists():                    # a night that already ran keeps its own report
        return None
    try:
        N._write_json(path, skip_report(night, reasons, now=now))
    except (OSError, RuntimeError):
        return None
    return str(path)


def classify(rep: Mapping[str, Any]) -> tuple[str, list[str]]:
    """(routines.log status, items) for a finished night report."""
    status, notes = rep.get("status"), [str(n) for n in rep.get("notes") or []]
    if status == "ok":
        return "PASS", []
    if status == "preflight_failed":
        for n in notes:
            if n.startswith("preflight: lock:"):
                return "SKIP", ["overlap: another night holds the rsi-night lock"]
            if "already done" in n:
                return "SKIP", [f"already done: night {rep.get('night')} finished earlier"]
        return "FAIL", [f"preflight: {n.split('preflight: ', 1)[-1]}" for n in notes[-3:]] or ["preflight: failed"]
    items = []
    if status == "budget_exhausted":
        items += [f"budget: {n.split('budget exhausted: ', 1)[1]}" for n in notes if n.startswith("budget exhausted: ")]
    if status == "aborted":
        items += [f"aborted: {n}" for n in notes if n.startswith("interrupted by")] or ["aborted: interrupted"]
    for k, v in sorted((rep.get("health") or {}).items()):
        if v in ("error", "unavailable", "fixture-missing", "stale", "truncated"):
            items.append(f"health {k}: {v}")
    for e in rep.get("experiments") or []:
        if e.get("status") in ("failed", "interrupted"):
            why = (e.get("verdict_reasons") or [e.get("status")])[0]
            items.append(f"experiment {e.get('experiment_id') or 'not started'}: {why}")
    items += [f"night: {n}" for n in notes if n.startswith("error:")]
    return "FAIL", items or [f"night: status {status}"]


class Watchdog:
    """The backstop above the night's own SIGALRM: SIGTERM after `term_s`, then `on_kill()` and a hard exit."""

    def __init__(self, term_s: float, kill_after_s: float, on_kill: Callable[[], None],
                 exit: Callable[[int], None] = os._exit) -> None:
        self.term_s, self.kill_after_s, self.on_kill, self._exit = term_s, kill_after_s, on_kill, exit
        self._done = threading.Event()
        self._t = threading.Thread(target=self._run, name="rsi-night-watchdog", daemon=True)
        self.fired: str | None = None

    def _run(self) -> None:
        if self._done.wait(self.term_s):
            return
        self.fired = "term"
        os.kill(os.getpid(), signal.SIGTERM)
        if self._done.wait(self.kill_after_s):
            return
        self.fired = "kill"
        try:
            self.on_kill()
        finally:
            self._exit(EXIT_HARD_TIMEOUT)

    def __enter__(self) -> Watchdog:
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._done.set()


def run_night_job(*, config_path: Path | None = None, night: str | None = None, now: datetime | None = None,
                  routines_log: Path | None = None, db_url: str | None = None, adapters: Any = None,
                  runner: Callable[..., Any] | None = None, watchdog: bool = True,
                  write_log: bool = True) -> JobResult:
    """The scheduled night (module docstring). `now` (tests, rehearsals) fixes the gate's clock and the log
    line's timestamp; by default both are the real time."""
    from agent_memory import rsi_night as N
    t0 = time.monotonic()
    fixed = now is not None
    now = (now or datetime.now()).astimezone()
    night = night or AP.night_key(now)
    log = routines_log_path(routines_log)
    res = JobResult(NIGHT_ROUTINE, "PASS", night, fields={"night": night})

    def finish() -> JobResult:
        res.lines = log_lines(res, now=now if fixed else datetime.now().astimezone(), secs=int(time.monotonic() - t0))
        if write_log:
            append_log(log, res.lines)
        return res

    try:
        cfg = N.load_config(config_path)
        prereqs = prerequisites(schedule_section(cfg.path))
    except (N.ConfigError, ValueError) as exc:
        res.status, res.items = "FAIL", [f"config: {_one_line(exc, 200)}"]
        return finish()
    res.fields["config"] = cfg.sha256[:12]
    gate = check_prerequisites(log, night, now, prereqs)
    if gate:
        res.status, res.items = "SKIP", gate
        res.fields["skipped"] = "prerequisites"
        res.report = _write_skip_report(night, gate, now)
        return finish()
    creds = credential_problems(cfg, runner=runner)
    if creds:
        res.status, res.items = "FAIL", creds
        res.fields["skipped"] = "credentials"
        res.report = _write_skip_report(night, creds, now)
        return finish()

    def killed() -> None:
        res.status, res.items = "FAIL", [f"hard timeout: the night did not stop {HARD_GRACE_S + KILL_GRACE_S}s after "
                                         f"its wall clock ({cfg.budget.wall_clock_s:g}s); process exited"]
        finish()

    guard = Watchdog(cfg.budget.wall_clock_s + HARD_GRACE_S, KILL_GRACE_S, killed) if watchdog else None
    if guard:
        guard.__enter__()
    try:
        rep = N.run_night(night=night, config_path=config_path, db_url=db_url, adapters=adapters)
    finally:
        if guard:
            guard.__exit__(None, None, None)
    res.status, res.items = classify(rep)
    staged = [e for e in rep.get("experiments") or [] if e.get("status") == "staged"]
    costs = rep.get("costs") or {}
    res.fields.update(status=rep.get("status"), candidates=len(rep.get("candidates") or []),
                      experiments=len(rep.get("experiments") or []), staged=len(staged),
                      usd=float(costs.get("total_usd") or 0.0),
                      reported_usd=float(costs.get("reported_usd") or 0.0),
                      estimated_usd=float(costs.get("estimated_usd") or 0.0),
                      checkpoint="advanced" if (rep.get("checkpoint") or {}).get("advanced") else "kept")
    if guard and guard.fired:
        res.items.insert(0, f"hard timeout: watchdog sent SIGTERM after {cfg.budget.wall_clock_s + HARD_GRACE_S:g}s")
        res.status = "FAIL"
    path = N.report_path(night)
    res.report = str(path) if path.is_file() and res.status != "SKIP" else None
    res.detail["report"] = rep
    return finish()


# =========================================================================== the brief job


def night_running(root: Path | None = None) -> bool:
    from agent_memory import rsi_night as N
    lock = (root or RA.sandbox_dir()) / "run" / f"{N.LOCK_NAME}.lock"
    if not lock.exists():
        return False
    return not N.sandbox_module().Sandbox._lock_stale(lock)


def wait_for_night(night: str, root: Path | None, wait_s: float, *, poll_s: float = 60.0,
                   sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> float:
    """Completion checkpoint for the brief: wait (up to wait_s) while the night holds its lock or has no report
    yet, so a brief that launchd starts at wake together with a delayed night waits for it. Returns the wait."""
    start = clock()
    report = (root or RA.sandbox_dir()) / "nights" / night / "report.json"
    while clock() - start < wait_s and (night_running(root) or not report.is_file()):
        sleep(min(poll_s, max(0.0, wait_s - (clock() - start))))
    return clock() - start


def run_brief_job(*, root: Path | None = None, night: str | None = None, now: datetime | None = None,
                  routines_log: Path | None = None, out_dir: Path | None = None, db_url: str | None = None,
                  write_log: bool = True, wait_s: float = 0.0, poll_s: float = 60.0,
                  sleep: Callable[[float], None] = time.sleep) -> JobResult:
    """The morning brief for the night key (default: the night that just ended) as markdown + JSON files in
    `out_dir` (default ~/.claude/parent/reports). Waits up to `wait_s` for the night to finish (its lock released
    and its report written). Read-only towards the sandbox and the database. Never pushes."""
    from agent_memory import rsi_brief as BR
    t0 = time.monotonic()
    fixed = now is not None
    now = (now or datetime.now()).astimezone()
    night = night or AP.night_key(now)
    log = routines_log_path(routines_log)
    res = JobResult(BRIEF_ROUTINE, "PASS", night, fields={"night": night})

    def finish() -> JobResult:
        res.lines = log_lines(res, now=now if fixed else datetime.now().astimezone(), secs=int(time.monotonic() - t0))
        if write_log:
            append_log(log, res.lines)
        return res

    if wait_s > 0:
        res.fields["waited_s"] = int(wait_for_night(night, root, wait_s, poll_s=poll_s, sleep=sleep))
    if night_running(root):
        res.status, res.items = "FAIL", [f"night running: the rsi-night lock is held; no brief for {night} yet"]
        return finish()
    try:
        b = BR.make_brief(night=night, root=root, db_url=db_url)
    except BR.ReportError as exc:
        res.status, res.items = "FAIL", [f"report: {_one_line(exc, 240)}"]
        return finish()
    md = BR.render_markdown(b)
    out = Path(out_dir or DEFAULT_REPORTS_DIR).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    mdp, jsp = out / f"rsi-brief-{night}.md", out / f"rsi-brief-{night}.json"
    mdp.write_text(md, encoding="utf-8")
    jsp.write_text(json.dumps(b, sort_keys=True, default=str, indent=1) + "\n", encoding="utf-8")
    ready = sum(1 for x in b["passed"] if x["actions"].get("apply"))
    res.fields.update(night_status=b["status"], ready=ready, staged_not_ready=len(b["passed"]) - ready,
                      failed=len(b["failed"]), pending=b["pending_decisions"]["total"],
                      apply=b["pending_decisions"]["with_apply"], usd=float(b["costs"]["total_usd"] or 0.0),
                      db=b["health"]["database"]["status"])
    if b["health"]["database"]["status"] != "ok":
        res.items.append(f"database: rows not read ({b['health']['database']['detail'] or 'unavailable'})")
        res.status = "FAIL"
    res.report = str(mdp)
    res.detail.update(brief=b, markdown=md, json_path=str(jsp))
    return finish()


# =========================================================================== CLI


def _now_arg(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.astimezone()


def cmd_night_job(args: argparse.Namespace) -> int:
    from agent_memory import rsi_night as N
    if args.plan:
        try:
            cfg = N.load_config(args.config)
            plan = paid_plan(cfg)
        except Exception as exc:  # noqa: BLE001 - ConfigError, PriceConfigError, a bad case list
            print(f"amem: rsi night-job --plan: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(plan, sort_keys=True, default=str) if args.json else render_plan(plan))
        return 0
    res = run_night_job(config_path=args.config, night=args.night, now=_now_arg(args.now),
                        routines_log=args.routines_log)
    if args.json:
        print(json.dumps({"status": res.status, "night": res.night, "items": res.items, "fields": res.fields,
                          "report": res.report, "lines": res.lines}, sort_keys=True, default=str))
    else:
        print("\n".join(res.lines))
    return res.exit_code


def cmd_brief_job(args: argparse.Namespace) -> int:
    res = run_brief_job(root=args.root.expanduser() if args.root else None, night=args.night, now=_now_arg(args.now),
                        routines_log=args.routines_log, out_dir=args.out, wait_s=args.wait_s)
    if args.json:
        print(json.dumps({"status": res.status, "night": res.night, "items": res.items, "fields": res.fields,
                          "report": res.report, "lines": res.lines}, sort_keys=True, default=str))
    else:
        print("\n".join(res.lines))
    return res.exit_code


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("night-job", help="the scheduled night: completion checkpoints, credentials, then `rsi night`; "
                                         "one routines.log line (launchd; prepared, not installed)")
    p.add_argument("--config", type=Path, default=None, help="night config (default: config/rsi-night.json)")
    p.add_argument("--night", default=None, help="night key YYYY-MM-DD (default: the current night)")
    p.add_argument("--now", default=None, help=argparse.SUPPRESS)
    p.add_argument("--routines-log", type=Path, default=None, help="default AMEM_ROUTINES_LOG or ~/.claude/parent/routines.log")
    p.add_argument("--plan", action="store_true", help="print the paid calls and the cost estimate; run nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_night_job)
    p = sub.add_parser("brief-job", help="the scheduled morning brief: markdown + JSON in the reports dir, one "
                                         "routines.log line; never pushes")
    p.add_argument("--root", type=Path, default=None, help="the RSI sandbox root (default RSI_SANDBOX_DIR or ~/sandbox)")
    p.add_argument("--night", default=None)
    p.add_argument("--now", default=None, help=argparse.SUPPRESS)
    p.add_argument("--routines-log", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None, help="report dir (default ~/.claude/parent/reports)")
    p.add_argument("--wait-s", type=float, default=0.0,
                   help="wait up to this long for the night to finish (lock released, report written)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_brief_job)
