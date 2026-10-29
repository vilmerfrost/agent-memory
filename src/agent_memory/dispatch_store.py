"""Upload dispatch-events.jsonl records into dispatch_runs, usage_snapshots and
dispatch_estimates (idempotent), and read tokens / test medians for refresh."""
from __future__ import annotations

from datetime import timedelta
import re
from typing import Any, Iterable
import uuid

import psycopg
from psycopg.types.json import Jsonb

from agent_memory import dispatch_estimates as D

HEX = re.compile(r"^[0-9a-f]{16,64}$")

RUN_START = """
insert into public.dispatch_runs
 (run_id,repo,parent_session_id,parent_tool,dispatched_at,n_workers,brief_chars,files_claimed,
  concurrent_sessions,status,meta)
values (%(run_id)s,%(repo)s,%(session)s,%(tool)s,%(dispatched_at)s,%(n_workers)s,%(brief_chars)s,
 %(files_claimed)s,%(concurrent)s,'open',%(meta)s)
on conflict(run_id) do update set
 repo=excluded.repo,parent_session_id=coalesce(dispatch_runs.parent_session_id,excluded.parent_session_id),
 parent_tool=excluded.parent_tool,dispatched_at=excluded.dispatched_at,n_workers=excluded.n_workers,
 brief_chars=coalesce(excluded.brief_chars,dispatch_runs.brief_chars),
 files_claimed=coalesce(excluded.files_claimed,dispatch_runs.files_claimed),
 concurrent_sessions=greatest(dispatch_runs.concurrent_sessions,excluded.concurrent_sessions),
 meta=excluded.meta || dispatch_runs.meta,updated_at=now()
where (dispatch_runs.repo,dispatch_runs.parent_tool,dispatch_runs.dispatched_at,dispatch_runs.n_workers,
       dispatch_runs.brief_chars,dispatch_runs.files_claimed,dispatch_runs.concurrent_sessions,dispatch_runs.meta)
  is distinct from
      (excluded.repo,excluded.parent_tool,excluded.dispatched_at,excluded.n_workers,
       coalesce(excluded.brief_chars,dispatch_runs.brief_chars),coalesce(excluded.files_claimed,dispatch_runs.files_claimed),
       greatest(dispatch_runs.concurrent_sessions,excluded.concurrent_sessions),excluded.meta || dispatch_runs.meta)
returning (xmax=0) as inserted
"""

RUN_END = """
insert into public.dispatch_runs
 (run_id,repo,parent_session_id,parent_tool,dispatched_at,briefed_at,n_workers,brief_chars,files_claimed,
  integration_test_ms,concurrent_sessions,status,meta)
values (%(run_id)s,%(repo)s,%(session)s,%(tool)s,%(dispatched_at)s,%(briefed_at)s,%(n_workers)s,
 %(brief_chars)s,%(files_claimed)s,%(integration_test_ms)s,%(concurrent)s,%(status)s,%(meta)s)
on conflict(run_id) do update set
 briefed_at=excluded.briefed_at,status=excluded.status,
 integration_test_ms=coalesce(excluded.integration_test_ms,dispatch_runs.integration_test_ms),
 concurrent_sessions=greatest(dispatch_runs.concurrent_sessions,excluded.concurrent_sessions),
 meta=dispatch_runs.meta || excluded.meta,updated_at=now()
where (dispatch_runs.briefed_at,dispatch_runs.status,dispatch_runs.integration_test_ms,
       dispatch_runs.concurrent_sessions,dispatch_runs.meta)
  is distinct from
      (excluded.briefed_at,excluded.status,coalesce(excluded.integration_test_ms,dispatch_runs.integration_test_ms),
       greatest(dispatch_runs.concurrent_sessions,excluded.concurrent_sessions),dispatch_runs.meta || excluded.meta)
returning (xmax=0) as inserted
"""

