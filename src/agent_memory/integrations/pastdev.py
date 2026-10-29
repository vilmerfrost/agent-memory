"""past.dev decision memory: the sink, the retriever, deletion and the C3 experiment (Mission 004A).

past.dev is an external SaaS. It receives only the sanitized decision records built here (sanitize.py):
one per proposal, decision or rejection, experiment, edit and lesson decision, each at most CAP_BYTES. No
transcripts, file contents, prompts, paths, emails, hosts, secrets, ids other than the record's own
`amem:` ref, nothing from a team repo, nothing tied to a repo or session whose ownership is not positively the
owner's (Mission 004C, F8: `private`, egress.py), and nothing that names a term on the egress deny list (egress.py:
AMEM_EGRESS_DENY plus every team and private repo and project, read at the start of each run). One past.dev project is
dedicated to RSI; its project key (`past_sk_...`) is read from AMEM_PASTDEV_KEY only (this module never
reads an env file). Without a key, everything here is `unavailable`. AMEM_PASTDEV=off stops the sink
(sync.switched_off) and the retriever.

API, checked on 2026-10-16 against the official docs:
  https://past.dev/docs/memory-api                  base URL, `Authorization: Bearer past_sk_...`, ingest + poll + recall
  https://past.dev/openapi.json                     IngestBatchRequest/Result, IngestionStatus, RecallRequest/Response,
                                                    DeleteDataPointsRequest, DeletionReceipt, the status codes below
  https://past.dev/docs/memory-api/how-it-works     202 queued / 200 unchanged, idempotency keys, rate limits
  https://past.dev/docs/memory-api/concepts         limits (1-1000 items, <=200-char keys and identities, <=1000 deletes)
Verified there:
- POST /ingest/batch {items: [{content, id, timestamp, label, metadata, audience, identity}], idempotencyKey}
  -> 202 queued or 200 (unchanged: true); items[] = {ordinal, sourceId, status, unchanged}. The key is per
  batch: an item has no idempotencyKey. Same key + different body = 409 idempotency-conflict; keys never expire.
- GET /ingest/{ingestionId} -> {status: processing|completed|failed, settled, blocked, readiness, failure}.
  Polls are free and outside the per-minute limit. 404 for an unknown id.
- POST /ingest/{ingestionId}/retry -> 202 queued: re-runs a failed ingestion, free (Mission 004B).
- POST /recall {query, identity (required), limit 1-200, occurredFrom, occurredTo} -> results[] with
  rank and sources[] = {sourceId, occurredAt, label, metadata, excerpts}.
- POST /data-points/delete {ids: [...]} (1-1000) -> 202 {operationId, ids}; 404 code data-point-not-found
  when any id is not a live data point, and then nothing is deleted.
- 500 requests/min per key (Pay as you go); that 429 has no body; wait 1 s and double. A 429 with a JSON
  body code project-cap-reached or spend-cap-reached is a credit cap: never retried (Mission 004C).

Ingestion state (Mission 004C, G-PD0, against the spec of 2026-10-17). A re-send of content past.dev already
holds is answered `200 unchanged`, even when its first ingestion failed, so a record is never re-sent to
recover; it is followed by its ingestion id, kept in its rsi_evidence link (metrics.ingestion_id):
  completed   sent. Only a completed ingestion (or an all-unchanged receipt) counts as sent.
  queued      still running when the poll budget ran out, or failed and retried: the next run checks it by
              its id (a `ingestions` batch: one free GET, no content leaves) instead of sending it again.
  failed      a failed ingestion gets the free POST .../retry, at most once per run and MAX_FREE_RETRIES in
              all; then it is dead-lettered (status failed).
  rejected    content-rejected: dead-lettered at once (past.dev batches are atomic, so every item of that
              ingestion), not split and re-sent.
A dead-lettered record is sent again only when its content changes.
Not verified (assumed): that `sourceId` echoes our `id` byte for byte (the docs say it is trimmed; a
mismatch fails the batch); that GET /ingest/<nil uuid> answers 404 with a valid key and 401/403 without
(the health check); that a recall `source.label` is the label we sent.

Mapping (blueprint): id = the record's amem: ref on the wire (`wire`: `amem:<kind>:<id>`, below), timestamp = the
record time, label = the kind, identity = "rsi", no metadata (past.dev feeds metadata to memory extraction).

Source ids on the wire (2026-10-24). past.dev now refuses a source id that holds '/' (HTTP 400 id-invalid: "Use
another separator, such as ':' or '-'"), so a ref `amem:<kind>/<id>` leaves as `amem:<kind>:<id>` (`wire`) and comes
back through `unwire` (ref ids never hold ':', sanitize.SAFE_REF_ID). Only the wire uses that form: the ingest item
(`_item`, so `amem sync plan --contents` shows the exact payload), recall answers (map_recall unwires) and deletes.
Links (rsi_evidence `past:<ref>`), dead letters and reviewed sets keep the ref. A link stores the id its data point
has at past.dev (metrics.source_id); a link written before the change has none, and its data point (if any) holds the
ref itself, so a delete sends that id, a --replay of the unchanged record sends nothing (a new id would duplicate it),
and a changed record goes out under the new id with the old one kept as metrics.legacy_id, deleted with it. The batch idempotencyKey is sha256 over the
canonical items only (so the 409 rule can never fire). It is content-based (Mission 004C, F5): a --replay of
unchanged records sends the same key, so past.dev answers the stored receipt and ingests nothing new, and a
re-send after a lost response is the same request. Each record's own sha256(content) is kept in its
rsi_evidence link (metrics.sha256).

Watermarks: proposals, experiments and edits change status after they are first written and have no
updated_at column, so change detection is by content: a record is sent when its link is missing or holds
another sha256. The watermark (`<record time>|<ref>` of the last record pushed) shows how far a table got.
A link whose record is gone or now refused (a team or private item, a secret) is retracted first: deleted at past.dev
and marked deleted. --replay re-sends every completed record that is not retired (same keys: see above).

Deletion is final (Mission 004B). The docs: "A deleted id cannot be sent again; use a new id." The id is
the record's amem: ref, so a record whose link is marked deleted is retired: never sent again, listed in
`retired`. To bring one back would need a new id scheme or a new project; nothing here does that.

Reviewed sets (Mission 004C, the first historical backfill). `amem sync plan --sink pastdev --contents --json`
lists every item the next run would send (the exact payload, `payload_sha256`, ownership class, local credits and
bytes) and every refused record with its reasons (never its content), reading in a read-only transaction. A
reviewer cuts that JSON down to the chosen records. `amem sync run --sink pastdev --only FILE` then narrows every
batch (sends, retractions, checks) to those refs and yields nothing at all when any reviewed ref has no current
payload or another sha256 (the CLI checks first, read-only, and exits 1 before any provider call or write).
`amem sync delete --sink pastdev --only FILE` deletes that set's data points again (delete_all with `refs`).

Cost (Mission 004B, from the pricing page and the spec): 1 credit per 350 UTF-8 bytes of content, rounded
up per item, at least 1 (local_credits); recall 0.1 credit. Credits are prepaid: at zero, sends answer 402
out-of-credits and nothing is billed. A project credit cap (console) answers 429 project-cap-reached (a spend
cap: spend-cap-reached), which is not retried. The sync loop stops a run as `capped` on either
(sync.provider_cap). POST /ingest/estimate prices a send
without writing or spending, but it still uploads the content, so it runs only where sending is approved;
GET /usage reads the month's balance and the project's spend and cap.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory.integrations import egress, http, refs, sync
from agent_memory.integrations.evidence import EvidenceRef, Health, NamedQuery, run_query, usable
from agent_memory.integrations.sanitize import KIND_ENTITY, KINDS, SAFE_REF_ID, sanitize_record, scrub_text, secret_like

BASE_URL = "https://api.past.dev/api/v1"
KEY_ENV = "AMEM_PASTDEV_KEY"
KEY_RE = re.compile(r"past_sk_[A-Za-z0-9_-]{8,200}")      # a project key; a management key (past_mk_) is refused
IDENTITY = "rsi"
BATCH_MAX = 100
DELETE_MAX = 1000
RECALL_MAX = 20
POLL_S = 10.0
NIL_INGESTION = "00000000-0000-0000-0000-000000000000"
TABLES = ("rsi_proposals", "rsi_decisions", "rsi_experiments", "edits", "lesson_candidates")
RETRACT = "retractions"
CHECK = "ingestions"           # batches that check a still-running ingestion by its id (no rows, no content)
FAILURE_CODES = ("model-unavailable", "model-response-unreadable", "index-unavailable", "content-rejected", "internal")
COMPLETED, QUEUED, FAILED, REJECTED = "completed", "queued", "failed", "rejected"     # link statuses
SENT = frozenset({COMPLETED, "unchanged"})      # "unchanged": a link written before Mission 004C
DEAD = frozenset({FAILED, REJECTED})
MAX_FREE_RETRIES = 3
CAP_CODES = ("project-cap-reached", "spend-cap-reached")
NOT_LIVE = "data-point-not-found"
AMEM = "amem:"


def wire(ref: str) -> str:
    """The source id past.dev gets for an amem ref: `amem:<kind>/<id>` -> `amem:<kind>:<id>` (past.dev refuses '/'
    since 2026-10-24). Anything else is returned as is."""
    return AMEM + ref[len(AMEM):].replace("/", ":", 1) if ref.startswith(AMEM) else ref


def unwire(sid: str) -> str:
    """The amem ref for a past.dev source id (`amem:<kind>:<id>` -> `amem:<kind>/<id>`). An id that already holds
    '/' (sent before 2026-10-24) or is not ours comes back unchanged."""
    if not sid.startswith(AMEM) or "/" in sid:
        return sid
    kind, sep, rest = sid[len(AMEM):].partition(":")
    return f"{AMEM}{kind}/{rest}" if sep and kind and rest else sid


# ---------------------------------------------------------------- config and HTTP


@dataclass(frozen=True)
class Config:
    key: str = field(repr=False)
    base_url: str = BASE_URL


def config(environ: Mapping[str, str] | None = None, *, base_url: str = BASE_URL) -> Config | None:
    """The project key from AMEM_PASTDEV_KEY, or None (unconfigured). Never reads an env file."""
    env = os.environ if environ is None else environ
    key = env.get(KEY_ENV, "").strip()
    return Config(key, base_url.rstrip("/")) if KEY_RE.fullmatch(key) else None


def switched_off(environ: Mapping[str, str] | None = None) -> bool:
    return sync.switched_off("pastdev", environ)


class Client:
    """The five calls this adapter makes. Errors carry the status and a cut body, never the key."""

    def __init__(self, cfg: Config, *, sleep: Callable[[float], None] = time.sleep, timeout: float = 10.0,
                 retries: int = 3):
        self.cfg, self.sleep, self.timeout, self.retries = cfg, sleep, timeout, retries
        self._reads = threading.local()

    @contextmanager
    def read_budget(self, seconds: float):
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('finite nonnegative read budget required')
        old = getattr(self._reads, 'deadline', None)
        self._reads.deadline = min(old, time.monotonic()+seconds) if old is not None else time.monotonic()+seconds
        try:
            yield
        finally:
            self._reads.deadline = old

    def _call(self, method: str, path: str, body: Any = None, *, ok: Callable[[int], bool] | None = None,
              retries: int | None = None) -> http.Response:
        deadline = getattr(self._reads, 'deadline', None) if method == 'GET' else None
        left = deadline-time.monotonic() if deadline is not None else None
        if left is not None and left <= 0:
            raise TimeoutError('past.dev status read budget exhausted')
        return http.request(method, self.cfg.base_url + path, body=body, timeout=min(self.timeout,left) if left is not None else self.timeout,
                            headers={"Authorization": f"Bearer {self.cfg.key}", "Accept": "application/json"},
                            retries=0 if left is not None else self.retries if retries is None else retries,
                            total_timeout=left, backoff=1.0, sleep=self.sleep,
                            ok=ok or (lambda s: 200 <= s < 300), final=cap_reached)

    def health(self) -> Health:
        try:
            self._call("GET", f"/ingest/{NIL_INGESTION}", ok=lambda s: s in (200, 404), retries=0)
        except http.HttpError as exc:
            return Health(False, f"HTTP {exc.status}" if exc.status else "unreachable")
        return Health(True)

    def ingest_batch(self, items: list[dict[str, Any]], key: str) -> dict[str, Any]:
        return _obj(self._call("POST", "/ingest/batch", {"items": items, "idempotencyKey": key}).json())

    def ingestion(self, ingestion_id: str) -> dict[str, Any]:
        return _obj(self._call("GET", f"/ingest/{uuid.UUID(ingestion_id)}").json())

    def retry_ingestion(self, ingestion_id: str) -> dict[str, Any]:
        """POST /ingest/{id}/retry: free; re-runs a failed ingestion and answers 202 queued (spec, 2026-10-17)."""
        return _obj(self._call("POST", f"/ingest/{uuid.UUID(ingestion_id)}/retry", {}).json())

    def recall(self, body: dict[str, Any]) -> dict[str, Any]:
        return _obj(self._call("POST", "/recall", body).json())

    def delete(self, ids: list[str]) -> dict[str, Any]:
        """`ids` are past.dev source ids as stored (live_ids: the wired id, or the ref for a link written before
        2026-10-24), not refs: a ref alone does not say which id its data point has."""
        return _obj(self._call("POST", "/data-points/delete", {"ids": ids}).json())

    def estimate(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /ingest/estimate: credits, fits, balance. Writes and spends nothing, but the content leaves."""
        return _obj(self._call("POST", "/ingest/estimate", {"items": items, "points": "none"}).json())

    def usage(self) -> dict[str, Any]:
        """GET /usage: {month, balance: {left, spent, ...}, project: {credits, creditCap, creditCapLeft, ...}}."""
        return _obj(self._call("GET", "/usage").json())


