"""Coverage snapshots and consumption records written by the daily-side routines (data-use plan R4).

The contract is coverage.py (docs/data-use/coverage.md). This module only builds records from what each job already
counted and writes them best effort. Dataset names are `dataset_key(<family>, <table>)`, the same names the night uses.

| job          | step       | dataset                      | snapshot (dataset + window)              | consumption (run + dataset)        |
|--------------|------------|------------------------------|------------------------------------------|------------------------------------|
| amem-daily   | deviations | sessions:sessions            | transcripts in the ingest window vs.     | sessions in the window -> picked   |
|              |            |                              | rows collected                           | -> scanned; skip reasons; findings |
| amem-daily   | deviations | deviations:deviations        | scan-eligible sessions vs. scanned       | -                                  |
| amem-daily   | score      | routine_runs:runs            | runs of question routines with scores    | unscored -> attempted -> scored    |
| amem-daily   | sync       | <family>:<table>.<sink>      | rows read vs. delivered (push acknowledged, readback unmeasured) | read -> attempted -> delivered |
| rsi-skills   | refresh    | skills:skill_calls           | log lines vs. calls collected and linked | calls -> prod -> scored            |
| rsi-harvest  | harvest    | deviations:deviations,       | handoffs only (rows in the window)       | sources -> new -> drafted          |
|              |            | coordination:handoffs        |                                          |                                    |

Rules (plan R4):
- A step that is off or not due records nothing: the guard reads a missing record as `analysis_not_run`, and a
  rerun under the same run key never overwrites what the run really did.
- An empty eligible window is `no_eligible_data` (snapshot) with `analysis_not_run` (consumption), never
  `not_collected` or `analysis_failed`.
- Rows read but not analysed are `read_excluded` or `analysis_not_run`, never `used_no_finding`.
- A failed step is `analysis_failed` with a short detail.
- Evidence refs: at most 20, only the records the job's output names (a drafted case's source, a stored deviation).
- Nothing here raises into the job. Builders return records; `write` returns problem lines for the job's notes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_memory import coverage as CV
from agent_memory import store
from agent_memory.integrations import refs

Record = CV.Snapshot | CV.Consumption

DAILY_JOB = "amem-daily"
SKILLS_JOB = "rsi-skills"
HARVEST_JOB = "rsi-harvest"

SESSIONS = CV.dataset_key("sessions", "sessions")
DEVIATIONS = CV.dataset_key("deviations", "deviations")
RUNS = CV.dataset_key("routine_runs", "runs")
SKILL_CALLS = CV.dataset_key("skills", "skill_calls")
HANDOFFS = CV.dataset_key("coordination", "handoffs")

DETAIL_MAX = 300
SYNC_DEFAULT_WINDOW = timedelta(days=1)       # a sink with no earlier sync: the delta of about one daily run
# Sink cursor keys that are not a Supabase table of a data family (data_families has the rest).
_SINK_TABLE_FAMILY = {"research_runs": "evidence_links", "sandbox_runs": "experiments"}


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """AMEM_COVERAGE_WRITE is not off. When off, the jobs also skip the extra reads that feed the records."""
    return CV.writes_enabled(environ)


def _detail(text: Any) -> str | None:
    if text is None or text == "":
        return None
    t = " ".join(str(text).split())
    return t if len(t) <= DETAIL_MAX else t[: DETAIL_MAX - 3] + "..."


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _failed(job: str, run_key: str, dataset: str, version: str, reason: Any, *,
            window: tuple[datetime, datetime] | None = None, run_id: Any = None) -> CV.Consumption:
    start, end = window if window else (None, None)
    return CV.Consumption(job=job, run_key=run_key, dataset=dataset, analysis_version=version, window_start=start,
                          window_end=end, status="analysis_failed", detail=_detail(reason) or "failed",
                          run_id=run_id)


def write(records: Iterable[Record], *, db_url: str | None = None,
          environ: Mapping[str, str] | None = None) -> list[str]:
    """Write the records (one savepoint each, coverage.record_best_effort). Returns one problem line per record that
    failed, or one line when the database could not be reached. Never raises; writes nothing when switched off."""
    recs = list(records)
    if not recs or not CV.writes_enabled(environ):
        return []
    try:
        with store.connect(db_url, connect_timeout=10) as conn:
            return CV.record_best_effort(conn, recs, environ=environ)
    except Exception as exc:  # noqa: BLE001 - coverage never fails the job it describes
        return [f"coverage: not written ({type(exc).__name__}: {str(exc).strip()[:160]})"]


# =========================================================================== amem-daily: deviations step

SCAN_STATES_SQL = """
select case
         when s.kind = 'eval' then 'kind_eval'
         when coalesce(s.env, 'prod') = 'sandbox' then 'env_sandbox'
         when s.scanned_at is not null then 'already_scanned'
         when coalesce(s.meta ->> 'scan_status', '') = 'skipped' then 'released'
         when coalesce(s.transcript_bytes, 0) < %(min_bytes)s
              or coalesce((s.meta ->> 'tool_calls')::int, 0) + coalesce((s.meta ->> 'prompts')::int, 0)
                 < %(min_activity)s then 'tiny'
         when coalesce((s.meta ->> 'scan_errors')::int, 0) >= %(max_errors)s then 'scan_errors_cap'
         when s.kind = 'worker' and coalesce(s.meta ->> 'agent_type', '') <> 'worker'
              and exists (select 1 from sessions p
                          where p.session_id = s.meta ->> 'parent_session' and p.scanned_at is not null)
              then 'covered_by_parent'
         else 'pending'
       end as state, count(*)
