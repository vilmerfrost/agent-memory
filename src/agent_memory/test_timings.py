"""Small pytest measurements, queued without credentials and uploaded once per run."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import config, store

MAX_BATCH_BYTES = 32_000_000
IDENTITY = re.compile(r"[A-Za-z0-9_.:/@+-]{1,200}\Z")


def identity(value: str | None) -> str | None:
    return value if isinstance(value, str) and IDENTITY.fullmatch(value) else None


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".test-timing-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, allow_nan=False, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class Capture:
    """Controller-side reports work with serial pytest and xdist alike."""

    def __init__(self, *, repo: str, head: str | None, dirty: bool | None,
                 workers: int, selection: str, log_ref: str):
        self.started = time.monotonic()
        self.run = dict(run_id=str(uuid.uuid4()), repo=repo, head=head, dirty=dirty,
                        workers=workers, selection=selection, log_ref=log_ref,
                        session_id=identity(os.environ.get("AMEM_TIMING_SESSION_ID")),
                        parent_span_id=identity(os.environ.get("AMEM_TIMING_PARENT_SPAN_ID")),
                        started_at=datetime.now(UTC).isoformat())
        # Snapshot before pytest's offline fixture scrubs inherited AMEM_* variables.
        parent = os.environ.get("PARENT_DIR")
        self.queue = (Path(parent)/"test-timing-queue"
                      if parent and self.run["parent_span_id"] and os.environ.get("AMEM_TIMING_OFF") != "1"
                      else None)
        self.tests: dict[str, dict] = {}
        self.collection_ready_ms: float | None = None
        self.collection_errors = self.worker_errors = 0

    def collected(self) -> None:
        self.collection_ready_ms = (time.monotonic()-self.started)*1000

    def report(self, report) -> None:
        if report.when not in ("setup", "call", "teardown"):
            return
        key = hashlib.sha256(report.nodeid.encode()).hexdigest()
        # Parametrized IDs can contain credentials or data values. Keep a stable
        # hash for comparisons, but never store those values in the timing batch.
        name = report.nodeid.split("[", 1)[0]
        name = "".join(c for c in name if c.isprintable())[:1800]
        if "[" in report.nodeid:
            name += f"[case-{key[:12]}]"
        row = self.tests.setdefault(key, dict(test_key=key, nodeid=name, worker_id=None,
                                              setup_ms=None, call_ms=None, teardown_ms=None,
                                              outcome="incomplete", complete=False))
        duration = report.duration*1000
        row[report.when+"_ms"] = duration if math.isfinite(duration) and duration >= 0 else None
        row["worker_id"] = identity(getattr(report, "worker_id", None))
        if report.failed:
            row["outcome"] = "failed" if report.when == "call" else "error"
        elif row["outcome"] not in ("failed", "error"):
            if report.skipped:
                row["outcome"] = "xfailed" if hasattr(report, "wasxfail") else "skipped"
            elif report.when == "call":
                row["outcome"] = "xpassed" if hasattr(report, "wasxfail") else "passed"
        if report.when == "teardown":
            row["complete"] = True

    def finish(self, code: int) -> dict:
        tests = list(self.tests.values())
        for row in tests:
            phases = [row[k] for k in ("setup_ms", "call_ms", "teardown_ms") if row[k] is not None]
            row["total_ms"] = sum(phases) if phases else None
        return {"schema": 1, "run": {**self.run, "ended_at": datetime.now(UTC).isoformat(),
                "elapsed_ms": (time.monotonic()-self.started)*1000, "exit_code": code,
                "collection_ready_ms": self.collection_ready_ms,
                "collection_errors": self.collection_errors, "worker_errors": self.worker_errors,
                "tests_reported": len(tests), "tests_complete": sum(t["complete"] for t in tests),
                "outcomes": dict(Counter(t["outcome"] for t in tests))}, "tests": tests}


def validate(batch: dict) -> dict:
    """Validate the entire batch before opening a database transaction."""
    if not isinstance(batch, dict) or batch.get("schema") != 1:
        raise ValueError("unsupported test timing schema")
    run, tests = batch.get("run"), batch.get("tests")
    if not isinstance(run, dict) or not isinstance(tests, list) or len(tests) > 100_000:
        raise ValueError("invalid test timing batch")
    uuid.UUID(run["run_id"])
    for k in ("session_id", "parent_span_id"):
        if run.get(k) is not None and identity(run[k]) is None:
            raise ValueError("invalid correlation identity")
    for k, maximum in (("repo", 200), ("log_ref", 240)):
        if not isinstance(run.get(k), str) or not 0 < len(run[k]) <= maximum:
            raise ValueError("invalid run label")
    if Path(run["log_ref"]).name != run["log_ref"]:
        raise ValueError("log reference must be a filename")
    if run.get("head") is not None and not re.fullmatch(r"[0-9a-f]{40,64}", run["head"]):
        raise ValueError("invalid commit")
    if (run.get("dirty") is not None and type(run["dirty"]) is not bool) or run.get("selection") not in ("full", "custom"):
        raise ValueError("invalid run context")
    for k in ("workers", "tests_reported", "tests_complete", "collection_errors", "worker_errors"):
        if type(run.get(k)) is not int or run[k] < 0:
            raise ValueError("invalid run count")
    if type(run.get("exit_code")) is not int:
        raise ValueError("invalid exit code")
    start, end = (datetime.fromisoformat(run[k]) for k in ("started_at", "ended_at"))
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("invalid run interval")
    if run.get("elapsed_ms") is None:
        raise ValueError("missing run duration")
    for value in [run.get("elapsed_ms"), run.get("collection_ready_ms")]:
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("invalid run duration")
    seen = set()
    for row in tests:
        if not isinstance(row, dict) or not re.fullmatch(r"[0-9a-f]{64}", row.get("test_key", "")):
            raise ValueError("invalid test identity")
        if row["test_key"] in seen:
            raise ValueError("duplicate test identity")
        seen.add(row["test_key"])
        if not isinstance(row.get("nodeid"), str) or not 0 < len(row["nodeid"]) <= 2048:
            raise ValueError("invalid test name")
        if row.get("worker_id") is not None and identity(row["worker_id"]) is None:
            raise ValueError("invalid worker")
        if row.get("outcome") not in ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "incomplete") or type(row.get("complete")) is not bool:
            raise ValueError("invalid test result")
        for k in ("setup_ms", "call_ms", "teardown_ms", "total_ms"):
            v = row.get(k)
            if v is not None and (type(v) not in (int, float) or not math.isfinite(v) or v < 0):
                raise ValueError("invalid test duration")
        total = sum(row[k] for k in ("setup_ms", "call_ms", "teardown_ms") if row.get(k) is not None)
        if row.get("total_ms") is None or not math.isclose(row["total_ms"], total):
            raise ValueError("invalid phase sum")
    if run["tests_reported"] != len(tests) or run["tests_complete"] != sum(t["complete"] for t in tests):
        raise ValueError("inconsistent counts")
    # Ignore supplied summaries and any extra payload fields; derive only our allowlist.
    return {"schema": 1, "run": {**run, "outcomes": dict(Counter(t["outcome"] for t in tests))}, "tests": tests}


RUN_FIELDS = ("run_id", "session_id", "parent_span_id", "repo", "head", "dirty", "selection", "workers",
              "started_at", "ended_at", "elapsed_ms", "exit_code", "collection_ready_ms",
              "collection_errors", "worker_errors", "tests_reported", "tests_complete", "outcomes", "log_ref")
TEST_FIELDS = ("test_key", "nodeid", "worker_id", "setup_ms", "call_ms", "teardown_ms", "total_ms", "outcome", "complete")


def upload(batch: dict, *, db_url: str | None = None) -> str:
    batch = validate(batch)
    run = batch["run"]
    target = db_url or config.db_url()
    options = conninfo_to_dict(target).get("options", "")+" -c statement_timeout=15000 -c lock_timeout=5000"
    target = make_conninfo(target, connect_timeout=5, options=options.strip())
    with store.connect(target) as conn:
        fields = ",".join(RUN_FIELDS)
        values = [Jsonb(run[k]) if k == "outcomes" else run.get(k) for k in RUN_FIELDS]
        conn.execute(f"insert into public.test_timing_runs ({fields},details_retained) "
                     f"values ({','.join(['%s']*len(values))},%s::timestamptz >= now()-interval '30 days') "
                     "on conflict(run_id) do nothing", (*values, run["ended_at"]))
        retained = conn.execute("select details_retained and ended_at >= now()-interval '30 days' "
                                "from public.test_timing_runs where run_id=%s", (run["run_id"],)).fetchone()[0]
        if retained and batch["tests"]:
            with conn.cursor() as cur:
                cur.executemany("insert into public.test_timing_cases (run_id,"+",".join(TEST_FIELDS)+") "
                                "values ("+",".join(["%s"]*(len(TEST_FIELDS)+1))+") on conflict do nothing",
                                [(run["run_id"], *(t.get(k) for k in TEST_FIELDS)) for t in batch["tests"]])
    return run["run_id"]


def flush_queue(queue: Path, *, db_url: str | None = None) -> dict:
    result = dict(uploaded=0, invalid=0, pending=0)
    if not queue.exists():
        return result
    if queue.is_symlink():
        raise ValueError("test timing queue cannot be a symlink")
    with (queue/".flush.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, "busy": True}
        for path in sorted(queue.glob("run-*.json"))[:100]:
            try:
                if path.is_symlink() or path.stat().st_size > MAX_BATCH_BYTES:
                    raise ValueError("invalid timing file")
                upload(json.loads(path.read_text()), db_url=db_url)
            except (ValueError, KeyError, TypeError, OverflowError):
                path.rename(path.with_suffix(".invalid"))
                result["invalid"] += 1
                continue
            # A DB/IO failure leaves this and subsequent batches untouched for retry.
            path.unlink()
            result["uploaded"] += 1
        result["pending"] = len(list(queue.glob("run-*.json")))
    return result


def prune(*, db_url: str | None = None) -> int:
    with store.connect(db_url) as conn:
        # One transaction keeps the summary's retention flag and its details consistent.
        rows = conn.execute("update public.test_timing_runs set details_retained=false "
                            "where details_retained and ended_at < now()-interval '30 days' returning run_id").fetchall()
        if rows:
            conn.execute("delete from public.test_timing_cases where run_id=any(%s::uuid[])", ([r[0] for r in rows],))
    return len(rows)


def report(*, run_id: str | None = None, session_id: str | None = None,
           limit: int = 10, db_url: str | None = None) -> dict:
    with store.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from public.test_timing_runs where (%s::uuid is null or run_id=%s::uuid) "
                    "and (%s::text is null or session_id=%s) order by ended_at desc limit 1",
                    (run_id, run_id, session_id, session_id))
        run = cur.fetchone()
        if run is None:
            return {"run": None, "slowest": []}
        cur.execute("select * from public.test_timing_cases where run_id=%s "
                    "order by total_ms desc nulls last,test_key limit %s", (run["run_id"], limit))
        return {"run": run, "slowest": cur.fetchall()}
