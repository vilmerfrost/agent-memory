"""Bounded deterministic analysis of existing measurements; no model calls.

Results are review leads, not causal conclusions or unnecessary-work verdicts.
The denominator is stored data in an explicit window, never all eligible source
events unless a separate collector has established that population.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from agent_memory import coverage as CV, store

FORMAT = "telemetry-analysis/2"


def _rows(conn, sql, params=()):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def analyze(conn, *, now: datetime | None = None, days: int = 7, limit: int = 5) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("analysis clock must be timezone-aware")
    since = now - timedelta(days=max(1, min(days, 30)))
    limit = max(1, min(limit, 20))
    findings: list[dict[str, Any]] = []

    def finding(kind, text, evidence, limitation, dataset):
        findings.append({"kind": kind, "summary": text, "evidence_refs": evidence[:5],
                         "limitation": limitation, "dataset": dataset, "advisory_only": True})

    quality = _rows(conn, """select count(*) as stored,count(e.elapsed_ms) as duration_known,
        count(s.session_id) filter(where coalesce(e.meta->>'api_scope','')<>'client_control') as session_resolved,
        count(*) filter(where e.meta->>'api_scope'='client_control') as client_control,
        count(*) filter(where coalesce(e.meta->>'api_scope','')<>'client_control') as session_expected,
        count(e.elapsed_ms) filter(where coalesce(e.meta->>'api_scope','')<>'client_control') as scoped_duration_known,
        count(*) filter(where e.category='api') as api_events,
        count(*) filter(where e.category='api' and s.session_id is null) as api_unassigned,
        count(*) filter(where e.status='running') as unfinished,max(e.created_at) as last_ingest
        from execution_spans e left join sessions s on s.session_id=e.session_id
        where coalesce(e.started_at,e.created_at)>=%s and coalesce(e.started_at,e.created_at)<%s""", (since, now))[0]
    operations = _rows(conn, """select source,operation,category,measurement_kind,count(*) as observations,
        count(elapsed_ms) as timed,sum(elapsed_ms)/1000 as cumulative_seconds,
        (array_agg(span_id order by elapsed_ms desc nulls last))[1:3] as span_ids
        from execution_spans where coalesce(started_at,created_at)>=%s and coalesce(started_at,created_at)<%s
        and source in ('claude.tool','codex.item') and category in ('command','tool','test_full','test_targeted')
        group by source,operation,category,measurement_kind
        order by cumulative_seconds desc nulls last limit %s""", (since, now, limit))
    for op in operations:
        if op['cumulative_seconds'] is not None:
            finding("recurring_operation", f"{op['observations']} captured {op['operation']} operations: "
                    f"{float(op['cumulative_seconds']):.3f} cumulative seconds",
                    ["execution_spans:"+s for s in op['span_ids']],
                    "Captured source window only; nested/parallel intervals overlap and provider boundaries differ. "
                    "This is cumulative work, not wall time or a proof of waste.", "command_timings:execution_spans")
    if quality['session_expected'] and (quality['session_resolved'] < quality['session_expected'] or quality['scoped_duration_known'] < quality['session_expected']):
        finding("measurement_gap", f"{quality['session_resolved']}/{quality['session_expected']} task-scoped stored spans resolve a session; "
                f"{quality['scoped_duration_known']}/{quality['session_expected']} have duration; {quality['api_unassigned']} API events unassigned; {quality['client_control']} known client-control events excluded from session eligibility",
                ["query:execution_spans.session_id->sessions.session_id"],
                "Missing identity/duration is unknown. No actor is inferred from nearby timestamps.",
                "command_timings:execution_spans")

    tests = _rows(conn, """select count(*) as stored,count(s.session_id) as session_resolved,
        count(e.span_id) as span_resolved,count(*) filter(where not r.details_retained) as expired_details,
        count(*) filter(where r.collection_errors>0 or r.worker_errors>0 or r.tests_complete<r.tests_reported) as incomplete,
        max(r.created_at) as last_ingest from test_timing_runs r
        left join sessions s on s.session_id=r.session_id left join execution_spans e on e.span_id=r.parent_span_id
        where r.started_at>=%s and r.started_at<%s""", (since, now))[0]
    slow = _rows(conn, """select c.run_id::text,c.test_key,c.nodeid,c.setup_ms,c.call_ms,c.teardown_ms,c.total_ms,c.outcome
        from test_timing_cases c join test_timing_runs r using(run_id)
        where r.started_at>=%s and r.started_at<%s and r.details_retained
        order by c.total_ms desc nulls last,c.run_id,c.test_key limit %s""", (since, now, limit))
    for case in slow:
        finding("slow_test_observation", f"Test {case['test_key'][:12]}: total {case['total_ms']} ms; setup {case['setup_ms']} ms",
                [f"test_timing_cases:{case['run_id']}:{case['test_key']}"],
                "One observed test; shared fixture setup is charged to its triggering case. Machine load and "
                "parallelism are uncontrolled. Logs remain local.", "test_timings:test_timing_runs")
    repeats = _rows(conn, """select r.repo,r.head,r.dirty,r.workers,r.selection,c.test_key,count(*) as observations,
        count(*) filter(where c.outcome in ('failed','error')) as nonpassing,
        (array_agg(r.run_id::text order by r.started_at))[1:5] as run_ids
        from test_timing_runs r join test_timing_cases c using(run_id)
        where r.started_at>=%s and r.started_at<%s and r.details_retained
        group by r.repo,r.head,r.dirty,r.workers,r.selection,c.test_key having count(*)>1
        order by observations desc,c.test_key limit %s""", (since, now, limit))
    for group in repeats:
        finding("repeat_test_review", f"Test {group['test_key'][:12]} observed {group['observations']} times "
                f"at the same recorded revision/worker/selection grouping; {group['nonpassing']} nonpassing",
                ["test_timing_runs:"+r for r in group['run_ids']],
                "Rerun necessity is not captured; a clean same-HEAD acceptance run may be required. "
                "Dirty=true does not prove identical trees. Never call these unnecessary reruns.", "test_timings:test_timing_runs")

    parents = _rows(conn, """select session_id,window_seconds,observed_wall_seconds,unclassified_seconds,
        descendant_sessions,cumulative_descendant_elapsed_seconds,category_union_seconds
        from session_parent_timing where window_start>=%s and window_start<%s and descendant_sessions>0
        order by unclassified_seconds desc nulls last,session_id limit %s""", (since, now, limit))
    for parent in parents:
        finding("parent_time_review", f"Parent has {parent['unclassified_seconds']} unclassified seconds "
                f"in a {parent['window_seconds']} second window; {parent['descendant_sessions']} descendants",
                ["sessions:"+parent['session_id']],
                "Unclassified is not measured idle/wait/thinking. Cumulative worker elapsed can exceed "
                "parent wall time. Task difficulty and retries are not controlled.", "coordination:sessions")
    contradictions = _rows(conn, """select s.session_id,s.outcome,t.run_id::text,t.head,t.exit_code
        from sessions s join lateral(select run_id,head,exit_code from test_timing_runs r
          where r.session_id=s.session_id order by ended_at desc limit 1) t on true
        where coalesce(s.started_at,s.created_at)>=%s and coalesce(s.started_at,s.created_at)<%s
          and s.outcome in ('shipped','pushed') and t.exit_code<>0 limit %s""", (since, now, limit))
    for row in contradictions:
        finding("completion_evidence_review", "A success-shaped session label coexists with its latest stored failed test run",
                ["sessions:"+row['session_id'], "test_timing_runs:"+row['run_id']],
                "This is a review lead, not a false-completion verdict: required criteria, exact accepted tree "
                "and later external checks may be unknown.", "coordination:sessions")
    freshness = _rows(conn, """select job,run_key,status,finished_at,meta->>'last_ok_at' as last_ok_at
        from rsi_checkpoints where job like 'sync:%%' or job='skills-refresh' order by job,finished_at desc limit 20""")
    checkpoint_total = conn.execute("select count(*) from rsi_checkpoints where job like 'sync:%%' or job='skills-refresh'").fetchone()[0]
    unknown_clock = 0
    for checkpoint in freshness:
        at = checkpoint['last_ok_at'] if checkpoint['job'].startswith('sync:') else checkpoint['finished_at']
        try:
            at = datetime.fromisoformat(at.replace('Z','+00:00')) if isinstance(at,str) else at
            if at is None or at.tzinfo is None or at>now:
                unknown_clock += 1
                continue
        except (ValueError,TypeError):
            unknown_clock += 1
            continue
        age=(now-at).total_seconds()/3600
        if checkpoint['status']=='failed' or age>26:
            finding('collector_freshness_review',f"{checkpoint['job']} checkpoint is {age:.1f} hours old; recorded state {checkpoint['status']}",
                    [f"rsi_checkpoints:{checkpoint['job']}/{checkpoint['run_key']}"],
                    'A 26-hour daily-cadence review threshold, not proof the collector is broken. Acknowledged sync is not independent sink readback.',
                    'reports_checkpoints:rsi_checkpoints')
    return {"format": FORMAT, "observed_at": now.isoformat(), "window_start": since.isoformat(),
            "window_end": now.isoformat(), "scope": "stored_rows_only", "days": days,
            "span_coverage": quality, "test_coverage": tests, "operations": operations, "slow_tests": slow,
            "repeat_groups": repeats, "parents": parents, "completion_review": contradictions,
            "collector_checkpoints": freshness, "findings": findings,
            "checkpoint_coverage": {'stored':checkpoint_total,'captured':len(freshness),'session_resolved':len(freshness),
                                    'unknown_clock':unknown_clock,'scope':'current_state_at_observed_at'},
            "limitations": ["No model calls", "No hidden reasoning", "No automatic promotion or Undo",
                            "Captured populations are not all eligible source events",
                            "Stored sync success is not independent external readback"]}


def records(report: dict[str, Any], *, run_key: str) -> list[Any]:
    start, end = datetime.fromisoformat(report['window_start']), datetime.fromisoformat(report['window_end'])
    result = []
    for dataset, group in (("command_timings:execution_spans", report['span_coverage']),
                           ("test_timings:test_timing_runs", report['test_coverage']),
                           ('reports_checkpoints:rsi_checkpoints',report['checkpoint_coverage'])):
        n, linked = int(group.get('session_expected',group['stored'])), int(group['session_resolved'])
        captured = int(group.get('captured',n))
        missing = (group.get('scoped_duration_known',group.get('duration_known', n)) != n or group.get('incomplete', 0) > 0
                   or group.get('expired_details', 0) > 0 or group.get('unknown_clock',0)>0)
        findings = [f for f in report['findings'] if f['dataset'] == dataset]
        scope = ("stored current collector checkpoints; bounded capture, independent of the analysis window"
                 if dataset == "reports_checkpoints:rsi_checkpoints" else
                 "stored task-scoped rows in explicit analysis window; known client-control traffic excluded"
                 if "session_expected" in group else "stored rows in the explicit deterministic analysis window")
        fields = (("stored_count", "checkpoint_identity", "freshness")
                  if dataset == "reports_checkpoints:rsi_checkpoints" else ("stored_count", "session_linkage"))
        result += [CV.Snapshot(dataset, start, end, scope,
                               n, captured, linked, captured-linked, measured_fields=fields,
                               unmeasured_fields=("some_required_measurements",) if missing else (),
                               last_collected_at=group.get('last_ingest'), last_analysed_at=end,
                               source_versions={"analysis": FORMAT}),
                   CV.Consumption("amem-daily", run_key, dataset, FORMAT, start, end,
                                  queried=captured, considered=captured, used=captured, findings=len(findings),
                                  detail="Bounded deterministic review; stored-row denominator, no causal verdict")]
    return result


def render(report: dict[str, Any]) -> str:
    lines = ["# Deterministic telemetry review", "", f"Window: {report['window_start']} to {report['window_end']}",
             "Scope: stored rows only. Missing measurements are unknown; cumulative time is not wall time.", ""]
    for finding in report['findings']:
        lines += ["- "+finding['summary'], "  Evidence: "+", ".join(finding['evidence_refs']),
                  "  Limit: "+finding['limitation']]
    if not report['findings']:
        lines.append("No review findings in the captured window. Check coverage before concluding all clear.")
    return "\n".join(lines)+"\n"


def run(*, db_url: str | None = None, now: datetime | None = None, days: int = 7, limit: int = 5) -> dict[str, Any]:
    with store.connect(db_url) as conn:
        conn.read_only = True
        return analyze(conn, now=now, days=days, limit=limit)


def save(report: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = "telemetry-analysis-" + report['observed_at'][:10]
    data = directory / (stem+".json")
    data.write_text(json.dumps(report, sort_keys=True, default=str)+"\n")
    (directory/(stem+".md")).write_text(render(report))
    return data


def daily_review(directory: Path, *, now: datetime) -> dict[str, Any]:
    """Morning brief reads a bounded recent daily report; never promotes a
    missing/stale/invalid file into an all-clear conclusion.
    """
    files = sorted(directory.glob('telemetry-analysis-*.json'))[-2:]
    for path in reversed(files):
        try:
            if path.is_symlink() or path.stat().st_size > 2_000_000:
                continue
            report = json.loads(path.read_text())
            observed = datetime.fromisoformat(report['observed_at'])
            if report.get('format') not in ('telemetry-analysis/1', FORMAT) or observed.tzinfo is None:
                continue
            if observed > now or now-observed > timedelta(hours=26):
                return {'status':'stale','detail':'daily measurement window is stale or in the future','report_ref':path.name}
            findings = []
            for item in report.get('findings',[])[:5]:
                if isinstance(item,dict) and item.get('evidence_refs') and item.get('limitation'):
                    findings.append({k:item[k] for k in ('summary','evidence_refs','limitation')})
            return {'status':'recorded','report_ref':path.name,'observed_at':observed.isoformat(),
                    'scope':'stored_rows_only','stored_spans':report['span_coverage']['stored'],
                    'stored_test_runs':report['test_coverage']['stored'],'findings':findings,
                    'detail':'Bounded review leads, not causal or task-acceptance verdicts'}
        except (OSError,ValueError,KeyError,TypeError):
            continue
    return {'status':'unavailable','detail':'daily telemetry report not recorded or unreadable; not an all clear'}
