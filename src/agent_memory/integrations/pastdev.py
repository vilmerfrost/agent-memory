"""past.dev decision memory: the sink, the retriever, deletion and the C3 experiment (Mission 004A).

past.dev is an external SaaS. It receives only the sanitized decision records built here (sanitize.py):
one per proposal, decision or rejection, experiment, edit and lesson decision, each at most CAP_BYTES. No
transcripts, file contents, prompts, paths, emails, hosts, secrets, ids other than the record's own
`amem:` ref, and nothing from a team repo. One past.dev project is dedicated to RSI; its project key
(`past_sk_...`) is read from AMEM_PASTDEV_KEY only (this module never reads an env file). Without a key,
everything here is `unavailable`. AMEM_PASTDEV=off stops the sink (sync.switched_off) and the retriever.

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
- POST /recall {query, identity (required), limit 1-200, occurredFrom, occurredTo} -> results[] with
  rank and sources[] = {sourceId, occurredAt, label, metadata, excerpts}.
- POST /data-points/delete {ids: [...]} (1-1000) -> 202 {operationId, ids}; 404 when any id is not a live
  data point, and then nothing is deleted.
- 500 requests/min per key (Pay as you go); a 429 has no body; wait 1 s and double.
Not verified (assumed): that `sourceId` echoes our `id` byte for byte (the docs say it is trimmed; a
mismatch fails the batch); that GET /ingest/<nil uuid> answers 404 with a valid key and 401/403 without
(the health check); that a recall `source.label` is the label we sent.

Mapping (blueprint): id = the amem: ref, timestamp = the record time, label = the kind, identity = "rsi",
no metadata (past.dev feeds metadata to memory extraction). The batch idempotencyKey is sha256 over the
canonical items (so the 409 rule can never fire), plus each item's last deletion time, the table's failure
count and, on --replay, the run time: a re-send after a deletion or a failed ingestion is a new request.
Each record's own sha256(content) is kept in its rsi_evidence link (metrics.sha256).

Watermarks: proposals, experiments and edits change status after they are first written and have no
updated_at column, so change detection is by content: a record is sent when its link is missing or holds
another sha256. The watermark (`<record time>|<ref>` of the last record pushed) shows how far a table got.
A link whose record is gone or now refused (a team item, a secret) is retracted first: deleted at past.dev
and marked deleted. --replay re-sends every record that is not retired.

Deletion is final (Mission 004B). The docs: "A deleted id cannot be sent again; use a new id." The id is
the record's amem: ref, so a record whose link is marked deleted is retired: never sent again, listed in
`retired`. To bring one back would need a new id scheme or a new project; nothing here does that.

Cost (Mission 004B, from the pricing page and the spec): 1 credit per 350 UTF-8 bytes of content, rounded
up per item, at least 1 (local_credits); recall 0.1 credit. Credits are prepaid: at zero, sends answer 402
out-of-credits and nothing is billed. A project credit cap (console) answers 429 project-cap-reached. The
sync loop stops a run as `capped` on either (sync.provider_cap). POST /ingest/estimate prices a send
without writing or spending, but it still uploads the content, so it runs only where sending is approved;
GET /usage reads the month's balance and the project's spend and cap.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory.integrations import http, refs, sync
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
FAILURE_CODES = ("model-unavailable", "model-response-unreadable", "index-unavailable", "content-rejected", "internal")


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

    def _call(self, method: str, path: str, body: Any = None, *, ok: Callable[[int], bool] | None = None,
              retries: int | None = None) -> http.Response:
        return http.request(method, self.cfg.base_url + path, body=body, timeout=self.timeout,
                            headers={"Authorization": f"Bearer {self.cfg.key}", "Accept": "application/json"},
                            retries=self.retries if retries is None else retries, backoff=1.0, sleep=self.sleep,
                            ok=ok or (lambda s: 200 <= s < 300))

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
        """POST /ingest/{id}/retry: free; re-runs a failed ingestion (docs: api-reference, 2026-10-16)."""
        return _obj(self._call("POST", f"/ingest/{uuid.UUID(ingestion_id)}/retry", {}).json())

    def recall(self, body: dict[str, Any]) -> dict[str, Any]:
        return _obj(self._call("POST", "/recall", body).json())

    def delete(self, ids: list[str]) -> dict[str, Any]:
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


_DEVS = "array(select 'amem:deviation/' || v::text from unnest({}.deviation_ids) v)"

_PROPOSALS_SQL = f"""
select p.id::text as id, p.title, p.summary, p.rationale, p.status, p.result, p.created_at, x.night,
       c.source as c_source, c.rule_key, c.kind as c_kind, e.instruction_source, e.rule_id, e.meta ->> 'kind' as e_kind,
       (select d.action from rsi_decisions d where d.proposal_id = p.id order by d.decided_at desc, d.id desc limit 1)
           as decision,
       (coalesce(p.meta ->> 'team' = 'true', false) or coalesce(x.meta ->> 'team' = 'true', false)
        or coalesce(c.metrics ->> 'team' = 'true', false) or coalesce(e.meta ->> 'team' = 'true', false)
        or {_team_sql("p.evidence_refs || coalesce(c.evidence_refs, '{}'::text[]) || coalesce(e.devs, '{}'::text[])")})
           as team
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
       e.created_at, (coalesce(e.meta ->> 'team' = 'true', false) or {_team_sql(_DEVS.format("e"))}) as team
