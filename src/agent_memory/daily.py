"""`amem daily`: the learning loop's daily job (docs/daily.md).

Steps, in order. A failed step is recorded and the next step still runs:

0. wait: `wait_for_db` tries the database for up to ~10 minutes first (launchd runs the job
   right after wake, often before DNS is back). The steps run either way.

1. ingest: the routines.log that cloud routines publish (`ingest git-ref` logic).
1b. tasks: dispatch tasks from `<parent dir>/*/tasks.jsonl` (`ingest tasks` logic).
2. score: every routine with a `questions/<routine>.toml`, one routine at a time.
   One routine's Jev error or crash never stops the others.
2b. distill: `lessons distill` (docs/lessons.md) turns new dispatch runs into trial
   lessons. Off unless AMEM_DISTILL_LIVE=1 (then not a failure, and the log line has no
   distill fields). Any run that ends as `error` fails the step.
3. escalations: open escalations are compared with the ids already pushed
   (state file) and counted (`new=`). Ids no longer open are pruned. No push since 2026-10-26: the job runs at
   00:40, and the 07:30 brief job's one morning push carries the new ones (alerts.push_morning), marking an id
   pushed only after ntfy answered 200 (`push=brief` here when there are new ones).
3b. deviations (daily): `sessions ingest` + `deviations scan --limit 40` (docs/deviations.md) when
   the last scan in the state file is >= 20 hours old (AMEM_DEVIATIONS_EVERY_HOURS). Off unless AMEM_DEVIATIONS_LIVE=1 (then not
   a failure, and the log line has no deviation fields). A session error is a warning (an
   amem-daily-warn line); it fails the step only when systemic (2+ errors, at least half the sessions). A
   malformed writer item next to valid ones is a counted warning (`deviation_skipped=N`, one
   stderr line per item), not a failure. A lost database before the scan loop is retried once,
   after `wait_for_db`.
3c. edits: `edits run` (docs/edits.md) after the deviations step. Off since 2026-10-26 (the RSI night has its
   own writer and stages its own proposals) unless AMEM_DAILY_EDITS=1 and AMEM_EDITS_LIVE=1 (then not a failure
   when off, and the log line has no edit fields). Legacy: kept edits are pushed with an Undo button (not with
   --no-push). Any candidate error fails the step; the others still run. The stage-mode push (rsi-push) follows
   the same switch. No alerts step any more: the safety-slip and last-day pushes joined the brief's push.
3d. sync (daily, last): `sync run` for each sink (clickhouse, langfuse, pastdev) that has its settings and whose
   kill switch is on, SYNC_BUDGET_S each, at most once per local day (state file `sync_last_day`). An unconfigured
   or switched-off sink is a skip. A sink that fails is a warning (an amem-daily-warn line), never a failure, so
   the night's prerequisite check is never blocked by it.
3e. coverage: the deviations, score and sync steps' coverage snapshots and consumption records (coverage_daily;
   job `amem-daily`, run key = the summary line's timestamp), written best effort. A step that was off or not due
   records nothing. A write problem is a stderr line and `coverage_problems=N` on the summary line, never a failure.
   AMEM_COVERAGE_WRITE=0 turns it off.
4. log: one summary line plus one `amem-daily-fail <reason>` line per failed
   step, appended to the routines log in the house format.

The push names counts and routines only. It never holds run summaries,
failures or ids. The ntfy topic is read from notify.env and never printed.
`http_post`, `mac_notify` and `NOTIFY_ENV` are module attributes so tests can
replace them (no network in tests).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import cli_cloud, config, deviations, distill, jev, scoring, sessions_ingest, store, tasks_ingest
from agent_memory import coverage_daily as COV
from agent_memory import routines_log as RL
from agent_memory.questions import QuestionCache

NAME = "amem-daily"
TS_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
PARENT_DIR = Path.home() / ".claude" / "parent"
DEFAULT_ROUTINES_LOG = PARENT_DIR / "routines.log"
DEFAULT_STATE = PARENT_DIR / "amem-daily-state.json"
NOTIFY_ENV = PARENT_DIR / "notify.env"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
SCORE_LIMIT = 100  # per routine, same default as `amem score`
DB_WAIT_TRIES = 10  # ~10 minutes: launchd runs the job right after wake, often before DNS is back
DB_WAIT_SECONDS = 60.0
_WS_RE = re.compile(r"\s+")
_REASON_MAX = 300


@dataclass
class DailyResult:
    scored: int = 0
    missing: int = 0  # fixture mode only: runs left unscored for lack of a fixture
    ingested: int = 0
    tasks: int = 0  # new dispatch tasks ingested (tasks.jsonl)
    escalations: int = 0
    new: int = 0
    push: str = "none"  # none | phone | mac
    distilled: distill.Report | None = None  # None: the distill step was off or failed early
    deviations: deviations.ScanReport | None = None  # None: off, not due this week, or failed early
    edits: Any = None  # edits.Report; None: the edits step was off or failed early
    rsi_push: list[str] | None = None  # one result per pushed proposal; None: not stage mode, or failed early
    synced: list[Any] | None = None  # sync.SyncReport per sink run today; None: no sink due, configured and on
    replay_observations: dict[str, Any] | None = None
    telemetry: dict[str, Any] | None = None
    coverage: list[Any] = field(default_factory=list)  # coverage records of the steps that ran (step_coverage)
    coverage_notes: list[str] = field(default_factory=list)  # coverage problems: stderr + one log-line field
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # logged as <name>-warn lines; the verdict stays PASS
    lines: list[str] = field(default_factory=list)  # what was appended to the routines log
    auth_failed: bool = False

    @property
    def verdict(self) -> str:
        return "FAIL" if self.failures else "PASS"

    def fail(self, reason: str) -> None:
        self.failures.append(_reason(reason))

    def warn(self, reason: str) -> None:
        self.warnings.append(_reason(reason))


def _reason(reason: str) -> str:
    text = _WS_RE.sub(" ", reason).strip()
    return text[: _REASON_MAX - 3] + "..." if len(text) > _REASON_MAX else text


def _err(msg: str) -> None:
    print(f"amem: daily: {msg}", file=sys.stderr)


def _exc(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".strip()


# ---------------------------------------------------------------- paths


def routines_log_path(arg: Path | None = None) -> Path:
    """--routines-log, else AMEM_ROUTINES_LOG, else ~/.claude/parent/routines.log."""
    if arg is not None:
        return arg.expanduser()
    env = os.environ.get("AMEM_ROUTINES_LOG", "").strip()
    return Path(env).expanduser() if env else DEFAULT_ROUTINES_LOG


def state_path(arg: Path | None = None) -> Path:
    """--state, else AMEM_DAILY_STATE, else ~/.claude/parent/amem-daily-state.json."""
    if arg is not None:
        return arg.expanduser()
    env = os.environ.get("AMEM_DAILY_STATE", "").strip()
    return Path(env).expanduser() if env else DEFAULT_STATE


# ---------------------------------------------------------------- push


def http_post(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST and return the HTTP status. Tests replace this."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return int(resp.status)


def _applescript(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mac_notify(title: str, body: str) -> None:
    """macOS notification (the fallback). Tests replace this."""
    script = f"display notification {_applescript(body)} with title {_applescript(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=20)


def read_notify_env(path: Path) -> dict[str, str]:
    """Only NTFY_TOPIC and NTFY_SERVER from a KEY=value file. Other keys are ignored."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in ("NTFY_TOPIC", "NTFY_SERVER"):
            out[key] = value.strip().strip('"').strip("'")
    return out