SNAPSHOT = """
insert into public.usage_snapshots
 (snapshot_id,run_id,phase,taken_at,tool,account_hash,limit_kind,limit_name,used_percent,resets_at,source)
select %(snapshot_id)s,%(run_id)s,%(phase)s,%(taken_at)s,%(tool)s,%(account_hash)s,%(limit_kind)s,
       %(limit_name)s,%(used_percent)s,%(resets_at)s,%(source)s
where exists (select 1 from public.dispatch_runs where run_id=%(run_id)s)
on conflict do nothing
returning true
"""

ESTIMATE = """
insert into public.dispatch_estimates
 (run_id,made_at,est_minutes,est_slow_minutes,est_usage,basis_runs,method_version)
select %(run_id)s,%(made_at)s,%(est_minutes)s,%(est_slow_minutes)s,%(est_usage)s,%(basis_runs)s,%(method_version)s
where exists (select 1 from public.dispatch_runs where run_id=%(run_id)s)
on conflict do nothing
returning true
"""

TOKENS = """
update public.dispatch_runs set tokens_fresh=%(tokens_fresh)s,tokens_cache_read=%(tokens_cache_read)s,
 meta=meta || %(meta)s,updated_at=now()
where run_id=%(run_id)s
  and (tokens_fresh,tokens_cache_read) is distinct from (%(tokens_fresh)s::bigint,%(tokens_cache_read)s::bigint)
returning false
"""


def _uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _int(value: Any, *, null: bool = True, maximum: int = 2**62) -> int | None | bool:
    """int, None (when allowed) or False (invalid)."""
    if value is None:
        return None if null else False
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return False
    return value


