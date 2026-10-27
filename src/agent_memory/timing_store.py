"""Idempotent persistence and retrieval of small timing facts."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import fcntl
import tempfile
from typing import Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import store
from agent_memory.execution_timing import Span, parse_record

UPSERT = """
insert into public.execution_spans
 (span_id,session_id,parent_span_id,tool,category,operation,started_at,ended_at,elapsed_ms,
  status,exit_code,measurement_kind,source,method_version,meta)
values (%(span_id)s,%(session_id)s,%(parent_span_id)s,%(tool)s,%(category)s,%(operation)s,
 %(started_at)s,%(ended_at)s,%(elapsed_ms)s,%(status)s,%(exit_code)s,%(measurement_kind)s,
 %(source)s,%(method_version)s,%(meta)s)
on conflict(span_id) do update set
 session_id=coalesce(execution_spans.session_id,excluded.session_id),
 started_at=coalesce(excluded.started_at,execution_spans.started_at),
 category=excluded.category,method_version=excluded.method_version,
 ended_at=excluded.ended_at,elapsed_ms=excluded.elapsed_ms,status=excluded.status,
 exit_code=excluded.exit_code,measurement_kind=excluded.measurement_kind,
 meta=(execution_spans.meta || excluded.meta) -
      case when coalesce(execution_spans.session_id,excluded.session_id) is not null
           then 'identity_unavailable' else '__no_key__' end,updated_at=now()
where excluded.method_version >= execution_spans.method_version
  and not (coalesce(excluded.meta->>'api_scope','')='client_control' and execution_spans.session_id is not null)
  and (excluded.ended_at is not null or execution_spans.ended_at is null)
  and (execution_spans.session_id is null or excluded.session_id is null
       or execution_spans.session_id=excluded.session_id)
  and (execution_spans.session_id,execution_spans.ended_at,execution_spans.elapsed_ms,execution_spans.status,
       execution_spans.exit_code,execution_spans.measurement_kind,execution_spans.meta,
       execution_spans.started_at,execution_spans.category,execution_spans.method_version)
      is distinct from
      (coalesce(execution_spans.session_id,excluded.session_id),excluded.ended_at,excluded.elapsed_ms,excluded.status,excluded.exit_code,
       excluded.measurement_kind,(execution_spans.meta || excluded.meta) -
       case when coalesce(execution_spans.session_id,excluded.session_id) is not null
            then 'identity_unavailable' else '__no_key__' end,
       coalesce(excluded.started_at,execution_spans.started_at),excluded.category,excluded.method_version)
