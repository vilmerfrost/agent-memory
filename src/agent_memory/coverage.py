"""Coverage snapshots, routine consumption and the "all clear" guard (plan section 4, docs/data-use/coverage.md).

A coverage snapshot says what a dataset held for one time window: what was eligible (an explicit
denominator), collected, linked and measured. A consumption record says what one job run did with one
dataset: queried, considered, used, skipped (with reason counts), the findings it produced and at most
MAX_EVIDENCE_REFS refs to the evidence it cited. Both are upserts on a natural key, so recording the same
thing twice leaves one row.

`verdict` is the guard: a job may say "all clear" only when every dataset that conclusion needs has a
snapshot covering the input window with status `complete`, and a consumption record for that run with
status `used_no_finding`. Anything else comes back as the specific missing-evidence distinction.

Routines write through `record_best_effort` (one savepoint per record, never raises, off with
AMEM_COVERAGE_WRITE=0) and name datasets with `dataset_key(<family>, <part>)`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory.integrations import refs

SNAPSHOT_STATUSES = ("complete", "no_eligible_data", "not_collected", "collected_unlinked", "measurement_unavailable")
CONSUMPTION_STATUSES = ("findings", "used_no_finding", "read_excluded", "analysis_not_run", "analysis_failed")
# The eight distinctions the plan requires coverage to keep apart (everything but complete and findings).
DISTINCTIONS = ("no_eligible_data", "not_collected", "collected_unlinked", "measurement_unavailable",
                "read_excluded", "used_no_finding", "analysis_not_run", "analysis_failed")
MAX_EVIDENCE_REFS = 20
MAX_FIELDS = 50

Recorded = Literal["inserted", "updated", "unchanged"]
WRITE_SWITCH = "AMEM_COVERAGE_WRITE"      # 0 / off / false / no: routines record nothing
DATASET_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")   # the migration's dataset check


def _nonneg(name: str, value: int | None) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True)
class Snapshot:
    dataset: str
    window_start: datetime
    window_end: datetime
    denominator: str                     # what "eligible" counts, e.g. "prod sessions modified in the window"
    eligible: int | None                 # None: the denominator could not be measured
    collected: int | None = None
    linked: int | None = None
    unlinked: int | None = None
    pending: int = 0
    rejected: int = 0
    expired: int = 0
    measured_fields: tuple[str, ...] = ()
    unmeasured_fields: tuple[str, ...] = ()
    last_collected_at: datetime | None = None
    last_analysed_at: datetime | None = None
    source_versions: Mapping[str, str] = field(default_factory=dict)
    checkpoints: Mapping[str, str] = field(default_factory=dict)
    status: str | None = None            # None: derived from the counts
    detail: str | None = None
    run_id: UUID | str | None = None

    def resolved_status(self) -> str:
        if self.status is not None:
            return self.status
        if self.eligible is None:
            return "measurement_unavailable"
        if self.eligible == 0:
            return "no_eligible_data"
        if (self.collected or 0) < self.eligible:
            return "not_collected"
        if (self.unlinked or 0) > 0:
            return "collected_unlinked"
        if self.unmeasured_fields:
            return "measurement_unavailable"
        return "complete"

    def validate(self) -> None:
        if not self.denominator.strip():
            raise ValueError("a snapshot needs an explicit denominator")
        if self.window_end < self.window_start:
            raise ValueError("window_end is before window_start")
        for name in ("eligible", "collected", "linked", "unlinked", "pending", "rejected", "expired"):
            _nonneg(name, getattr(self, name))
        if len(self.measured_fields) > MAX_FIELDS or len(self.unmeasured_fields) > MAX_FIELDS:
            raise ValueError(f"at most {MAX_FIELDS} measured / unmeasured fields")
        if self.resolved_status() not in SNAPSHOT_STATUSES:
            raise ValueError(f"unknown snapshot status {self.resolved_status()!r}")


@dataclass(frozen=True)
class Consumption:
    job: str
    run_key: str                         # the job's run key (rsi_checkpoints.run_key), e.g. the night
    dataset: str
    analysis_version: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    source_checkpoint: str | None = None
    queried: int = 0
    considered: int = 0
    used: int = 0
    skipped: Mapping[str, int] = field(default_factory=dict)    # reason -> count
    findings: int = 0
    evidence_refs: Sequence[str] = ()
    status: str | None = None            # None: derived from the counts
    detail: str | None = None
    run_id: UUID | str | None = None

    def resolved_status(self) -> str:
        if self.status is not None:
            return self.status
        if self.findings > 0:
            return "findings"
        if self.used > 0:
            return "used_no_finding"
        if self.queried > 0:
            return "read_excluded"
        return "analysis_not_run"

    def validate(self) -> None:
        for name in ("queried", "considered", "used", "findings"):
            _nonneg(name, getattr(self, name))
        if not self.used <= self.considered <= self.queried:
            raise ValueError("counts must satisfy used <= considered <= queried")
        for reason, n in self.skipped.items():
            if not isinstance(reason, str) or not reason:
                raise ValueError("skip reasons are non-empty strings")
            _nonneg(f"skipped[{reason}]", n)
        if len(self.evidence_refs) > MAX_EVIDENCE_REFS:
            raise ValueError(f"at most {MAX_EVIDENCE_REFS} evidence refs (cite the evidence, not every row)")
        bad = [r for r in self.evidence_refs if not refs.is_ref(r)]
        if bad:
            raise ValueError(f"not an evidence ref: {bad[0][:80]!r}")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("give both window ends or neither")
        if self.window_start and self.window_end and self.window_end < self.window_start:
            raise ValueError("window_end is before window_start")
        if self.resolved_status() not in CONSUMPTION_STATUSES:
            raise ValueError(f"unknown consumption status {self.resolved_status()!r}")


_SNAPSHOT_COLS = ("dataset", "window_start", "window_end", "denominator", "eligible", "collected", "linked",
                  "unlinked", "pending", "rejected", "expired", "measured_fields", "unmeasured_fields",
                  "last_collected_at", "last_analysed_at", "source_versions", "checkpoints", "status", "detail",
                  "run_id")
_CONSUMPTION_COLS = ("job", "run_key", "run_id", "dataset", "analysis_version", "window_start", "window_end",
                     "source_checkpoint", "queried", "considered", "used", "skipped", "findings", "evidence_refs",
                     "status", "detail")


def _upsert_sql(table: str, cols: tuple[str, ...], key: tuple[str, ...]) -> str:
    rest = [c for c in cols if c not in key]
    return (f"insert into public.{table} ({', '.join(cols)}) values ({', '.join(f'%({c})s' for c in cols)}) "
            f"on conflict ({', '.join(key)}) do update set "
            + ", ".join(f"{c} = excluded.{c}" for c in rest) + ", updated_at = now() "
            f"where ({', '.join(f'{table}.{c}' for c in rest)}) is distinct from "
            f"({', '.join(f'excluded.{c}' for c in rest)}) "
            "returning (xmax = 0) as inserted")


SNAPSHOT_UPSERT = _upsert_sql("coverage_snapshots", _SNAPSHOT_COLS, ("dataset", "window_start", "window_end"))
CONSUMPTION_UPSERT = _upsert_sql("routine_consumption", _CONSUMPTION_COLS, ("job", "run_key", "dataset"))


def _result(row: tuple[Any, ...] | None) -> Recorded:
    return "unchanged" if row is None else ("inserted" if row[0] else "updated")


def record_snapshot(conn: psycopg.Connection, snap: Snapshot) -> Recorded:
    """Upsert one snapshot by (dataset, window). The same snapshot twice: one row, second call `unchanged`."""
    snap.validate()
    row = {c: getattr(snap, c) for c in _SNAPSHOT_COLS}
    row.update(status=snap.resolved_status(), measured_fields=list(snap.measured_fields),
               unmeasured_fields=list(snap.unmeasured_fields), source_versions=Jsonb(dict(snap.source_versions)),
               checkpoints=Jsonb(dict(snap.checkpoints)), run_id=str(snap.run_id) if snap.run_id else None)
    return _result(conn.execute(SNAPSHOT_UPSERT, row).fetchone())


def record_consumption(conn: psycopg.Connection, c: Consumption) -> Recorded:
    """Upsert one consumption record by (job, run_key, dataset). Replay leaves one row."""
    c.validate()
    row = {k: getattr(c, k) for k in _CONSUMPTION_COLS}
    row.update(status=c.resolved_status(), skipped=Jsonb(dict(c.skipped)), evidence_refs=list(c.evidence_refs),
               run_id=str(c.run_id) if c.run_id else None)
    return _result(conn.execute(CONSUMPTION_UPSERT, row).fetchone())


def snapshots(conn: psycopg.Connection, dataset: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from public.coverage_snapshots where %(d)s::text is null or dataset = %(d)s "
                    "order by window_end desc, dataset limit %(n)s", {"d": dataset, "n": limit})
        return cur.fetchall()


def consumption(conn: psycopg.Connection, job: str | None = None, run_key: str | None = None,
                limit: int = 50) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from public.routine_consumption where (%(j)s::text is null or job = %(j)s) "
                    "and (%(r)s::text is null or run_key = %(r)s) order by recorded_at desc, dataset limit %(n)s",
                    {"j": job, "r": run_key, "n": limit})
        return cur.fetchall()


# --------------------------------------------------------------------------- the "all clear" guard


@dataclass(frozen=True)
class Requirement:
    dataset: str
    allow_no_eligible: bool = False      # True: an empty dataset does not block the conclusion


@dataclass(frozen=True)
class Gap:
    dataset: str
    distinction: str                     # one of DISTINCTIONS
    detail: str


@dataclass(frozen=True)
class Verdict:
    conclusion: Literal["all_clear", "findings", "incomplete"]
    findings: int
    gaps: tuple[Gap, ...]
    datasets: tuple[str, ...]

    @property
    def all_clear(self) -> bool:
        return self.conclusion == "all_clear"

    def as_dict(self) -> dict[str, Any]:
        return {"conclusion": self.conclusion, "findings": self.findings, "datasets": list(self.datasets),
                "gaps": [{"dataset": g.dataset, "distinction": g.distinction, "detail": g.detail} for g in self.gaps]}


def _req(r: str | Requirement) -> Requirement:
    return r if isinstance(r, Requirement) else Requirement(r)


def evaluate(required: Iterable[str | Requirement], snaps: Mapping[str, Mapping[str, Any] | None],
             uses: Mapping[str, Mapping[str, Any] | None]) -> Verdict:
    """The guard on plain rows: `snaps` and `uses` map dataset -> its row (or None when missing)."""
    reqs = [_req(r) for r in required]
    if not reqs:
        raise ValueError("an all-clear conclusion needs at least one required dataset")
    gaps: list[Gap] = []
    findings = 0
    for req in reqs:
        s, u = snaps.get(req.dataset), uses.get(req.dataset)
        if s is None:
            gaps.append(Gap(req.dataset, "not_collected", "no coverage snapshot for this dataset" if u is None
                            else "no coverage snapshot covers the input window"))
        elif s["status"] != "complete" and not (s["status"] == "no_eligible_data" and req.allow_no_eligible):
            gaps.append(Gap(req.dataset, s["status"], s.get("detail") or f"snapshot status {s['status']}"))
        if u is None:
            gaps.append(Gap(req.dataset, "analysis_not_run", "no consumption record for this run"))
            continue
        findings += int(u.get("findings") or 0)
        empty = s is not None and s["status"] == "no_eligible_data"
        if u["status"] in ("read_excluded", "analysis_failed") or (u["status"] == "analysis_not_run" and not empty):
            # Nothing to analyse is reported once, as no_eligible_data, not again as analysis_not_run.
            gaps.append(Gap(req.dataset, u["status"], u.get("detail") or f"consumption status {u['status']}"))
    conclusion: Literal["all_clear", "findings", "incomplete"] = (
        "findings" if findings else "incomplete" if gaps else "all_clear")
    return Verdict(conclusion, findings, tuple(gaps), tuple(r.dataset for r in reqs))


def verdict(conn: psycopg.Connection, *, job: str, run_key: str,
            required: Sequence[str | Requirement]) -> Verdict:
    """Read this run's consumption records and the snapshots that cover their input windows, then evaluate."""
    reqs = [_req(r) for r in required]
    names = [r.dataset for r in reqs]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from public.routine_consumption where job = %s and run_key = %s and dataset = any(%s)",
                    (job, run_key, names))
        uses = {r["dataset"]: r for r in cur.fetchall()}
        snaps: dict[str, dict[str, Any] | None] = {}
        for name in names:
            u = uses.get(name)
            if u is None:
                # Not analysed: the latest snapshot still tells whether the data was there to analyse.
                cur.execute("select * from public.coverage_snapshots where dataset = %s "
                            "order by window_end desc, updated_at desc limit 1", (name,))
            elif u["window_start"] is None:
                # Without an input window no snapshot can be shown to cover the run's input.
                snaps[name] = None
                continue
            else:
                cur.execute("select * from public.coverage_snapshots where dataset = %s and window_start <= %s "
                            "and window_end >= %s order by updated_at desc limit 1",
                            (name, u["window_start"], u["window_end"]))
            snaps[name] = cur.fetchone()
    return evaluate(reqs, snaps, uses)