from sessions s
where coalesce(s.started_at, s.created_at) >= %(since)s
group by 1
"""
# The states mirror deviations._PICK_SQL: `pending` is exactly what a scan may pick.


def scan_states(since: datetime, *, db_url: str | None = None) -> Counter[str]:
    """Sessions of the scan window (started at or after `since`) by scan state."""
    from agent_memory import deviations as D

    with store.connect(db_url, connect_timeout=10) as conn:
        rows = conn.execute(SCAN_STATES_SQL, {"since": since, "min_bytes": D.MIN_BYTES,
                                              "min_activity": D.MIN_ACTIVITY,
                                              "max_errors": D.MAX_SCAN_ERRORS}).fetchall()
    return Counter({str(state): int(n) for state, n in rows})


def deviation_refs(report: Any, *, db_url: str | None = None) -> list[str]:
    """amem:deviation/<id> of the deviations this scan stored, at most MAX_EVIDENCE_REFS (scan order)."""
    from agent_memory import deviations as D

    keys = [D.source_key(o.session_id, j.finding) for o in report.outcomes if o.status == "ok" for j in o.judged]
    if not keys:
        return []
    with store.connect(db_url, connect_timeout=10) as conn:
        found = dict(conn.execute("select source_key, id from deviations where source_key = any(%s)",
                                  (keys,)).fetchall())
    out = [refs.amem("deviation", str(found[k])) for k in dict.fromkeys(keys) if k in found]
    return out[: CV.MAX_EVIDENCE_REFS]


def deviations_version() -> str:
    from agent_memory import deviations as D, distill

    return f"deviations-scan:{D.QUESTION_FILE}:writer={distill.WRITER_MODEL}"


def deviations_records(*, run_key: str, since: datetime, now: datetime, ingest: Any = None,
                       before: Mapping[str, int] | None = None, after: Mapping[str, int] | None = None,
                       report: Any = None, evidence: Sequence[str] = (), error: str | None = None) -> list[Record]:
    """The deviations step of one amem-daily run.

    `ingest` (sessions_ingest.IngestReport) gives the sessions:sessions snapshot of the ingest window. `before` /
    `after` are scan_states() just before and after the scan: before -> the consumption funnel and its skip
    reasons, after -> the deviations:deviations snapshot (eligible sessions in the window vs. scanned). `error`: the
    step failed (analysis_failed)."""
    window = (since, now)
    version = deviations_version()
    out: list[Record] = []
    if ingest is not None:
        collected = max(int(ingest.files) - int(ingest.skipped), 0)
        bad = int(getattr(ingest, "cloud_bad", 0) or 0)
        out.append(CV.Snapshot(
            dataset=SESSIONS, window_start=since, window_end=now,
            denominator="transcripts touched in the window (not empty, not a resumed copy) plus cloud digests",
            eligible=collected + bad, collected=collected, last_collected_at=now,
            measured_fields=("kind", "tool", "outcome", "instructions", "skills"),
            detail=_detail(f"{bad} cloud digest(s) could not be ingested") if bad else None))
    if error is not None:
        out.append(_failed(DAILY_JOB, run_key, SESSIONS, version, error, window=window))
        return out
    if report is None:
        return out
    if after is not None:
        scanned, pending, capped = (int(after.get(k, 0)) for k in ("already_scanned", "pending", "scan_errors_cap"))
        gaps = []
        if pending:
            gaps.append(f"{pending} eligible session(s) not scanned yet")
        if capped:
            gaps.append(f"{capped} given up after repeated scan errors")
        out.append(CV.Snapshot(
            dataset=DEVIATIONS, window_start=since, window_end=now,
            denominator="sessions started in the window that the deviation scan covers (not eval, sandbox, tiny, "
                        "released, or a subagent whose parent was scanned)",
            eligible=scanned + pending + capped, collected=scanned, pending=pending, rejected=capped,
            measured_fields=("rule_id", "followed_grade", "result_grade", "jev_verdict"),
            last_analysed_at=now, checkpoints={"deviations_last_scan": now.isoformat()},
            source_versions={"scan": version}, detail=_detail("; ".join(gaps)) if gaps else None))
    status = Counter(o.status for o in report.outcomes)
    considered = len(report.outcomes)
    used = status["ok"]
    skipped: Counter[str] = Counter()
    notes: list[str] = []
    if before is not None:
        pre = Counter({k: int(v) for k, v in before.items() if v})
        pending_pre = pre.pop("pending", 0)
        skipped.update(pre)
        if pending_pre > considered:
            skipped["over_limit"] += pending_pre - considered
        queried = sum(pre.values()) + max(pending_pre, considered)
    else:
        queried = considered
        notes.append("the window's sessions could not be classified: counts cover the picked sessions only")
    if status["error"]:
        skipped["scan_error"] += status["error"]
    if status["skipped"]:
        skipped["released"] += status["skipped"]
    first_error = next((o for o in report.outcomes if o.status == "error"), None)
    if first_error is not None:
        notes.insert(0, f"{status['error']} of {considered} session(s) failed; first: {first_error.reason}")
    if getattr(report, "skipped", 0):
        notes.append(f"{report.skipped} malformed writer item(s) skipped")
    failed = status["error"] > 0 and used == 0
    out.append(CV.Consumption(
        job=DAILY_JOB, run_key=run_key, dataset=SESSIONS, analysis_version=version, window_start=since,
        window_end=now, source_checkpoint=f"sessions started >= {since.isoformat()}", queried=queried,
        considered=considered, used=used, skipped=dict(skipped), findings=0 if failed else int(report.found),
        evidence_refs=() if failed else tuple(evidence)[: CV.MAX_EVIDENCE_REFS],
        status="analysis_failed" if failed else None, detail=_detail("; ".join(notes)) if notes else None))
    return out


# =========================================================================== amem-daily: score step

RUN_TOTALS_SQL = """
select min(r.started_at), count(*), count(*) filter (where exists (select 1 from scores s where s.run_id = r.id))
from runs r where r.routine = any(%s)
"""


def run_totals(routines: Sequence[str], *, db_url: str | None = None) -> tuple[datetime | None, int, int]:
    """(earliest start, runs, runs with scores) of the routines that have a question file."""
    with store.connect(db_url, connect_timeout=10) as conn:
        first, n, scored = conn.execute(RUN_TOTALS_SQL, (list(routines),)).fetchone()
    return first, int(n), int(scored)


def score_version() -> str:
    from agent_memory import jev

    return f"jev-score:{jev.MODEL}:{jev.mode()}"


def score_records(*, run_key: str, now: datetime, reports: Sequence[Any], errors: Sequence[str] = (),
                  totals: tuple[datetime | None, int, int] | None = None, error: str | None = None) -> list[Record]:
    """The score step: `reports` (scoring.ScoreReport, one per routine that ran), `errors` (routines that raised),
    `totals` (run_totals after scoring). `error`: the step failed before any routine ran."""
    version = score_version()
    if error is not None:
        return [_failed(DAILY_JOB, run_key, RUNS, version, error)]
    out: list[Record] = []
    window = None
    if totals is not None:
        first, n, scored = totals
        start = min(_utc(first), now) if first is not None and n else now
        window = (start, now)
        unscored = n - scored
        out.append(CV.Snapshot(
            dataset=RUNS, window_start=start, window_end=now,
            denominator="runs of routines with a question file (all time, to the end of the score step)",
            eligible=n, collected=n, measured_fields=() if unscored else ("scores",),
            unmeasured_fields=("scores",) if unscored else (), last_analysed_at=now,
            detail=_detail(f"{unscored} run(s) without scores") if unscored else None))
    queried = sum(int(getattr(r, "candidates", 0)) for r in reports)
    used = sum(int(r.scored) for r in reports)
    missing = sum(len(r.missing) for r in reports)
    failed = sum(len(r.failed) for r in reports)
    considered = used + missing + failed
    queried = max(queried, considered)
    skipped = Counter({"no_question_file": sum(int(r.skipped) for r in reports),
                       "over_limit": sum(int(getattr(r, "over_limit", 0)) for r in reports),
                       "jev_fixture_missing": missing, "jev_error": failed})
    notes = list(errors)
    first_fail = next((f for r in reports for f in r.failed), None)
    if first_fail is not None:
        notes.append(f"{failed} Jev error(s); first: run {first_fail[0]}: {first_fail[2]}")
    status = None
    if used == 0 and (failed or errors):
        status = "analysis_failed"
    elif used == 0 and considered == 0:
        status = "analysis_not_run"           # nothing attempted: nothing unscored, or all over the limit
    start, end = window if window else (None, None)
    out.append(CV.Consumption(
        job=DAILY_JOB, run_key=run_key, dataset=RUNS, analysis_version=version, window_start=start, window_end=end,
        source_checkpoint="runs without scores at the start of the step", queried=queried, considered=considered,
        used=used, skipped={k: v for k, v in skipped.items() if v}, status=status,
        detail=_detail("; ".join(notes)) if notes else None))
    return out


# =========================================================================== amem-daily: sync step


def family_of(table: str) -> str | None:
    """The data family that owns a sink's cursor key (a Supabase table), or None (e.g. past.dev retractions)."""
    from agent_memory import data_families

    for fam in data_families.FAMILIES:
        if table in fam.tables:
            return fam.key
    return _SINK_TABLE_FAMILY.get(table)


