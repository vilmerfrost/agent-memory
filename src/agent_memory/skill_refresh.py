"""The skill-score refresh as a recorded run, and the night's `skill_refresh` prerequisite (data-use plan R2).

Why: the night selects skill candidates from `skill_scores`. Until 2026-10-26 the scores were refreshed at 03:30,
after the 01:30 night, so every night read the previous morning's scores (baseline finding S1). The night must know
which refresh it used and how far that refresh's input reached, instead of trusting a schedule or a file time.

Recording (`amem ingest skills` and `amem skills scores --refresh`):

- `amem ingest skills PATH` records when it read the skill log completely: rsi_checkpoints job `skills-ingest`,
  run_key `latest`, meta.read_at (+ path and counts). A failed ingest records nothing, so the last good read stays.
- `amem skills scores --refresh` is one run: rsi_checkpoints job `skills-refresh`, run_key = the run id
  (`sr-<UTC time>-<6 hex>`). It is written `running` first, then `done` in the same transaction as the new scores
  (both or neither), or `failed` with the error. meta: window_start, window_end, scored (score rows), calls,
  matched, basis, snapshot_at.
  The input window is the skill calls the refresh scored: window_start = the earliest call's time; window_end = how
  far the ingested log reached = the last complete log read (`skills-ingest` read_at), or, when no read is recorded
  (rows ingested by an older amem), the newest skill_calls.created_at (basis `latest_insert`: an ingest that found
  nothing new leaves no trace there, so this end can be earlier than the truth, never later); never later than the
  refresh's own snapshot.
  `runs` is not used on purpose: a failed `runs` row becomes a routine-failure candidate in the night's delta.
- The command's last stdout line is the contract the agent-config wrapper copies into routines.log (keys fixed):
      skills-refresh run=<id> window_start=<ISO|-> window_end=<ISO|-> status=<ok|failed> scored=<n>

The prerequisite (`check`, run by the night in S1 right after the delta, before any candidate is sighted):

- Night window end = the S1 delta's `until` (now - settle_s): the end of the evidence window the night reads.
- Fresh: the latest successful refresh has window_end >= night window end - max_lag_h. max_lag_h defaults to 2 h:
  a refresh inside the 00:40 amem-daily chain (R7) passes even when amem-daily ran long and the night started late;
  the old 03:30 refresh (about 22 h before the next night) never does.
- Failed: the latest refresh failed or never finished, and no successful one is fresh. Missing: none recorded.
- Stale: a successful refresh exists but its window ends too early (or its window is unknown).
- Decision: fresh -> `use`. Failed or missing -> `skip_skill_candidates` in every mode (the dependency policy:
  skip only what needs the prerequisite; deviation, routine and finding candidates continue). Stale -> by mode:
  `report` (the default) -> `warn`, the candidates are still considered; `enforce` -> `skip_skill_candidates`.
  The default stays `report` until the refresh runs before the night (R7 schedule change): with the 03:30 refresh
  every night is stale, and `enforce` would skip every skill candidate.
- Source of truth: the recorded runs in the database. Only when that read fails (no connection, a database
  error) the night falls back to routines.log `rsi-skills` lines (their `run=` and `window_end=` fields; a line
  without window_end has an unknown window).

The night report records `prerequisites.skill_refresh` (`as_report`); the morning brief turns a stale, failed or
missing refresh into a prerequisite warning (`warning`).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from agent_memory import routines_log as RL, skills, store

JOB = "skills-refresh"                 # rsi_checkpoints job of a refresh run (run_key = the run id)
INGEST_JOB = "skills-ingest"           # rsi_checkpoints job of the last complete skill-log read
INGEST_KEY = "latest"
LOG_ROUTINE = "rsi-skills"             # the agent-config wrapper's routines.log line (fallback source)
LINE_PREFIX = "skills-refresh"
MODES = ("report", "enforce")
DEFAULT_MODE = "report"
DEFAULT_MAX_LAG_H = 2.0
MAX_LAG_H_LIMIT = 48.0
MODE_ENV = "AMEM_SKILL_REFRESH_MODE"   # overrides the night config's skill_refresh.mode
STATUSES = ("fresh", "stale", "failed", "missing")
RUN_STATUSES = ("ok", "failed", "running")
DECISIONS = ("use", "warn", "skip_skill_candidates")
SKILL_SOURCE = "skill_scores"          # rsi_candidates.source of a skill candidate (rsi_night._skill_sightings)
RECENT_RUNS = 20


def _iso(v: datetime | None) -> str | None:
    return v.astimezone(UTC).isoformat(timespec="seconds") if isinstance(v, datetime) else None


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if not isinstance(v, str) or not v:
        return None
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return RL.parse_ts(v)
    return t if t.tzinfo else None


def new_run_id(now: datetime | None = None) -> str:
    return f"sr-{(now or datetime.now(UTC)).astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


# =========================================================================== recording


def record_ingest(*, path: Path, read_at: datetime, new: int, present: int, unparsed: int,
                  db_url: str | None = None) -> None:
    """The last complete read of the skill log (after a successful `amem ingest skills`)."""
    meta = {"read_at": _iso(read_at), "path": str(path), "new": new, "present": present, "unparsed": unparsed}
    with store.connect(db_url, autocommit=True) as conn:
        conn.execute("insert into rsi_checkpoints (job, run_key, status, finished_at, meta) values "
                     "(%s, %s, 'done', now(), %s) on conflict (job, run_key) do update set status = 'done', "
                     "finished_at = now(), meta = excluded.meta", (INGEST_JOB, INGEST_KEY, Jsonb(meta)))


@dataclass
class RefreshResult:
    run: str
    status: str = "failed"                       # ok | failed
    window_start: datetime | None = None
    window_end: datetime | None = None
    scored: int = 0                              # skill_scores rows written
    calls: int = 0                               # skill calls scored
    matched: int = 0                             # calls linked to a session by this refresh
    basis: str | None = None                     # what window_end rests on: ingest | latest_insert | none
    error: str | None = None
    recorded: bool = False                       # the run row reached the database

    def line(self) -> str:
        return (f"{LINE_PREFIX} run={self.run} window_start={_iso(self.window_start) or '-'} "
                f"window_end={_iso(self.window_end) or '-'} status={self.status} scored={self.scored}")


_WINDOW_SQL = f"""
select min(c.ts), max(c.created_at), count(*), now(),
       (select meta->>'read_at' from rsi_checkpoints where job = '{INGEST_JOB}' and run_key = '{INGEST_KEY}'
          and status = 'done')
