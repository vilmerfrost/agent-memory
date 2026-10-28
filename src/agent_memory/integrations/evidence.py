"""EvidenceRef, the Retriever protocol and the query facade (blueprint A1 step 4, C4 "Produces").

The investigator never talks to ClickHouse, Langfuse or past.dev directly. It calls `gather()` (or
`amem evidence query`), which asks Supabase first and then each optional retriever. Every retriever
answers only a fixed set of named, parameterized queries; there is no free-form query path for a model.
A retriever that is down, unconfigured or switched off answers `unavailable`; one that raises answers
`error`. Neither ever blocks the caller.

Evidence from another system is used only after `usable()` re-read its `amem:` subject refs from
Supabase (blueprint C1 rule). `record()` stores an external link in `rsi_evidence`, whose unique
`(system, external_ref)` key makes it idempotent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg import sql

from agent_memory.integrations import refs, http

STATUSES = ("ok", "unavailable", "error", "unknown-query", "bad-params")


class Unavailable(Exception):
    """Raised by a retriever's query() when policy, not failure, stops this one query (Mission 005.5: data not
    approved for this provider, a withheld repo, a disabled provider). run_query answers `unavailable` with the
    message as the detail, never `error`."""


@dataclass
class EvidenceRef:
    ref: str                                        # this record, in its own system (ch:, lf:, past:, amem:)
    source: str                                     # sb | ch | lf | pd | ncm
    kind: str                                       # metric | trace | score | decision-memory | row | research-run
    subject_refs: list[str] = field(default_factory=list)   # the amem: refs it is about
    metrics: dict[str, Any] = field(default_factory=dict)   # numbers and labels only
    summary: str | None = None                      # sanitized, short; None for ClickHouse (no text)
    observed_at: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        refs.parse(self.ref)
        for s in self.subject_refs:
            if refs.parse(s).scheme != "amem":
                raise refs.RefError(f"subject refs are amem: refs, got {s!r}")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Health:
    ok: bool
    detail: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class NamedQuery:
    name: str
    params: Mapping[str, str]       # param name -> regex its value must fully match
    description: str
    required: tuple[str, ...] = ()

    def check(self, params: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in params.items():
            pat = self.params.get(k)
            if pat is None:
                raise ValueError(f"{self.name}: unknown param {k!r}")
            if not isinstance(v, str) or not re.fullmatch(pat, v):
                raise ValueError(f"{self.name}: bad value for {k!r}")
            out[k] = v
        missing = [k for k in self.required if k not in out]
        if missing:
            raise ValueError(f"{self.name}: missing param(s) {', '.join(missing)}")
        return out


class Retriever(Protocol):
    source: str

    def health(self) -> Health: ...

    def named_queries(self) -> Mapping[str, NamedQuery]: ...

    def query(self, name: str, params: Mapping[str, str]) -> list[EvidenceRef]: ...


@dataclass
class QueryResult:
    source: str
    status: str
    refs: list[EvidenceRef] = field(default_factory=list)
    detail: str = ""

    retryable: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"source": self.source, "status": self.status, "detail": self.detail,
                "refs": [r.to_json() for r in self.refs]}


def transient_read_error(exc: Exception) -> bool:
    """Only transport/temporary server failures; never auth, policy or malformed data."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return isinstance(exc, http.HttpError) and exc.retryable


def run_query(r: Retriever, name: str, params: Mapping[str, str]) -> QueryResult:
    nq = r.named_queries().get(name)
    if nq is None:
        return QueryResult(r.source, "unknown-query", detail=name)
    try:
        checked = nq.check(params)
    except ValueError as exc:
        return QueryResult(r.source, "bad-params", detail=str(exc))
    # Policy exclusions must be decided before contacting even the health URL.
    preflight = getattr(r, '_preflight_query', None)
    if callable(preflight):
        try:
            preflight(name, checked)
        except Unavailable as exc:
            return QueryResult(r.source, "unavailable", detail=str(exc)[:200])
    try:
        h = r.health()
    except Exception as exc:  # noqa: BLE001 - a broken health check is "unavailable", never a crash
        return QueryResult(r.source, "unavailable", detail=f"health: {type(exc).__name__}", retryable=transient_read_error(exc))
    if not h.ok:
        return QueryResult(r.source, "unavailable", detail=h.detail, retryable=getattr(h, "retryable", False))
    try:
        return QueryResult(r.source, "ok", r.query(name, checked))
    except Unavailable as exc:
        return QueryResult(r.source, "unavailable", detail=str(exc)[:200])
    except TimeoutError as exc:
        return QueryResult(r.source, "error", detail=f"timeout ({type(exc).__name__})", retryable=True)
    except Exception as exc:  # noqa: BLE001 - one retriever's failure never blocks the others
        return QueryResult(r.source, "error", detail=type(exc).__name__, retryable=transient_read_error(exc))


