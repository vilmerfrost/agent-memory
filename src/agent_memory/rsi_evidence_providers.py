"""The investigator's real evidence providers, read-only and policy-gated, and `amem rsi evidence-probe`
(Mission 005.5, Phase C; docs/rsi/evidence-providers.md).

    build(environ, connect=, subject_rule=)   Supabase, ClickHouse, Langfuse and past.dev retrievers for one pack
    probe(environ, ...)                       the night's evidence queries once, summarized per provider
    amem rsi evidence-probe [--json] [--candidate KEY] [--since ISO] [--policy FILE] [--timeout-s S]

`rsi_evidence_pack.default_retrievers` (what `amem rsi night` uses at S4 when no stand-ins are given) is `build()`.
Every provider answers through the shared evidence interface (integrations/evidence.py: named queries only,
`run_query`, EvidenceRef). Nothing here writes: not the database, not the night checkpoint, not a provider.

Gates. A provider is asked only when it passes, in order (the first that fails names the reason; nothing reaches
the network before step 4):
    1 configured   its env config is present and valid: AMEM_DB_URL (the probe never falls back to the local
                   default database), AMEM_CLICKHOUSE_URL/_USER/_KEY, AMEM_LANGFUSE_HOST/_PUBLIC_KEY/_SECRET_KEY,
                   AMEM_PASTDEV_KEY.
    2 switched on  AMEM_RSI_EVIDENCE_<PROVIDER>=off switches investigator reads off; for past.dev also its own
                   kill switch AMEM_PASTDEV=off. A sink switch (AMEM_SYNC_<SINK>=off) stops writes only, not reads.
    3 approved     config/rsi-evidence-providers.json (owner-approved; the investigator never writes it): `read`
                   is true and the query is listed. A missing or broken file fails closed for every external
                   provider. past.dev ships with `read: false` (see the file for why).
    4 health       the provider's own health check (one request).
    per query      a parameter that leaves the Mac must not name a withheld term (egress.py: AMEM_EGRESS_DENY plus
                   every team or private repo and project name in Supabase) or look like an email; a repo-scoped
                   rule (`claude-md:<repo>:...`, `agents-md:<repo>:...`) leaves only when that repo is positively
                   own (egress.py F8: an own session has it, no team session does). For ClickHouse that is the
                   `rule` parameter; for past.dev, whose recall words come from the candidate's rule, the
                   candidate's rule (`subject_rule`).
A gate that stops a query raises evidence.Unavailable, so the step answers `unavailable` with the reason, never
`error`. A provider that fails (HTTP error, timeout) answers `error`; neither stops the night.

Read-only by construction. Each client is a subclass whose write calls raise ReadOnlyViolation before any
request: ClickHouse sends only /ping and the fixed named SELECTs (`clickhouse.QUERIES`), Langfuse only GETs,
past.dev only the free health GET and POST /recall (a read that costs 0.1 credit). Supabase connections run every
statement in a READ ONLY transaction (autocommit off, read_only on). Each provider has a Meter: calls made
(HTTP calls, or SQL statements for Supabase; retries inside one call are not counted) and writes refused.

Untrusted data. Provider answers become EvidenceRefs (refs parsed by the ref grammar, metrics numbers and labels
only); a model sees them only through rsi_evidence_pack.render_for_model (nonce envelope, sanitizer). The probe
prints counts, timestamps, sample sizes and at most three refs per provider, each through the same neutralizer;
it never prints row contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config as amem_config
from agent_memory.integrations import clickhouse, egress, langfuse, pastdev
from agent_memory.integrations.evidence import (EvidenceRef, Health, NamedQuery, Retriever, SupabaseRetriever,
                                                Unavailable)

POLICY_PATH = amem_config.REPO_ROOT / "config" / "rsi-evidence-providers.json"
POLICY_FORMAT = "rsi-evidence-providers/1"
PROBE_FORMAT = "rsi-evidence-probe/1"
SERVICES = {"sb": "supabase", "ch": "clickhouse", "lf": "langfuse", "pd": "pastdev"}
RETRIEVER_OF = {v: k for k, v in SERVICES.items()}
EXTERNAL = ("clickhouse", "langfuse", "pastdev")
SWITCH_ENV = "AMEM_RSI_EVIDENCE_{}"              # .format("CLICKHOUSE") ...: "off" switches investigator reads off
OFF = frozenset({"off", "0", "false", "no"})
S1_NOTE = ("S1 provider queries (ClickHouse, Langfuse) are what delta_since runs with providers; the night passes "
           "them only once rsi_night.PackEvidence.delta does (today its S1 reads Supabase only)")
PASTDEV_BASE_URL = pastdev.BASE_URL             # module level so a test can point it at a local mock
CONNECT_TIMEOUT_S = 10
_QUERY_NAME = re.compile(r"\*|[a-z][a-z0-9_]{0,63}")
_DIGITS = re.compile(r"[0-9]{1,6}")


class ReadOnlyViolation(RuntimeError):
    """A write was attempted through an evidence client. Raised before any request leaves."""


# ---------------------------------------------------------------- the policy file


@dataclass(frozen=True)
class ProviderPolicy:
    read: bool
    queries: tuple[str, ...] = ()
    data: str = ""
    approval: str = ""

    def allows(self, query: str) -> bool:
        return self.read and ("*" in self.queries or query in self.queries)


@dataclass(frozen=True)
class Policy:
    providers: Mapping[str, ProviderPolicy]
    path: str
    sha256: str | None = None
    problem: str = ""                     # why the file is not usable (then every external provider is closed)

    def of(self, service: str) -> ProviderPolicy | None:
        return self.providers.get(service)


_FAIL_CLOSED_SB = ProviderPolicy(True, ("*",), "authoritative store", "authoritative store (no usable policy)")


def load_policy(path: Path | str | None = None) -> Policy:
    """The owner-approved read policy. Any problem fails closed for the external providers (Supabase, the
    authoritative store the night reads anyway, stays readable)."""
    p = Path(path) if path is not None else POLICY_PATH

    def closed(why: str, sha: str | None = None) -> Policy:
        return Policy({"supabase": _FAIL_CLOSED_SB}, str(p), sha, why)

    try:
        raw = p.read_bytes()
    except OSError as exc:
        return closed(f"policy unreadable ({type(exc).__name__})")
    sha = hashlib.sha256(raw).hexdigest()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        return closed("policy is not JSON", sha)
    if not isinstance(doc, dict) or doc.get("format") != POLICY_FORMAT or not isinstance(doc.get("providers"), dict):
        return closed(f"policy format is not {POLICY_FORMAT}", sha)
    out: dict[str, ProviderPolicy] = {}
    for name, spec in doc["providers"].items():
        if name not in SERVICES.values():
            return closed(f"policy names an unknown provider {str(name)[:40]!r}", sha)
        if not isinstance(spec, dict) or not isinstance(spec.get("read"), bool):
            return closed(f"policy {name}: `read` must be true or false", sha)
        qs = spec.get("queries", [])
        if not isinstance(qs, list) or not all(isinstance(q, str) and _QUERY_NAME.fullmatch(q) for q in qs):
            return closed(f"policy {name}: `queries` must be a list of query names", sha)
        out[name] = ProviderPolicy(spec["read"], tuple(qs), str(spec.get("data") or "")[:300],
                                   str(spec.get("approval") or "")[:300])
    if "supabase" not in out:
        out["supabase"] = _FAIL_CLOSED_SB
    return Policy(out, str(p), sha)


# ---------------------------------------------------------------- meters and read-only clients


class Meter:
    """Requests one provider made (calls; SQL statements for Supabase) and writes it refused."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.refused = 0

    def hit(self) -> None:
        with self._lock:
            self.calls += 1

    def refuse(self, what: str) -> ReadOnlyViolation:
        with self._lock:
            self.refused += 1
        return ReadOnlyViolation(what)