from skill_calls c left join sessions s on s.session_id = c.session_id
where {skills.CALL_FILTER}
"""


def _upsert(conn: psycopg.Connection, run: str, status: str, meta: dict[str, Any]) -> None:
    conn.execute("insert into rsi_checkpoints (job, run_key, status, finished_at, meta) values "
                 "(%s, %s, %s, case when %s = 'running' then null else now() end, %s) "
                 "on conflict (job, run_key) do update set status = excluded.status, "
                 "finished_at = excluded.finished_at, meta = rsi_checkpoints.meta || excluded.meta",
                 (JOB, run, status, status, Jsonb(meta)))


def run_refresh(*, db_url: str | None = None, now: datetime | None = None, run_id: str | None = None
                ) -> RefreshResult:
    """One recorded refresh: match sessions, rewrite skill_scores and record the run with its input window. Never
    raises on a database error: the result says `failed` and why (the caller prints its line)."""
    res = RefreshResult(run_id or new_run_id(now))
    try:
        with store.connect(db_url, autocommit=True) as conn:
            _upsert(conn, res.run, "running", {"started_at": _iso(now or datetime.now(UTC))})
            res.recorded = True
    except psycopg.Error as exc:
        res.error = f"database unavailable ({type(exc).__name__})"
        return res
    try:
        with store.connect(db_url) as conn:      # one transaction: the scores and the `done` row commit together
            res.matched, res.scored = skills.rewrite_scores(conn)
            first, newest_insert, calls, snapshot, read_at = conn.execute(_WINDOW_SQL).fetchone()
            res.calls, res.window_start = int(calls), (first if calls else None)
            read = _ts(read_at)
            # A recorded read is exact (rows it inserted carry a created_at a moment later, which would overstate it)
            end, res.basis = ((read, "ingest") if read is not None else
                              (newest_insert, "latest_insert") if newest_insert is not None else (None, "none"))
            res.window_end = min(end, snapshot) if end is not None else None
            _upsert(conn, res.run, "done", {
                "window_start": _iso(res.window_start), "window_end": _iso(res.window_end), "scored": res.scored,
                "calls": res.calls, "matched": res.matched, "basis": res.basis, "snapshot_at": _iso(snapshot),
                "ingest_read_at": _iso(read)})
        res.status = "ok"
    except psycopg.Error as exc:
        res.status, res.error = "failed", f"{type(exc).__name__}: {str(exc).strip()[:200]}"
        res.window_start = res.window_end = None
        res.scored = 0
        try:
            with store.connect(db_url, autocommit=True) as conn:
                _upsert(conn, res.run, "failed", {"error": res.error})
        except psycopg.Error:
            res.recorded = False
    return res


# =========================================================================== the night's prerequisite


@dataclass(frozen=True)
class RefreshRun:
    run: str | None
    status: str                                  # ok | failed | running
    started_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None


@dataclass
class Check:
    status: str                                  # fresh | stale | failed | missing
    decision: str                                # use | warn | skip_skill_candidates
    mode: str
    max_lag_h: float
    night_window_end: datetime
    source: str                                  # db | routines.log | none
    run: RefreshRun | None = None
    reason: str | None = None
    skipped: set[str] = field(default_factory=set)   # skill candidate keys left out tonight

    @property
    def skip(self) -> bool:
        return self.decision == "skip_skill_candidates"

    @property
    def required_end(self) -> datetime:
        return self.night_window_end - timedelta(hours=self.max_lag_h)

    def as_report(self) -> dict[str, Any]:
        r = self.run
        return {"run": r.run if r else None, "window_start": _iso(r.window_start) if r else None,
                "window_end": _iso(r.window_end) if r else None, "status": self.status,
                "run_status": r.status if r else None, "mode": self.mode, "decision": self.decision,
                "night_window_end": _iso(self.night_window_end), "required_end": _iso(self.required_end),
                "max_lag_h": self.max_lag_h, "source": self.source, "reason": self.reason,
                "skipped_candidates": len(self.skipped), "skipped_keys": sorted(self.skipped)[:20]}


def settings(section: Any) -> tuple[str, float]:
    """(mode, max_lag_h) from the night config's `skill_refresh` object; AMEM_SKILL_REFRESH_MODE overrides the
    mode. ValueError on a bad value (the night refuses a bad config)."""
    sec = section if isinstance(section, dict) else {}
    mode = os.environ.get(MODE_ENV) or sec.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise ValueError(f"skill_refresh.mode must be one of {MODES}, got {mode!r}")
    lag = sec.get("max_lag_h", DEFAULT_MAX_LAG_H)
    if isinstance(lag, bool) or not isinstance(lag, (int, float)) or not 0 < lag <= MAX_LAG_H_LIMIT:
        raise ValueError(f"skill_refresh.max_lag_h must be a number in (0, {MAX_LAG_H_LIMIT:g}], got {lag!r}")
    return mode, float(lag)


def db_runs(conn: psycopg.Connection) -> list[RefreshRun]:
    """The recent recorded refreshes, newest first."""
    rows = conn.execute("select run_key, status, started_at, meta from rsi_checkpoints where job = %s "
                        "order by started_at desc, run_key desc limit %s", (JOB, RECENT_RUNS)).fetchall()
    out = []
    for run, status, started, meta in rows:
        m = meta if isinstance(meta, dict) else {}
        out.append(RefreshRun(run, {"done": "ok", "running": "running"}.get(status, "failed"), _ts(started),
                              _ts(m.get("window_start")), _ts(m.get("window_end"))))
    return out


def log_runs(log: Path) -> list[RefreshRun]:
    """`rsi-skills` lines in routines.log, newest first. PASS = ok; any other word = failed."""
    runs = [r for r in RL.parse_file(log).runs if r.routine == LOG_ROUTINE]
    runs.sort(key=lambda r: (r.started_at, r.line_no), reverse=True)
    out = []
    for r in runs:
        start, end = _ts(str(r.meta.get("window_start") or "")), _ts(str(r.meta.get("window_end") or ""))
        win = r.meta.get("window")                # the plan's `window=<from>..<to>` spelling
        if isinstance(win, str) and ".." in win and (start is None or end is None):
            a, _, b = win.partition("..")
            start, end = start or _ts(a), end or _ts(b)
        run = r.meta.get("run")
        out.append(RefreshRun(str(run) if run not in (None, "") else None, "ok" if r.status == "pass" else "failed",
                              r.started_at, start, end))
    return out


def decide(runs: Sequence[RefreshRun], *, night_window_end: datetime, mode: str = DEFAULT_MODE,
           max_lag_h: float = DEFAULT_MAX_LAG_H, source: str = "db") -> Check:
    """The prerequisite's status and decision from the recorded runs (newest first). Window and result only."""
    def check(status: str, run: RefreshRun | None, reason: str | None) -> Check:
        decision = ("use" if status == "fresh" else
                    ("warn" if mode == "report" else "skip_skill_candidates") if status == "stale"
                    else "skip_skill_candidates")
        return Check(status, decision, mode, max_lag_h, night_window_end, source, run, reason)

    required = night_window_end - timedelta(hours=max_lag_h)
    latest = runs[0] if runs else None
    ok = next((r for r in runs if r.status == "ok"), None)
    if ok and ok.window_end is not None and ok.window_end >= required:
        return check("fresh", ok, None)
    if latest is not None and latest.status != "ok":
        word = "did not finish" if latest.status == "running" else "failed"
        when = _iso(latest.started_at) or "an unknown time"
        last_ok = (f"; the last successful refresh {ok.run} covers skill calls to {_iso(ok.window_end) or 'an unknown time'}"
                   if ok else "; no successful refresh is recorded")
        return check("failed", latest, f"the latest skill refresh {latest.run or '(no run id)'} {word} (started {when})"
                                       + last_ok)
    if ok is not None:
        if ok.window_end is None:
            return check("stale", ok, f"skill refresh {ok.run or '(no run id)'} recorded no input window")
        lag_h = (night_window_end - ok.window_end).total_seconds() / 3600
        return check("stale", ok, f"skill refresh {ok.run or '(no run id)'} covers skill calls to "
                                  f"{_iso(ok.window_end)}, {lag_h:.1f} h before the night's window end "
                                  f"{_iso(night_window_end)} (tolerance {max_lag_h:g} h)")
    return check("missing", None, "no skill refresh is recorded" + (" in routines.log" if source == "routines.log"
                                                                      else ""))