from edits e order by e.created_at, e.id
"""
# Mission 004C (F2): ingestion never sets runs.meta.team, so a lesson is also a team item when its run's repo
# (runs.meta.repo) or the lesson's scope_repo is a team repo: a repo or project of any session flagged team.
_LESSONS_SQL = """
select lc.run_id::text as id, lc.decision, lc.reason, lc.considered_at, l.slug, l.title,
       (coalesce(r.meta ->> 'team' = 'true', false)
        or exists (select 1 from sessions s where s.meta ->> 'team' = 'true'
                   and (s.repo in (r.meta ->> 'repo', l.scope_repo) or s.project in (r.meta ->> 'repo', l.scope_repo))))
           as team
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


def build_records(conn: psycopg.Connection) -> list[Record]:
    """Every decision record, in table order (TABLES) and time order. Reads only; refuses nothing (sanitize does)."""
    out: list[Record] = []
    proposals: dict[str, dict[str, Any]] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        for p in cur.execute(_PROPOSALS_SQL).fetchall():
            rat = p["rationale"] if isinstance(p["rationale"], Mapping) else {}
            f = {"kind": "proposal", "ref": refs.amem("proposal", p["id"]),
                 "rule": _rule(p["instruction_source"], p["rule_id"]) or _rule(p["c_source"], p["rule_key"]),
                 "hypothesis": p["summary"] or p["title"], "change_kind": rat.get("kind") or p["e_kind"] or p["c_kind"],
                 "result": _result(p["status"], p["result"]), "decision": p["decision"], "reason": rat.get("why"),
                 "night": p["night"] or _night(p["created_at"]), "team": bool(p["team"])}
            proposals[p["id"]] = f
            out.append(Record("rsi_proposals", f["ref"], p["created_at"], f))
        for d in cur.execute(_DECISIONS_SQL).fetchall():
            base = proposals.get(d["proposal_id"]) or {"team": True}       # no proposal row: never send
            f = {"kind": "rejection" if d["action"] == "reject" else "decision", "ref": refs.amem("decision", d["id"]),
                 "rule": base.get("rule"), "hypothesis": base.get("hypothesis"), "change_kind": base.get("change_kind"),
                 "result": d["worker_result"], "decision": d["action"], "reason": d["reason"],
                 "night": _night(d["decided_at"]), "team": base.get("team") is True}
            out.append(Record("rsi_decisions", f["ref"], d["decided_at"], f))
        for x in cur.execute(_EXPERIMENTS_SQL).fetchall():
            hyp = x["hypothesis"] if isinstance(x["hypothesis"], Mapping) else {}
            meta = x["meta"] if isinstance(x["meta"], Mapping) else {}
            f = {"kind": "experiment", "ref": refs.amem("experiment", x["id"]),
                 "rule": hyp.get("rule") or meta.get("rule"), "hypothesis": _hypothesis(x["hypothesis"]),
                 "change_kind": x["kind"], "result": _result(x["status"], x["verdict"]),
                 "night": x["night"] or _night(x["created_at"]), "team": _team(meta, hyp)}
            out.append(Record("rsi_experiments", f["ref"], x["created_at"], f))
        for e in cur.execute(_EDITS_SQL).fetchall():
            meta = e["meta"] if isinstance(e["meta"], Mapping) else {}
            f = {"kind": "edit", "ref": refs.amem("edit", e["id"]), "rule": _rule(e["instruction_source"], e["rule_id"]),
                 "hypothesis": e["summary"] or e["rule_title"], "change_kind": meta.get("kind") or e["verdict"],
                 "result": e["status"], "reason": e["why"], "night": _night(e["created_at"]), "team": bool(e["team"])}
            out.append(Record("edits", f["ref"], e["created_at"], f))
        for lc in cur.execute(_LESSONS_SQL).fetchall():
            f = {"kind": "lesson", "ref": refs.amem("run", lc["id"]), "rule": _rule("lessons", lc["slug"]),
                 "hypothesis": lc["title"], "decision": lc["decision"], "reason": lc["reason"],
                 "night": _night(lc["considered_at"]), "team": bool(lc["team"])}
            out.append(Record("lesson_candidates", f["ref"], lc["considered_at"], f))
    return out


