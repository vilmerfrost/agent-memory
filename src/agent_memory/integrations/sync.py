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
7. A record-level refusal (400/409/413/415/422) splits the batch and dead-letters only the refused rows; a
   push that raises `Refused` (the provider's final verdict) dead-letters its rows without a split or retry.

The cursor is one rsi_checkpoints row: job `sync:<sink>`, run_key `cursor`, meta
`{"watermarks": {table: opaque}, "failures": {table: {...}}, "last_ok_at": ...}`. A watermark is owned
by its sink (for example `<iso ts>|<id>`); `--replay` starts from no watermark, which backfills.
"""

from __future__ import annotations

import json
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
TABLE_COUNTS = ("seen", "delivered", "dead", "failed", "backoff", "stopped")


def job(sink: str) -> str:
    return f"sync:{sink}"


@dataclass
class Batch:
    table: str                                     # the cursor key (usually the source table)
    rows: list[dict[str, Any]]                     # filtered and mapped to the sink's shape
    watermark: str                                 # the cursor value after this batch
    evidence: list[EvidenceRef] = field(default_factory=list)   # links to record once pushed
    key: str = ""                                  # idempotency key for the sink, if it uses one
    failure_key: str = ""                          # optional per-record retry scope, separate from cursor


class Sink(Protocol):
    name: str

    def health(self) -> Health: ...

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[Batch]: ...

    def push(self, batch: Batch) -> None: ...


@dataclass
class SyncReport:
    sink: str
    status: str = "ok"            # ok | partial | capped | unavailable | off | failed
    batches: int = 0
    rows: int = 0
    failed: int = 0
    dead_letters: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    skipped_tables: list[str] = field(default_factory=list)
    seconds: float = 0.0
    detail: str = ""
    # Per cursor key, rows this run read: seen = delivered + dead + failed + backoff + stopped (coverage records).
    tables: dict[str, dict[str, int]] = field(default_factory=dict)
    last_ok_at: str | None = None  # the cursor's previous successful run (or try), before this one
    persistent_failures: dict[str, Any] = field(default_factory=dict)
    outstanding_dead_letters: int = 0
    pending_rows: int = 0
    settlement: dict[str, int] = field(default_factory=dict)

    def count(self, table: str, **n: int) -> None:
        t = self.tables.setdefault(table, dict.fromkeys(TABLE_COUNTS, 0))
        for k, v in n.items():
            t[k] += v

    def line(self) -> str:
        extra = f" skipped={','.join(self.skipped_tables)}" if self.skipped_tables else ""
        extra += f" dead_letters={self.dead_letters}" if self.dead_letters else ""
        extra += f" pending={self.pending_rows}" if self.pending_rows else ""
        extra += f" unresolved={len(self.persistent_failures)}" if self.persistent_failures else ""
        extra += f" outstanding_dead_letters={self.outstanding_dead_letters}" if self.outstanding_dead_letters else ""
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


# Mission 004B. A refusal of the content itself (not auth, not overload): split the batch, push the parts, and
# dead-letter only the records that are still refused, so one bad record never blocks its table.
RECORD_ERRORS = frozenset({400, 409, 413, 415, 422})
MAX_DEAD_LETTERS = 500
# Client-side daily limits (rows, requests) per sink. NOT a billing cap: they bound what this loop sends, and
# the provider bills whatever it receives. Override with AMEM_SYNC_<SINK>_MAX_ROWS_PER_DAY / _MAX_REQUESTS_PER_DAY.
DEFAULT_LIMITS = {"clickhouse": (100_000, 2_000), "langfuse": (500, 1_000), "pastdev": (300, 200)}


def limits(sink: str, environ: Mapping[str, str] | None = None) -> tuple[int, int]:
    env = os.environ if environ is None else environ
    rows, reqs = DEFAULT_LIMITS.get(sink, (10_000, 1_000))

    def num(name: str, default: int) -> int:
        v = env.get(f"AMEM_SYNC_{sink.upper()}_{name}", "").strip()
        return int(v) if v.isdigit() else default
    return num("MAX_ROWS_PER_DAY", rows), num("MAX_REQUESTS_PER_DAY", reqs)


class Refused(Exception):
    """Raised by a sink's push(): the provider refused these records for good (Mission 004C: a past.dev
    ingestion that failed as content-rejected, or failed after its free retries). No split and no retry:
    `rows` are dead-lettered, `evidence` (the sink's record of the refusal, so the rows are not sent again
    while unchanged) is recorded, and the table moves on."""

    def __init__(self, message: str, *, rows: list[dict[str, Any]] | None = None,
                 evidence: list[EvidenceRef] | None = None):
        super().__init__(message)
        self.rows = rows
        self.evidence = list(evidence or [])


def _record_error(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    return isinstance(status, int) and status in RECORD_ERRORS


def provider_cap(exc: BaseException) -> bool:
    """The provider's own spending stop: 402 (out of credits) or a 429 that names a reached cap (past.dev
    project-cap-reached / spend-cap-reached). Retrying cannot help, so the run stops as `capped`."""
    status = getattr(exc, "status", None)
    return status == 402 or (status == 429 and "cap-reached" in str(exc))


def _row_key(sink: Sink | None, batch: Batch, r: Mapping[str, Any]) -> str:
    """A dead letter's key: the sink's own (`dead_key`, e.g. past.dev's ref for a wired id), else the row's id."""
    key = getattr(sink, "dead_key", None)
    k = key(r) if key is not None else None
    return str(k or r.get("id") or r.get("trace_id") or r.get("source_id") or batch.key or "?")[:200]


def _dead(batch: Batch, rows: list[dict[str, Any]], exc: BaseException, now: datetime,
          sink: Sink | None = None) -> list[dict[str, Any]]:
    err = f"{type(exc).__name__}: {exc}"[:200]
    return [{"table": batch.table, "key": _row_key(sink, batch, r), "error": err, "at": _iso(now)} for r in rows]


def _refused(batch: Batch, exc: Refused, usage: dict[str, Any], now: datetime,
             sink: Sink | None = None) -> tuple[Batch, list[dict[str, Any]]]:
    """A Refused push: the rows did go out (they count), nothing is delivered, the evidence is recorded."""
    usage["rows"] += len(batch.rows)
    return Batch(batch.table, [], batch.watermark, exc.evidence), _dead(batch, exc.rows or batch.rows, exc, now, sink)


def _deliver(sink: Sink, batch: Batch, usage: dict[str, Any],
             now: datetime) -> tuple[list[Batch], list[dict[str, Any]], BaseException | None]:
    """(pushed parts, dead letters, a retryable error or None)."""
    usage["requests"] += 1
    try:
        sink.push(batch)
        usage["rows"] += len(batch.rows)
        return [batch], [], None
    except Refused as exc:
        part, dead = _refused(batch, exc, usage, now, sink)
        return [part], dead, None
    except Exception as exc:  # noqa: BLE001
        if not _record_error(exc):
            return [], [], exc
        split = getattr(sink, "split", None)
        parts = split(batch) if split and len(batch.rows) > 1 else [batch]
        if len(parts) <= 1:
            return [], _dead(batch, batch.rows, exc, now, sink), None
    ok: list[Batch] = []
    dead: list[dict[str, Any]] = []
    for part in parts:
        usage["requests"] += 1
        try:
            sink.push(part)
            usage["rows"] += len(part.rows)
            ok.append(part)
        except Refused as exc:
            done, refused = _refused(part, exc, usage, now, sink)
            ok.append(done)
            dead += refused
        except Exception as exc:  # noqa: BLE001
            if not _record_error(exc):
                return ok, dead, exc
            dead += _dead(part, part.rows, exc, now, sink)
    return ok, dead, None


def run(sink: Sink, conn: psycopg.Connection, *, budget_s: float = 60.0, replay: bool = False,
        clock: Callable[[], float] = time.monotonic, now: datetime | None = None,
        environ: Mapping[str, str] | None = None) -> SyncReport:
    """Serialize each sink's shared cursor and daily usage, including scoped exports."""
    if switched_off(sink.name, environ):
        return SyncReport(sink.name, status="off", detail="kill switch")
    lock = f"amem:sync:{sink.name}"
    held = conn.execute("select pg_try_advisory_lock(hashtextextended(%s, 0))", (lock,)).fetchone()[0]
    if not held:
        return SyncReport(sink.name, status="unavailable", detail="another sync for this sink is active")
    try:
        return _run(sink, conn, budget_s=budget_s, replay=replay, clock=clock, now=now, environ=environ)
    finally:
        conn.rollback()  # a failed SQL statement must not prevent releasing the session lock
        conn.execute("select pg_advisory_unlock(hashtextextended(%s, 0))", (lock,))
        conn.commit()


def _run(sink: Sink, conn: psycopg.Connection, *, budget_s: float = 60.0, replay: bool = False,
         clock: Callable[[], float] = time.monotonic, now: datetime | None = None,
         environ: Mapping[str, str] | None = None) -> SyncReport:
    rep = SyncReport(sink.name)
    start = clock()
    now = now or datetime.now(UTC)
    if switched_off(sink.name, environ):
        rep.status, rep.detail = "off", "kill switch"
        return rep
    meta = load(conn, sink.name)
    rep.last_ok_at = meta.get("last_ok_at")
    stored: dict[str, str] = dict(meta.get("watermarks") or {})
    watermarks: dict[str, str] = dict(stored)
    if replay:
        # A replay starts its tables over from no cursor. A sink narrowed to some tables (Mission 004C, `--tables`)
        # names them in `replay_scope`, and every other table keeps its cursor; None: every table starts over.
        scope = getattr(sink, "replay_scope", None)
        watermarks = {} if scope is None else {t: m for t, m in stored.items() if t not in scope}
    failures: dict[str, dict[str, Any]] = dict(meta.get("failures") or {})
    dead_letters: list[dict[str, Any]] = list(meta.get("dead_letters") or [])
    usage: dict[str, Any] = dict(meta.get("usage") or {})
    if usage.get("day") != now.date().isoformat():
        usage = {"day": now.date().isoformat(), "rows": 0, "requests": 0}
    max_rows, max_requests = limits(sink.name, environ)
    rep.usage = usage
    try:
        h = sink.health()
    except Exception as exc:  # noqa: BLE001
        h = Health(False, f"health: {type(exc).__name__}")
    if not h.ok:
        rep.status, rep.detail = "unavailable", h.detail
        _save(conn, sink.name, "skipped", {"last_error": f"unavailable: {h.detail}"[:300],
              "last_try_at": _iso(now), 'export_status': 'unavailable'})
        conn.commit()
        rep.seconds = clock() - start
        return rep
    blocked = {t for t, f in failures.items() if not _due(f, now)}
    in_scope = getattr(sink, 'failure_in_scope', lambda key: True)
    rep.skipped_tables = sorted(k for k in blocked if in_scope(k))
    for batch in sink.batches(conn, {} if replay else stored, replay=replay):
        n = len(batch.rows)
        failure_key = batch.failure_key or batch.table
        if failure_key in blocked or batch.table in blocked:
            rep.count(batch.table, seen=n, backoff=n)
            continue
        if clock() - start > budget_s:
            rep.status, rep.detail = "partial", "time budget"
            rep.count(batch.table, seen=n, stopped=n)
            break
        if usage["rows"] + len(batch.rows) > max_rows or usage["requests"] + 1 > max_requests:
            rep.status = "capped"
            rep.detail = f"client-side daily limit: {usage['rows']}/{max_rows} rows, {usage['requests']}/{max_requests} requests"
            rep.count(batch.table, seen=n, stopped=n)
            break
        ok, dead, err = _deliver(sink, batch, usage, now)
        landed_n = sum(len(p.rows) for p in ok)
        if err is None:                    # refused rows become dead letters; the table moves on
            dead_n = min(len(dead), n - landed_n)
            rep.count(batch.table, seen=n, delivered=landed_n, dead=dead_n, failed=n - landed_n - dead_n)
        else:                              # the batch is sent again later: nothing of it is dead yet
            rep.count(batch.table, seen=n, delivered=landed_n,
                      **{"stopped" if provider_cap(err) else "failed": n - landed_n})
        for part in ok:                    # idempotent links for whatever did land
            for ev in part.evidence:
                record(conn, ev)
        if err is not None and provider_cap(err):   # the provider's cap: stop the whole run, move nothing more
            rep.status, rep.detail = "capped", f"provider cap: {err}"[:200]
            _save(conn, sink.name, "running", {"usage": usage})
            conn.commit()
            break
        if err is not None:                # retryable: the table backs off, its watermark stays
            f = failures.get(failure_key, {})
            n = int(f.get("attempts", 0)) + 1
            wait = BACKOFF_MIN[min(n - 1, len(BACKOFF_MIN) - 1)]
            failures[failure_key] = {"attempts": n, "dead": n >= MAX_ATTEMPTS, "last_error": f"{type(err).__name__}: {err}"[:300],
                                     "next_retry_at": _iso(now + timedelta(minutes=wait))}
            blocked.add(failure_key)
            rep.failed += 1
            _save(conn, sink.name, "running", {"failures": failures, "usage": usage})
            conn.commit()
            continue
        # a row delivered now is no longer dead: e.g. past.dev's id-invalid letters of 2026-10-24, re-sent once ids
        # were wired (its record had no link, so the next run sent it again)
        landed = {(batch.table, _row_key(sink, batch, r)) for p in ok for r in p.rows}
        dead_letters = [d for d in dead_letters if (d.get("table"), d.get("key")) not in landed]
        dead_letters = (dead_letters + dead)[-MAX_DEAD_LETTERS:]
        rep.dead_letters += len(dead)
        watermarks[batch.table] = batch.watermark
        failures.pop(failure_key, None)
        checkpoint = getattr(sink, 'checkpoint', None)
        if callable(checkpoint):
            checkpoint(batch, watermarks)
        _save(conn, sink.name, "running", {"watermarks": watermarks, "failures": failures, "usage": usage,
                                           "dead_letters": dead_letters})
        conn.commit()          # the links and the advanced watermark land together
        rep.batches += 1
        rep.rows += sum(len(p.rows) for p in ok)
    # a dead letter for a record the sink already holds is stale: e.g. the 2026-10-24 past.dev replay re-sent two
    # records sent on 2026-10-17 under '/' ids, got 400 id-invalid, and left letters for records that were never lost
    settled = getattr(sink, "settled_keys", None)
    if callable(settled) and dead_letters:
        held = settled(conn)
        dead_letters = [d for d in dead_letters if d.get("key") not in held]
    if rep.failed and rep.status == "ok":
        rep.status = "failed" if not rep.batches else "partial"
    rep.persistent_failures = {k: v for k, v in failures.items() if in_scope(k)}
    rep.outstanding_dead_letters = sum(in_scope(str(d.get('table', ''))) for d in dead_letters)
    completion = getattr(sink, 'completion', None)
    if callable(completion):
        rep.settlement = completion(conn)
        rep.pending_rows = rep.settlement.get('queued', 0)
    incomplete = []
    if rep.persistent_failures:
        incomplete.append(f"{len(rep.persistent_failures)} unresolved retry scopes")
    if rep.outstanding_dead_letters:
        incomplete.append(f"{rep.outstanding_dead_letters} outstanding dead letters")
    if rep.pending_rows:
        incomplete.append(f"{rep.pending_rows} records awaiting provider processing")
    rejected = rep.settlement.get('failed', 0) + rep.settlement.get('rejected', 0)
    if rejected:
        incomplete.append(f"{rejected} failed or rejected records")
    if incomplete:
        if rep.status == 'ok':
            rep.status = 'partial'
        rep.detail = '; '.join(filter(None, [rep.detail, *incomplete]))
    final = {"watermarks": watermarks, "failures": failures, "last_try_at": _iso(now), "usage": usage,
             "dead_letters": dead_letters, "export_status": rep.status, "settlement": rep.settlement,
             "last_error": rep.detail if rep.status != 'ok' else None}
    if rep.status == "ok":
        final["last_ok_at"] = _iso(now)
    _save(conn, sink.name, "failed" if rep.status == "failed" else "done", final)
    conn.commit()
    rep.seconds = clock() - start
    return rep


def plan(sink: Sink, conn: psycopg.Connection, *, replay: bool = False,
         environ: Mapping[str, str] | None = None, contents: bool = False, limit: int | None = None,
         cls: str | None = None) -> dict[str, Any]:
    """A dry run: what the next `run` (or a `--replay` backfill) would send, per table, against the
    client-side daily limits. Read-only: the connection is put in read-only mode, health() is not called
    (it may wake or write to the provider) and nothing is pushed. A sink may price batches with cost(), and
    report `masked` (Mission 004C, F8): rows masked (ClickHouse) or withheld (past.dev) as team and as private,
    per table with own, and the owner accounts used, so the owner can review them before a backfill.

    `contents` (Mission 004C backfill review; a sink with describe()): also every outgoing record, as the exact
    payload, its sha256, ownership class and size (`records`), the actions without content (`actions`: past.dev
    retractions and ingestion checks) and the refused records with their reasons, never their content
    (`refused_records`). `cls` keeps only records of that class; `limit` cuts each list (the totals stay)."""
    was = conn.read_only
    conn.read_only = True
    try:
        return _plan(sink, conn, replay=replay, environ=environ, contents=contents, limit=limit, cls=cls)
    finally:
        conn.rollback()
        conn.read_only = was


def _plan(sink: Sink, conn: psycopg.Connection, *, replay: bool, environ: Mapping[str, str] | None,
          contents: bool = False, limit: int | None = None, cls: str | None = None) -> dict[str, Any]:
    meta = load(conn, sink.name)
    watermarks: dict[str, str] = {} if replay else dict(meta.get("watermarks") or {})
    tables: dict[str, dict[str, int]] = {}
    describe = getattr(sink, "describe", None) if contents else None
    if contents and describe is None:
        raise ValueError(f"{sink.name}: no record preview (--contents)")
    records: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for batch in sink.batches(conn, watermarks, replay=replay):
        t = tables.setdefault(batch.table, {"batches": 0, "rows": 0, "bytes": 0})
        t["batches"] += 1
        t["rows"] += len(batch.rows)
        t["bytes"] += sum(len(json.dumps(r, default=str).encode("utf-8")) for r in batch.rows)
        cost = getattr(sink, "cost", None)
        if cost is not None and batch.rows:
            t["cost"] = t.get("cost", 0) + int(cost(batch))
        if describe is not None:
            for e in describe(batch):
                (records if "payload" in e else actions).append(e)
    max_rows, max_requests = limits(sink.name, environ)
    rows = sum(t["rows"] for t in tables.values())
    reqs = sum(t["batches"] for t in tables.values())
    out: dict[str, Any] = {"sink": sink.name, "replay": replay, "tables": tables, "rows": rows, "requests": reqs,
                           "limit_rows_per_day": max_rows, "limit_requests_per_day": max_requests,
                           "days_at_limit": max(-(-rows // max_rows) if max_rows else 0, -(-reqs // max_requests) if max_requests else 0)}
    for attr in ("refused", "retired"):
        v = getattr(sink, attr, None)
        if isinstance(v, list):
            out[attr] = len(v)
    masked = getattr(sink, "masked", None)      # Mission 004C (F8): rows masked or withheld as team vs private
    if isinstance(masked, dict):
        out["masked"] = masked
    if any("cost" in t for t in tables.values()):
        out["cost"] = sum(t.get("cost", 0) for t in tables.values())
    review = getattr(sink, "review", None)      # Mission 004C: a reviewed set's check (past.dev --only)
    if isinstance(review, dict):
        out["review"] = review
    if describe is not None:
        refused_of = getattr(sink, "refused_records", None)
        refused = list(refused_of()) if refused_of is not None else []
        if cls is not None:
            records = [r for r in records if r.get("class") == cls]
            refused = [r for r in refused if r.get("class", cls) == cls]     # a count per table has no class
        cut = (lambda xs: xs) if limit is None else (lambda xs: xs[:max(0, limit)])
        out.update({"contents": {"class": cls, "limit": limit,
                                 "sha256": "sha256 of the canonical JSON of payload (sorted keys, no spaces, UTF-8)"},
                    "records_total": len(records), "records": cut(records),
                    "refused_total": len(refused), "refused_records": cut(refused), "actions": actions})
    return out


def status(conn: psycopg.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("select job, status, finished_at, meta from rsi_checkpoints where job like 'sync:%%' "
                        "and run_key = %s order by job", (RUN_KEY,)).fetchall()
    out = []
    for j, st, fin, meta in rows:
        meta = meta or {}
        fails = meta.get("failures") or {}
        out.append({"sink": j.split(":", 1)[1], "status": st if st == 'running' else meta.get('export_status', st), "finished_at": fin.isoformat() if fin else None,
                    "tables": sorted((meta.get("watermarks") or {}).keys()), "last_ok_at": meta.get("last_ok_at"),
                    "dead": sorted(t for t, f in fails.items() if f.get("dead")),
                    "failing": sorted(t for t, f in fails.items() if not f.get("dead")),
                    "dead_letters": len(meta.get("dead_letters") or []), "usage": meta.get("usage") or {},
                    "last_error": meta.get("last_error"), "settlement": meta.get('settlement', {})})
    return out