CREDIT_BYTES = 350


def local_credits(items: Iterable[Mapping[str, Any]]) -> int:
    """The pricing page's rule, computed here without sending anything: ceil(bytes / 350), at least 1 per item."""
    return sum(max(1, -(-len(str(it.get("content") or "").encode("utf-8")) // CREDIT_BYTES)) for it in items)


def _obj(v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise http.HttpError(None, "past.dev: expected a JSON object")
    return v


def cap_reached(resp: http.Response) -> bool:
    """A 429 whose JSON body names a reached credit cap. The per-minute rate limit's 429 has no body, so it is
    still retried; a cap is final (http.request raises at once, sync stops the run as `capped`)."""
    if resp.status != 429:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("code") in CAP_CODES


def _ingestion_id(v: Any) -> str | None:
    try:
        return str(uuid.UUID(str(v))) if v else None
    except ValueError:
        return None


def _client(cfg: Config | None, client: Client | None, sleep: Callable[[float], None]) -> Client | None:
    if client is not None:
        return client
    return Client(cfg, sleep=sleep) if cfg is not None else None


# ---------------------------------------------------------------- decision records from Supabase


@dataclass(frozen=True)
class Record:
    table: str                       # the cursor key (source table)
    ref: str                         # amem:<entity>/<id>
    at: datetime                     # the record time (past.dev timestamp)
    fields: dict[str, Any]           # what sanitize_record reads: kind, ref, rule, ..., team


def _team_sql(arr: str) -> str:
    """True when any amem:deviation/ or amem:session/ ref in `arr` (text[]) is from a team repo."""
    return (f"(exists (select 1 from deviations d join sessions s on s.session_id = d.session_id "
            f"where ('amem:deviation/' || d.id::text) = any({arr}) "
            f"and (d.meta ->> 'team' = 'true' or s.meta ->> 'team' = 'true')) "
            f"or exists (select 1 from sessions s where ('amem:session/' || s.session_id) = any({arr}) "
            f"and s.meta ->> 'team' = 'true'))")


def _private_sql(arr: str) -> str:
    """True when any amem:deviation/ or amem:session/ ref in `arr` (text[]) is not positively own (Mission 004C,
    F8; egress.py): its session is team or private, or the ref resolves to no session at all."""
    own = egress.class_sql("s.meta")
    return (f"exists (select 1 from unnest({arr}) as r(ref) where "
            f"(starts_with(r.ref, 'amem:deviation/') and not exists (select 1 from deviations d join sessions s "
            f"on s.session_id = d.session_id where ('amem:deviation/' || d.id::text) = r.ref and {own} = 'own')) "
            f"or (starts_with(r.ref, 'amem:session/') and not exists (select 1 from sessions s "
            f"where ('amem:session/' || s.session_id) = r.ref and {own} = 'own')))")


_DEVS = "array(select 'amem:deviation/' || v::text from unnest({}.deviation_ids) v)"
_PROPOSAL_REFS = "p.evidence_refs || coalesce(c.evidence_refs, '{}'::text[]) || coalesce(e.devs, '{}'::text[])"

_PROPOSALS_SQL = f"""
select p.id::text as id, p.title, p.summary, p.rationale, p.status, p.result, p.created_at, x.night,
       c.source as c_source, c.rule_key, c.kind as c_kind, e.instruction_source, e.rule_id, e.meta ->> 'kind' as e_kind,
       (select d.action from rsi_decisions d where d.proposal_id = p.id order by d.decided_at desc, d.id desc limit 1)
           as decision,
       (coalesce(p.meta ->> 'team' = 'true', false) or coalesce(x.meta ->> 'team' = 'true', false)
        or coalesce(c.metrics ->> 'team' = 'true', false) or coalesce(e.meta ->> 'team' = 'true', false)
        or {_team_sql(_PROPOSAL_REFS)})
           as team,
       {_private_sql(_PROPOSAL_REFS)} as private
from rsi_proposals p
left join rsi_experiments x on x.id = p.experiment_id
left join rsi_candidates c on c.id = p.candidate_id
left join lateral (select ed.instruction_source, ed.rule_id, ed.meta, {_DEVS.format("ed")} as devs from edits ed
                   where ed.meta ->> 'proposal_id' = p.id::text order by ed.created_at desc limit 1) e on true
order by p.created_at, p.id
"""
_DECISIONS_SQL = """
select d.id::text as id, d.proposal_id::text as proposal_id, d.action, d.reason, d.decided_at, r.result as worker_result
from rsi_decisions d left join rsi_decision_results r on r.decision_id = d.id
order by d.decided_at, d.id
"""
_EXPERIMENTS_SQL = ("select id, night, kind, hypothesis, status, verdict, meta, created_at from rsi_experiments "
                    "order by created_at, id")
_EDITS_SQL = f"""
select e.id::text as id, e.status, e.instruction_source, e.rule_id, e.rule_title, e.why, e.summary, e.verdict, e.meta,
       e.created_at, (coalesce(e.meta ->> 'team' = 'true', false) or {_team_sql(_DEVS.format("e"))}) as team,
       {_private_sql(_DEVS.format("e"))} as private
from edits e order by e.created_at, e.id
"""
# Mission 004C (F2): ingestion never sets runs.meta.team, so a lesson is also a team item when its run's repo
# (runs.meta.repo) or the lesson's scope_repo is a team repo: a repo or project of any session flagged team.
# F8: and a private item when one of those names is not a repo or project of an own session (an unknown repo).
_LESSONS_SQL = f"""
select lc.run_id::text as id, lc.decision, lc.reason, lc.considered_at, l.slug, l.title,
       (coalesce(r.meta ->> 'team' = 'true', false)
        or exists (select 1 from sessions s where s.meta ->> 'team' = 'true'
                   and (s.repo in (r.meta ->> 'repo', l.scope_repo) or s.project in (r.meta ->> 'repo', l.scope_repo))))
           as team,
       exists (select 1 from (values (r.meta ->> 'repo'), (l.scope_repo)) as n(v) where btrim(n.v) <> ''
               and not exists (select 1 from sessions s where (s.repo = n.v or s.project = n.v)
                               and {egress.class_sql('s.meta')} = 'own'))
           as private
from lesson_candidates lc join runs r on r.id = lc.run_id left join lessons l on l.id = lc.lesson_id
where lc.decision in ('accept', 'reject')
order by lc.considered_at, lc.run_id
"""


def _night(ts: datetime) -> str:
    from agent_memory import rsi_approval   # lazy: rsi_approval imports a lot

    return rsi_approval.night_key(ts)


def _rule(source: Any, rule_id: Any) -> str | None:
    return f"{source}/{rule_id}" if source and rule_id else None


def _result(status: Any, result: Any) -> str | None:
    return f"{status}: {result}" if status and result else (status or result or None)


def _hypothesis(h: Any) -> str:
    if isinstance(h, str):
        return h
    if not isinstance(h, Mapping):
        return ""
    for k in ("text", "statement", "claim", "summary", "why"):
        if isinstance(h.get(k), str) and h[k].strip():
            return h[k]
    return "; ".join(f"{k}: {v}" for k, v in sorted(h.items())
                     if k not in ("team", "rule") and isinstance(v, (str, int, float)) and not isinstance(v, bool))


def _team(*metas: Any) -> bool:
    return any(isinstance(m, Mapping) and m.get("team") is True for m in metas)


def build_records(conn: psycopg.Connection, owners: Sequence[str] | None = None, *,
                  only: Sequence[str] | None = None) -> list[Record]:
    """Every decision record, in table order (TABLES) and time order. Reads only; refuses nothing (sanitize does).
    `owners`: the owner accounts for the ownership rule (default: egress.owner_accounts()).
    `only`: at most20 authoritative refs for evidence hydration; source queries use indexed ID predicates.
    None retains the ordinary full exporter. Selected decisions read only their bounded parent proposals."""
    accounts = egress.owner_accounts() if owners is None else owners
    selected = None if only is None else set(only)
    if selected is not None and len(selected) > 20:
        raise ValueError('selected memory lookup is limited to20 refs')
    ids: dict[str, list[str]] = {}
    if selected is not None:
        for ref in sorted(selected):
            if _ours(ref):
                r = refs.parse(ref)
                ids.setdefault(r.kind, []).append(r.id)
        # A decision inherits ownership and hypothesis from its own proposal, not every proposal.
        if ids.get('decision'):
            parents = conn.execute('select proposal_id::text from rsi_decisions where id=any(%s::uuid[])',
                                   (ids['decision'],)).fetchall()
            ids['proposal'] = sorted(set(ids.get('proposal', [])) | {p[0] for p in parents})

    def q(text: str) -> str:
        return egress.bind_owners(text, accounts)

    def rows(cur, text: str, entity: str, key: str, *, has_where: bool = False):
        if selected is None:
            return cur.execute(q(text)).fetchall()
        wanted = ids.get(entity, [])
        if not wanted:
            return []
        before, order = text.rsplit('order by', 1)
        typ = 'text' if entity == 'experiment' else 'uuid'
        statement = before + (' and ' if has_where else ' where ') + f'{key}=any(%s::{typ}[]) order by' + order
        return cur.execute(q(statement), (wanted,)).fetchall()

    out: list[Record] = []
    proposals: dict[str, dict[str, Any]] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        for p in rows(cur, _PROPOSALS_SQL, 'proposal', 'p.id'):
            rat = p["rationale"] if isinstance(p["rationale"], Mapping) else {}
            f = {"kind": "proposal", "ref": refs.amem("proposal", p["id"]),
                 "rule": _rule(p["instruction_source"], p["rule_id"]) or _rule(p["c_source"], p["rule_key"]),
                 "hypothesis": p["summary"] or p["title"], "change_kind": rat.get("kind") or p["e_kind"] or p["c_kind"],
                 "result": _result(p["status"], p["result"]), "decision": p["decision"], "reason": rat.get("why"),
                 "night": p["night"] or _night(p["created_at"]), "team": bool(p["team"]), "private": bool(p["private"])}
            proposals[p["id"]] = f
            out.append(Record("rsi_proposals", f["ref"], p["created_at"], f))
        for d in rows(cur, _DECISIONS_SQL, 'decision', 'd.id'):
            base = proposals.get(d["proposal_id"]) or {"team": True}       # no proposal row: never send
            f = {"kind": "rejection" if d["action"] == "reject" else "decision", "ref": refs.amem("decision", d["id"]),
                 "rule": base.get("rule"), "hypothesis": base.get("hypothesis"), "change_kind": base.get("change_kind"),
                 "result": d["worker_result"], "decision": d["action"], "reason": d["reason"],
                 "night": _night(d["decided_at"]), "team": base.get("team") is True,
                 "private": base.get("private") is True}
            out.append(Record("rsi_decisions", f["ref"], d["decided_at"], f))
        for x in rows(cur, _EXPERIMENTS_SQL, 'experiment', 'id'):
            hyp = x["hypothesis"] if isinstance(x["hypothesis"], Mapping) else {}
            meta = x["meta"] if isinstance(x["meta"], Mapping) else {}
            f = {"kind": "experiment", "ref": refs.amem("experiment", x["id"]),
                 "rule": hyp.get("rule") or meta.get("rule"), "hypothesis": _hypothesis(x["hypothesis"]),
                 "change_kind": x["kind"], "result": _result(x["status"], x["verdict"]),
                 "night": x["night"] or _night(x["created_at"]), "team": _team(meta, hyp)}
            out.append(Record("rsi_experiments", f["ref"], x["created_at"], f))
        for e in rows(cur, _EDITS_SQL, 'edit', 'e.id'):
            meta = e["meta"] if isinstance(e["meta"], Mapping) else {}
            f = {"kind": "edit", "ref": refs.amem("edit", e["id"]), "rule": _rule(e["instruction_source"], e["rule_id"]),
                 "hypothesis": e["summary"] or e["rule_title"], "change_kind": meta.get("kind") or e["verdict"],
                 "result": e["status"], "reason": e["why"], "night": _night(e["created_at"]), "team": bool(e["team"]),
                 "private": bool(e["private"])}
            out.append(Record("edits", f["ref"], e["created_at"], f))
        for lc in rows(cur, _LESSONS_SQL, 'run', 'lc.run_id', has_where=True):
            f = {"kind": "lesson", "ref": refs.amem("run", lc["id"]), "rule": _rule("lessons", lc["slug"]),
                 "hypothesis": lc["title"], "decision": lc["decision"], "reason": lc["reason"],
                 "night": _night(lc["considered_at"]), "team": bool(lc["team"]), "private": bool(lc["private"])}
            out.append(Record("lesson_candidates", f["ref"], lc["considered_at"], f))
    return out if selected is None else [record for record in out if record.ref in selected]


def corpus(conn: psycopg.Connection, environ: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """(ref, content) for every record the sink may send: exactly what past.dev receives."""
    out = []
    deny = egress.load(conn, environ)
    for rec in build_records(conn, egress.owner_accounts(environ)):
        content, _ = sanitize_record(rec.fields, deny)
        if content is not None:
            out.append((rec.ref, content))
    return out


# ---------------------------------------------------------------- the sink


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data: Any) -> str:
    raw = data if isinstance(data, str) else json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fields(content: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in content.splitlines())


def _links(conn: psycopg.Connection) -> dict[str, dict[str, Any]]:
    """Source id -> the stored link's metrics, for every past.dev link in rsi_evidence."""
    rows = conn.execute("select external_ref, metrics from rsi_evidence where system = 'pastdev' "
                        "and external_ref like %s", ("past:%",)).fetchall()
    return {ext[len("past:"):]: dict(m or {}) for ext, m in rows}


def _evidence(sid: str, metrics: dict[str, Any], summary: str | None = None) -> EvidenceRef | None:
    try:
        subject = [sid] if refs.parse(sid).scheme == "amem" else []
        return EvidenceRef(refs.past(sid), "pd", "decision-memory", subject, metrics, summary)
    except refs.RefError:
        return None


LINK_KEYS = ("sha256", "kind", "status", "ingestion_id", "retries", "unchanged", "failure", "source_id", "legacy_id")


def live_ids(ref: str, metrics: Mapping[str, Any]) -> list[str]:
    """The past.dev source ids a link's data point may have: metrics.source_id (the wired id), else the ref itself
    (a link written before 2026-10-24, when the ref was the id), plus metrics.legacy_id (that older point, still live
    after the record was re-sent under the wired id)."""
    sid = metrics.get("source_id")
    out = [sid if isinstance(sid, str) and sid else ref]
    old = metrics.get("legacy_id")
    if isinstance(old, str) and old and old not in out:
        out.append(old)
    return out


def _link(ref: str, content: str, metrics: Mapping[str, Any]) -> EvidenceRef:
    """One record's rsi_evidence link: its content hash, kind and ingestion state (never the content)."""
    f = _fields(content)
    m = {k: metrics[k] for k in LINK_KEYS if metrics.get(k) is not None}
    m["kind"] = f["kind"]
    ev = _evidence(ref, m, f"{f['kind']}: {f['rule']}" if "rule" in f else f["kind"])
    assert ev is not None                      # a sanitized ref always parses
    return ev


def _key(items: list[dict[str, Any]]) -> str:
    """The batch idempotencyKey: content only (Mission 004C, F5 and G-PD0), so a replay or a re-send after a
    lost response is the same request, and the 409 rule (same key, other body) can never fire."""
    return _sha({"items": items})


def _item(rec: Record, content: str) -> dict[str, Any]:
    """The exact ingest item for one record (the only shape that leaves; no metadata). The id is the wired ref."""
    return {"id": wire(rec.ref), "content": content, "timestamp": _ts(rec.at), "label": rec.fields["kind"],
            "identity": IDENTITY}


def payload_sha256(item: Mapping[str, Any]) -> str:
    """The sha256 a reviewer signs off (Mission 004C backfill review): over the canonical JSON of the exact item
    (sorted keys, no spaces, UTF-8), so a change to the content, time, label or identity changes it."""
    return _sha(dict(item))


# ---------------------------------------------------------------- reviewed sets (Mission 004C backfill review)

REVIEW_SHA_RE = re.compile(r"[0-9a-f]{64}")


class ReviewError(ValueError):
    """The reviewed-set file is not usable (shape, a bad ref or hash, a duplicate, empty)."""


def load_reviewed(data: Any) -> dict[str, str]:
    """ref -> reviewed payload sha256 from a reviewed-set document: the JSON of `amem sync plan --sink pastdev
    --contents --json` cut down to the chosen records (`{"records": [{"ref", "sha256", ...}, ...]}`), or a bare
    list of such objects. Other keys are ignored."""
    rows = data.get("records") if isinstance(data, Mapping) else data
    if not isinstance(rows, list) or not rows:
        raise ReviewError("a reviewed set lists at least one record: {\"records\": [{\"ref\": ..., \"sha256\": ...}]}")
    out: dict[str, str] = {}
    for r in rows:
        ref, sha = (r.get("ref"), r.get("sha256")) if isinstance(r, Mapping) else (None, None)
        if not isinstance(ref, str) or not _ours(ref):
            raise ReviewError(f"not a past.dev record ref: {str(ref)[:80]!r}")
        if not isinstance(sha, str) or not REVIEW_SHA_RE.fullmatch(sha):
            raise ReviewError(f"{ref}: sha256 must be 64 lowercase hex characters")
        if ref in out:
            raise ReviewError(f"{ref}: listed twice")
        out[ref] = sha
    return out


def _with(ev: EvidenceRef, **metrics: Any) -> EvidenceRef:
    m = {**ev.metrics, **metrics}
    return EvidenceRef(ev.ref, ev.source, ev.kind, ev.subject_refs, {k: v for k, v in m.items() if v is not None},
                       ev.summary)


def delete_ids(client: Client, ids: Sequence[str], *, chunk: int = DELETE_MAX,
               done: Callable[[list[str]], None] | None = None) -> tuple[list[str], list[str], int]:
    """Delete source ids in calls of <= `chunk` (<= 1000). past.dev deletes all or nothing and answers 404
    data-point-not-found when any id is not live, so that 404 splits the call in halves until the dead ids are
    found. Any other 404 (a wrong path, say) raises: it says nothing about the ids.
    Returns (deleted, already gone, calls). `done` runs after each top-level chunk with its ids."""
    chunk = max(1, min(chunk, DELETE_MAX))
    deleted: list[str] = []
    gone: list[str] = []
    calls = 0

    def go(part: list[str]) -> None:
        nonlocal calls
        calls += 1
        try:
            client.delete(part)
        except http.HttpError as exc:
            if exc.status != 404 or NOT_LIVE not in str(exc):
                raise
            if len(part) == 1:
                gone.extend(part)
                return
            mid = len(part) // 2
            go(part[:mid])
            go(part[mid:])
            return
        deleted.extend(part)

    for i in range(0, len(ids), chunk):
        part = list(ids[i:i + chunk])
        go(part)
        if done:
            done(part)
    return deleted, gone, calls


class PastDevSink:
    """sync.Sink for past.dev: `amem sync run --sink pastdev`. See the module docstring for the cursor."""

    name = "pastdev"

    def __init__(self, cfg: Config | None = None, *, environ: Mapping[str, str] | None = None,
                 client: Client | None = None, batch_max: int = BATCH_MAX, poll_s: float = POLL_S,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC), tables: Sequence[str] | None = None,
                 since: datetime | None = None, only: Mapping[str, str] | None = None):
        self._environ = environ
        self.cfg = cfg if cfg is not None else config(environ)
        self.client = _client(self.cfg, client, sleep)
        self.batch_max = max(1, min(batch_max, BATCH_MAX))
        self.poll_s, self.sleep, self.clock, self.now = poll_s, sleep, clock, now
        # Mission 004C (F4): `--tables` / `--since` narrow what is sent (a record older than `since` waits for a
        # run without it). Retractions and checks of running ingestions are never narrowed.
        unknown = sorted(set(tables or ()) - set(TABLES))
        if unknown:
            raise ValueError(f"unknown table(s) for pastdev: {', '.join(unknown)} (known: {', '.join(TABLES)})")
        self.tables = tuple(t for t in TABLES if not tables or t in tables)
        self.since = since
        self.replay_scope = frozenset(self.tables) if tables else None     # sync.run: the cursors a replay resets
        self.refused: list[tuple[str, list[str]]] = []      # (ref, reasons) from the last batches() call
        self.retired: list[str] = []                         # refs deleted at past.dev: never sent again
        self.ownership: dict[str, dict[str, int]] = {}       # table -> records per class (F8), last batches() call
        self.owners: tuple[str, ...] = ()
        # Mission 004C backfill review. `only`: a reviewed set (load_reviewed: ref -> payload sha256). Every batch is
        # narrowed to those refs, and when any of them has no current payload or another sha256, batches() yields
        # nothing at all (`review` says why). `classes`: ref -> (table, class) of every record of the last call.
        if only is not None and (tables or since is not None):
            raise ValueError("a reviewed set (--only) cannot be combined with --tables or --since")
        self.only = dict(only) if only is not None else None
        self.classes: dict[str, tuple[str, str]] = {}
        self.review: dict[str, Any] | None = None

    @property
    def masked(self) -> dict[str, Any]:
        """Records of the last batches() call withheld as team and as private (never sent), in total and per
        table (with own), for `amem sync plan` (Mission 004C, F8)."""
        return {egress.TEAM: sum(n[egress.TEAM] for n in self.ownership.values()),
                egress.PRIVATE: sum(n[egress.PRIVATE] for n in self.ownership.values()),
                "tables": {t: dict(n) for t, n in sorted(self.ownership.items())},
                "owner_accounts": list(self.owners)}

    def health(self) -> Health:
        if switched_off(self._environ):
            return Health(False, "kill switch")
        if self.client is None:
            return Health(False, "not configured")
        return self.client.health()

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[sync.Batch]:
        owners = egress.owner_accounts(self._environ)
        records = build_records(conn, owners)
        links = _links(conn)
        now = _ts(self.now())
        self.refused, self.retired = [], []
        self.ownership, self.classes, self.review = {}, {}, None
        # Mission 004C (F3, F8): env terms + team and private repo names, read now
        deny = egress.env_terms(self._environ) | egress.team_names(conn, owners=owners)
        self.owners = owners
        ready: dict[str, tuple[Record, str]] = {}
        for rec in records:
            cls = egress.TEAM if rec.fields.get("team") is True else \
                egress.PRIVATE if rec.fields.get("private") is True else egress.OWN
            n = self.ownership.setdefault(rec.table, dict.fromkeys(egress.CLASSES, 0))
            n[cls] += 1
            self.classes[rec.ref] = (rec.table, cls)
            content, reasons = sanitize_record(rec.fields, deny)
            if content is None:
                self.refused.append((rec.ref, reasons))
            else:
                ready[rec.ref] = (rec, content)
        # 0. a reviewed set (Mission 004C backfill review): every reviewed ref must have a current payload with the
        #    reviewed sha256, or nothing at all is yielded (no send, no retraction, no check)
        states: dict[str, str] = {}
        if self.only is not None:
            why = dict(self.refused)
            problems = [{"ref": ref, "reason": (f"refused: {', '.join(why[ref])}" if ref in why else "not found")
                         if ref not in ready else "changed"}
                        for ref, want in sorted(self.only.items())
                        if ref not in ready or payload_sha256(_item(*ready[ref])) != want]
            self.review = {"ok": not problems, "refs": len(self.only), "problems": problems, "states": states}
            if problems:
                return
        only = self.only

        def keep(ref: str) -> bool:
            return only is None or ref in only

        # 1. retract: a live link whose record is gone or now refused
        gone = sorted(sid for sid, m in links.items() if not m.get("deleted") and sid not in ready and keep(sid))
        for i in range(0, len(gone), DELETE_MAX):
            part = gone[i:i + DELETE_MAX]
            evs = [ev for sid in part if (ev := _evidence(sid, {"deleted": True, "deleted_at": now})) is not None]
            yield sync.Batch(RETRACT, [{"id": sid, "ids": live_ids(sid, links[sid])} for sid in part],
                             f"{now}|{part[-1]}", evs)
        # 2. send: a record whose link is missing or holds another hash; a deleted id is retired for good.
        #    Same hash: a running ingestion is checked by its id (3), a dead-lettered one waits for new content,
        #    a sent one goes again only on --replay (same key: past.dev answers the stored receipt).
        pending: dict[str, list[EvidenceRef]] = {}
        for table in TABLES:
            todo = []
            for ref, (rec, content) in ready.items():
                if rec.table != table or not keep(ref):
                    continue
                link = links.get(ref) or {}
                if link.get("deleted"):
                    self.retired.append(ref)
                    states[ref] = "retired"
                    continue
                sha = _sha(content)
                # a link from before 2026-10-24 (no source_id): its data point, if any, has the ref as its id
                legacy = bool(link) and not link.get("source_id")
                if link.get("sha256") == sha:
                    iid = _ingestion_id(link.get("ingestion_id"))
                    if link.get("status") == QUEUED and iid:
                        pending.setdefault(iid, []).append(_link(rec.ref, content, link))
                        states[ref] = "check"
                        continue
                    # a replay re-sends a sent record under the same key, except one sent under the old id: under
                    # the wired id it would be a second data point, so it stays as it is
                    if link.get("status") in DEAD or (link.get("status") in SENT and (not replay or legacy)):
                        states[ref] = "dead-lettered" if link.get("status") in DEAD else "already-sent"
                        continue
                if table not in self.tables or (self.since is not None and rec.at < self.since):
                    continue                                    # out of this run's scope (F4)
                old = link.get("legacy_id") or (ref if legacy else None)
                todo.append((rec, content, {"sha256": sha, "status": QUEUED, "source_id": wire(ref), "legacy_id": old}))
                states[ref] = "send"
            for i in range(0, len(todo), self.batch_max):
                part = todo[i:i + self.batch_max]
                items = [_item(rec, content) for rec, content, _ in part]
                evs = [_link(rec.ref, content, m) for rec, content, m in part]
                last = part[-1][0]
                yield sync.Batch(table, items, f"{_ts(last.at)}|{last.ref}", evs, _key(items))
        # 3. check: one batch per still-running ingestion; no rows, no content, one free GET (and maybe a retry)
        for iid, evs in sorted(pending.items()):
            yield sync.Batch(CHECK, [], f"{now}|{iid}", evs, iid)

    def cost(self, batch: sync.Batch) -> int:
        """Credits this batch would spend (local rule; a retraction or an ingestion check spends none)."""
        return 0 if batch.table in (RETRACT, CHECK) else local_credits(batch.rows)

    def describe(self, batch: sync.Batch) -> list[dict[str, Any]]:
        """`amem sync plan --contents` (Mission 004C backfill review): per item of a send batch, the exact payload
        that would leave, its sha256 (payload_sha256), its class and its local credit and size estimate. A
        retraction or an ingestion check carries no content: only the refs and the action."""
        if batch.table == RETRACT:
            return [{"action": "delete", "ref": r["id"], "ids": list(r.get("ids") or [r["id"]])} for r in batch.rows]
        if batch.table == CHECK:
            return [{"action": "check", "ref": ev.subject_refs[0], "ingestion_id": batch.key}
                    for ev in batch.evidence if ev.subject_refs]
        return [{"ref": unwire(it["id"]), "table": batch.table,
                 "class": self.classes.get(unwire(it["id"]), ("", egress.PRIVATE))[1], "payload": dict(it), "sha256": payload_sha256(it), "bytes": len(str(it["content"]).encode("utf-8")),
                 "credits": local_credits([it])} for it in batch.rows]

    def refused_records(self) -> list[dict[str, Any]]:
        """The records of the last batches() call the sanitizer refused, with reasons and never their content."""
        return [{"ref": ref, "table": self.classes.get(ref, ("", ""))[0],
                 "class": self.classes.get(ref, ("", egress.PRIVATE))[1], "reasons": list(reasons)}
                for ref, reasons in self.refused]

    def split(self, batch: sync.Batch) -> list[sync.Batch]:
        """One item per batch with a content-addressed key, so the sync loop can dead-letter only refused items."""
        if batch.table in (RETRACT, CHECK):
            return [batch]
        return [sync.Batch(batch.table, [item], batch.watermark,
                           [e for e in batch.evidence if e.ref == f"past:{unwire(str(item.get('id')))}"], _key([item]))
                for item in batch.rows]

    def settled_keys(self, conn: psycopg.Connection) -> set[str]:
        """Refs past.dev holds (a sent, not deleted link): sync drops their stale dead letters."""
        return {sid for sid, m in _links(conn).items() if m.get("status") in SENT and not m.get("deleted")}

    def completion(self, conn: psycopg.Connection) -> dict[str, int]:
        """Stored provider settlement, separate from transport acceptance."""
        counts = {COMPLETED: 0, QUEUED: 0, FAILED: 0, REJECTED: 0}
        for ref, metrics in _links(conn).items():
            if metrics.get('deleted') or (self.only is not None and ref not in self.only):
                continue
            status = metrics.get('status')
            if status in counts:
                counts[status] += 1
        return counts

    def dead_key(self, row: Mapping[str, Any]) -> str:
        """sync's dead-letter key for one row: the record's ref (an ingest item carries the wired id)."""
        return unwire(str(row.get("id") or ""))

    def push(self, batch: sync.Batch) -> None:
        if switched_off(self._environ):
            raise RuntimeError("kill switch")
        if self.client is None:
            raise RuntimeError("not configured")
        if batch.table == RETRACT:
            delete_ids(self.client, [i for r in batch.rows for i in (r.get("ids") or [r["id"]])])
            return
        if batch.table == CHECK:                        # a running ingestion: one free GET, never a re-send
            retries = max((int(ev.metrics.get("retries") or 0) for ev in batch.evidence), default=0)
            self._settle(batch, batch.key, *self._poll(batch.key, retries, 0.0))
            return
        res = self.client.ingest_batch(batch.rows, batch.key)
        items = res.get("items")
        if not isinstance(items, list) or len(items) != len(batch.rows):
            raise RuntimeError("ingest: the receipt does not list every item")
        by_ord: dict[int, Mapping[str, Any]] = {}
        for it in items:
            if isinstance(it, Mapping) and isinstance(it.get("ordinal"), int):
                by_ord[it["ordinal"]] = it
        for i, row in enumerate(batch.rows):
            it = by_ord.get(i)
            if it is None or it.get("sourceId") != row["id"]:
                raise RuntimeError("ingest: past.dev answered with another source id")
        iid = _ingestion_id(res.get("ingestionId"))
        if res.get("unchanged") is True or res.get("status") == COMPLETED:
            status, code, retries = COMPLETED, None, 0
        elif iid is None:
            raise RuntimeError("ingest: the receipt has no ingestion id to follow")
        elif self.poll_s > 0:
            status, code, retries = self._poll(iid, 0, self.poll_s)
        else:
            status, code, retries = QUEUED, None, 0
        batch.evidence = [_with(ev, unchanged=True if by_ord[i].get("unchanged") is True else None)
                          for i, ev in enumerate(batch.evidence)]
        self._settle(batch, iid, status, code, retries)

    def _settle(self, batch: sync.Batch, iid: str | None, status: str, code: str | None, retries: int) -> None:
        """Write the ingestion state into the batch's links (the loop records them once push returns or raises
        sync.Refused). A rejected or finally failed ingestion is dead-lettered, never split and re-sent."""
        batch.evidence = [_with(ev, status=status, ingestion_id=iid, retries=retries or None, failure=code)
                          for ev in batch.evidence]
        if status in DEAD:
            rows = [{"id": ev.subject_refs[0]} for ev in batch.evidence if ev.subject_refs]
            raise sync.Refused(f"ingestion {status}: {code}", rows=rows, evidence=batch.evidence)

    def _poll(self, ingestion_id: str, retries: int, budget: float) -> tuple[str, str | None, int]:
        """(status, failure code, free retries used) of one ingestion, polled for up to `budget` seconds:
        completed; queued (still running, or failed and retried: a later run checks it again); rejected
        (content-rejected); failed (MAX_FREE_RETRIES used up). At most one free retry per call."""
        deadline = self.clock() + budget
        wait, retried, code = 0.5, False, None
        while True:
            st = self.client.ingestion(ingestion_id) if self.client else {}
            s = st.get("status")
            if s == COMPLETED:
                return COMPLETED, None, retries
            if s == "failed":
                failure = st.get("failure")
                code = failure.get("code") if isinstance(failure, Mapping) else None
                code = code if code in FAILURE_CODES else "unknown"
                if code == "content-rejected":
                    return REJECTED, code, retries
                if retried:
                    return QUEUED, code, retries         # failed again after this run's retry: the next run looks
                if retries >= MAX_FREE_RETRIES:
                    return FAILED, code, retries
                if self.client:
                    self.client.retry_ingestion(ingestion_id)      # free: re-runs it under the same id
                retries, retried = retries + 1, True
            if self.clock() + wait > deadline:
                return QUEUED, code, retries
            self.sleep(wait)
            wait = min(wait * 2, 4.0)


def settle_pending(conn: psycopg.Connection, *, client: Client | None = None,
                   budget_s: float = 30, limit: int = 20,
                   environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read pending provider jobs and refresh local receipts. Never ingest, retry or delete content."""
    if not math.isfinite(budget_s) or budget_s <= 0 or not 1 <= limit <= 100:
        raise ValueError('positive settlement budget and limit1..100 required')
    if switched_off(environ):
        return {'status': 'off', 'checked_jobs': 0, 'completed_records': 0}
    client = client or _client(config(environ), None, time.sleep)
    if client is None:
        return {'status': 'unavailable', 'checked_jobs': 0, 'completed_records': 0}
    lock = 'amem:sync:pastdev'
    if not conn.execute('select pg_try_advisory_lock(hashtextextended(%s,0))', (lock,)).fetchone()[0]:
        return {'status': 'partial', 'detail': 'another past.dev sync is active', 'checked_jobs': 0, 'completed_records': 0}
    deadline = time.monotonic()+budget_s
    checked = completed = failed_reads = provider_failed_jobs = 0
    try:
        jobs = conn.execute("select metrics->>'ingestion_id' from rsi_evidence where system='pastdev' "
            "and metrics->>'status'='queued' and not coalesce((metrics->>'deleted')::boolean,false) "
            "group by metrics->>'ingestion_id' order by min(observed_at) limit %s", (limit,)).fetchall()
        for (iid,) in jobs:
            if time.monotonic() >= deadline:
                break
            if _ingestion_id(iid) is None:
                failed_reads += 1
                continue
            try:
                with client.read_budget(max(0.001, deadline-time.monotonic())):
                    state = client.ingestion(iid)
                checked += 1
            except (http.HttpError, TimeoutError, ValueError):
                failed_reads += 1
                continue
            if state.get('status') == COMPLETED:
                completed += conn.execute("update rsi_evidence set metrics=(metrics-'failure') || %s, "
                    "observed_at=clock_timestamp() where system='pastdev' and metrics->>'status'='queued' "
                    "and metrics->>'ingestion_id'=%s", (Jsonb({'status': COMPLETED}), iid)).rowcount
                conn.commit()
            elif state.get('status') == 'failed':
                provider_failed_jobs += 1
                failure = state.get('failure')
                code = failure.get('code') if isinstance(failure, Mapping) else None
                code = code if code in FAILURE_CODES else 'unknown'
                status = REJECTED if code == 'content-rejected' else QUEUED
                conn.execute("update rsi_evidence set metrics=metrics || %s, observed_at=clock_timestamp() "
                    "where system='pastdev' and metrics->>'status'='queued' and metrics->>'ingestion_id'=%s",
                    (Jsonb({'status': status, 'failure': code}), iid))
                conn.commit()
            elif state.get('status') not in ('queued', 'processing', 'failed'):
                failed_reads += 1  # malformed or unknown state cannot become successful settlement
        pending = conn.execute("select count(*) from rsi_evidence where system='pastdev' and metrics->>'status'='queued' "
            "and not coalesce((metrics->>'deleted')::boolean,false)").fetchone()[0]
        return {'status': 'partial' if pending or failed_reads or provider_failed_jobs else 'ok', 'checked_jobs': checked,
                'completed_records': completed, 'pending_records': pending, 'failed_reads': failed_reads,
                'provider_failed_jobs': provider_failed_jobs}
    finally:
        conn.rollback()
        conn.execute('select pg_advisory_unlock(hashtextextended(%s,0))', (lock,))
        conn.commit()


# ---------------------------------------------------------------- deletion


def delete_all(conn: psycopg.Connection, client: Client | None = None, *,
               environ: Mapping[str, str] | None = None, refs: Iterable[str] | None = None) -> dict[str, int]:
    """Delete every past.dev data point linked in rsi_evidence (and not yet deleted), <= 1000 ids per call,
    and mark each link deleted. Works with AMEM_PASTDEV=off: it only removes what was sent. The deleted ids
    are retired (past.dev refuses them), but a record never sent before would still go on the next sync,
    so set the kill switch first. `refs` (Mission 004C, the rollback of a reviewed backfill: `amem sync delete
    --only`): only these refs; a ref with no live link is left alone (counted as `not_linked`)."""
    client = client or _client(config(environ), None, time.sleep)
    if client is None:
        raise RuntimeError(f"past.dev is not configured ({KEY_ENV})")
    links = {sid: m for sid, m in _links(conn).items() if not m.get("deleted")}
    live = set(links)
    wanted = None if refs is None else set(refs)
    # each link's data point ids (live_ids: the wired id, or the ref for a link written before 2026-10-24)
    owner = {pid: ref for ref in sorted(live if wanted is None else live & wanted) for pid in live_ids(ref, links[ref])}
    ids = sorted(owner)

    def mark(part: list[str]) -> None:
        conn.execute("update rsi_evidence set metrics = metrics || %s, observed_at = now() "
                     "where system = 'pastdev' and external_ref = any(%s)",
                     (Jsonb({"deleted": True, "deleted_at": _ts(datetime.now(UTC))}),
                      sorted({f"past:{owner[s]}" for s in part})))
        conn.commit()

    deleted, gone, calls = delete_ids(client, ids, done=mark)
    out = {"deleted": len(deleted), "gone": len(gone), "calls": calls}
    if wanted is not None:
        out["not_linked"] = len(wanted - live)
    return out


# ---------------------------------------------------------------- the retriever


QUERY_RE = r"(?!.*\S{24})(?=.*[A-Za-z0-9])[A-Za-z0-9 .,;!?'()_-]{1,200}"   # no ':', '/', '@', '\', '~'; no 24+ runs
DATE_RE = r"\d{4}-\d{2}-\d{2}"


def _ours(sid: Any) -> bool:
    if not isinstance(sid, str):
        return False
    try:
        r = refs.parse(sid)
    except refs.RefError:
        return False
    return r.scheme == "amem" and r.kind in KIND_ENTITY.values() and bool(SAFE_REF_ID.fullmatch(r.id))


def _iso(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC).isoformat(timespec="seconds")
    except ValueError:
        return None


def map_recall(res: Mapping[str, Any]) -> list[EvidenceRef]:
    """Recall results -> one EvidenceRef per amem: source id, best rank first, no duplicates. Nothing of
    past.dev's own text comes back (summary None): the caller re-reads the amem: ref from Supabase."""
    results = res.get("results")
    if not isinstance(results, list):
        raise ValueError("recall: no results list")
    rows = [r for r in results if isinstance(r, Mapping)]
    rows.sort(key=lambda r: r["rank"] if isinstance(r.get("rank"), int) else 1 << 30)
    out: list[EvidenceRef] = []
    seen: set[str] = set()
    for r in rows:
        for s in r.get("sources") or []:
            sid = s.get("sourceId") if isinstance(s, Mapping) else None
            sid = unwire(sid) if isinstance(sid, str) else sid       # the wired id back to its ref
            if not _ours(sid) or sid in seen:
                continue
            seen.add(sid)
            metrics: dict[str, Any] = {"rank": r["rank"]} if isinstance(r.get("rank"), int) else {}
            if s.get("label") in KINDS:
                metrics["kind"] = s["label"]
            out.append(EvidenceRef(refs.past(sid), "pd", "decision-memory", [sid], metrics, None, _iso(s.get("occurredAt"))))
    return out


class PastDevRetriever:
    """Optional retriever `pd`: one named query, `recall` (query, limit <= 20, from/to dates)."""

    source = "pd"
    _QUERIES = {"recall": NamedQuery("recall", {"query": QUERY_RE, "limit": r"[1-9]|1[0-9]|20", "from": DATE_RE,
                                                "to": DATE_RE},
                                     "past.dev decision memory: amem: refs of past decisions like this one", ("query",))}

    def __init__(self, connect: Callable[[], psycopg.Connection], cfg: Config | None = None, *,
                 environ: Mapping[str, str] | None = None, client: Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._connect, self._environ = connect, environ
        self.client = _client(cfg if cfg is not None else config(environ), client, sleep)

    def health(self) -> Health:
        if switched_off(self._environ):
            return Health(False, "kill switch")
        if self.client is None:
            return Health(False, "not configured")
        return self.client.health()

    def named_queries(self) -> Mapping[str, NamedQuery]:
        return self._QUERIES

    def query(self, name: str, params: Mapping[str, str]) -> list[EvidenceRef]:
        if switched_off(self._environ) or self.client is None:
            return []
        if name != "recall":
            raise ValueError(f"unknown query {name!r}")
        q = " ".join(params["query"].split())
        if secret_like(q) or scrub_text(q)[0] != q:
            raise ValueError("the query holds text the sanitizer would scrub")
        limit = min(int(params.get("limit", "10")), RECALL_MAX)
        # Leave room for stale/deleted source references removed by the authority
        # check. One recall only, the existing20-result cap remains unchanged.
        body: dict[str, Any] = {"query": q, "identity": IDENTITY, "limit": min(RECALL_MAX, limit * 2)}
        if "from" in params:
            body["occurredFrom"] = f"{date.fromisoformat(params['from']).isoformat()}T00:00:00Z"
        if "to" in params:
            body["occurredTo"] = f"{date.fromisoformat(params['to']).isoformat()}T23:59:59Z"
        found = map_recall(self.client.recall(body))
        with self._connect() as conn:
            return [ev for ev in found if usable(conn, ev)][:limit]


def recall_fn(r: PastDevRetriever, *, limit: int = RECALL_MAX) -> Callable[[str], list[str]]:
    """A retrieve(query) -> [amem ref] function for evaluate(), through the facade (status checked)."""

    def retrieve(query: str) -> list[str]:
        res = run_query(r, "recall", {"query": query, "limit": str(limit)})
        if res.status != "ok":
            raise RuntimeError(f"past.dev recall: {res.status} {res.detail}")
        return [ev.subject_refs[0] for ev in res.refs]

    return retrieve


# ---------------------------------------------------------------- experiment C3: past.dev vs Postgres full text

_FTS_SQL = """
with q as (
    select to_tsquery('simple', coalesce(string_agg(quote_literal(l), ' | '), '')) as tq
    from unnest(tsvector_to_array(to_tsvector('english', %(q)s))) as l
), d as (
    select t.ref, to_tsvector('english', t.body) as tv from unnest(%(refs)s::text[], %(bodies)s::text[]) as t(ref, body)
)
select d.ref from d, q where d.tv @@ q.tq order by ts_rank_cd(d.tv, q.tq) desc, d.ref limit %(k)s
"""


def fts_baseline(conn: psycopg.Connection, docs: Sequence[tuple[str, str]], *,
                 k: int = RECALL_MAX) -> Callable[[str], list[str]]:
    """The C3 baseline: Postgres full text (english tsvector, OR of the query's lexemes, ts_rank_cd) over the
    same sanitized records (`corpus`). Disposable: the records travel as query parameters; nothing is written."""
    ref_list, bodies = [d[0] for d in docs], [d[1] for d in docs]

    def retrieve(query: str) -> list[str]:
        rows = conn.execute(_FTS_SQL, {"q": query, "refs": ref_list, "bodies": bodies, "k": k}).fetchall()
        return [r[0] for r in rows]

    return retrieve


def evaluate(gold: Mapping[str, Iterable[str]] | Iterable[tuple[str, Iterable[str]]],
             retrieve: Callable[[str], Sequence[str]], *, k: int = 5) -> dict[str, float]:
    """recall@k (share of each query's relevant refs in the top k, averaged) and MRR (1 / rank of the first
    relevant ref, 0 when none is returned, averaged) over a hand-labelled gold set {query: relevant refs}."""
    items = list(gold.items()) if isinstance(gold, Mapping) else list(gold)
    if not items:
        raise ValueError("empty gold set")
    rec_sum = rr_sum = 0.0
    for query, relevant in items:
        rel = set(relevant)
        if not rel:
            raise ValueError(f"gold query without relevant refs: {query!r}")
        got = list(dict.fromkeys(retrieve(query)))
        rec_sum += len(rel & set(got[:k])) / len(rel)
        rr_sum += next((1.0 / (i + 1) for i, ref in enumerate(got) if ref in rel), 0.0)
    n = len(items)
    return {"queries": n, f"recall_at_{k}": round(rec_sum / n, 6), "mrr": round(rr_sum / n, 6)}


C3_MARGIN = 0.10


def c3_winner(pastdev: Mapping[str, float], baseline: Mapping[str, float], *, margin: float = C3_MARGIN) -> str:
    """'pastdev' only when its recall@5 is at least the baseline's + 0.10; otherwise 'baseline'."""
    return "pastdev" if pastdev["recall_at_5"] - baseline["recall_at_5"] >= margin - 1e-9 else "baseline"