def send(title: str, body: str, notify: Mapping[str, str] | None = None) -> str:
    """Push like routine-audit: ntfy when a topic is set and it answers 200 ('phone'),
    else a macOS notification ('mac'). Raises nothing. Never prints the topic."""
    env = read_notify_env(NOTIFY_ENV) if notify is None else notify
    topic = (env.get("NTFY_TOPIC") or "").strip()
    if topic:
        server = (env.get("NTFY_SERVER") or DEFAULT_NTFY_SERVER).strip().rstrip("/")
        headers = {"Title": title, "Priority": "default", "Tags": "label"}
        try:
            status = http_post(f"{server}/{topic}", body.encode("utf-8"), headers)
        except Exception:
            status = None
        if status == 200:
            return "phone"
    try:
        mac_notify(title, body)
    except Exception:
        pass
    return "mac"


def message(new_rows: list[dict[str, Any]], open_count: int) -> tuple[str, str]:
    """Title and body: counts and routine names only (no summaries, failures or ids)."""
    per = Counter(str(r["routine"]) for r in new_rows)
    parts = ", ".join(f"{name} {n}" for name, n in sorted(per.items()))
    n = len(new_rows)
    title = f"amem: {n} new run{'s' if n != 1 else ''} to label"
    body = (f"{n} new escalation{'s' if n != 1 else ''} ({open_count} open): {parts}. "
            "Run `amem escalations`, then `amem label <run_id> up|down`.")
    return title, body


# ---------------------------------------------------------------- state


def load_state(path: Path, result: DailyResult) -> set[str]:
    """Run ids pushed before. A missing file is an empty set; an unreadable one is
    recorded as a failure and treated as empty."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = data["pushed_run_ids"]
        if not isinstance(ids, list):
            raise ValueError("pushed_run_ids is not a list")
        return {str(i) for i in ids}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.fail(f"state: unreadable {path.name} ({type(exc).__name__}); starting empty")
        return set()


def _state_dict(path: Path) -> dict[str, Any]:
    """The whole state file as a dict; {} when missing or unreadable (never raises)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save_state(path: Path, pushed: set[str], now: datetime) -> None:
    """Write the pushed ids. Other keys in the file (the deviations step's) are kept."""
    data = _state_dict(path)
    data.update({"pushed_run_ids": sorted(pushed), "updated_at": now.strftime(TS_FORMAT)})
    _write_state(path, data)