_CH_READS = frozenset(q.sql for q in clickhouse.QUERIES.values())
SPECS: dict[str, Mapping[str, NamedQuery]] = {
    "clickhouse": {n: q.spec for n, q in clickhouse.QUERIES.items()},
    "langfuse": langfuse.LangfuseRetriever._QUERIES,
    "pastdev": pastdev.PastDevRetriever._QUERIES,
}


class ReadOnlyClickHouseClient(clickhouse.Client):
    """/ping and the fixed named SELECTs only; DDL, INSERT and any other statement raise before a request."""

    def __init__(self, cfg: clickhouse.Config, *, meter: Meter, **kw: Any):
        super().__init__(cfg, **kw)
        self.meter = meter

    def ping(self) -> None:
        self.meter.hit()
        super().ping()

    def select(self, statement: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        if statement not in _CH_READS:
            raise self.meter.refuse("clickhouse: only the named read queries")
        self.meter.hit()
        return super().select(statement, params)

    def command(self, statement: str) -> None:
        raise self.meter.refuse("clickhouse: no statements (read-only evidence client)")

    def insert(self, table: str, body: bytes, token: str) -> None:
        raise self.meter.refuse("clickhouse: no inserts (read-only evidence client)")


@dataclass
class ReadOnlyLangfuseClient(langfuse.LangfuseClient):
    """GET only: spans, scores, datasets and dataset items raise before a request."""

    meter: Meter = field(default_factory=Meter, repr=False)

    def _call(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, body: Any = None,
              retries: int | None = None, timeout: float | None = None) -> Any:
        if method != "GET" or body is not None:
            raise self.meter.refuse(f"langfuse: {method} refused (read-only evidence client)")
        self.meter.hit()
        return super()._call(method, path, params=params, body=body, retries=retries, timeout=timeout)

    def export_spans(self, body: Mapping[str, Any]) -> None:
        raise self.meter.refuse("langfuse: no span export (read-only evidence client)")

    def create_scores(self, items: Sequence[Mapping[str, Any]]) -> None:
        raise self.meter.refuse("langfuse: no scores (read-only evidence client)")

    def ensure_dataset(self, name: str) -> str:
        raise self.meter.refuse("langfuse: no dataset create (read-only evidence client)")

    def upsert_dataset_item(self, *a: Any, **k: Any) -> None:
        raise self.meter.refuse("langfuse: no dataset items (read-only evidence client)")


class ReadOnlyPastDevClient(pastdev.Client):
    """The free health GET and POST /recall only; ingest, retry, delete, estimate and usage raise first."""

    ALLOWED = frozenset({("GET", f"/ingest/{pastdev.NIL_INGESTION}"), ("POST", "/recall")})

    def __init__(self, cfg: pastdev.Config, *, meter: Meter, **kw: Any):
        super().__init__(cfg, **kw)
        self.meter = meter

    def _call(self, method: str, path: str, body: Any = None, *, ok: Callable[[int], bool] | None = None,
              retries: int | None = None) -> Any:
        if (method, path) not in self.ALLOWED:
            raise self.meter.refuse(f"past.dev: {method} {path.split('/')[1] if '/' in path else path} refused")
        self.meter.hit()
        return super()._call(method, path, body, ok=ok, retries=retries)

    def ingest_batch(self, items: list[dict[str, Any]], key: str) -> dict[str, Any]:
        raise self.meter.refuse("past.dev: no ingest (read-only evidence client)")

    def ingestion(self, ingestion_id: str) -> dict[str, Any]:
        raise self.meter.refuse("past.dev: no ingestion reads (not needed)")

    def retry_ingestion(self, ingestion_id: str) -> dict[str, Any]:
        raise self.meter.refuse("past.dev: no retries (read-only evidence client)")

    def delete(self, ids: list[str]) -> dict[str, Any]:
        raise self.meter.refuse("past.dev: no deletes (read-only evidence client)")

    def estimate(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        raise self.meter.refuse("past.dev: no estimate (it uploads content)")

    def usage(self) -> dict[str, Any]:
        raise self.meter.refuse("past.dev: no usage reads (not needed)")


def _metered_cursor(meter: Meter) -> type[psycopg.Cursor[Any]]:
    class MeteredCursor(psycopg.Cursor):  # type: ignore[type-arg]
        def execute(self, query: Any, params: Any = None, **kw: Any) -> Any:
            meter.hit()
            return super().execute(query, params, **kw)

    return MeteredCursor


def read_only_connect(connect: Callable[[], psycopg.Connection], meter: Meter | None = None
                      ) -> Callable[[], psycopg.Connection]:
    """A factory whose connections run every statement in a READ ONLY transaction (autocommit off, read_only on;
    no session-level SET, which a transaction pooler would leak to other clients). A connection that cannot be
    made read-only is closed and refused. With a meter: every statement is counted."""
    def ro() -> psycopg.Connection:
        conn = connect()
        try:
            if conn.autocommit:
                conn.autocommit = False
            conn.read_only = True
        except psycopg.Error:
            pass
        if getattr(conn, "autocommit", True) or getattr(conn, "read_only", None) is not True:
            try:
                conn.close()
            finally:
                raise psycopg.OperationalError("evidence: the connection could not be made read-only")
        if meter is not None and isinstance(conn, psycopg.Connection):
            conn.cursor_factory = _metered_cursor(meter)
        return conn
    return ro


# ---------------------------------------------------------------- what may leave as a query parameter


def _rule_repo(rule: str) -> str | None:
    """The repo a repo-scoped rule key names (`claude-md:<repo>:<rule id>`), else None. `global` is not a repo."""
    prefix, sep, rest = rule.partition(":")
    if not sep or prefix not in clickhouse.REPO_PREFIXES:
        return None
    repo = rest.partition(":")[0]
    return None if repo == "global" else repo


_OWN_REPO_SQL = """
select exists (select 1 from sessions s where %(r)s in (s.repo, s.project) and {cls} = 'own'),
       exists (select 1 from sessions s where %(r)s in (s.repo, s.project) and {cls} = 'team')"""


class EgressGate:
    """The egress rules for query parameters, read lazily from Supabase (read-only) and cached for one build."""

    def __init__(self, connect: Callable[[], psycopg.Connection] | None, environ: Mapping[str, str]):
        self._connect, self._environ = connect, environ
        self._lock = threading.Lock()
        self._deny: egress.Deny | None = None
        self._own: dict[str, bool] = {}

    def _withheld(self) -> egress.Deny:
        with self._lock:
            if self._deny is None:
                if self._connect is None:
                    raise Unavailable("withheld repo names unreadable (no supabase)")
                try:
                    with self._connect() as conn:
                        self._deny = egress.team_names(conn, self._environ)
                except psycopg.Error as exc:
                    raise Unavailable(f"withheld repo names unreadable ({type(exc).__name__})") from None
            return self._deny

    def check_params(self, params: Mapping[str, str]) -> None:
        env = egress.env_terms(self._environ)
        for v in params.values():
            if _DIGITS.fullmatch(v):
                continue
            if env.match(v) or egress.email_like(v) or self._withheld().match(v):
                raise Unavailable("a query parameter names a withheld term (egress deny list)")

    def repo_own(self, repo: str) -> bool:
        with self._lock:
            if repo in self._own:
                return self._own[repo]
        if self._connect is None:
            raise Unavailable("repo ownership unreadable (no supabase)")
        q = egress.bind_owners(_OWN_REPO_SQL.format(cls=egress.class_sql("s.meta")),
                               egress.owner_accounts(self._environ))
        try:
            with self._connect() as conn:
                own, team = conn.execute(q, {"r": repo}).fetchone()
        except psycopg.Error as exc:
            raise Unavailable(f"repo ownership unreadable ({type(exc).__name__})") from None
        with self._lock:
            self._own[repo] = bool(own) and not bool(team)
            return self._own[repo]

    def check_rule(self, rule: str | None) -> None:
        if not rule:
            return
        self.check_params({"rule": rule})
        repo = _rule_repo(rule)
        if repo is None:
            return
        if repo in egress.MASKS or re.fullmatch(r"p_[0-9a-f]{16}", repo) or not self.repo_own(repo):
            raise Unavailable("repo-scoped rule of a repo that is not positively own (team or private): not sent")


# ---------------------------------------------------------------- the gated retriever


class GatedRetriever:
    """evidence.Retriever around one external provider: the gates in the module docstring, then the inner one."""

    def __init__(self, service: str, inner: Retriever | None, policy: ProviderPolicy | None, *, block: str = "",
                 gate: EgressGate | None = None, subject_rule: str | None = None, meter: Meter | None = None):
        self.service, self.inner, self.policy = service, inner, policy
        self.block = block or ("" if inner is not None else "no retriever")
        self.gate, self.subject_rule = gate, subject_rule
        self.meter = meter or Meter()
        self.source = RETRIEVER_OF[service]

    def health(self) -> Health:
        if self.block or self.inner is None:
            return Health(False, self.block)
        return self.inner.health()

    def named_queries(self) -> Mapping[str, NamedQuery]:
        # the provider's own specs even when it is closed, so a closed provider answers `unavailable` (via health),
        # never `unknown-query`
        return self.inner.named_queries() if self.inner is not None else SPECS.get(self.service, {})

    def query(self, name: str, params: Mapping[str, str]) -> list[EvidenceRef]:
        if self.block or self.inner is None or self.policy is None:
            raise Unavailable(self.block or "not approved")
        if not self.policy.allows(name):
            raise Unavailable(f"query {name} is not approved for investigator reads")
        if self.gate is not None:
            self.gate.check_params(params)
            if "rule" in params:
                self.gate.check_rule(params["rule"])
            if self.service == "pastdev":           # recall words come from the candidate's rule
                if not self.subject_rule:
                    raise Unavailable("recall words of unknown origin: not sent")
                self.gate.check_rule(self.subject_rule)
        return self.inner.query(name, params)


# ---------------------------------------------------------------- build


@dataclass
class ProviderState:
    service: str
    configured: bool
    approved: bool
    reason: str = ""                      # why it is unavailable before any request ("" when it may be asked)
    meter: Meter = field(default_factory=Meter)


@dataclass
class Providers:
    retrievers: dict[str, Retriever | None]
    states: dict[str, ProviderState]
    policy: Policy
    environ: Mapping[str, str]


def _switched_off(env: Mapping[str, str], service: str) -> str:
    name = SWITCH_ENV.format(service.upper())
    if (env.get(name) or "").strip().lower() in OFF:
        return f"switched off ({name}=off)"
    if service == "pastdev" and (env.get("AMEM_PASTDEV") or "").strip().lower() == "off":
        return "switched off (AMEM_PASTDEV=off)"
    return ""


def _not_approved(pol: ProviderPolicy | None, policy: Policy) -> str:
    if pol is None:
        return f"not approved: {policy.problem or 'not in the evidence policy'}"
    if not pol.read:
        return f"not approved: {pol.approval or 'read is false in the evidence policy'}"[:300]
    return ""


def build(environ: Mapping[str, str] | None = None, *, connect: Callable[[], psycopg.Connection] | None = None,
          subject_rule: str | None = None, policy: Policy | None = None,
          require_db_url: bool = False) -> Providers:
    """The night's evidence retrievers, gated and read-only (module docstring). `environ` None: the process env as
    the CLI loads it. `connect`: the night's database (default store.connect). `subject_rule`: the candidate's
    rule (past.dev recall words come from it). `require_db_url`: no AMEM_DB_URL means Supabase is not configured
    (the probe), instead of the local default database (the night, as before)."""
    if environ is None:
        amem_config.load_env()
        env: Mapping[str, str] = os.environ
    else:
        env = environ
    pol = policy if policy is not None else load_policy()
    states: dict[str, ProviderState] = {}
    out: dict[str, Retriever | None] = {}

    # Supabase: the authoritative store; read-only, metered connections
    sb_state = ProviderState("supabase", True, True)
    conn_f = connect
    if conn_f is None:
        url = (env.get("AMEM_DB_URL") or "").strip()
        if require_db_url and not url:
            sb_state.configured, sb_state.reason = False, "not configured (AMEM_DB_URL unset)"
        elif environ is not None and url:
            conn_f = lambda: psycopg.connect(url, connect_timeout=CONNECT_TIMEOUT_S)  # noqa: E731
        else:
            from agent_memory import store
            conn_f = store.connect
    sb_pol = pol.of("supabase")
    if sb_state.configured and (block := _not_approved(sb_pol, pol)):
        sb_state.approved, sb_state.reason = False, block
    ro = read_only_connect(conn_f, sb_state.meter) if conn_f is not None and not sb_state.reason else None
    out["sb"] = SupabaseRetriever(ro) if ro is not None else None
    states["supabase"] = sb_state
    gate = EgressGate(ro, env)

    def external(service: str, problem: Callable[[], str], make: Callable[[Meter], Retriever]) -> None:
        p = pol.of(service)
        try:                                     # no configuration value may make build() raise
            unconfigured = problem()
        except Exception as exc:  # noqa: BLE001 - e.g. a malformed URL: this provider is unavailable, not the pack
            unconfigured = f"configuration unreadable ({type(exc).__name__})"
        st = ProviderState(service, configured=not unconfigured, approved=bool(p and p.read))
        st.reason = unconfigured or _switched_off(env, service) or _not_approved(p, pol)   # the gate order
        inner: Retriever | None = None
        if not st.reason:
            try:
                inner = make(st.meter)
            except Exception as exc:  # noqa: BLE001 - an adapter that cannot be built is unavailable
                st.reason = f"adapter: {type(exc).__name__}"
        out[RETRIEVER_OF[service]] = GatedRetriever(service, inner, p, block=st.reason, gate=gate,
                                                    subject_rule=subject_rule, meter=st.meter)
        states[service] = st

    def ch_problem() -> str:
        return clickhouse.config(env).problem

    def lf_problem() -> str:
        why = langfuse.LangfuseClient.from_env(env).problem
        return f"{why} (AMEM_LANGFUSE_HOST/_PUBLIC_KEY/_SECRET_KEY)" if why else ""

    def pd_problem() -> str:
        return "" if pastdev.config(env, base_url=PASTDEV_BASE_URL) is not None else "not configured (AMEM_PASTDEV_KEY)"

    reads_env = {k: v for k, v in env.items() if k != "AMEM_SYNC_PASTDEV"}   # a sink switch stops writes only
    external("clickhouse", ch_problem, lambda m: clickhouse.ClickHouseRetriever(
        ReadOnlyClickHouseClient(clickhouse.config(env), meter=m)))
    external("langfuse", lf_problem, lambda m: langfuse.LangfuseRetriever(
        ReadOnlyLangfuseClient.from_env(env, meter=m)))
    external("pastdev", pd_problem, lambda m: pastdev.PastDevRetriever(
        ro or _no_db, pastdev.config(env, base_url=PASTDEV_BASE_URL), environ=reads_env,
        client=ReadOnlyPastDevClient(pastdev.config(env, base_url=PASTDEV_BASE_URL), meter=m)))  # type: ignore[arg-type]
    return Providers(out, states, pol, env)


def _no_db() -> psycopg.Connection:
    raise Unavailable("no supabase: recalled refs cannot be re-read")


# ---------------------------------------------------------------- the probe


def _neutral() -> Any:
    from agent_memory.rsi_evidence_pack import _Neutral
    return _Neutral(160)


def _ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _data_ts(ev: EvidenceRef) -> datetime | None:
    """When the data behind one answer was observed. ClickHouse answers carry the query time as observed_at, so
    for them only a data column (week_start) counts."""
    if ev.source == "ch":
        return _ts(ev.metrics.get("week_start"))
    return _ts(ev.observed_at)


def _status(statuses: Sequence[str]) -> str:
    s = {("error" if x in ("bad-params", "unknown-query") else x) for x in statuses}
    if "error" in s:
        return "error"
    if "ok" in s:
        return "ok"
    return "unavailable"


@dataclass
class _Seen:
    queries: list[dict[str, Any]] = field(default_factory=list)
    refs: list[EvidenceRef] = field(default_factory=list)
    health: list[tuple[str, str]] = field(default_factory=list)      # (status, detail) of an idle health check


def _summary(state: ProviderState, seen: _Seen, nz: Any) -> dict[str, Any]:
    from agent_memory.rsi_evidence_pack import _n

    statuses = [q["status"] for q in seen.queries] + [h[0] for h in seen.health]
    if state.reason:
        status, reason = "unavailable", state.reason
    elif not statuses:
        status, reason = "unavailable", "not asked"
    else:
        status = _status(statuses)
        bad = [f"{q['step']}: {q['detail'] or q['status']}" for q in seen.queries if q["status"] != "ok"]
        bad += [f"health: {d or s}" for s, d in seen.health if s != "ok"]
        reason = "; ".join(bad)
    ts = [t for t in (_data_ts(ev) for ev in seen.refs) if t is not None]
    ns = [n for n in (_n(ev.metrics) for ev in seen.refs) if n is not None]
    return {"status": status, "reason": nz.text(reason) or "", "configured": state.configured,
            "approved": state.approved, "rows": len(seen.refs),
            "newest_ts": max(ts).astimezone(UTC).isoformat(timespec="seconds") if ts else None,
            "n": {"values": ns[:10], "sum": sum(ns), "rows_with_n": len(ns)},
            "requests": state.meter.calls, "writes_refused": state.meter.refused,
            "queries": [{**q, "detail": nz.text(q["detail"]) or ""} for q in seen.queries],
            "sample_refs": [nz.ref(ev.ref) for ev in seen.refs[:3]]}


def _jev(env: Mapping[str, str]) -> dict[str, Any]:
    mode = "fixture"
    try:
        from agent_memory import rsi_night
        mode = str(rsi_night.load_config().stages.get("triage", "fixture"))
    except Exception:  # noqa: BLE001 - the probe reports Jev, it does not need the night config to load
        pass
    key = bool((env.get("TYPESAFE_API_KEY") or "").strip())
    live = (env.get("AMEM_JEV_LIVE") or "").strip() == "1"
    configured = key and live
    return {"status": "live" if configured and mode == "live" else "fixture", "configured": configured,
            "triage_stage": mode if mode in ("fixture", "live") else "fixture", "called": False,
            "reason": "never called by the probe (paid); the night's triage stage decides", "requests": 0,
            "rows": 0, "newest_ts": None, "n": {"values": [], "sum": 0, "rows_with_n": 0}, "writes_refused": 0}


def probe(environ: Mapping[str, str] | None = None, *, policy: Policy | None = None,
          candidate_key: str | None = None, since: str | None = None, timeout_s: float | None = None,
          connect: Callable[[], psycopg.Connection] | None = None,
          now: datetime | None = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Run the night's evidence queries once (S1: the delta and its provider steps; S4: the pack's steps for one
    candidate) against whatever is configured, and summarize per provider. Writes nothing anywhere."""
    from agent_memory import rsi_evidence_pack as P

    if environ is None:
        amem_config.load_env()
        env: Mapping[str, str] = os.environ
    else:
        env = environ
    pol = policy if policy is not None else load_policy()
    lim = P.PackLimits() if timeout_s is None else P.PackLimits(timeout_s=timeout_s)
    dl = P.DeltaLimits() if timeout_s is None else P.DeltaLimits(timeout_s=timeout_s)
    nz = _neutral()
    at = now or datetime.now(UTC)
    seen = {s: _Seen() for s in SERVICES.values()}
    stage_of: dict[int, str] = {}

    prov = build(env, connect=connect, policy=pol, require_db_url=connect is None)
    rs = prov.retrievers
    # Supabase first: health, the candidate, the delta (S1, read-only)
    sb = rs.get("sb")
    cand: dict[str, Any] | None = None
    cand_from: str | None = None
    window_since: str | None = None
    window_until: str | None = None
    experiments: list[str] = []
    delta_counts: dict[str, int] | None = None
    s1 = P._Runner({"sb": sb}, dl.timeout_s, clock)
    if isinstance(sb, SupabaseRetriever):
        state, h = s1.call("sb", sb.health)
        hs = ("ok", "") if state == "ok" and h.ok else (
            ("error", f"timeout after {dl.timeout_s:g}s") if state == "timeout" else
            ("unavailable", h.detail if state == "ok" else type(h).__name__))
        seen["supabase"].health.append(hs)
        if hs[0] == "ok":
            def read_delta() -> Any:
                with sb.connect() as conn:
                    found = P.find_candidate(conn, candidate_key) if candidate_key else None
                    return found, P.delta_since(P._when(since), conn=conn, limits=dl, now=at)
            state, val = s1.call("sb", read_delta)
            if state == "ok":
                found, d = val
                window_since, window_until = d.since, d.until
                delta_counts = d.counts()
                experiments = [x["id"] for x in d.experiments]
                for kind in ("eval_failures", "routine_failures", "experiments"):
                    for row in getattr(d, kind):
                        t = row.get("created_at") or row.get("finished_at")
                        seen["supabase"].refs.append(EvidenceRef(row["ref"], "sb", kind, [], {}, None, t))
                if found:
                    cand, cand_from = found, "--candidate"
                elif not candidate_key:
                    top = sorted(((n, r) for r, n in d.deviation_rules.items()
                                  if r != "other" and not r.endswith(":unknown")), key=lambda x: (-x[0], x[1]))
                    if top:
                        cand, cand_from = {"rule_key": top[0][1], "dedupe_key": top[0][1]}, "delta-top-rule"
            else:
                seen["supabase"].queries.append({"stage": "S1", "step": "sb.delta", "status": "error" if
                                                 state == "timeout" else "unavailable", "rows": 0,
                                                 "detail": "timeout" if state == "timeout" else type(val).__name__})
    if cand is None and candidate_key and P._RULE.fullmatch(candidate_key):
        cand, cand_from = {"rule_key": candidate_key, "dedupe_key": candidate_key}, "--candidate (rule key)"
    if window_until is None:
        until = at - timedelta(seconds=dl.settle_s)
        window_until = P._iso(until)
        window_since = P._iso(P._when(since) or until - timedelta(days=dl.first_window_days))

    c = P._candidate(cand) if cand is not None else None
    pd = rs.get("pd")
    if isinstance(pd, GatedRetriever):              # recall words come from this candidate's rule (build_pack: the same)
        pd.subject_rule = c.rule if c is not None else None

    # S1: the delta's provider steps, as the night's delta runs them
    steps = P._delta_steps(P._when(window_since), P._when(window_until), experiments)
    runner = P._Runner({k: rs.get(k) for k in ("ch", "lf", "pd")}, dl.timeout_s, clock)
    done = runner.phase(steps)
    for st, _ in done:
        stage_of[id(st)] = "S1"
    # S4: the pack's steps for the candidate
    if c is not None:
        r4 = P._Runner({k: rs.get(k) for k in P.RETRIEVERS}, lim.timeout_s, clock)
        d4 = P.run_steps(c, r4, lim)
        for st, _ in d4:
            stage_of[id(st)] = "S4"
        done += d4
        runners = [runner, r4]
    else:
        runners = [runner]
    for st, res in done:
        svc = P.SERVICE[st.retr]
        seen[svc].queries.append({"stage": stage_of.get(id(st), "S4"), "step": st.name, "status": res.status,
                                  "rows": len(res.refs), "detail": res.detail})
        seen[svc].refs += res.refs
    # providers no step asked: their health once (one request), unless a gate already closed them
    asked = {P.SERVICE[st.retr] for st, _ in done}
    for retr, svc in SERVICES.items():
        if svc in asked or prov.states[svc].reason or (svc == "supabase" and seen[svc].health):
            continue
        r = rs.get(retr)
        if r is None:
            continue
        state, val = runners[-1].call(retr, r.health)
        seen[svc].health.append(("ok", "") if state == "ok" and val.ok else
                                (("error", f"timeout after {lim.timeout_s:g}s") if state == "timeout" else
                                 ("unavailable", val.detail if state == "ok" else type(val).__name__)))

    providers = {svc: _summary(prov.states[svc], seen[svc], nz) for svc in SERVICES.values()}
    if delta_counts is not None:
        providers["supabase"]["delta"] = delta_counts
    providers["jev"] = _jev(env)
    return {"format": PROBE_FORMAT, "at": at.astimezone(UTC).isoformat(timespec="seconds"), "read_only": True,
            "policy": {"path": pol.path, "sha256": pol.sha256, "problem": pol.problem},
            "candidate": {"key": nz.label(c.key) if c else None, "rule": nz.label(c.rule) if c and c.rule else None,
                          "from": cand_from},
            "window": {"since": window_since, "until": window_until},
            "timeout_s": lim.timeout_s,
            "notes": [S1_NOTE],
            "health": {k: v["status"] for k, v in providers.items()},
            "providers": providers}


def render_text(rep: Mapping[str, Any]) -> str:
    pol = rep["policy"]
    out = [f"evidence probe {rep['at']} (read-only; policy {Path(pol['path']).name}"
           + (f" sha256 {pol['sha256'][:12]}" if pol.get("sha256") else "")
           + (f"; POLICY PROBLEM: {pol['problem']}" if pol.get("problem") else "") + ")"]
    cand = rep["candidate"]
    out.append(f"  candidate: {cand['key'] or '-'}" + (f" ({cand['from']})" if cand["from"] else "")
               + f"   window ({rep['window']['since']}, {rep['window']['until']}]")
    out += [f"  note: {n}" for n in rep.get("notes") or []]
    for name, p in rep["providers"].items():
        head = f"  {name:<11} {p['status']:<11}"
        if name == "jev":
            out.append(f"{head} configured={'yes' if p['configured'] else 'no'} triage={p['triage_stage']} "
                       f"called=no ({p['reason']})")
            continue
        line = (f"{head} rows={p['rows']} newest={p['newest_ts'] or '-'} n_sum={p['n']['sum']} "
                f"requests={p['requests']}" + (f" writes_refused={p['writes_refused']}" if p["writes_refused"] else ""))
        if p.get("delta"):
            line += " delta=" + ",".join(f"{k}:{v}" for k, v in p["delta"].items())
        out.append(line)
        if p["reason"]:
            out.append(f"  {'':<11} reason: {p['reason']}")
        if p["sample_refs"]:
            out.append(f"  {'':<11} refs: {' '.join(str(r) for r in p['sample_refs'])}")
    return "\n".join(out)


def cmd_probe(args: argparse.Namespace) -> int:
    from agent_memory import rsi_evidence_pack as P
    try:
        P._when(args.since)
    except ValueError as exc:
        print(f"amem: rsi evidence-probe: {exc}", file=sys.stderr)
        return 2
    pol = load_policy(args.policy) if args.policy else None
    rep = probe(policy=pol, candidate_key=args.candidate, since=args.since, timeout_s=args.timeout_s)
    print(json.dumps(rep, sort_keys=True, default=str) if args.json else render_text(rep))
    return 0


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """`amem rsi evidence-probe` (a subparser of `amem rsi`)."""
    p = sub.add_parser("evidence-probe", help="run the night's evidence queries once and show what each provider "
                                              "returns (read-only; counts, timestamps, n, requests; never calls Jev)")
    p.add_argument("--candidate", metavar="KEY", help="an rsi_candidates key or a rule key (default: the delta's "
                                                      "most frequent deviation rule)")
    p.add_argument("--since", metavar="ISO", default=None, help="the delta checkpoint (default: the first-night window)")
    p.add_argument("--policy", type=Path, default=None, help="another evidence policy file (default: "
                                                             "config/rsi-evidence-providers.json)")
    p.add_argument("--timeout-s", type=float, default=None, help="per-provider time budget (default: the night's)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_probe)
