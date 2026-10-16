"""The sink loop: Supabase rows -> one external system (blueprint C2, adapted to the existing RSI schema).

    amem sync run --sink clickhouse|langfuse|pastdev [--budget S] [--replay]

1. Kill switch (`AMEM_SYNC_<SINK>=off`, and `AMEM_PASTDEV=off` for past.dev): status `off`, nothing read.
2. Health check. A sink that is down: status `unavailable`; the cursor does not move; exit 0.
3. `sink.batches()` reads Supabase rows newer than the cursor, already filtered (what may leave) and mapped
   (the sink's shape). It only reads.
4. `sink.push(batch)` sends one batch. A push must be idempotent: the same batch twice adds nothing.
5. On success, the batch's evidence links (rsi_evidence) and the advanced watermark are written and
   committed together. On failure the table's watermark stays, the failure is counted with backoff
   (1, 5, 30, 120 min), and after 6 attempts the table is marked dead (shown by `amem sync status`).
6. A time budget stops the loop between batches (status `partial`); the next run continues.

The cursor is one rsi_checkpoints row: job `sync:<sink>`, run_key `cursor`, meta
`{"watermarks": {table: opaque}, "failures": {table: {...}}, "last_ok_at": ...}`. A watermark is owned
by its sink (for example `<iso ts>|<id>`); `--replay` starts from no watermark, which backfills.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import psycopg

from agent_memory.integrations.evidence import EvidenceRef, Health, record

RUN_KEY = "cursor"
MAX_ATTEMPTS = 6
BACKOFF_MIN = (1, 5, 30, 120)
SINKS = ("clickhouse", "langfuse", "pastdev")


def job(sink: str) -> str:
    return f"sync:{sink}"


@dataclass
class Batch:
    table: str                                     # the cursor key (usually the source table)
    rows: list[dict[str, Any]]                     # filtered and mapped to the sink's shape
    watermark: str                                 # the cursor value after this batch
    evidence: list[EvidenceRef] = field(default_factory=list)   # links to record once pushed
    key: str = ""                                  # idempotency key for the sink, if it uses one


class Sink(Protocol):
    name: str

    def health(self) -> Health: ...

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[Batch]: ...

    def push(self, batch: Batch) -> None: ...


@dataclass
class SyncReport:
    sink: str
    status: str = "ok"            # ok | partial | unavailable | off | failed
    batches: int = 0
    rows: int = 0
    failed: int = 0
    skipped_tables: list[str] = field(default_factory=list)
    seconds: float = 0.0
    detail: str = ""

    def line(self) -> str:
        extra = f" skipped={','.join(self.skipped_tables)}" if self.skipped_tables else ""
        return (f"sync {self.sink}: {self.status} batches={self.batches} rows={self.rows} failed={self.failed}"
                f"{extra} {self.seconds:.1f}s" + (f" ({self.detail})" if self.detail else ""))


def switched_off(sink: str, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if env.get(f"AMEM_SYNC_{sink.upper()}", "").strip().lower() == "off":
        return True
    return sink == "pastdev" and env.get("AMEM_PASTDEV", "").strip().lower() == "off"


def load(conn: psycopg.Connection, sink: str) -> dict[str, Any]:
    row = conn.execute("select meta from rsi_checkpoints where job = %s and run_key = %s",
                       (job(sink), RUN_KEY)).fetchone()
    return dict(row[0]) if row and row[0] else {}


def _save(conn: psycopg.Connection, sink: str, status: str, meta: dict[str, Any]) -> None:
    from agent_memory import rsi_approval   # lazy

    rsi_approval.checkpoint(conn, job(sink), RUN_KEY, status, meta)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _due(failure: Mapping[str, Any] | None, now: datetime) -> bool:
    if not failure:
        return True
    if failure.get("dead"):
        return False
    nxt = failure.get("next_retry_at")
    return not nxt or datetime.fromisoformat(nxt) <= now


def run(sink: Sink, conn: psycopg.Connection, *, budget_s: float = 60.0, replay: bool = False,
        clock: Callable[[], float] = time.monotonic, now: datetime | None = None,
        environ: Mapping[str, str] | None = None) -> SyncReport:
    rep = SyncReport(sink.name)
    start = clock()
    now = now or datetime.now(UTC)
    if switched_off(sink.name, environ):
        rep.status, rep.detail = "off", "kill switch"
        return rep
    meta = load(conn, sink.name)
    watermarks: dict[str, str] = {} if replay else dict(meta.get("watermarks") or {})
    failures: dict[str, dict[str, Any]] = dict(meta.get("failures") or {})
    try:
        h = sink.health()
    except Exception as exc:  # noqa: BLE001
        h = Health(False, f"health: {type(exc).__name__}")
    if not h.ok:
        rep.status, rep.detail = "unavailable", h.detail
        _save(conn, sink.name, "skipped", {"last_error": f"unavailable: {h.detail}"[:300], "last_try_at": _iso(now)})
        conn.commit()
        rep.seconds = clock() - start
        return rep
    blocked = {t for t, f in failures.items() if not _due(f, now)}
    rep.skipped_tables = sorted(blocked)
    for batch in sink.batches(conn, watermarks, replay=replay):
        if batch.table in blocked:
            continue
        if clock() - start > budget_s:
            rep.status, rep.detail = "partial", "time budget"
            break
        try:
            sink.push(batch)
        except Exception as exc:  # noqa: BLE001 - one table's failure never stops the others
            conn.rollback()
            f = failures.get(batch.table, {})
            n = int(f.get("attempts", 0)) + 1
            wait = BACKOFF_MIN[min(n - 1, len(BACKOFF_MIN) - 1)]
            failures[batch.table] = {"attempts": n, "dead": n >= MAX_ATTEMPTS, "last_error": f"{type(exc).__name__}: {exc}"[:300],
                                     "next_retry_at": _iso(now + timedelta(minutes=wait))}
            blocked.add(batch.table)
            rep.failed += 1
            _save(conn, sink.name, "running", {"failures": failures})
            conn.commit()
            continue
        for ev in batch.evidence:
            record(conn, ev)
        watermarks[batch.table] = batch.watermark
        failures.pop(batch.table, None)
        _save(conn, sink.name, "running", {"watermarks": watermarks, "failures": failures})
        conn.commit()          # the links and the advanced watermark land together
        rep.batches += 1
        rep.rows += len(batch.rows)
    if rep.failed and rep.status == "ok":
        rep.status = "failed" if not rep.batches else "partial"
    final = {"watermarks": watermarks, "failures": failures, "last_try_at": _iso(now)}
    if rep.status in ("ok", "partial"):
        final["last_ok_at"] = _iso(now)
    _save(conn, sink.name, "failed" if rep.status == "failed" else "done", final)
    conn.commit()
    rep.seconds = clock() - start
    return rep


def status(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("select job, status, finished_at, meta from rsi_checkpoints where job like 'sync:%%' "
                        "and run_key = %s order by job", (RUN_KEY,)).fetchall()
    out = []
    for j, st, fin, meta in rows:
        meta = meta or {}
        fails = meta.get("failures") or {}
        out.append({"sink": j.split(":", 1)[1], "status": st, "finished_at": fin.isoformat() if fin else None,
                    "tables": sorted((meta.get("watermarks") or {}).keys()), "last_ok_at": meta.get("last_ok_at"),
                    "dead": sorted(t for t, f in fails.items() if f.get("dead")),
                    "failing": sorted(t for t, f in fails.items() if not f.get("dead")),
                    "last_error": meta.get("last_error")})
    return out