def plan_push(open_rows: list[dict[str, Any]], pushed_before: set[str]) -> tuple[list[dict[str, Any]], set[str]]:
    """(new rows, pushed ids still open). Ids no longer open are pruned."""
    open_ids = {str(r["run_id"]) for r in open_rows}
    kept = pushed_before & open_ids
    new_rows = [r for r in open_rows if str(r["run_id"]) not in kept]
    return new_rows, kept


# ---------------------------------------------------------------- steps


def sleep(seconds: float) -> None:
    """time.sleep. Tests replace this."""
    time.sleep(seconds)


def wait_for_db(tries: int = DB_WAIT_TRIES) -> bool:
    """True once the Postgres database answers. A failed connect (no DNS or route yet, e.g.
    right after wake) is tried again DB_WAIT_SECONDS later, `tries` times in all. Never raises:
    False means the steps run anyway and record their own failures. True without a check
    when the backend is not Postgres."""
    try:
        if config.backend() != "postgres":
            return True
    except config.BackendError:
        return True  # the steps' own guard records it
    for attempt in range(1, tries + 1):
        try:
            with store.connect(connect_timeout=10) as conn:
                conn.execute("select 1")
            return True
        except psycopg.OperationalError as exc:
            if attempt == tries:
                _err(f"db: not reachable after {tries} tries ({_exc(exc)}); running the steps anyway")
                return False
            _err(f"db: not reachable ({_exc(exc)}); try {attempt + 1}/{tries} in {DB_WAIT_SECONDS:g}s")
            sleep(DB_WAIT_SECONDS)
        except Exception as exc:  # not a network problem (bad URL, auth): waiting will not help
            _err(f"db: {_exc(exc)}")
            return False
    return False


def _postgres_guard(step: str, result: DailyResult) -> bool:
    """Scoring and escalations are Postgres only in V1, and AMEM_REQUIRE_REMOTE applies."""
    try:
        active = config.backend()
    except config.BackendError as exc:
        result.fail(f"{step}: {exc}")
        return False
    if active != "postgres":
        result.fail(f"{step}: needs the Postgres backend in V1 (backend={active})")
        return False
    error = config.remote_write_error(for_backend="postgres")
    if error is not None:
        _err(error)
        result.fail(f"{step}: AMEM_REQUIRE_REMOTE=1 refused a local or empty database")
        return False
    return True


def step_ingest(result: DailyResult) -> None:
    error = config.remote_write_error()
    if error is not None:
        _err(error)
        result.fail("ingest: AMEM_REQUIRE_REMOTE=1 refused a local or empty database")
        return
    repo, ref, path = cli_cloud.GIT_REF_REPO, cli_cloud.GIT_REF_BRANCH, cli_cloud.GIT_REF_PATH
    try:
        text = cli_cloud.fetch_gh(repo, ref, path)
        if text is None:  # 404: lost repo access or a deleted/renamed branch must not go silent
            result.fail(f"ingest: routines.log not found at {repo}@{ref}:{path} "
                        "(404: branch/file missing or no access)")
            return
        parsed = cli_cloud.parse_published(text)
        new, present = store.ingest_runs(parsed.runs)
    except Exception as exc:  # gh, parse, DB or REST error: record it, keep going
        result.fail(f"ingest: {_exc(exc)}")
        return
    result.ingested = new
    _err(f"ingest: {new} new, {present} already present, {len(parsed.unparsed)} unparsed")


def step_tasks(result: DailyResult, parent_dir: Path) -> None:
    """`amem ingest tasks` over parent_dir (docs/tasks.md). No tasks.jsonl at all is
    not a failure and touches no database."""
    try:
        if not tasks_ingest.discover(parent_dir):
            _err(f"tasks: no */tasks.jsonl under {parent_dir}; nothing to ingest")
            return
    except OSError as exc:
        result.fail(f"tasks: {_exc(exc)}")
        return
    if not _postgres_guard("tasks", result):
        return
    try:
        parsed = tasks_ingest.parse_parent(parent_dir)
        report = tasks_ingest.ingest(parsed.runs)
    except Exception as exc:  # read, parse or DB error: record it, keep going
        result.fail(f"tasks: {_exc(exc)}")
        return
    result.tasks = report.new
    _err(f"tasks: {tasks_ingest.report_line(parsed, report)}")


def run_key(now: datetime) -> str:
    """The daily's run key in its coverage records (routine_consumption.run_key): its summary line's timestamp."""
    return now.strftime(TS_FORMAT)


def _cover(result: DailyResult, build: Any, **kwargs: Any) -> None:
    """Collect one step's coverage records (written by step_coverage). A builder problem is a coverage note, never a
    step failure. Nothing when AMEM_COVERAGE_WRITE is off."""
    if not COV.enabled():
        return
    try:
        result.coverage.extend(build(**kwargs))
    except Exception as exc:  # noqa: BLE001 - coverage never fails the step it describes
        result.coverage_notes.append(_reason(f"coverage: {getattr(build, '__name__', 'records')}: {_exc(exc)}"))