returning (xmax=0) as inserted
"""


def upsert_conn(conn: psycopg.Connection, spans: Iterable[Span]) -> tuple[int, int]:
    new = updated = 0
    touched: set[str] = set()
    records = []
    for span in spans:
        d = span.record()
        d["started_at"], d["ended_at"], d["meta"] = span.started_at, span.ended_at, Jsonb(span.meta)
        records.append(d)
    if records:
        with conn.cursor() as cursor:
            cursor.executemany(UPSERT, records, returning=True)
            for record in records:
                row = cursor.fetchone()
                if row:
                    new += bool(row[0])
                    updated += not bool(row[0])
                    if record['session_id']:
                        touched.add(record['session_id'])
                cursor.nextset()
    refresh_profiles(conn, touched)
    return new, updated


def refresh_profiles(conn: psycopg.Connection, session_ids: Iterable[str]) -> None:
    touched=set(session_ids)
    if touched:
        # Provider totals are cumulative work measurements, separate from unioned wall time.
        # A missing API event remains NULL; model output is never substituted for API time.
        conn.execute("""
        update public.sessions s set
          api_seconds=coalesce(t.api_seconds,s.api_seconds),
          tool_seconds=coalesce(t.tool_seconds,s.tool_seconds),
          meta=s.meta || jsonb_build_object('timing_method','execution_spans_v1')
        from (
          select session_id,
            sum(elapsed_ms/1000) filter(where category='api' and measurement_kind='provider_reported') as api_seconds,
            sum(elapsed_ms/1000) filter(where source='codex.item' and category in ('tool','command','test_full','test_targeted')) as tool_seconds
          from public.execution_spans where session_id=any(%s) group by session_id
        ) t where s.session_id=t.session_id and s.tool='codex'
        """, (sorted(touched),))


def upsert(spans: Iterable[Span], *, db_url: str | None = None) -> tuple[int, int]:
    with store.connect(db_url) as conn:
        return upsert_conn(conn, spans)


@dataclass
class SpoolReport:
    files: int = 0
    records: int = 0
    new: int = 0
    updated: int = 0
    invalid: int = 0
    incomplete: int = 0


def spool_paths(root: Path | None = None) -> list[Path]:
    parent = root or (Path(os.environ.get("AMEM_CLAUDE_HOME") or Path.home()/".claude")/"parent")
    return sorted(p for pattern in ("timing-events.jsonl", "*/timing-events.jsonl")
                  for p in parent.glob(pattern) if p.is_file() and not p.is_symlink())


def ingest_spools(paths: list[Path] | None = None, *, root: Path | None = None,
                  db_url: str | None = None, cursor_path: Path | None = None,
                  _locked: bool = False) -> SpoolReport:
    if cursor_path is not None and not _locked:
        cursor_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with cursor_path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return ingest_spools(paths, root=root, db_url=db_url, cursor_path=cursor_path, _locked=True)
    cursors = {}
    if cursor_path is not None and cursor_path.exists():
        try: cursors = json.loads(cursor_path.read_text())
        except (ValueError, OSError): cursors = {}
        if not isinstance(cursors, dict): cursors = {}
    report = SpoolReport()
    for path in spool_paths(root) if paths is None else paths:
        if path.name.startswith(".env") or path.is_symlink():
            raise ValueError("timing input must be a regular JSONL event file")
        report.files += 1
        batch: list[Span] = []
        stat = path.stat()
        previous = cursors.get(str(path.resolve()), {})
        offset = previous.get('offset', 0) if isinstance(previous, dict) and previous.get('inode') == stat.st_ino else 0
        if not isinstance(offset, int) or not 0 <= offset <= stat.st_size:
            offset = 0
        with path.open(encoding="utf-8", errors="replace") as source, store.connect(db_url) as conn:
            source.seek(offset)
            while True:
                position = source.tell()
                line = source.readline(32769)
                if not line:
                    offset = position
                    break
                if len(line) > 32768:
                    while line and not line.endswith('\n'):
                        line = source.readline(32769)
                    report.invalid += 1
                    offset = source.tell()
                    continue
                if not line.endswith("\n"):
                    report.incomplete += 1
                    offset = position
                    break
                try:
                    row = json.loads(line) if len(line) <= 32768 else None
                except ValueError:
                    row = None
                span = parse_record(row)
                if span is None:
                    report.invalid += 1
                    continue
                report.records += 1
                batch.append(span)
                if len(batch) >= 500:
                    n, u = upsert_conn(conn, batch)
                    report.new += n
                    report.updated += u
                    batch.clear()
            n, u = upsert_conn(conn, batch)
            report.new += n
            report.updated += u
        # The transaction committed before advancing its cursor. A crash before
        # the atomic save simply replays idempotent rows; a partial tail stays pending.
        if cursor_path is not None:
            cursors[str(path.resolve())] = {'inode': stat.st_ino, 'offset': offset}
            fd, name = tempfile.mkstemp(dir=cursor_path.parent, prefix='.timing-cursor-')
            try:
                with os.fdopen(fd,'w') as out:
                    json.dump(cursors,out)
                os.replace(name,cursor_path)
            finally:
                Path(name).unlink(missing_ok=True)
    return report


def summary(session_id: str, *, db_url: str | None = None) -> dict | None:
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from public.session_parent_timing where session_id=%s", (session_id,))
        return cur.fetchone()


def coverage(*, db_url: str | None = None) -> list[dict]:
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from public.session_timing_coverage order by tool")
        return cur.fetchall()