def sink_tables(sink: str) -> list[str]:
    """The cursor keys a sink normally covers (for a sink that failed before it read anything)."""
    try:
        if sink == "clickhouse":
            from agent_memory.integrations import clickhouse
            return [t.name for t in clickhouse.TABLES]
        if sink == "langfuse":
            from agent_memory.integrations import langfuse
            return [langfuse.TABLE]
        if sink == "pastdev":
            from agent_memory.integrations import pastdev
            return list(pastdev.TABLES)
    except Exception:  # noqa: BLE001 - an import problem only loses the per-table failure rows
        return []
    return []


def sink_dataset(table: str, sink: str) -> str | None:
    fam = family_of(table)
    return CV.dataset_key(fam, f"{table}.{sink}") if fam else None


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _utc(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def sync_records(*, run_key: str, now: datetime, sink: str, report: Any = None, error: str | None = None
                 ) -> list[Record]:
    """One sink of the sync step. Per table the sink read: a snapshot of what was delivered (the sink acknowledged
    the push; nothing is read back, so `readback` stays unmeasured) and the consumption funnel. A sink that raised
    or was unavailable: analysis_failed for each table it covers."""
    version = f"sync:{sink}"
    reason = error
    if report is not None and report.status == "unavailable":
        reason = f"sink unavailable: {report.detail or 'health check failed'}"
    if reason is not None:
        out: list[Record] = []
        for table in sink_tables(sink):
            ds = sink_dataset(table, sink)
            if ds:
                out.append(_failed(DAILY_JOB, run_key, ds, version, reason))
        return out
    if report is None:
        return []
    start = _ts(getattr(report, "last_ok_at", None)) or now - SYNC_DEFAULT_WINDOW
    start = min(start, now)
    out = []
    tables: dict[str, dict[str, int]] = {}
    recovery = set()
    for name, counts in (getattr(report, 'tables', None) or {}).items():
        table = name.split(':', 1)[0] if sink == 'clickhouse' else name
        if table != name:
            recovery.add(table)
        combined = tables.setdefault(table, {})
        for key, value in counts.items():
            combined[key] = combined.get(key, 0)+int(value)
    for table, c in sorted(tables.items()):
        ds = sink_dataset(table, sink)
        if ds is None:
            continue
        seen, delivered, dead, failed = (int(c.get(k, 0)) for k in ("seen", "delivered", "dead", "failed"))
        backoff, stopped = int(c.get("backoff", 0)), int(c.get("stopped", 0))
        considered = delivered + dead + failed
        notes = [f"delivery acknowledged by {sink}; not read back"]
        if table in recovery:
            notes.append('includes bounded recovery and source reconciliation attempts')
        if stopped:
            notes.append(f"{stopped} row(s) left for the next run ({report.status}: {report.detail or 'stopped'})")
        if backoff:
            notes.append(f"{backoff} row(s) waiting for a retry (backoff)")
        if failed:
            notes.append(f"{failed} row(s) failed to push")
        if dead:
            notes.append(f"{dead} row(s) refused (dead letters)")
        out.append(CV.Snapshot(
            dataset=ds, window_start=start, window_end=now,
            denominator=(f"{table} delivery attempts, including recovery and reconciliation" if table in recovery else
                         f"{table} rows newer than the {sink} cursor, read by this sync"),
            eligible=seen, collected=delivered, pending=backoff + stopped + failed, rejected=dead,
            unmeasured_fields=("readback",), last_collected_at=now, detail=_detail("; ".join(notes))))
        status = None
        if failed and not delivered:
            status = "analysis_failed"
        elif seen and not considered:
            status = "analysis_not_run"
        out.append(CV.Consumption(
            job=DAILY_JOB, run_key=run_key, dataset=ds, analysis_version=version, window_start=start,
            window_end=now, source_checkpoint=f"sync:{sink} cursor", queried=seen, considered=considered,
            used=delivered, skipped={k: v for k, v in (("dead_letter", dead), ("push_failed", failed),
                                                       ("backoff", backoff), ("stopped", stopped)) if v},
            status=status, detail=_detail("; ".join(notes[1:])) if len(notes) > 1 else None))
    return out


# =========================================================================== rsi-skills: skill score refresh


def refresh_stats_sql() -> str:
    from agent_memory import skill_refresh, skills

    f = skills.CALL_FILTER
    return (f"select count(*), count(*) filter (where {f}), "
            f"count(*) filter (where {f} and c.session_id is null), "
            f"count(*) filter (where {f} and s.outcome = any(%(final)s)), "
            f"(select (meta ->> 'unparsed')::int from rsi_checkpoints where job = '{skill_refresh.INGEST_JOB}' "
            f"and run_key = '{skill_refresh.INGEST_KEY}' and status = 'done') "
            "from skill_calls c left join sessions s on s.session_id = c.session_id")


def refresh_stats(conn: Any) -> dict[str, int | None]:
    """Counts for the refresh's records, read inside the refresh's own transaction (same rows as its scores)."""
    from agent_memory import skills

    q, considered, no_session, used, unparsed = conn.execute(
        refresh_stats_sql(), {"final": list(skills.WEIGHTS)}).fetchone()
    return {"queried": int(q), "considered": int(considered), "no_session": int(no_session), "used": int(used),
            "unparsed": None if unparsed is None else int(unparsed)}


def refresh_version() -> str:
    from agent_memory import skills

    return f"skill-scores:prior={skills.PRIOR}x{skills.PRIOR_WEIGHT}"


def refresh_records(res: Any, *, now: datetime, stats: Mapping[str, int | None] | None) -> list[Record]:
    """One `amem skills scores --refresh` run (skill_refresh.RefreshResult). run_key = the refresh run id."""
    version = refresh_version()
    if res.status != "ok":
        return [_failed(SKILLS_JOB, res.run, SKILL_CALLS, version, res.error or "refresh failed")]
    if stats is None:
        return []
    end = _utc(res.window_end) if res.window_end is not None else _utc(now)
    start = min(_utc(res.window_start), end) if res.window_start is not None else end
    considered, no_session = int(stats["considered"] or 0), int(stats["no_session"] or 0)
    unparsed = stats.get("unparsed")
    notes = []
    if unparsed:
        notes.append(f"{unparsed} skill-log line(s) unparsed at the last read: never collected")
    if no_session:
        notes.append(f"{no_session} call(s) not linked to a session")
    if res.basis != "ingest":
        notes.append(f"window end from {res.basis}: no complete skill-log read is recorded")
    snap = CV.Snapshot(
        dataset=SKILL_CALLS, window_start=start, window_end=end,
        denominator="prod skill calls in the log up to the last complete read, plus its unparsed lines",
        eligible=considered + int(unparsed or 0), collected=considered, linked=considered - no_session,
        unlinked=no_session, measured_fields=("skill", "project", "session_id"), last_analysed_at=_utc(now),
        checkpoints={"skills-refresh": res.run}, detail=_detail("; ".join(notes)) if notes else None)
    queried, used = int(stats["queried"] or 0), int(stats["used"] or 0)
    skipped = {"env_sandbox": queried - considered, "no_session": no_session,
               "outcome_not_final": considered - no_session - used}
    use = CV.Consumption(
        job=SKILLS_JOB, run_key=res.run, dataset=SKILL_CALLS, analysis_version=version, window_start=start,
        window_end=end, source_checkpoint=f"window end from {res.basis}", queried=queried, considered=considered,
        used=used, skipped={k: v for k, v in skipped.items() if v},
        status="analysis_not_run" if considered == 0 else None,
        detail=_detail(f"{res.scored} skill_scores row(s) written; scores, not findings"))
    return [snap, use]


# =========================================================================== rsi-harvest: evals harvest


def harvest_run_key(now: datetime) -> str:
    return "harvest-" + _utc(now).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def harvest_records(report: Any, *, now: datetime, days: int, limit: int, run_key: str | None = None,
                    run_id: Any = None, error: str | None = None) -> list[Record]:
    """One `amem evals harvest` run (evals_harvest.Report): per source dataset, the sources read, the new ones,
    the drafts written (each draft is a finding, citing its source) and the skip reasons. The handoffs snapshot
    counts the handoffs in the window; a missing handoffs table writes no snapshot (the guard: not_collected)."""
    key = run_key or harvest_run_key(now)
    since = now - timedelta(days=days)
    version = f"evals-harvest:days={days}:limit={limit}"
    if error is not None:
        return [_failed(HARVEST_JOB, key, ds, version, error, window=(since, now), run_id=run_id)
                for ds in (DEVIATIONS, HANDOFFS)]
    out: list[Record] = []
    for name, ds in (("deviations", DEVIATIONS), ("handoffs", HANDOFFS)):
        got = report.sources.get(name, Counter())
        queried, already, drafted = int(got["queried"]), int(got["already_drafted"]), int(got["drafted"])
        skipped = {k: int(v) for k, v in got.items() if k not in ("queried", "drafted") and v}
        if name == "handoffs":
            if not report.handoffs_table:
                out.append(CV.Consumption(
                    job=HARVEST_JOB, run_key=key, dataset=ds, analysis_version=version, window_start=since,
                    window_end=now, status="analysis_not_run", run_id=run_id,
                    detail="no public.handoffs table: handoffs are not collected"))
                continue
            incomplete = int(got["incomplete-handoff"])
            out.append(CV.Snapshot(
                dataset=ds, window_start=since, window_end=now, denominator="handoffs created in the window",
                eligible=queried, collected=queried, linked=queried - incomplete, unlinked=incomplete,
                measured_fields=("session_id", "handoff_path", "reason"), last_analysed_at=now,
                detail=_detail(f"{incomplete} handoff(s) without a session or path") if incomplete else None))
        cited = [r for r in report.cited.get(name, []) if refs.is_ref(r)][: CV.MAX_EVIDENCE_REFS]
        out.append(CV.Consumption(
            job=HARVEST_JOB, run_key=key, dataset=ds, analysis_version=version, window_start=since, window_end=now,
            source_checkpoint="drafts ledger and draft headers in --out", queried=queried,
            considered=queried - already, used=drafted, skipped=skipped, findings=drafted,
            evidence_refs=tuple(cited) if drafted else (), run_id=run_id,
            status="analysis_not_run" if queried == 0 else None))
    return out