def _cover_read(result: DailyResult, read: Any, *args: Any, **kwargs: Any) -> Any:
    """A read that only feeds coverage records: None (and a coverage note) when it fails or coverage is off."""
    if not COV.enabled():
        return None
    try:
        return read(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        result.coverage_notes.append(_reason(f"coverage: {getattr(read, '__name__', 'read')}: {_exc(exc)}"))
        return None


def step_score(result: DailyResult, *, limit: int = SCORE_LIMIT, now: datetime | None = None) -> None:
    now = now or datetime.now().astimezone()
    if not _postgres_guard("score", result):
        return
    if config.jev_live_flag() and jev.mode() != "live":
        result.fail("score: AMEM_JEV_LIVE=1 but TYPESAFE_API_KEY is empty; Jev ran in fixture mode")
    try:
        cache = QuestionCache()
        routines = cache.routines()
    except Exception as exc:
        result.fail(f"score: {_exc(exc)}")
        _cover(result, COV.score_records, run_key=run_key(now), now=now, reports=[], error=_exc(exc))
        return
    reports: list[scoring.ScoreReport] = []
    errors: list[str] = []
    for routine in routines:
        try:
            report = scoring.score_runs(routine=routine, limit=limit, cache=cache)
        except Exception as exc:  # bad question file, DB or SDK error: this routine only
            result.fail(f"score {routine}: {_exc(exc)}")
            errors.append(f"score {routine}: {_exc(exc)}")
            continue
        reports.append(report)
        result.scored += report.scored
        result.missing += len(report.missing)
        if report.missing:  # fixture mode only; one line per routine keeps launchd.log readable
            _err(f"score {routine}: {len(report.missing)} runs have no Jev fixture (left unscored); "
                 f"first: run {report.missing[0][0]}: {report.missing[0][2]}")
        if report.failed:
            n = len(report.failed)
            result.fail(f"score {routine}: {n} Jev error{'s' if n != 1 else ''}; first: {report.failed[0][2]}")
    totals = _cover_read(result, COV.run_totals, routines)
    _cover(result, COV.score_records, run_key=run_key(now), now=now, reports=reports, errors=errors, totals=totals)


def step_distill(result: DailyResult, *, limit: int = distill.DEFAULT_LIMIT) -> None:
    """`lessons distill` for new dispatch runs. Off (not a failure) unless AMEM_DISTILL_LIVE=1."""
    if not distill.live():
        _err(f"distill: off ({distill.LIVE_ENV} is not 1)")
        return
    if not _postgres_guard("distill", result):
        return
    try:
        report = distill.distill(limit=limit)
    except Exception as exc:  # bad thresholds or DB error before the loop
        result.fail(f"distill: {_exc(exc)}")
        return
    result.distilled = report
    _err(f"distill: {report.summary()}")
    if report.errors:
        first = next(o for o in report.outcomes if o.decision == "error")
        n = report.errors
        result.fail(f"distill: {n} error{'s' if n != 1 else ''}; first: run {first.run_id}: {first.reason}")


DEVIATIONS_KEY = "deviations_last_scan"
DEVIATIONS_EVERY_ENV = "AMEM_DEVIATIONS_EVERY_HOURS"
DEVIATIONS_EVERY_DEFAULT_HOURS = 20  # daily at 00:40, with slack so launchd jitter never skips a day
DEVIATIONS_LIMIT = 40  # sessions per daily scan (one writer call each); 15 left a growing backlog


def deviations_every() -> timedelta:
    """AMEM_DEVIATIONS_EVERY_HOURS (a positive number), else 20 hours."""
    raw = os.environ.get(DEVIATIONS_EVERY_ENV, "").strip()
    try:
        hours = float(raw) if raw else DEVIATIONS_EVERY_DEFAULT_HOURS
    except ValueError:
        hours = DEVIATIONS_EVERY_DEFAULT_HOURS
    if hours <= 0:
        hours = DEVIATIONS_EVERY_DEFAULT_HOURS
    return timedelta(hours=hours)


def step_deviations(result: DailyResult, *, state_file: Path, now: datetime) -> None:
    """Daily: `sessions ingest` + `deviations scan` (docs/deviations.md). Off (not a failure)
    unless AMEM_DEVIATIONS_LIVE=1. Runs when the last scan in the state file is at least
    `deviations_every()` old (or missing). The timestamp moves only when the scan finished; those sessions
    are retried on the next run. Per-session errors are a warning (one bad transcript or model reply must not
    block amem-rsi-night); they fail the step only when systemic: 2+ errors and at least half the sessions."""
    if not deviations.live():
        _err(f"deviations: off ({deviations.LIVE_ENV} is not 1)")
        return
    last_raw = _state_dict(state_file).get(DEVIATIONS_KEY)
    last = None
    if isinstance(last_raw, str):
        try:
            last = datetime.strptime(last_raw, TS_FORMAT)
        except ValueError:
            last = None
    every = deviations_every()
    if last is not None and now - last < every:
        _err(f"deviations: not due (last scan {last_raw}, every {every.total_seconds() / 3600:g} hours)")
        return
    if not _postgres_guard("deviations", result):
        return
    since = now - timedelta(days=deviations.DEFAULT_DAYS)   # deviations.scan's own window start
    cover = {"run_key": run_key(now), "since": since, "now": now}
    ingested = before = None
    for attempt in (1, 2):
        try:
            root = sessions_ingest.projects_dir()
            if not root.is_dir():
                raise FileNotFoundError(f"no projects folder at {root}")
            ingested = sessions_ingest.ingest(root, days=deviations.DEFAULT_DAYS, now=now)
            _err(f"deviations: {ingested.line()}")
            before = _cover_read(result, COV.scan_states, since)
            report = deviations.scan(days=deviations.DEFAULT_DAYS, limit=DEVIATIONS_LIMIT, now=now)
            break
        except psycopg.OperationalError as exc:  # the database went away: one retry once it is back
            if attempt == 1 and wait_for_db():
                _err(f"deviations: {_exc(exc)}; the database answers again, retrying once")
                continue
            result.fail(f"deviations: {_exc(exc)}")
            _cover(result, COV.deviations_records, **cover, ingest=ingested, error=f"deviations: {_exc(exc)}")
            return
        except Exception as exc:  # ingest, question file or DB error before the loop
            result.fail(f"deviations: {_exc(exc)}")
            _cover(result, COV.deviations_records, **cover, ingest=ingested, error=f"deviations: {_exc(exc)}")
            return
    result.deviations = report
    after = _cover_read(result, COV.scan_states, since)
    _cover(result, COV.deviations_records, **cover, ingest=ingested, before=before, after=after, report=report,
           evidence=_cover_read(result, COV.deviation_refs, report) or [])
    _err(report.summary())
    if report.auth_failed:
        result.auth_failed = True
        result.fail(f"deviations: Claude authentication failed; stopped scan; {report.pending} sessions pending")
        return  # Keep the last successful scan timestamp unchanged for recovery.
    for o in report.outcomes:  # a counted warning (deviation_skipped=N), not a failure: see docs/daily.md
        for reason in o.skipped_reasons:
            _err(f"deviations: invalid item skipped: session {o.session_id}: {reason}")
    if report.errors:
        first = next(o for o in report.outcomes if o.status == "error")
        n = report.errors
        reason = (f"deviations: {n} error{'s' if n != 1 else ''} in {len(report.outcomes)} sessions; "
                  f"first: session {first.session_id}: {first.reason}")
        if n >= 2 and 2 * n >= len(report.outcomes):
            result.fail(reason)
        else:
            _err(f"warning: {reason}")
            result.warn(reason)
    data = _state_dict(state_file)
    data[DEVIATIONS_KEY] = now.strftime(TS_FORMAT)
    data.setdefault("pushed_run_ids", [])
    try:
        _write_state(state_file, data)
    except OSError as exc:
        result.fail(f"state: cannot write {state_file.name}: {_exc(exc)}")


DAILY_EDITS_ENV = "AMEM_DAILY_EDITS"


def edits_in_daily(environ: Mapping[str, str] | None = None) -> bool:
    """Since 2026-10-26 amem-daily runs no edits step: the RSI night has its own writer and stages its own
    proposals. AMEM_DAILY_EDITS=1 brings the old morning edits step back (and with it the stage-mode push);
    AMEM_EDITS_LIVE / AMEM_EDITS_MODE alone no longer do. `amem edits run|undo|undo-poll` are not affected."""
    env = os.environ if environ is None else environ
    return env.get(DAILY_EDITS_ENV, "").strip() == "1"


def step_edits(result: DailyResult, *, push: bool, notify: Mapping[str, str] | None) -> None:
    """`amem edits run` (docs/edits.md): edit the rules Jev flagged, push each kept edit with
    an Undo button. Off (not a failure) unless AMEM_DAILY_EDITS=1 and AMEM_EDITS_LIVE=1. One candidate's
    writer or improve error never stops the others; any error fails the step."""
    from agent_memory import edits  # lazy: edits_push imports this module

    if not edits_in_daily():
        _err(f"edits: not run by amem-daily ({DAILY_EDITS_ENV} is not 1; the night writes and stages proposals)")
        return
    if not edits.live():
        _err(f"edits: off ({edits.LIVE_ENV} is not 1)")
        return
    if not _postgres_guard("edits", result):
        return
    try:
        report = edits.run(push=push, notify=dict(notify) if notify is not None else None)
    except Exception as exc:  # the candidate query failed before the loop
        result.fail(f"edits: {_exc(exc)}")
        return
    result.edits = report
    for o in report.outcomes:
        _err(f"edits: {o.line()}")
    _err(report.summary())
    if report.errors:
        first = next(o for o in report.outcomes if o.status in ("error", "proposed"))
        n = report.errors
        result.fail(f"edits: {n} error{'s' if n != 1 else ''}; first: {first.source} {first.rule_id}: {first.reason}")


def step_rsi_push(result: DailyResult, *, push: bool, notify: Mapping[str, str] | None) -> None:
    """Stage mode only (AMEM_EDITS_LIVE=1 and AMEM_EDITS_MODE=stage): one push per newly staged proposal, with
    Reject and the terminal Apply line (`amem rsi push`, docs/rsi/approval.md). Nothing here changes production."""
    from agent_memory import edits, rsi_approval  # lazy, like step_edits

    if not (edits_in_daily() and edits.live() and edits.stage_mode()):
        return
    if not _postgres_guard("rsi-push", result):
        return
    try:
        result.rsi_push = rsi_approval.push_proposals(push=push, notify=dict(notify) if notify is not None else None)
    except Exception as exc:  # the proposals stay staged; the next run pushes them
        result.fail(f"rsi-push: {_exc(exc)}")
        return
    _err(f"rsi-push: {len(result.rsi_push)} proposal(s) {' '.join(result.rsi_push)}".rstrip())


def step_escalations(result: DailyResult, *, push: bool = True, state_file: Path, now: datetime) -> None:
    """Count the open escalations and the new ones (not pushed yet). Since 2026-10-26 amem-daily runs at 00:40 and
    sends no phone push: the 07:30 brief job's single morning push carries the new escalations
    (alerts.push_morning) and marks them pushed (`push=brief` on the log line; `none` with --no-push or nothing
    new). Here the state only loses ids that are no longer open."""
    if not _postgres_guard("escalations", result):
        return
    try:
        rows = scoring.escalations()
    except Exception as exc:
        result.fail(f"escalations: {_exc(exc)}")
        return
    pushed_before = load_state(state_file, result)
    new_rows, pushed = plan_push(rows, pushed_before)
    result.escalations = len(rows)
    result.new = len(new_rows)
    if new_rows and push:
        result.push = "brief"   # the morning brief's push carries them
    try:
        save_state(state_file, pushed, now)
    except OSError as exc:
        result.fail(f"state: cannot write {state_file.name}: {_exc(exc)}")


SYNC_KEY = "sync_last_day"
SYNC_RECOVERY_KEY = "sync_incomplete_day"
SYNC_BUDGET_S = 120.0  # per sink and day; sync.run stops between batches, and its client-side daily limits apply
SYNC_STOP = ("failed", "unavailable")  # a sink that is configured and switched on but did not sync: a warning


def sync_configured(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """The sink has its settings (URL and keys). Reads env only: no network, no database."""
    env = os.environ if environ is None else environ
    if name == "clickhouse":
        from agent_memory.integrations import clickhouse
        return not clickhouse.config(env).problem
    if name == "langfuse":
        from agent_memory.integrations import langfuse
        return not langfuse.LangfuseClient.from_env(env).problem
    if name == "pastdev":
        from agent_memory.integrations import pastdev
        return pastdev.config(env) is not None
    return False


def step_sync(result: DailyResult, *, state_file: Path, now: datetime) -> None:
    """Daily: `amem sync run --sink <sink>` (incremental, SYNC_BUDGET_S each) for every sink that is configured and
    switched on (docs/daily.md; docs/integrations/cloud-activation.md: daily, so ClickHouse can idle), at most once
    per local day (the state file's sync_last_day), plus one completion after a failed daily scan
    recovers. Repeated failed scans do not repeat exports. A sink without its settings or with its switch off is a skip.
    Never a failure: a sink that fails, is unreachable or hits the provider's cap is a warning, so the night's
    prerequisite check never sees a `sync` step fail (rsi_schedule: ingest, tasks and deviations block)."""
    from agent_memory import cli_integrations
    from agent_memory.integrations import sync

    day = now.date().isoformat()
    prior = _state_dict(state_file)
    recovering = (prior.get(SYNC_RECOVERY_KEY) == day
                  and not result.failures and not result.auth_failed)
    if prior.get(SYNC_KEY) == day and not recovering:
        _err(f"sync: not due (ran on {day})")
        return
    if recovering:
        _err("sync: completing exports after the daily scan recovered")
    todo = []
    for name in sync.SINKS:
        if sync.switched_off(name):
            _err(f"sync {name}: off (kill switch)")
        elif not sync_configured(name):
            _err(f"sync {name}: not configured")
        else:
            todo.append(name)
    if not todo:
        return
    try:
        backend = config.backend()
        refused = config.remote_write_error(for_backend="postgres") if backend == "postgres" else None
    except config.BackendError as exc:
        backend, refused = "?", str(exc)
    if backend != "postgres" or refused is not None:
        result.warn(f"sync: needs the Postgres backend ({refused or f'backend={backend}'}); not run")
        return
    reports: list[Any] = []
    for name in todo:
        try:
            sink = cli_integrations.sink_for(name)
            with store.connect() as conn:
                rep = sync.run(sink, conn, budget_s=SYNC_BUDGET_S)
        except Exception as exc:  # adapter, database or provider error: this sink only, never the day
            result.warn(f"sync {name}: {_exc(exc)}")
            _cover(result, COV.sync_records, run_key=run_key(now), now=now, sink=name, error=f"sync {name}: {_exc(exc)}")
            continue
        reports.append(rep)
        _err(rep.line())
        if rep.status in SYNC_STOP or (rep.status == "capped" and rep.detail.startswith("provider cap")):
            result.warn(f"sync {name}: {rep.status} ({rep.detail})")
        _cover(result, COV.sync_records, run_key=run_key(now), now=now, sink=name, report=rep)
    result.synced = reports
    data = _state_dict(state_file)
    data[SYNC_KEY] = day
    if result.failures or result.auth_failed:
        data[SYNC_RECOVERY_KEY] = day
    else:
        data.pop(SYNC_RECOVERY_KEY, None)
    data.setdefault("pushed_run_ids", [])
    try:
        _write_state(state_file, data)
    except OSError as exc:
        result.warn(f"sync: cannot write {state_file.name}: {_exc(exc)}")


def step_coverage(result: DailyResult) -> None:
    """Write the steps' coverage records (coverage_daily.write: one savepoint each, never raises). Problems become
    stderr lines and one `coverage_problems=N` field on the summary line; the verdict never changes."""
    result.coverage_notes.extend(COV.write(result.coverage))
    for note in result.coverage_notes:
        _err(note)


def step_observations(result: DailyResult, *, now: datetime, reports_dir: Path) -> None:
    """One existing daily owner: recover immutable replay evidence and analyze
    current measurements deterministically. No model, promotion or new routine.
    Capture failures become explicit coverage gaps, never an invented all clear.
    """
    if config.backend() != "postgres":
        return
    from agent_memory import coverage as CV, replay_evidence, telemetry_analysis as TA
    from datetime import timedelta
    try:
        recovered = replay_evidence.run(limit=100)
        result.replay_observations = recovered.as_dict()
        if recovered.missing_artifacts or recovered.unresolved_native:
            result.warn(f"replay evidence: artifacts_missing={recovered.missing_artifacts} "
                        f"native_sessions_unresolved={recovered.unresolved_native}; historical gaps are not guessed")
        for failure in recovered.failures[:5]:
            result.coverage_notes.append("replay evidence: " + failure)
    except Exception as exc:
        result.coverage_notes.append(f"replay evidence: analysis_failed ({type(exc).__name__})")
    try:
        report = TA.run(now=now, days=7, limit=5)
        report['replay_evidence'] = result.replay_observations
        path = TA.save(report, reports_dir)
        result.telemetry = {"format": TA.FORMAT, "findings": len(report['findings']),
                            "stored_spans": report['span_coverage']['stored'],
                            "stored_test_runs": report['test_coverage']['stored'], "report_path": str(path)}
        if CV.writes_enabled():
            result.coverage.extend(TA.records(report, run_key=run_key(now)))
    except Exception as exc:
        result.coverage_notes.append(f"telemetry analysis: analysis_failed ({type(exc).__name__})")
        for dataset in (("command_timings:execution_spans", "test_timings:test_timing_runs",'reports_checkpoints:rsi_checkpoints')
                        if CV.writes_enabled() else ()):
            result.coverage += [CV.Snapshot(dataset, now-timedelta(days=7), now,
                                           "stored rows in explicit analysis window; query failed", None),
                               CV.Consumption(NAME, run_key(now), dataset, TA.FORMAT,
                                              now-timedelta(days=7), now, status="analysis_failed",
                                              detail=f"query/report unavailable: {type(exc).__name__}")]


def log_lines(ts: str, result: DailyResult, secs: int) -> list[str]:
    head = (f"{ts} {NAME} {result.verdict} scored={result.scored} escalations={result.escalations} "
            f"new={result.new} push={result.push} tasks={result.tasks} ")
    if result.distilled is not None:  # only when the step ran, so the line stays the same when it is off
        d = result.distilled
        head += (f"distilled={len(d.outcomes)} lessons_new={d.new} lessons_merged={d.merged} "
                 f"lessons_rejected={d.rejected} distill_errors={d.errors} ")
    if result.deviations is not None:  # only when the step ran, so other runs keep the same line
        s = result.deviations
        v = s.verdicts()
        head += (f"deviations={s.found} deviation_sessions={len(s.outcomes)} "
                 f"deviations_edit={v['edit_instruction']} deviations_enforce={v['enforce']} "
                 f"deviations_leave={v['leave']} deviation_errors={s.errors} deviation_skipped={s.skipped} ")
    if result.edits is not None:  # only when the step ran
        e = result.edits
        head += (f"edits={e.edits} edits_kept={e.kept} edits_reverted={e.reverted} "
                 f"edits_suggested={e.suggested} edit_errors={e.errors} ")
    if result.rsi_push is not None:  # stage mode only, so legacy lines stay the same
        staged = result.edits.count("staged") if result.edits is not None else 0
        head += f"edits_staged={staged} rsi_push={len(result.rsi_push)} "
    if result.synced is not None:  # only when the sync step ran a sink, so other runs keep the same line
        head += (f"sync={','.join(f'{r.sink}:{r.status}' for r in result.synced) or 'none'} "
                 f"sync_rows={sum(r.rows for r in result.synced)} ")
    if result.coverage_notes:  # a coverage problem is a note, never a failure; absent when there is none
        head += f"coverage_problems={len(result.coverage_notes)} "
    if result.telemetry is not None and result.telemetry['findings']:
        head += f"telemetry_findings={result.telemetry['findings']} "
    head += f"{secs}s"
    return [head, *(f"{ts} {NAME}-fail {reason}" for reason in result.failures),
            *(f"{ts} {NAME}-warn {reason}" for reason in result.warnings)]


def _run_daily(
    *,
    push: bool = True,
    routines_log: Path | None = None,
    state: Path | None = None,
    now: datetime | None = None,
    notify: Mapping[str, str] | None = None,
    parent_dir: Path | None = None,
) -> DailyResult:
    """Run the steps. Appends to the routines log; raises OSError only when that
    append fails. `notify` replaces notify.env (tests). `parent_dir` (tasks.jsonl
    folders) defaults to the routines log's folder, i.e. ~/.claude/parent."""
    start = time.monotonic()
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:  # the house format needs an offset
        now = now.astimezone()
    result = DailyResult()
    log_file = routines_log_path(routines_log)
    # The 01:30 night waits while this marker is live (rsi_schedule.wait_for_prerequisites). It goes away only
    # after the summary line is written, so the night never sees "not running" without the line.
    try:
        RL.mark_running(log_file, NAME, now)
    except OSError as exc:
        _err(f"running marker: {_exc(exc)} (the night will not wait for this run)")
    try:
        wait_for_db()
        step_ingest(result)
        step_tasks(result, (parent_dir or log_file.parent).expanduser())
        step_score(result, now=now)
        step_distill(result)
        step_escalations(result, push=push, state_file=state_path(state), now=now)
        step_deviations(result, state_file=state_path(state), now=now)
        from agent_memory import claude_auth, daily_recovery
        result.auth_failed = result.auth_failed or claude_auth.failed()
        if result.auth_failed:
            try:
                daily_recovery.record(result, state=state_path(state), now=now, push=push, notify=notify)
            except (OSError, ValueError) as exc:
                result.fail(f"auth-recovery: cannot persist recovery state: {type(exc).__name__}")
        if not result.auth_failed:
            step_edits(result, push=push, notify=notify)
        step_rsi_push(result, push=push, notify=notify)
        step_observations(result, now=now, reports_dir=log_file.parent / "reports")
        step_sync(result, state_file=state_path(state), now=now)  # after replay observations, so exports include them
        step_coverage(result)
        result.auth_failed = result.auth_failed or claude_auth.failed()
        try:
            # Catch auth failures from optional steps as well, without duplicate alerts.
            daily_recovery.record(result, state=state_path(state), now=now, push=push, notify=notify)
        except (OSError, ValueError) as exc:
            result.fail(f"auth-recovery: cannot persist recovery state: {type(exc).__name__}")
        secs = int(time.monotonic() - start)
        result.lines = log_lines(now.strftime(TS_FORMAT), result, secs)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in result.lines))
    finally:
        RL.clear_running(log_file, NAME)
    return result


