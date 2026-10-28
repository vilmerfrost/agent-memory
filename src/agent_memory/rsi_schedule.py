"""Mission 005.5: the unattended nightly RSI jobs (docs/rsi/night-ops.md). Prepared, never installed by code.

    amem rsi night-job   launchd com.vilmerfrost.amem-rsi-night (deploy/launchd/): gate, then `amem rsi night`
    amem rsi brief-job   launchd com.vilmerfrost.amem-rsi-brief: the morning brief to a report file
    amem rsi night-job --plan   the paid calls and the cost estimate the configured night would make (no run)

Night job, in order:
1. Completion checkpoints, not the clock: every prerequisite routine must have a PASS line in routines.log
   (`schedule.prerequisites` in the night config, default DEFAULT_PREREQS): amem-daily passed for the night (its
   00:40 run after the evening carries the night's key: 00:00-11:59 counts as the previous day), amem-archive passed
   on the night's own date (08:45 that day), amem-ingest's latest line is a PASS younger than 25 h. While amem-daily
   still runs (its running marker next to routines.log), the gate waits for it, up to 30 minutes, then skips with
   "still running". Otherwise the night is skipped (`SKIP`, one `-fail` line per reason). A FAIL whose every
   `<routine>-fail <step>` line names a step in the prerequisite's `nonblocking_steps` (amem-daily default:
   DAILY_NONBLOCKING_STEPS, the steps the night does not read) counts as completed: the night runs and carries one
   warning per failed step (`-warn` lines, the report's `prerequisite_warnings`, the brief).
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

A skipped night writes a `preflight_failed` rsi-night/1 report with a `skipped` object (reason prerequisites or
credentials, a summary, the items) unless that night already has one, so the morning brief names the real cause and
says "not analyzed" instead of zero counts. Nothing here writes outside routines.log, the RSI sandbox and the brief's report dir.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config, redact, routines_log as RL, rsi_approval as AP, rsi_artifact as RA

NIGHT_ROUTINE = "amem-rsi-night"
BRIEF_ROUTINE = "amem-rsi-brief"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
HARD_GRACE_S = 600          # the watchdog's SIGTERM comes this long after the night's own wall clock
KILL_GRACE_S = 120          # then a hard exit if the night still has not finished
EXIT_HARD_TIMEOUT = 124
MAX_ITEMS = 8
RSI_SECURITY_ENV = "AMEM_RSI_SECURITY"     # the `security` used for the sandbox items' presence check, if set
SECURITY_ENV = "AMEM_ADMIN_SECURITY"        # else credentials.SECURITY_ENV (tests and demos: a tripwire)
DEFAULT_REPORTS_DIR = Path.home() / ".claude" / "parent" / "reports"
PAID_STAGE_MODES = {"triage": ("live",), "hypothesizer": ("claude-cli",), "implementer": ("writer", "claude", "codex"),
                    "replay_agent": ("claude", "codex"), "judge": ("codex", "claude"), "replay_jev": ("live",)}

# Gate G2b (2026-10-17, ~/sandbox-dev/sandbox/replays/g2b-result.json): measured booked cost per call or run.
G2B_MEASURED_USD = {"claude-haiku-5-5": round(0.008122 / 6, 6),     # 6 agent runs, CLI-reported
                    "gpt-6-luna": round(0.005275 / 3, 6),           # 3 Codex judge calls, tokens x price table
                    "jev-1.14.0": 0.01}                             # 1 Jev call, flat table price

# The paid plan's `room`: what one experiment can take at most, from the code's own limits. Model stages count at
# their full timeout (hypothesizer and staging timeout_s, each case's timeout_s capped by rsi_replay's container
# limit, rsi_replay.JUDGE_TIMEOUT_S). The allowances below cover the steps that wait on no model.
CHECK_RUN_S = 10            # a hidden check is a deterministic script: < 1 s per run on night 2026-10-24 (cap 300 s)
JEV_CALL_S = 30             # one Jev call: the TypeSafe SDK's 10 s per HTTP operation, a few operations
NIGHT_OVERHEAD_S = 600      # once per night: delta, pool, triage, evidence packs (all of it took 37 s on 2026-10-24)
# Observed subscription tokens one experiment may use: the stop approved for one experiment (2026-10-25). The
# qualification replays used about 163k for both providers together (docs/rsi/subscription-migration.md).
SUBSCRIPTION_TOKENS_PER_EXPERIMENT = 2_000_000


@dataclass(frozen=True)
class Prereq:
    routine: str
    same_night: bool = True             # a PASS dated on the night key's date
    max_age_h: float | None = None      # a PASS younger than this many hours
    # Steps whose failure does not block the night: a FAIL run whose every `<routine>-fail <step>...` line names one
    # of these counts as completed, and each failed step becomes a warning in the night report. Any other step, a
    # step name not listed here, or a FAIL with no step line still blocks (fail closed).
    nonblocking_steps: tuple[str, ...] = ()
    # Which run is "the night's own" (same_night): "date" = dated on the night key's calendar date (amem-archive at
    # 08:45 that day); "night_key" = a run whose own night key is the night (amem-daily at 00:40 after the evening:
    # between the night's date 12:00 and the next day 11:59).
    match: str = "date"
    # While the routine's running marker is live (routines_log.running), the night waits up to this long for its
    # line, then skips with "still running". 0: never wait.
    wait_s: float = 0.0


MATCHES = ("date", "night_key")
# amem-daily runs at 00:40, after the evening it belongs to, so its line carries the night's key, and the 01:30
# night waits up to 30 minutes while it still runs (a long deviations scan).
DAILY_WAIT_S = 1800.0
DEFAULT_MATCH = {"amem-daily": "night_key"}
DEFAULT_WAIT_S = {"amem-daily": DAILY_WAIT_S}


# amem-daily steps (daily.py) the night does not read. The night's S1 delta (rsi_evidence_pack.DELTA_SQL) reads
# sessions + deviations (the `deviations` step) and runs (the `ingest` step: cloud routines; the `tasks` step:
# dispatch runs), so those three block. `score` (scores) and `edits` (edits) only enrich S4 evidence packs, which
# are re-read live every night; distill (lessons), escalations (a phone push), rsi-push (a phone push) and state
# (the daily state file) feed nothing the night reads. docs/rsi/night-ops.md has the table.
DAILY_NONBLOCKING_STEPS = ("score", "distill", "escalations", "edits", "rsi-push", "state")
DEFAULT_NONBLOCKING = {"amem-daily": DAILY_NONBLOCKING_STEPS}
DEFAULT_PREREQS = (Prereq("amem-daily", same_night=True, nonblocking_steps=DAILY_NONBLOCKING_STEPS,
                          match="night_key", wait_s=DAILY_WAIT_S),
                   Prereq("amem-archive", same_night=True), Prereq("amem-ingest", same_night=False, max_age_h=25))
_STEP_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_STEP_AT = re.compile(r"([a-z][a-z0-9_-]{0,31})(?::|\s|$)")


@dataclass
class JobResult:
    routine: str
    status: str                          # PASS | FAIL | SKIP
    night: str
    items: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # `<routine>-warn` lines; they never change the status
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
            steps = p.get("nonblocking_steps", DEFAULT_NONBLOCKING.get(p["routine"], ()))
            if not isinstance(steps, (list, tuple)) or not all(isinstance(s, str) and _STEP_RE.fullmatch(s)
                                                                for s in steps):
                raise ValueError(f"schedule.prerequisites: bad nonblocking_steps in {p!r}")
            match = p.get("match", DEFAULT_MATCH.get(p["routine"], "date"))
            if match not in MATCHES:
                raise ValueError(f"schedule.prerequisites: bad match in {p!r} (one of {', '.join(MATCHES)})")
            wait = p.get("wait_running_s", DEFAULT_WAIT_S.get(p["routine"], 0.0))
            if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 4 * 3600:
                raise ValueError(f"schedule.prerequisites: bad wait_running_s in {p!r} (0..14400 seconds)")
            out.append(Prereq(p["routine"], bool(p.get("same_night", True)),
                              float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None,
                              tuple(steps), match, float(wait)))
        else:
            raise ValueError(f"schedule.prerequisites: bad entry {p!r}")
    return tuple(out)


def _night_date(night: str) -> date:
    return date.fromisoformat(night)


def failed_step(detail: str) -> str | None:
    """The step a `<routine>-fail` detail names: the word before the first `:` or space (`score dispatch: ...` is
    the score step). None when the detail does not start with a step-shaped word."""
    m = _STEP_AT.match(detail)
    return m.group(1) if m else None


@dataclass
class Gate:
    """The prerequisite check. `reasons` block the night (one routines.log item each, `prerequisite <routine>: ...`),
    `causes` say the same in headline words (one per reason), `warnings` are failed steps the night does not read."""
    reasons: list[str] = field(default_factory=list)
    causes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    routines: list[str | None] = field(default_factory=list)   # the prerequisite each reason is about
    waited_s: float = 0.0                                       # how long the night waited for a running routine

    def block(self, reason: str, cause: str, routine: str | None = None) -> None:
        self.reasons.append(reason)
        self.causes.append(cause)
        self.routines.append(routine)

    def drop(self, routine: str) -> None:
        keep = [i for i, r in enumerate(self.routines) if r != routine]
        self.reasons = [self.reasons[i] for i in keep]
        self.causes = [self.causes[i] for i in keep]
        self.routines = [self.routines[i] for i in keep]

    def summary(self) -> str:
        if not self.causes:
            return ""
        more = len(self.causes) - 1
        return self.causes[0] + (f"; and {more} more prerequisite problem{'s' if more != 1 else ''} (see the notes)"
                                 if more else "")


def evaluate_prerequisites(log: Path, night: str, now: datetime, prereqs: Sequence[Prereq]) -> Gate:
    """Whether the night may run (no reasons) and which tolerated step failures it carries as warnings. Reads
    routines.log only. A FAIL run of a prerequisite blocks unless every failed step it names is in that prerequisite's
    `nonblocking_steps`; then the run counts as completed (its date and age rules still apply)."""
    g = Gate()
    try:
        runs = RL.parse_file(log).runs
    except OSError as exc:
        g.block(f"routines.log: cannot read {log.name} ({type(exc).__name__})",
                f"routines.log could not be read ({type(exc).__name__})")
        return g
    for p in prereqs:
        r = p.routine
        mine = [x for x in runs if x.routine == r and x.started_at <= now]
        if not mine:
            g.block(f"prerequisite {r}: no run in routines.log", f"prerequisite {r} has no run in routines.log", r)
            continue
        last = max(mine, key=lambda x: (x.started_at, x.line_no))
        ts = last.started_at.strftime(TS_FORMAT)
        word = last.meta.get("raw_status") or last.status.upper()
        tolerated = False
        if last.status != "pass":
            fails = [_one_line(f, 260) for f in last.meta.get("failures") or []]
            blocking = [f for f in fails if failed_step(f) not in p.nonblocking_steps]
            g.warnings += [f"prerequisite {r}: step {failed_step(f)} failed at {ts}; the night does not read it: {f}"
                           for f in fails if f not in blocking]
            tolerated = (last.status == "fail" and bool(fails) and not blocking
                         and not last.meta.get("orphan_detail"))
            if not tolerated:
                shown = blocking or fails
                g.block(f"prerequisite {r}: last run {word} at {ts}" + (f" ({'; '.join(shown)})" if shown else ""),
                        f"prerequisite {r} failed (" + ("; ".join(shown) if shown
                                                        else f"last run {word} at {ts}, no failed step named") + ")", r)
                continue
        done = "PASS" if not tolerated else "completed run"
        if p.same_night and p.match == "night_key" and run_night_key(last.started_at) != night:
            g.block(f"prerequisite {r}: no PASS for night {night} (last {done} {ts})",
                    f"prerequisite {r} did not complete for night {night} (last {done} {ts})", r)
        elif p.same_night and p.match != "night_key" and last.started_at.date() != _night_date(night):
            g.block(f"prerequisite {r}: no PASS on {night} (last {done} {ts})",
                    f"prerequisite {r} did not complete on {night} (last {done} {ts})", r)
        if p.max_age_h is not None and now - last.started_at > timedelta(hours=p.max_age_h):
            g.block(f"prerequisite {r}: last {done} {ts} is older than {p.max_age_h:g}h",
                    f"prerequisite {r} last completed at {ts}, more than {p.max_age_h:g}h ago", r)
    return g


def run_night_key(ts: datetime) -> str:
    """The night a routine run belongs to, in the run's own UTC offset (as written in routines.log): 12:00 to
    11:59 the next day (rsi_approval.night_key)."""
    return (ts - timedelta(hours=12)).strftime("%Y-%m-%d")


def wait_for_prerequisites(log: Path, night: str, now: datetime, prereqs: Sequence[Prereq], *,
                           poll_s: float = 60.0, sleep: Callable[[float], None] = time.sleep,
                           clock: Callable[[], float] = time.monotonic) -> Gate:
    """evaluate_prerequisites, but while a blocked prerequisite with `wait_s` > 0 is still running (its live
    routines_log running marker), wait for it: re-read routines.log every `poll_s`, up to its `wait_s`. A routine
    that finishes in time is judged on its new line (PASS, a tolerated FAIL, or a blocking FAIL). One still running
    after `wait_s` blocks with a "still running" reason instead of its stale line's reason. A routine that is not
    running (no marker, a dead pid) is judged at once: nothing to wait for."""
    start = clock()
    while True:
        elapsed = clock() - start
        at = now + timedelta(seconds=elapsed)
        g = evaluate_prerequisites(log, night, at, prereqs)
        g.waited_s = elapsed
        waiting = [(p, info) for p in prereqs if p.wait_s > 0 and p.routine in g.routines
                   for info in [RL.running(log, p.routine, at)] if info]
        if not waiting:
            return g
        due = [(p, info) for p, info in waiting if elapsed >= p.wait_s]
        if due:
            for p, info in due:
                g.drop(p.routine)
                mins = f"{p.wait_s / 60:g} min"
                g.block(f"prerequisite {p.routine}: still running after {mins} (started {info['started_at']}, "
                        f"pid {info['pid']}); the night waited and did not run",
                        f"prerequisite {p.routine} was still running after the night waited {mins}", p.routine)
            return g
        _err_line(f"night-job: waiting for {', '.join(p.routine for p, _ in waiting)} (running since "
                  f"{waiting[0][1]['started_at']}; waited {int(elapsed)}s)")
        sleep(max(0.0, min(poll_s, min(p.wait_s for p, _ in waiting) - elapsed)) or 0.01)


def _err_line(msg: str) -> None:
    print(f"amem: rsi {msg}", file=sys.stderr, flush=True)


def check_prerequisites(log: Path, night: str, now: datetime, prereqs: Sequence[Prereq]) -> list[str]:
    """Reasons the night must not run yet (empty: every prerequisite completed). Reads routines.log only."""
    return evaluate_prerequisites(log, night, now, prereqs).reasons


_WS = re.compile(r"\s+")


def _one_line(s: str, cap: int = 300) -> str:
    s = _WS.sub(" ", redact.redact(str(s))).strip()
    return s if len(s) <= cap else s[: cap - 3] + "..."


def _kv(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}"
    return re.sub(r"[^A-Za-z0-9._:/+@-]", "_", str(v))[:80] or "-"


def log_lines(res: JobResult, *, now: datetime, secs: int) -> list[str]:
    """The house format (routines_log.py): one summary line, one `<routine>-fail <item>: <detail>` per item, then
    one `<routine>-warn <item>: <detail>` per warning."""
    ts = now.strftime(TS_FORMAT)
    head = f"{ts} {res.routine} {res.status}"
    for k, v in res.fields.items():
        head += f" {k}={_kv(v)}"
    head += f" {secs}s"
    if res.report:
        head += f" report={res.report.replace(' ', '%20')}"
    return [head, *(f"{ts} {res.routine}-fail {_one_line(i)}" for i in res.items[:MAX_ITEMS]),
            *(f"{ts} {res.routine}-warn {_one_line(w)}" for w in res.warnings[:MAX_ITEMS])]


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
    exe = (os.environ.get(RSI_SECURITY_ENV, "").strip() or os.environ.get(SECURITY_ENV, "").strip()
           or "/usr/bin/security")
    run = runner or subprocess.run
    keychain = (os.environ.get("AMEM_RSI_KEYCHAIN") or "").strip()      # rehearsal only (temp HOME); see rsi_sandbox
    extra = [str(Path(keychain).expanduser())] if keychain else []
    try:
        proc = run([exe, "find-generic-password", "-s", service, "-a", account, *extra], capture_output=True, timeout=10,
                   check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"keychain lookup failed ({type(exc).__name__})"
    if proc.returncode == 0:
        return None
    return "keychain item not found" if proc.returncode == 44 else f"keychain lookup exit {proc.returncode}"


def paid_stages(cfg: Any) -> list[str]:
    settings = {"hypothesizer": (cfg.hypothesizer, "auth_mode"), "implementer": (cfg.implementer, "auth_mode"),
                "replay_agent": (cfg.replay, "auth_mode"), "judge": (cfg.replay, "judge_auth_mode")}
    return sorted(k for k, v in cfg.stages.items() if v in PAID_STAGE_MODES.get(k, ())
                  and (k not in settings or settings[k][0].get(settings[k][1], "api") != "subscription"))


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
    from agent_memory.rsi_subscription_config import credential_problem
    subscription_roles = []
    for provider, settings in (cfg.replay.get("provider_profiles") or {}).items():
        subscription_roles.append((provider, settings["auth"]))
    if st.get("replay_agent") in ("claude", "codex"):
        if cfg.replay.get("auth_mode") == "subscription":
            subscription_roles.append((st["replay_agent"], cfg.replay["auth"]))
        else:
            refs.append(("replay agent (replay.credentials)", cfg.replay.get("credentials") or "none"))
    if st.get("judge") in ("codex", "claude"):
        if cfg.replay.get("judge_auth_mode") == "subscription":
            subscription_roles.append((st["judge"], cfg.replay["judge_auth"]))
        else:
            refs.append(("judge (replay.judge_credentials)", cfg.replay.get("judge_credentials") or "none"))
    if st.get("implementer") in ("claude", "codex"):
        refs.append(("implementer (implementer.credentials)", cfg.implementer.get("credentials") or "none"))
    for name, ref in refs:
        why = keychain_present(str(ref), runner=runner) if ref != "none" else "none configured for a live stage"
        if why:
            out.append(f"credentials {name}: {why}")
    if st.get("hypothesizer") == "claude-cli":
        if cfg.hypothesizer.get("auth_mode") == "subscription":
            subscription_roles.append(("claude", cfg.hypothesizer["auth"]))
        exe = os.environ.get("AMEM_EDITS_CLAUDE", "").strip() or "claude"
        if not shutil.which(exe):
            out.append("credentials hypothesizer: the claude CLI is not on PATH")
    seen = set()
    for provider, settings in subscription_roles:
        key = (provider, settings["home"])
        if key in seen:
            continue
        seen.add(key)
        problem = credential_problem(provider, settings)
        if problem:
            out.append(f"credentials {provider}: {problem}")
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
    profiles = rp.get("provider_profiles") or {}
    provider_count = len(profiles) or 1
    n_cases = cases if cases is not None else len(rsi_replay.load_cases(rp.get("cases")))
    reps = int(rp.get("reps") or 1)
    pairs = n_cases * reps
    if cases is None and rp.get("cases") is None:
        # No explicit list: each experiment replays only its target's cases (rsi_replay.plan_pairs), spread to
        # exactly one provider's share of the night's A/B pairs. Plan for the biggest such replay.
        from agent_memory import rsi_verdict
        n_cases, reps, pairs = rsi_replay.largest_auto_plan(
            rsi_replay.load_cases(), min_pairs=rsi_verdict.load_policy().min_pairs,
            max_pairs=max(1, b.max_ab_pairs // provider_count), reps=reps)
        if not profiles and n_cases:
            # The single-provider path runs every case equally often (rsi_replay.plan_cases)
            reps = max(1, b.max_ab_pairs // n_cases) if n_cases * reps > pairs else reps
            pairs = n_cases * reps
    exps = b.max_experiments
    rows: list[dict[str, Any]] = []
    per_exp: dict[str, int] = {}       # model -> the calls one experiment can make
    per_night: dict[str, int] = {}     # model -> the calls made once per night (triage)

    def per_call_cap(model: str) -> float:
        est = b.estimate(model)
        return est if est > 0 else prices.estimate(model)

    def add(stage: str, model: str, each: int, *, cap: float | None = None, note: str = "",
            once: bool = False) -> None:
        """`each`: calls per experiment (once=True: per night)."""
        if each <= 0:
            return
        book = per_night if once else per_exp
        book[model] = book.get(model, 0) + each
        count = each if once else each * exps
        if count <= 0:
            return
        call_cap = b.call_cap(model) if stage not in ("replay-agent",) else None
        if call_cap is not None:
            count = min(count, call_cap)
        subscription = model in b.subscription_models
        expected = None if subscription else G2B_MEASURED_USD.get(model)
        worst = cap if cap is not None else per_call_cap(model)
        if subscription:
            worst = 0.0  # excluded from API-dollar subtotal; allowance consumption is unknown
        rows.append({"stage": stage, "model": model, "count": count, "budget_call_cap": call_cap,
                     "billing_source": "subscription" if subscription else "api",
                     "expected_usd_each": expected, "worst_usd_each": round(worst, 6),
                     "expected_basis": "G2b measured" if expected is not None else "no measurement (worst case only)",
                     "expected_usd": round(count * expected, 6) if expected is not None else None,
                     "worst_usd": round(count * worst, 6), "note": note})

    jev_model = "jev-1.14.0"
    if st["triage"] == "live":
        add("triage", jev_model, int(cfg.candidates["max_considered"]), once=True,
            note="one Jev call per candidate considered")
    if st["hypothesizer"] == "claude-cli":
        from agent_memory import edits_writer
        add("hypothesize", str(cfg.hypothesizer.get("model") or edits_writer.writer_model()), 1,
            note="host `claude -p`, no tools, hooks off; pending hypotheses from an earlier night need no call")
    if st["implementer"] in ("claude", "codex"):
        add("implement", str(cfg.implementer.get("model") or st["implementer"]), 1, note="sandbox arm I")
    if st["implementer"] == "writer":
        from agent_memory import rsi_night
        add("implement", str(cfg.implementer.get("model") or rsi_night.WRITER_MODEL), 1,
            note="host `claude -p` section writer, no tools, hooks off; the stub change when it falls back")
    if st["replay_agent"] in ("claude", "codex"):
        for provider, settings in (profiles or {st["replay_agent"]: rp}).items():
            model = str(settings.get("model") or provider)
            cap = float(settings.get("max_budget_usd")) if provider == "claude" and settings.get("max_budget_usd") else None
            add("replay-agent", model, 2 * pairs, cap=cap,
                note=f"{provider}: {pairs} A/B pairs; providers are compared separately, depending on target")
    if st["judge"] in ("codex", "claude"):
        add("replay-judge", str(rp.get("judge_model") or st["judge"]), pairs * provider_count, note="one blind judge call per pair")
    if st["replay_jev"] == "live":
        add("replay-jev", jev_model, provider_count, note="one Jev call per provider replay")
    room = night_room(cfg, pairs=pairs, per_exp=per_exp, per_night=per_night)
    expected = [r["expected_usd"] for r in rows]
    exp_total = round(sum(x for x in expected if x is not None), 6)
    worst_total = round(sum(r["worst_usd"] for r in rows), 6)
    return {"config_sha256": cfg.sha256, "paid_stages": paid_stages(cfg), "cases": n_cases, "reps": reps,
            "ab_pairs_per_provider": pairs,
            "ab_pairs_per_experiment": pairs * provider_count, "max_ab_pairs": b.max_ab_pairs, "max_experiments": exps,
            "max_hypotheses": min(b.max_hypotheses, rsi_budget.HARD_MAX_HYPOTHESES), "model_retries": b.model_retries,
            "wall_clock_s": b.wall_clock_s, "max_usd": b.max_usd, "calls": rows,
            "expected_usd": exp_total, "expected_incomplete": any(x is None for x in expected),
            "worst_case_usd": worst_total, "budget_stops_at_usd": b.max_usd,
            "within_cap": worst_total <= b.max_usd + 1e-9, "prices": prices.describe(),
            "max_subscription_tokens": b.max_subscription_tokens,
            "subscription_models": list(b.subscription_models), "room": room,
            "currency": "USD", "invoice_cap_verified": False,
            "accounting_scope": "api-only" if b.subscription_models else "all-recorded",
            "basis_note": "Expected/worst figures are local API-cost estimates or configured CLI bounds, not invoices. "
                          "New calls require room for booked spend plus in-flight reservations. "
                          "Unknown paid usage is conservatively booked; actual provider charges can exceed an estimate. "
                          "Subscription billing is separate and unknown. USD accounting is not proof of a EUR invoice cap."}


def night_room(cfg: Any, *, pairs: int, per_exp: Mapping[str, int], per_night: Mapping[str, int]) -> dict[str, Any]:
    """Whether the night's caps leave room for `max_experiments` experiments, each at its worst case.

    `pairs`: A/B pairs per provider replay. `per_exp` / `per_night`: model calls per experiment / once per night
    (paid stages only; free stand-ins have `free_model_calls`). Per-night caps (calls, sandbox runs, wall clock,
    subscription tokens) must cover max_experiments x one experiment; `max_ab_pairs` is per replay and must cover
    one. The night itself stops at the experiment cap (rsi_night runs at most one experiment per candidate)."""
    from agent_memory import rsi_budget, rsi_replay
    b, st, rp = cfg.budget, cfg.stages, cfg.replay
    exps = b.max_experiments
    base = {k: v for k, v in rp.items() if k != "provider_profiles"}
    profiles = rp.get("provider_profiles") or {st["replay_agent"]: {}}
    limit = int(rsi_replay.DEFAULT_LIMITS["timeout_s"])
    case_s = min(max((int(c.timeout_s) for c in rsi_replay.load_cases(rp.get("cases"))), default=limit), limit)
    live_agent, live_judge = st["replay_agent"] in ("claude", "codex"), st["judge"] in ("codex", "claude")
    # one experiment: wall clock (each model stage at its timeout) and container runs
    secs = {"hypothesize": float(cfg.hypothesizer.get("timeout_s") or 600) if st["hypothesizer"] == "claude-cli" else 0.0,
            "implement": (float(cfg.implementer.get("timeout_s") or 900) if st["implementer"] in ("claude", "codex")
                          else float(cfg.implementer.get("timeout_s") or 300) if st["implementer"] == "writer" else 0.0),
            "stage": float(cfg.staging.get("timeout_s") or 600), "replay": 0.0}
    runs = 0
    for settings in profiles.values():
        s = {**base, **settings}
        subscription = live_agent and s.get("auth_mode") == "subscription"
        per_run = (int(s.get("max_tool_calls") or 8) if subscription else 1) + 1     # agent (tool) containers + check
        runs += 2 * pairs * per_run + (pairs if live_judge and s.get("judge_auth_mode") != "subscription" else 0)
        secs["replay"] += (2 * pairs * (case_s + CHECK_RUN_S) + (pairs * rsi_replay.JUDGE_TIMEOUT_S if live_judge else 0)
                           + (JEV_CALL_S if st["replay_jev"] == "live" else 0))
    one_s = sum(secs.values())
    checks: list[dict[str, Any]] = []

    def check(limit_name: str, need: float, cap: float, unit: str = "") -> None:
        checks.append({"limit": limit_name, "need": need, "cap": cap, "unit": unit, "fits": need <= cap + 1e-9})

    for model in sorted(set(per_exp) | set(per_night)):
        check(f"calls {model}", per_night.get(model, 0) + exps * per_exp.get(model, 0), b.call_cap(model))
    check("A/B pairs per experiment", pairs * len(profiles), b.max_ab_pairs)
    check("sandbox runs", exps * runs, b.max_sandbox_runs)
    check("wall clock", NIGHT_OVERHEAD_S + exps * one_s, b.wall_clock_s, "s")
    if b.subscription_models:
        check("subscription tokens", exps * SUBSCRIPTION_TOKENS_PER_EXPERIMENT, b.max_subscription_tokens)
    # How many experiments the wall clock holds when every model stage and case runs to its full timeout. The
    # night still stops cleanly at the wall clock (rsi_budget); this only says how far the worst case reaches.
    wall_fit = max(0, int((b.wall_clock_s - NIGHT_OVERHEAD_S) // one_s)) if one_s > 0 else exps
    return {"experiments": exps, "fits": all(c["fits"] for c in checks), "checks": checks,
            "worst_case_experiments_in_wall_clock": min(exps, wall_fit),
            "hard_max_wall_clock_s": rsi_budget.HARD_MAX_WALL_CLOCK_S,
            "worst_case_wall_clock_s": NIGHT_OVERHEAD_S + exps * one_s,
            "per_experiment": {"calls": dict(sorted(per_exp.items())), "ab_pairs": pairs * len(profiles),
                               "sandbox_runs": runs, "wall_clock_s": one_s,
                               "wall_clock_parts_s": secs, "case_timeout_s": case_s,
                               **({"subscription_tokens": SUBSCRIPTION_TOKENS_PER_EXPERIMENT}
                                  if b.subscription_models else {})},
            "per_night": {"calls": dict(sorted(per_night.items())), "overhead_s": NIGHT_OVERHEAD_S}}


def render_room(room: Mapping[str, Any]) -> list[str]:
    one = room["per_experiment"]
    out = [f"Room for {room['experiments']} experiment(s) "
           + ("(every limit fits):" if room["fits"] else "(NOT every limit fits; the night stops at the first):"),
           f"  one experiment at most: {one['sandbox_runs']} sandbox runs, {one['ab_pairs']} A/B pairs, "
           f"{one['wall_clock_s']:g} s (model stages at their timeouts, case timeout {one['case_timeout_s']} s), calls "
           + (", ".join(f"{m} {n}" for m, n in one["calls"].items()) or "none paid")]
    def num(x: float) -> str:
        return f"{int(x):,}" if float(x).is_integer() else f"{x:,.1f}"

    for c in room["checks"]:
        out.append(f"  - {c['limit']}: need {num(c['need'])}{c['unit']} of {num(c['cap'])}{c['unit']} "
                   + ("fits" if c["fits"] else "DOES NOT FIT"))
    worst, hard = room.get("worst_case_wall_clock_s"), room.get("hard_max_wall_clock_s")
    if worst is not None and hard is not None and worst > hard:
        out.append(f"  ! worst-case wall clock {num(worst)}s is over the code's hard maximum {num(hard)}s: at every "
                   f"timeout only {room['worst_case_experiments_in_wall_clock']} of {room['experiments']} "
                   "experiment(s) finish; the night stops cleanly at the wall clock (later experiments are not "
                   "run, never cut short silently).")
    return out


def render_plan(plan: Mapping[str, Any]) -> str:
    out = [f"Paid plan for config {plan['config_sha256'][:12]} (prices {plan['prices']['version']}): "
           f"{plan['max_experiments']} experiment(s), <= {plan['max_hypotheses']} hypotheses per candidate, "
           f"{plan['ab_pairs_per_experiment']} A/B pair(s) ({plan.get('ab_pairs_per_provider', plan['ab_pairs_per_experiment'])} "
           f"per provider: {plan['cases']} case(s), up to {plan['reps']} rep(s) each, cap "
           f"{plan['max_ab_pairs']}), retries {plan['model_retries']}, wall clock {plan['wall_clock_s']:g}s."]
    if not plan["calls"]:
        out.append("No paid call: every stage is a stub or a fixture. Expected $0.")
    for r in plan["calls"]:
        if r.get("billing_source") == "subscription":
            out.append(f"- {r['stage']}: {r['count']} x {r['model']} -> subscription usage; billed dollars and allowance draw unknown. {r['note']}")
            continue
        exp = f"${r['expected_usd']:.4f}" if r["expected_usd"] is not None else "unmeasured"
        out.append(f"- {r['stage']}: {r['count']} x {r['model']} -> expected {exp} ({r['expected_basis']}), "
                   f"worst ${r['worst_usd']:.4f}. {r['note']}")
    out.append(f"Total: expected ${plan['expected_usd']:.4f}" + (" + unmeasured calls" if plan["expected_incomplete"] else "")
               + f", planned API estimate ${plan['worst_case_usd']:.4f}, accounting cap max_usd ${plan['max_usd']:.2f} "
               + ("(the API estimates fit)" if plan["within_cap"] else "(the accounting limit stops further calls)") + ".")
    out.append(plan["basis_note"])
    if plan.get("subscription_models"):
        out.append(f"Subscription token stop: {plan['max_subscription_tokens']} observed tokens. API totals exclude subscription usage. No API fallback.")
    if plan.get("room"):
        out += render_room(plan["room"])
    return "\n".join(out)


# =========================================================================== the night job


SKIP_KINDS = ("prerequisites", "credentials")


def credentials_summary(problems: Sequence[str]) -> str:
    """The credentials skip in headline words: the first problem, and how many more."""
    if not problems:
        return ""
    more = len(problems) - 1
    return (f"the credentials and budget check failed ({_one_line(problems[0], 200)}"
            + (f"; and {more} more" if more else "") + ")")


def skip_report(night: str, reasons: Sequence[str], *, now: datetime, kind: str = "prerequisites",
                summary: str | None = None, warnings: Sequence[str] = ()) -> dict[str, Any]:
    """A valid rsi-night/1 report for a night that did not run: status preflight_failed, the reasons as notes, and
    `skipped` ({by, reason: prerequisites | credentials, summary, items}) so a reader can tell a skip (nothing was
    read: the zero counts and the `skipped` health mean "not analyzed", not "nothing found") from a night whose own
    preflight failed. `prerequisite_warnings`: failed prerequisite steps the night does not read."""
    from agent_memory import rsi_night as N
    if kind not in SKIP_KINDS:
        raise ValueError(f"skip kind {kind!r} not in {SKIP_KINDS}")
    iso = now.isoformat(timespec="seconds")
    items = [_one_line(r, 260) for r in reasons][:30]
    if summary is None:
        summary = credentials_summary(reasons) if kind == "credentials" else (items[0] if items else "")
    rep: dict[str, Any] = {
        "format": N.FORMAT, "night": night, "started_at": iso, "finished_at": iso, "status": "preflight_failed",
        "checkpoint": {"before": None, "after": None, "advanced": False},
        "delta": {"since": None, "sessions": 0, "deviations": 0, "eval_failures": 0, "routine_failures": 0},
        "patterns": [], "candidates": [], "experiments": [],
        "costs": {"by_model": {}, "sandbox_runs": 0, "providers": {}, "total_usd": 0.0},
        "health": {k: "skipped" for k in N.HEALTH_KEYS}, "pending_decisions": [],
        "notes": [f"skipped by {NIGHT_ROUTINE}: {r}" for r in items],
        "skipped": {"by": NIGHT_ROUTINE, "reason": kind, "summary": _one_line(summary, 300), "items": items}}
    if warnings:
        rep["prerequisite_warnings"] = [_one_line(w, 300) for w in warnings][:N.MAX_PREREQ_WARNINGS]
    return rep


def _write_skip_report(night: str, reasons: Sequence[str], now: datetime, *, kind: str = "prerequisites",
                       summary: str | None = None, warnings: Sequence[str] = ()) -> str | None:
    from agent_memory import rsi_night as N
    path = N.report_path(night)
    if path.exists():                    # a night that already ran keeps its own report
        return None
    try:
        N._write_json(path, skip_report(night, reasons, now=now, kind=kind, summary=summary, warnings=warnings))
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
    reasons = rep.get("health_reasons") or {}
    for k, v in sorted((rep.get("health") or {}).items()):
        if v in ("error", "unavailable", "fixture-missing", "stale", "truncated"):
            why = "; ".join(str(x) for x in (reasons.get(k) or [])[:2])
            items.append(f"health {k}: {v}" + (f" ({why})" if why else ""))
    w = rep.get("writer")
    if isinstance(w, dict) and w.get("all_fell_back") is True:     # no writer change was tested (2026-10-25)
        top = next((str(x.get("reason")) for x in w.get("reasons") or [] if isinstance(x, dict)), "no reason recorded")
        items.append(f"writer never ran: {w.get('fell_back')}/{w.get('experiments')} experiments fell back to the "
                     f"stub ({top})")
    for e in rep.get("experiments") or []:
        if e.get("status") in ("failed", "interrupted"):
            why = (e.get("verdict_reasons") or [e.get("status")])[0]
            items.append(f"experiment {e.get('experiment_id') or 'not started'}: {why}")
    items += [f"night: {n}" for n in notes if n.startswith("error:")]
    return "FAIL", items or [f"night: status {status}"]


@contextlib.contextmanager
def term_guard() -> Iterator[dict[str, int]]:
    """While the night job runs, SIGTERM never kills it outright. Inside `Night.run` the night's own handler
    turns it into a clean abort; outside it (unwinding, the final DB writes) it is only counted. It wraps the
    night run only (the gate before it is quick and has no watchdog armed). So the
    watchdog's last stage always gets to write its FAIL line. (launchd's own stop still ends with SIGKILL.)"""
    state = {"term": 0}
    if threading.current_thread() is not threading.main_thread():
        yield state
        return

    def note(_signum: int, _frame: Any) -> None:
        state["term"] += 1
    old = signal.signal(signal.SIGTERM, note)
    try:
        yield state
    finally:
        signal.signal(signal.SIGTERM, old)


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
                  write_log: bool = True, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic, poll_s: float = 60.0) -> JobResult:
    """The scheduled night (module docstring). `now` (tests, rehearsals) fixes the gate's clock and the log
    line's timestamp; by default both are the real time. `sleep`, `clock` and `poll_s` drive the gate's wait for a
    prerequisite that is still running (wait_for_prerequisites)."""
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
    gate = wait_for_prerequisites(log, night, now, prereqs, poll_s=poll_s, sleep=sleep, clock=clock)
    if gate.waited_s >= 1:
        res.fields["waited_s"] = int(gate.waited_s)
    res.warnings = list(gate.warnings)
    if gate.reasons:
        res.status, res.items = "SKIP", gate.reasons
        res.fields["skipped"] = "prerequisites"
        res.report = _write_skip_report(night, gate.reasons, now, kind="prerequisites", summary=gate.summary(),
                                        warnings=gate.warnings)
        return finish()
    creds = credential_problems(cfg, runner=runner)
    if creds:
        res.status, res.items = "FAIL", creds
        res.fields["skipped"] = "credentials"
        res.report = _write_skip_report(night, creds, now, kind="credentials", warnings=gate.warnings)
        return finish()

    def killed() -> None:
        res.status, res.items = "FAIL", [f"hard timeout: the night did not stop {HARD_GRACE_S + KILL_GRACE_S}s after "
                                         f"its wall clock ({cfg.budget.wall_clock_s:g}s); process exited"]
        finish()

    guard = Watchdog(cfg.budget.wall_clock_s + HARD_GRACE_S, KILL_GRACE_S, killed) if watchdog else None
    with term_guard() as terms:
        if guard:
            guard.__enter__()
        try:
            rep = N.run_night(night=night, config_path=config_path, db_url=db_url, adapters=adapters,
                              prerequisite_warnings=gate.warnings, routines_log=log)
        finally:
            if guard:
                guard.__exit__(None, None, None)
    res.status, res.items = classify(rep)
    if res.status == "SKIP":       # overlap / already done: nothing went wrong, so -warn lines, not -fail (routine-audit)
        res.warnings, res.items = res.warnings + res.items, []
    if terms["term"] and rep.get("status") != "aborted":
        res.items.append(f"signal: {terms['term']} SIGTERM outside the night's stages (noted, not obeyed)")
        res.status = "FAIL"
    staged = [e for e in rep.get("experiments") or [] if e.get("status") == "staged"]
    costs = rep.get("costs") or {}
    res.fields.update(status=rep.get("status"), candidates=len(rep.get("candidates") or []),
                      experiments=sum(1 for e in rep.get("experiments") or [] if e.get("experiment_id")), staged=len(staged),
                      usd=float(costs.get("total_usd") or 0.0),
                      reported_usd=float(costs.get("reported_usd") or 0.0),
                      estimated_usd=float(costs.get("estimated_usd") or 0.0),
                      checkpoint="advanced" if (rep.get("checkpoint") or {}).get("advanced") else "kept")
    sr = (rep.get("prerequisites") or {}).get("skill_refresh")
    if isinstance(sr, dict):            # which refresh the night used, and whether it was fresh (skill_refresh.py)
        res.fields["skill_refresh"] = sr.get("status")
        if sr.get("run"):
            res.fields["skill_refresh_run"] = sr["run"]
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
                  sleep: Callable[[float], None] = time.sleep, push: bool = False,
                  notify: dict[str, str] | None = None, state: Path | None = None) -> JobResult:
    """The morning brief for the night key (default: the night that just ended) as markdown + JSON files in
    `out_dir` (default ~/.claude/parent/reports). Waits up to `wait_s` for the night to finish (its lock released
    and its report written). Stores the completed snapshot for Night Desk. With `push` (the scheduled CLI run),
    the one morning push (alerts.push_morning): the night's result plus amem-daily's new escalations, safety slips
    and last-day proposals; nothing when all of them are quiet. `state`: amem-daily's state file (pushed ids)."""
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
    out = Path(out_dir or DEFAULT_REPORTS_DIR).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    from agent_memory import telemetry_analysis
    b['telemetry_review'] = telemetry_analysis.daily_review(out,now=now)
    md = BR.render_markdown(b)
    mdp, jsp = out / f"rsi-brief-{night}.md", out / f"rsi-brief-{night}.json"
    mdp.write_text(md, encoding="utf-8")
    jsp.write_text(json.dumps(b, sort_keys=True, default=str, indent=1) + "\n", encoding="utf-8")
    # Publish only after both local files exist. A failed upload is visible to the
    # routine monitor; ingest --backfill can retry without rerunning the night.
    from agent_memory import dashboard
    try:
        report_path, _ = BR.find_report(root or RA.sandbox_dir(), night)
        report, _ = BR.load_report(report_path)
        dashboard.upsert_night(b, report, db_url=db_url)
    except (OSError, ValueError, BR.ReportError, psycopg.Error):
        res.status = "FAIL"
        res.items.append("dashboard: night snapshot upload failed; local brief retained")
    ready = sum(1 for x in b["passed"] if x["actions"].get("apply"))
    res.fields.update(night_status=b["status"], ready=ready, staged_not_ready=len(b["passed"]) - ready,
                      failed=len(b["failed"]), pending=b["pending_decisions"]["total"],
                      apply=b["pending_decisions"]["with_apply"], usd=float(b["costs"]["total_usd"] or 0.0),
                      db=b["health"]["database"]["status"])
    if b["health"]["database"]["status"] != "ok":
        res.items.append(f"database: rows not read ({b['health']['database']['detail'] or 'unavailable'})")
        res.status = "FAIL"
    if push:
        from agent_memory import alerts
        try:
            sent, extra, problems = alerts.push_morning(b, notify=notify, db_url=db_url, state_file=state)
            res.fields["push"] = sent
            res.fields["escalations_new"] = len(extra.new_rows)
            res.fields["safety"] = len(extra.safety)
            res.fields["expiring"] = len(extra.expiring)
            res.warnings += [f"push {_one_line(p, 200)}" for p in problems]   # a part left out is a warning
        except Exception as exc:  # the brief is written; a failed push is a note, not a failed brief
            res.fields["push"] = "error"
            res.items.append(f"push: {_one_line(exc, 160)}")
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
                        routines_log=args.routines_log, out_dir=args.out, wait_s=args.wait_s,
                        push=not args.no_push, state=args.state)
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
                                         "routines.log line, and the one morning push (the night's result, new "
                                         "escalations, safety slips, last-day proposals)")
    p.add_argument("--root", type=Path, default=None, help="the RSI sandbox root (default RSI_SANDBOX_DIR or ~/sandbox)")
    p.add_argument("--night", default=None)
    p.add_argument("--now", default=None, help=argparse.SUPPRESS)
    p.add_argument("--routines-log", type=Path, default=None)
    p.add_argument("--out", type=Path, default=None, help="report dir (default ~/.claude/parent/reports)")
    p.add_argument("--wait-s", type=float, default=0.0,
                   help="wait up to this long for the night to finish (lock released, report written)")
    p.add_argument("--no-push", action="store_true", help="write the brief, send no phone push")
    p.add_argument("--state", type=Path, default=None,
                   help="amem-daily's state file, for the escalation ids already pushed (default AMEM_DAILY_STATE "
                        "or ~/.claude/parent/amem-daily-state.json)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_brief_job)