def check(conn: psycopg.Connection | None, *, night_window_end: datetime, mode: str = DEFAULT_MODE,
          max_lag_h: float = DEFAULT_MAX_LAG_H, log: Path | None = None) -> Check:
    """The prerequisite from the database; routines.log only when the database read fails."""
    why = "no database connection"
    if conn is not None:
        try:
            return decide(db_runs(conn), night_window_end=night_window_end, mode=mode, max_lag_h=max_lag_h)
        except psycopg.Error as exc:
            why = f"database read failed ({type(exc).__name__})"
    from agent_memory import rsi_schedule
    path = rsi_schedule.routines_log_path(log)
    try:
        runs = log_runs(path)
    except OSError as exc:
        c = decide([], night_window_end=night_window_end, mode=mode, max_lag_h=max_lag_h, source="none")
        c.reason = f"{why}; routines.log unreadable ({type(exc).__name__}): no skill refresh can be verified"
        return c
    c = decide(runs, night_window_end=night_window_end, mode=mode, max_lag_h=max_lag_h, source="routines.log")
    c.reason = f"{why}; read routines.log" + (f": {c.reason}" if c.reason else "")
    return c


_STATUS_WORDS = {"stale": "is stale", "failed": "failed", "missing": "is missing"}
_DECISION_WORDS = {"warn": "skill candidates are still considered (report mode)",
                   "skip_skill_candidates": "skill candidates are skipped tonight; other candidates continue"}