def gather(name: str, params: Mapping[str, str], retrievers: Sequence[Retriever]) -> list[QueryResult]:
    """Supabase first (always), then the optional retrievers in the given order."""
    ordered = sorted(retrievers, key=lambda r: 0 if r.source == "sb" else 1)
    return [run_query(r, name, params) for r in ordered]


# ---------------------------------------------------------------- Supabase: authority and re-reads


def exists(conn: psycopg.Connection, ref: str) -> bool:
    """True when an amem: ref names an existing row. Only the tables in refs.AMEM_ENTITIES."""
    r = refs.parse(ref)
    if r.scheme != "amem":
        raise refs.RefError(f"only amem: refs resolve in Supabase, got {ref!r}")
    table, key = refs.AMEM_ENTITIES[r.kind]
    q = sql.SQL("select 1 from {} where {}::text = %s limit 1").format(sql.Identifier(table), sql.Identifier(key))
    return conn.execute(q, (r.id,)).fetchone() is not None


def usable(conn: psycopg.Connection, ev: EvidenceRef) -> bool:
    """Evidence from any system is usable only when every amem: subject ref still resolves."""
    return all(exists(conn, s) for s in ev.subject_refs)


def record(conn: psycopg.Connection, ev: EvidenceRef, *, valid_days: int | None = None) -> str:
    """Store the external link in rsi_evidence (idempotent on (system, external_ref))."""
    from agent_memory import rsi_approval   # lazy: rsi_approval imports a lot

    return rsi_approval.add_evidence(conn, system=refs.parse(ev.ref).system, kind=ev.kind,
                                     subject_refs=list(ev.subject_refs), summary=ev.summary or "",
                                     metrics=dict(ev.metrics), external_ref=ev.ref, valid_days=valid_days)


class SupabaseRetriever:
    """The authoritative retriever: one ref, every evidence row linked to a ref, and the question queries of
    sb_queries (Mission 004B: patterns, attempts, decisions, evals, promotion history)."""

    source = "sb"
    _QUERIES = {
        "ref": NamedQuery("ref", {"ref": r"amem:[a-z_]+/[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}"},
                          "does this amem: ref exist", ("ref",)),
        "evidence_for": NamedQuery("evidence_for",
                                   {"ref": r"amem:[a-z_]+/[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}", "limit": r"[1-9][0-9]?"},
                                   "rsi_evidence rows whose subject_refs contain this ref", ("ref",)),
    }

    def __init__(self, connect: Callable[[], psycopg.Connection]):
        self._connect = connect

    @property
    def connect(self) -> Callable[[], psycopg.Connection]:
        """The connection factory (Mission 005: evidence packs re-read subject refs through it, read-only)."""
        return self._connect

    def health(self) -> Health:
        try:
            with self._connect() as conn:
                conn.execute("select 1")
            return Health(True)
        except psycopg.Error as exc:
            return Health(False, type(exc).__name__)

    def named_queries(self) -> Mapping[str, NamedQuery]:
        from agent_memory.integrations import sb_queries   # lazy: sb_queries imports this module

        return {**self._QUERIES, **sb_queries.SPECS}

    def query(self, name: str, params: Mapping[str, str]) -> list[EvidenceRef]:
        from agent_memory.integrations import sb_queries

        with self._connect() as conn:
            if name in sb_queries.RUN:
                return sb_queries.RUN[name](conn, params)
            if name == "ref":
                if not exists(conn, params["ref"]):
                    return []
                return [EvidenceRef(params["ref"], "sb", "row", [params["ref"]])]
            rows = conn.execute(
                "select id, system, kind, external_ref, subject_refs, summary, metrics, observed_at from rsi_evidence "
                "where subject_refs @> array[%s]::text[] order by observed_at desc limit %s",
                (params["ref"], int(params.get("limit", "20")))).fetchall()
        out = []
        for id_, system, kind, ext, subjects, summary, metrics, observed in rows:
            ref = ext if ext and refs.is_ref(ext) else refs.amem("evidence", str(id_))
            out.append(EvidenceRef(ref, _SOURCE_OF.get(system, "sb"), kind,
                                   [s for s in subjects if refs.is_ref(s) and s.startswith("amem:")],
                                   dict(metrics or {}), summary, observed.isoformat() if observed else None))
        return out


_SOURCE_OF = {"supabase": "sb", "clickhouse": "ch", "langfuse": "lf", "pastdev": "pd", "ncm": "ncm"}


def flatten(results: Iterable[QueryResult]) -> list[EvidenceRef]:
    return [r for res in results if res.status == "ok" for r in res.refs]