def _text(value: Any, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def _conc(value: Any) -> dict[str, int] | None:
    if isinstance(value, dict) and all(isinstance(v, int) and v >= 0 for v in value.values()):
        return {k: v for k, v in value.items() if k in D.TOOLS}
    return None


def parse_record(row: Any) -> tuple[str, dict] | None:
    if not isinstance(row, dict) or row.get("v") != D.SCHEMA or not (run_id := _uuid(row.get("run_id"))):
        return None
    kind = row.get("type")
    if kind in ("start", "end"):
        repo = _text(row.get("repo"), 200)
        tool = row.get("tool") if row.get("tool") in D.TOOLS else None
        session = row.get("session") if isinstance(row.get("session"), str) and len(row["session"]) <= 200 else None
        workers = _int(row.get("n_workers"), null=False, maximum=10_000)
        brief, files = _int(row.get("brief_chars")), _int(row.get("files_claimed"))
        concurrent = _int(row.get("concurrent"), maximum=10_000)
        started = D.parse_ts(row.get("at") if kind == "start" else row.get("dispatched_at"))
        if not repo or not tool or workers is False or brief is False or files is False \
                or concurrent is False or started is None:
            return None
        meta = {"schema": D.SCHEMA}
        if (by_tool := _conc(row.get("concurrent_by_tool"))) is not None:
            meta["concurrent_by_tool"] = by_tool
        params = {"run_id": run_id, "repo": repo, "session": session, "tool": tool, "dispatched_at": started,
                  "n_workers": workers, "brief_chars": brief, "files_claimed": files,
                  "concurrent": concurrent}
        if kind == "start":
            notes = row.get("notes")
            if isinstance(notes, list) and all(isinstance(n, str) for n in notes):
                meta["start_notes"] = [n[:200] for n in notes[:5]]
            return "start", {**params, "meta": Jsonb(meta)}
        status = row.get("status") if row.get("status") in D.STATUSES else None
        briefed = D.parse_ts(row.get("at"))
        test_ms = _int(row.get("integration_test_ms"), maximum=2**31 - 1)
        if status is None or test_ms is False or (row.get("at") is not None and briefed is None) \
                or (briefed is not None and briefed < started):
            return None
        if status == "abandoned":
            meta["abandoned_at"] = row.get("ended_at") if isinstance(row.get("ended_at"), str) else None
        return "end", {**params, "briefed_at": briefed, "status": status, "integration_test_ms": test_ms,
                       "meta": Jsonb(meta)}
    if kind == "snapshot":
        snapshot_id = _uuid(row.get("snapshot_id"))
        used = row.get("used_percent")
        taken = D.parse_ts(row.get("taken_at"))
        if not snapshot_id or row.get("phase") not in ("start", "end") or taken is None \
                or not isinstance(used, (int, float)) or isinstance(used, bool) or not 0 <= used <= 1000 \
                or not isinstance(row.get("account_hash"), str) or not HEX.match(row["account_hash"]):
            return None
        tool, lk, ln = _text(row.get("tool"), 40), _text(row.get("limit_kind"), 40), _text(row.get("limit_name"), 120)
        resets = D.parse_ts(row.get("resets_at"))
        if not tool or not lk or not ln or (row.get("resets_at") is not None and resets is None):
            return None
        return "snapshot", {"snapshot_id": snapshot_id, "run_id": run_id, "phase": row["phase"],
                            "taken_at": taken, "tool": tool, "account_hash": row["account_hash"],
                            "limit_kind": lk, "limit_name": ln, "used_percent": used, "resets_at": resets,
                            "source": _text(row.get("source"), 40) or "vitals"}
    if kind == "estimate":
        made = D.parse_ts(row.get("at"))
        est, slow, basis = _int(row.get("est_minutes")), _int(row.get("est_slow_minutes")), _int(row.get("basis_runs"))
        usage = row.get("est_usage")
        method = _text(row.get("method_version"), 60)
        if made is None or est is False or slow is False or basis is False or not method \
                or not isinstance(usage, dict) \
                or not all(isinstance(k, str) and isinstance(v, (int, float)) for k, v in usage.items()):
            return None
        return "estimate", {"run_id": run_id, "made_at": made, "est_minutes": est, "est_slow_minutes": slow,
                            "est_usage": Jsonb(usage), "basis_runs": basis or 0, "method_version": method}
    if kind == "tokens":
        fresh, cached = _int(row.get("tokens_fresh"), null=False), _int(row.get("tokens_cache_read"))
        if fresh is False or cached is False:
            return None
        counted = _int(row.get("sessions_counted"))
        return "tokens", {"run_id": run_id, "tokens_fresh": fresh, "tokens_cache_read": cached,
                          "meta": Jsonb({"tokens_scope": "workers",
                                         "tokens_sessions": counted if counted is not False else None})}
    return None


SQL = {"start": RUN_START, "end": RUN_END, "snapshot": SNAPSHOT, "estimate": ESTIMATE, "tokens": TOKENS}


def upsert_conn(conn: psycopg.Connection, records: Iterable[tuple[str, dict]]) -> tuple[int, int]:
    """In spool order, so a run row exists before its snapshots and estimate."""
    new = updated = 0
    with conn.cursor() as cursor:
        for kind, params in records:
            cursor.execute(SQL[kind], params)
            row = cursor.fetchone()
            if row:
                new += bool(row[0])
                updated += not bool(row[0])
    return new, updated


class DbSource:
    """Read-only lookups for `amem dispatch refresh`."""

    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def test_medians(self) -> dict[str, float]:
        rows = self.conn.execute(
            "select repo, percentile_cont(0.5) within group (order by elapsed_ms) from public.test_timing_runs "
            "where selection='full' and exit_code=0 group by repo").fetchall()
        return {repo: float(ms) for repo, ms in rows if ms is not None}

    def tokens(self, run: dict) -> tuple[int, int, int] | None:
        started, ended = D.parse_ts(run.get("dispatched_at")), D.parse_ts(run.get("briefed_at"))
        if not started or not ended:
            return None
        row = self.conn.execute(
            "select count(*), sum(coalesce(tokens_input,0)+coalesce(tokens_output,0)+coalesce(tokens_cache_creation,0)),"
            " sum(coalesce(tokens_cache_read,0)) from public.sessions"
            " where parent_session_id=%s and started_at >= %s and started_at <= %s"
            " and tokens_updated_at is not null",
            (run["session"], started - timedelta(minutes=2), ended)).fetchone()
        if not row or not row[0]:
            return None
        return int(row[1] or 0), int(row[2] or 0), int(row[0])