def warning(rep: Any) -> str | None:
    """The one-line prerequisite warning for a report's `prerequisites.skill_refresh`, or None when it is fresh or
    absent (older reports)."""
    if not isinstance(rep, dict) or rep.get("status") not in _STATUS_WORDS:
        return None
    run = rep.get("run")
    n = rep.get("skipped_candidates")
    text = (f"prerequisite skill_refresh {_STATUS_WORDS[rep['status']]}"      # the decision first: the cap cuts the end
            + (f" (run {run}, window to {rep.get('window_end') or 'unknown'})" if run else "")
            + ": " + _DECISION_WORDS.get(str(rep.get("decision")), f"decision {rep.get('decision')}")
            + (f" ({n} skipped)" if rep.get("decision") == "skip_skill_candidates" and isinstance(n, int) and n else "")
            + f". Why: {rep.get('reason') or 'no reason recorded'}")
    text = " ".join(text.split())
    return text if len(text) <= 300 else text[:297] + "..."


def report_problems(sr: Any) -> list[str]:
    """Contract check of a night report's `prerequisites.skill_refresh` (rsi_night.validate_report)."""
    if not isinstance(sr, dict):
        return ["prerequisites.skill_refresh must be an object"]
    errs = []
    if sr.get("status") not in STATUSES:
        errs.append(f"prerequisites.skill_refresh.status {sr.get('status')!r} not in {STATUSES}")
    if sr.get("decision") not in DECISIONS:
        errs.append(f"prerequisites.skill_refresh.decision {sr.get('decision')!r} not in {DECISIONS}")
    if sr.get("mode") not in MODES:
        errs.append(f"prerequisites.skill_refresh.mode {sr.get('mode')!r} not in {MODES}")
    if sr.get("run_status") is not None and sr["run_status"] not in RUN_STATUSES:
        errs.append(f"prerequisites.skill_refresh.run_status {sr['run_status']!r} not in {RUN_STATUSES}")
    for k in ("run", "window_start", "window_end", "reason"):
        if sr.get(k) is not None and not isinstance(sr[k], str):
            errs.append(f"prerequisites.skill_refresh.{k} must be a string or null")
    if (sr.get("status") == "fresh") != (sr.get("decision") == "use"):
        errs.append("prerequisites.skill_refresh: decision use goes with status fresh only")
    return errs