# --------------------------------------------------------------------------- routine writers


def writes_enabled(environ: Mapping[str, str] | None = None) -> bool:
    v = ((os.environ if environ is None else environ).get(WRITE_SWITCH) or "").strip().lower()
    return v not in ("0", "off", "false", "no")


def dataset_key(family_key: str, part: str) -> str:
    """`<family>:<part>`, the dataset name every routine writes (e.g. `deviations:deviations`,
    `skills:skill_calls`). The family must exist in data_families, so a dataset always maps to its owner."""
    from agent_memory import data_families
    data_families.family(family_key)                    # KeyError for an unknown family
    key = f"{family_key}:{part}"
    if not DATASET_RE.fullmatch(key):
        raise ValueError(f"dataset key {key!r} does not match {DATASET_RE.pattern}")
    return key


def record_best_effort(conn: psycopg.Connection, records: Iterable[Snapshot | Consumption], *,
                       environ: Mapping[str, str] | None = None) -> list[str]:
    """Write each record in its own savepoint and return one problem line per record that failed (for the job's
    notes). Never raises: coverage must not fail the routine it describes. Nothing is written when
    AMEM_COVERAGE_WRITE is off."""
    if not writes_enabled(environ):
        return []
    problems: list[str] = []
    for r in records:
        try:
            with conn.transaction():
                if isinstance(r, Snapshot):
                    record_snapshot(conn, r)
                else:
                    record_consumption(conn, r)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            problems.append(f"coverage {getattr(r, 'dataset', '?')}: {type(exc).__name__}: {str(exc)[:160]}")
    return problems