def _run_daily_locked(*, push: bool = True, routines_log: Path | None = None, state: Path | None = None,
              now: datetime | None = None, notify: Mapping[str, str] | None = None,
              parent_dir: Path | None = None) -> DailyResult:
    from agent_memory import claude_auth, daily_recovery
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    log_file = routines_log_path(routines_log)
    with daily_recovery.job_lock(log_file.with_name("amem-daily.lock")) as acquired:
        if not acquired:
            result = DailyResult()
            result.fail("overlap: another daily scan is running")
            return result
        with claude_auth.batch() as auth:
            result = _run_daily(push=push, routines_log=log_file, state=state, now=now,
                                notify=notify, parent_dir=parent_dir)
            result.auth_failed = result.auth_failed or auth.failed
        return result


def run_daily(**kwargs):
    from agent_memory.client_maintenance import lease
    log = routines_log_path(kwargs.get('routines_log'))
    with lease(log) as acquired:
        if not acquired:
            result = DailyResult()
            result.fail('client maintenance: update in progress')
            now = kwargs.get('now') or datetime.now().astimezone()
            result.lines = log_lines(now.strftime(TS_FORMAT), result, 0)
            log.parent.mkdir(parents=True,exist_ok=True)
            with log.open('a') as fh:
                fh.write('\n'.join(result.lines)+'\n')
            return result
        return _run_daily_locked(**kwargs)