def corpus(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(ref, content) for every record the sink may send: exactly what past.dev receives."""
    out = []
    for rec in build_records(conn):
        content, _ = sanitize_record(rec.fields)
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


def delete_ids(client: Client, ids: Sequence[str], *, chunk: int = DELETE_MAX,
               done: Callable[[list[str]], None] | None = None) -> tuple[list[str], list[str], int]:
    """Delete source ids in calls of <= `chunk` (<= 1000). past.dev deletes all or nothing and answers 404
    when any id is not live, so a 404 splits the call in halves until the dead ids are found.
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
            if exc.status != 404:
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
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self._environ = environ
        self.cfg = cfg if cfg is not None else config(environ)
        self.client = _client(self.cfg, client, sleep)
        self.batch_max = max(1, min(batch_max, BATCH_MAX))
        self.poll_s, self.sleep, self.clock, self.now = poll_s, sleep, clock, now
        self.refused: list[tuple[str, list[str]]] = []      # (ref, reasons) from the last batches() call
        self.retired: list[str] = []                         # refs deleted at past.dev: never sent again

    def health(self) -> Health:
        if switched_off(self._environ):
            return Health(False, "kill switch")
        if self.client is None:
            return Health(False, "not configured")
        return self.client.health()

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[sync.Batch]:
        records = build_records(conn)
        links = _links(conn)
        failures = (sync.load(conn, self.name).get("failures") or {})
        now = _ts(self.now())
        self.refused, self.retired = [], []
        ready: dict[str, tuple[Record, str]] = {}
        for rec in records:
            content, reasons = sanitize_record(rec.fields)
            if content is None:
                self.refused.append((rec.ref, reasons))
            else:
                ready[rec.ref] = (rec, content)
        # 1. retract: a live link whose record is gone or now refused
        gone = sorted(sid for sid, m in links.items() if not m.get("deleted") and sid not in ready)
        for i in range(0, len(gone), DELETE_MAX):
            part = gone[i:i + DELETE_MAX]
            evs = [ev for sid in part if (ev := _evidence(sid, {"deleted": True, "deleted_at": now})) is not None]
            yield sync.Batch(RETRACT, [{"id": sid} for sid in part], f"{now}|{part[-1]}", evs)
        # 2. send: a record whose link is missing or holds another hash; a deleted id is retired for good
        for table in TABLES:
            attempts = int((failures.get(table) or {}).get("attempts", 0))
            todo = []
            for ref, (rec, content) in ready.items():
                if rec.table != table:
                    continue
                link = links.get(ref) or {}
                if link.get("deleted"):
                    self.retired.append(ref)
                    continue
                sha = _sha(content)
                if replay or link.get("sha256") != sha:
                    todo.append((rec, content, sha, link))
            for i in range(0, len(todo), self.batch_max):
                part = todo[i:i + self.batch_max]
                items = [{"id": rec.ref, "content": content, "timestamp": _ts(rec.at), "label": rec.fields["kind"],
                          "identity": IDENTITY} for rec, content, _, _ in part]
                key = _sha({"items": items, "attempt": attempts, "replay": now if replay else None})
                evs = []
                for rec, content, sha, _ in part:
                    f = _fields(content)
                    summary = f"{f['kind']}: {f['rule']}" if "rule" in f else f["kind"]
                    ev = _evidence(rec.ref, {"sha256": sha, "kind": f["kind"], "status": "sent"}, summary)
                    assert ev is not None                      # a sanitized ref always parses
                    evs.append(ev)
                last = part[-1][0]
                yield sync.Batch(table, items, f"{_ts(last.at)}|{last.ref}", evs, key)

    def cost(self, batch: sync.Batch) -> int:
        """Credits this batch would spend (local rule; a retraction spends none)."""
        return 0 if batch.table == RETRACT else local_credits(batch.rows)

    def split(self, batch: sync.Batch) -> list[sync.Batch]:
        """One item per batch with a content-addressed key, so the sync loop can dead-letter only refused items."""
        if batch.table == RETRACT:
            return [batch]
        return [sync.Batch(batch.table, [item], batch.watermark,
                           [e for e in batch.evidence if e.ref == f"past:{item.get('id')}"], _sha({"item": item}))
                for item in batch.rows]

    def push(self, batch: sync.Batch) -> None:
        if switched_off(self._environ):
            raise RuntimeError("kill switch")
        if self.client is None:
            raise RuntimeError("not configured")
        if batch.table == RETRACT:
            delete_ids(self.client, [r["id"] for r in batch.rows])
            return
        res = self.client.ingest_batch(batch.rows, batch.key)
        items = res.get("items")
        if not isinstance(items, list) or len(items) != len(batch.rows):
            raise RuntimeError("ingest: the receipt does not list every item")
        by_ord: dict[int, Mapping[str, Any]] = {}
        for it in items:
            if isinstance(it, Mapping) and isinstance(it.get("ordinal"), int):
                by_ord[it["ordinal"]] = it
        status = "unchanged" if res.get("unchanged") is True else str(res.get("status"))
        if status == "queued" and self.poll_s > 0:
            status = self._poll(str(res.get("ingestionId")))
        evs = []
        for i, (row, ev) in enumerate(zip(batch.rows, batch.evidence, strict=True)):
            it = by_ord.get(i)
            if it is None or it.get("sourceId") != row["id"]:
                raise RuntimeError("ingest: past.dev answered with another source id")
            st = "unchanged" if it.get("unchanged") is True else status
            evs.append(EvidenceRef(ev.ref, ev.source, ev.kind, ev.subject_refs, {**ev.metrics, "status": st}, ev.summary))
        batch.evidence = evs                            # the loop records these links once push returns

    def _poll(self, ingestion_id: str) -> str:
        """completed, or queued when the poll budget ran out (the send is durably accepted). Raises on failed."""
        deadline = self.clock() + self.poll_s
        wait = 0.5
        retried = False
        while True:
            st = self.client.ingestion(ingestion_id) if self.client else {}
            s = st.get("status")
            if s == "completed":
                return "completed"
            if s == "failed":
                code = (st.get("failure") or {}).get("code") if isinstance(st.get("failure"), Mapping) else None
                code = code if code in FAILURE_CODES else "unknown"
                if code == "content-rejected":
                    # Mission 004B: past.dev batches are atomic, so this is a record-level refusal. A 422 makes the
                    # sync loop split the batch and dead-letter only the refused item(s).
                    raise http.HttpError(422, "ingestion failed: content-rejected")
                if not retried and self.client:
                    self.client.retry_ingestion(ingestion_id)      # free; then keep polling
                    retried = True
                    continue
                raise RuntimeError(f"ingestion failed: {code}")
            if self.clock() + wait > deadline:
                return "queued"
            self.sleep(wait)
            wait = min(wait * 2, 4.0)


# ---------------------------------------------------------------- deletion


def delete_all(conn: psycopg.Connection, client: Client | None = None, *,
               environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """Delete every past.dev data point linked in rsi_evidence (and not yet deleted), <= 1000 ids per call,
    and mark each link deleted. Works with AMEM_PASTDEV=off: it only removes what was sent. The deleted ids
    are retired (past.dev refuses them), but a record never sent before would still go on the next sync,
    so set the kill switch first."""
    client = client or _client(config(environ), None, time.sleep)
    if client is None:
        raise RuntimeError(f"past.dev is not configured ({KEY_ENV})")
    ids = sorted(sid for sid, m in _links(conn).items() if not m.get("deleted"))

    def mark(part: list[str]) -> None:
        conn.execute("update rsi_evidence set metrics = metrics || %s, observed_at = now() "
                     "where system = 'pastdev' and external_ref = any(%s)",
                     (Jsonb({"deleted": True, "deleted_at": _ts(datetime.now(UTC))}), [f"past:{s}" for s in part]))
        conn.commit()

    deleted, gone, calls = delete_ids(client, ids, done=mark)
    return {"deleted": len(deleted), "gone": len(gone), "calls": calls}


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
        body: dict[str, Any] = {"query": q, "identity": IDENTITY, "limit": limit}
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
