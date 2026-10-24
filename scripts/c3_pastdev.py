#!/usr/bin/env python3
"""C3: verify past.dev against the SYNTHETIC dataset (Mission 004C, G-PD3). Prepared; not yet run against past.dev.

    uv run python scripts/c3_pastdev.py --db-url URL --gold synth-out/gold.json [--out FILE] [--json] [--allow-prior-use]
    uv run python scripts/c3_pastdev.py --db-url URL --gold synth-out/gold.json --dry-run

Before: a disposable local Postgres, migrated with the Keychain tripwire (AMEM_ADMIN_SECURITY=<script that exits
44>, AMEM_NO_DOTENV=1), then `amem synth seed --db-url URL --out synth-out` (writes gold.json).

The database comes from --db-url only (never AMEM_DB_URL) and must be localhost / 127.0.0.1. The project key comes
from AMEM_PASTDEV_KEY as for `amem sync run`: the env files load as in the CLI (so run this script WITHOUT
AMEM_NO_DOTENV=1; process env wins over the files). Nothing here prints the key or the database password, and the
report is scrubbed of the key before it is written.

Steps, each timed, into one JSON report (default: next to gold.json, c3-report.json). Every past.dev call goes
through one recording client (C3Client): the bodies as sent, statuses, latencies and the answers stay in memory.
  preflight     the database is local, holds the seeded dataset (sentinel and counts equal gold) and no past.dev
                link or cursor yet; the key is configured; GET /usage (free): project.creditCap set and <= 250 and
                project.ingestionCap set and <= 100, else the run is REFUSED (exit 2) before any data is sent; no
                prior ingestion this month (the synthetic ids are fixed and past.dev retires deleted ids, so a second
                C3 on the same project fails; --allow-prior-use overrides); the usage baseline is recorded
  plan          (check 1) `amem sync plan --sink pastdev --json` in-process: the send set equals gold pastdev.send,
                refused equals gold refuse_team (team) + refuse_private (private); only then the free
                POST /ingest/estimate of exactly that sanitized send set, whose credits equal pastdev.local_credits.
                A failed plan stops the run: nothing is ingested
  sync_run      (check 2) `amem sync run --sink pastdev` in-process (batch size 1, 180 s poll budget): every
                receipt 202, every ingestion `completed` within 180 s, the links hold the ingestionId, every
                returned sourceId equals our amem: ref byte for byte; then the free polls wait (<= 180 s) for the
                project to settle (readiness.failed must be 0)
  recall        (check 3) 3 synthetic queries (derived from gold and the synth story) through PastDevRetriever,
                as `amem evidence query --source pd recall`: every raw sourceId is one of gold's send refs and
                re-reads in the local database
  duplicates    (check 4) a second run sends nothing; `--replay` re-sends with the same keys, gets the stored
                receipts (no new ingestion) and costs 0 (/usage delta); one synthetic record changed locally is
                sent alone and costs exactly its free estimate
  failure_path  a run against a bad host with a dummy key (the real key goes nowhere else): `unavailable`, the
                cursor unchanged; a fake transport inside the runner (a loopback server answering 429
                project-cap-reached): the run stops `capped` after one request, no retry, no backoff, links unchanged
  deletion      (check 5) pastdev.delete_all: every delete 202; recall no longer returns the ids (<= 3 rounds in
                60 s); every link is marked deleted; a later run sends nothing and lists every id as retired
  metrics       (check 6) latency p50/p95 of ingest, send-to-completed and recall; the /usage credits used in the
                run within 10% of the local estimate (plan + changed record + 0.1 per recall)
  leak_scan     (check 7) no strict marker in (a) our request bodies, (b) the stored payloads read back from
                GET /requests and /requests/{id}, (c) the recall answers. Strict: every gold marker whose policy is
                not `decision_text`, any ZZLEAK string not on the decision allowlist (decision_text markers go to
                past.dev by design: synth.py gold policy), sk-ant-, @example.com, the team and private refs and
                repo names. The same scan is a tripwire on every outgoing body: a hit stops the run unsent.

Budget guard: the run stops (the current step fails, nothing more is sent, later steps are skipped; leak_scan
still scans what was sent) when a spending call (ingest, recall) would bring the run's credits over 100 by the
local rule, or when /usage (read after every step) reports more than 100 credits used since the baseline.

`--dry-run` prints the plan and touches nothing (no database connection, no past.dev request).
Exit: 0 all steps ok, 1 a step failed, 2 bad arguments, an unsafe database or a project without both caps.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import config as amem_config, synth
from agent_memory.integrations import evidence as E, http, pastdev as PD, sync

BASE_URL = PD.BASE_URL                     # tests point this at a local fake
BAD_URL = "https://amem-c3-bad-host.invalid/api/v1"
DUMMY_KEY = "past_sk_c3dummynotarealkey0000"   # the only key a bad or fake host ever sees
CREDIT_CAP_MAX = 250                       # the owner's stop rule (G-PD1): both caps set, at most these
INGESTION_CAP_MAX = 100
MAX_RUN_CREDITS = 100                      # the budget guard
BATCH_MAX = 1                              # one data point per ingestion (same credits; research doc G-PD0)
COMPLETE_S = 180.0                         # each ingestion must complete within this
SETTLE_S, SETTLE_POLL_S = 180.0, 2.0
SYNC_BUDGET_S = 1800.0
RECALL_LIMIT = 10
RECALL_CREDITS = 0.1
DELETE_RECALL_ROUNDS, DELETE_RECALL_WAIT_S = 3, 20.0
USAGE_WAIT_S, USAGE_POLL_S = 30.0, 2.0
REQUEST_LOG_MAX = 200
TOLERANCE = 0.10
EPS = 1e-6
CHANGED_SUMMARY = "SYNTHETIC: added one line to the rule; revised once for the C3 duplicate check"
MARK_RE = re.compile(r"(?i)zzleak(?:-\d{4})?")
LITERALS = ("sk-ant-", "@example.com", "pipeline-poc", "client-notes", "synth-client-", "synth-other-")
STEPS = ("preflight", "plan", "sync_run", "recall", "duplicates", "failure_path", "deletion", "metrics", "leak_scan")
# query -> the story records it is about (names resolved against gold in _queries); none holds a marker
RECALL_QUERIES = (
    ("reproduce the failure before changing code", ("proposal", "decision", "experiment:c2", "lesson:accept", "edit:r1")),
    ("does NCM solve easy sudoku puzzles", ("experiment:ncm",)),
    ("why was the debugging skill proposal rejected", ("proposal", "decision")),
)
PLAN = {
    "preflight": f"database local + seeded (sentinel, counts = gold), no past.dev links yet; key set; GET /usage: "
                 f"creditCap <= {CREDIT_CAP_MAX} and ingestionCap <= {INGESTION_CAP_MAX} (else REFUSED, exit 2); "
                 "no prior ingestion; usage baseline",
    "plan": "sync plan --sink pastdev: send = gold send, refused = gold team + private; free estimate of exactly that "
            "set = local_credits (a mismatch stops the run unsent)",
    "sync_run": f"sync run --sink pastdev (batch 1): every receipt 202, completed within {COMPLETE_S:.0f} s, links hold "
                "the ingestionId, sourceId = amem: ref byte for byte; project settles, readiness.failed 0",
    "recall": f"{len(RECALL_QUERIES)} synthetic queries via PastDevRetriever: every sourceId is a gold send ref and "
              "re-reads locally",
    "duplicates": "second run sends nothing; --replay: stored receipts, 0 credits; one changed record costs exactly "
                  "its estimate",
    "failure_path": f"sync run against {BAD_URL} (dummy key): unavailable, cursor unchanged; fake 429 "
                    "project-cap-reached: capped after 1 request, no retry",
    "deletion": "delete_all: every delete 202; recall no longer returns the ids; links deleted; a later run sends "
                "nothing, ids retired",
    "metrics": "latency p50/p95 (ingest, send-to-completed, recall); /usage credits used within 10% of the local "
               "estimate",
    "leak_scan": "0 strict markers / sk-ant- / @example.com / team or private refs in our bodies, in GET /requests "
                 "payloads and in recall answers",
}


class Stop(http.HttpError):
    """The runner's own stop (budget guard or leak tripwire), raised before anything is sent. Status 402, so the
    sync loop stops the run as `capped` (sync.provider_cap) instead of backing the table off."""

    def __init__(self, why: str):
        super().__init__(402, f"c3 stop: {why}")


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None


def _project(usage: Any) -> dict[str, Any]:
    p = usage.get("project") if isinstance(usage, Mapping) else None
    return dict(p) if isinstance(p, Mapping) else {}


def _pct(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile, rounded to ms."""
    v = sorted(values)
    return round(v[max(0, math.ceil(q * len(v)) - 1)], 3) if v else None


def _lat(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": _pct(values, 0.5), "p95": _pct(values, 0.95)}


def _url_host(url: str) -> str | None:
    return urllib.parse.urlsplit(url).hostname if url else None


# ---------------------------------------------------------------- leak needles and the recording client


@dataclass(frozen=True)
class Needles:
    """What may never leave: a ZZLEAK string not on the decision allowlist, and the labelled literals."""

    allowed: frozenset[str]                   # decision_text markers: past.dev receives them by design
    literals: tuple[tuple[str, str], ...]     # (label, needle), matched case-insensitively
    key: str | None = None                    # never in a body we send (past.dev's own logs may show it)

    @classmethod
    def from_gold(cls, gold: Mapping[str, Any], key: str | None = None) -> Needles:
        allowed = frozenset(m["marker"] for m in gold.get("markers", []) if m.get("policy") == synth.DECISION)
        pd = gold.get("pastdev", {})
        lits = [(n, n) for n in LITERALS]
        for ref in [*pd.get("refuse_team", []), *pd.get("refuse_private", [])]:
            lits.append((ref, ref))
            lits.append((ref, ref.split("/", 1)[-1]))       # the bare id too
        return cls(allowed, tuple(lits), key or None)

    def scan(self, text: str, *, outgoing: bool = False) -> tuple[list[str], int]:
        """(hits, decision_text markers seen). A hit names a marker or a literal's label, never the key."""
        hits: list[str] = []
        allowed = 0
        for m in MARK_RE.finditer(text):
            if m.group(0) in self.allowed:
                allowed += 1
            else:
                hits.append(m.group(0))
        low = text.lower()
        hits += [label for label, needle in self.literals if needle and needle.lower() in low]
        if outgoing and self.key and self.key in text:
            hits.append("the project key")
        return hits, allowed


class Guard:
    """The budget guard: a local tally of the credits each spending call costs (pricing rule) and the /usage
    delta since the baseline. Once stopped, every further call raises Stop before it is sent."""

    def __init__(self, max_credits: float):
        self.max = max_credits
        self.baseline: float | None = None
        self.used = 0.0                 # /usage project.credits - baseline (last read)
        self.spent = 0.0                # local tally: new ingestions + 0.1 per recall
        self.seen: set[str] = set()     # ingestion ids already charged (a replay answers the stored receipt)
        self.stopped: str | None = None

    @staticmethod
    def cost(method: str, path: str, body: Any) -> float:
        """The most a call can cost: an ingest at the local rule, a recall 0.1; everything else is free."""
        if method == "POST" and path == "/ingest/batch" and isinstance(body, Mapping):
            return float(PD.local_credits(body.get("items") or []))
        if method == "POST" and path == "/recall":
            return RECALL_CREDITS
        return 0.0

    def before(self, cost: float) -> None:
        if self.stopped:
            raise Stop(self.stopped)
        if cost > 0 and max(self.spent, self.used) + cost > self.max + EPS:
            self.stopped = (f"budget guard: this call would bring the run to {max(self.spent, self.used) + cost:.1f} "
                            f"credits, over {self.max:g}")
            raise Stop(self.stopped)

    def charge(self, method: str, path: str, body: Any, res: Any) -> None:
        if method == "POST" and path == "/recall":
            self.spent += RECALL_CREDITS
            return
        if not (method == "POST" and path == "/ingest/batch" and isinstance(res, Mapping) and isinstance(body, Mapping)):
            return
        iid = str(res.get("ingestionId") or "")
        if res.get("unchanged") is True or (iid and iid in self.seen):
            return
        items = body.get("items") or []
        got = res.get("items") if isinstance(res.get("items"), list) else []
        if len(got) == len(items):
            items = [it for it, r in zip(items, got, strict=True) if not (isinstance(r, Mapping) and r.get("unchanged") is True)]
        self.spent += PD.local_credits(items) if items else 0
        if iid:
            self.seen.add(iid)

    def observe(self, credits: float) -> None:
        if self.baseline is None:
            return
        self.used = credits - self.baseline
        if self.used > self.max + EPS and not self.stopped:
            self.stopped = f"budget guard: /usage reports {self.used:.1f} credits used in this run, over {self.max:g}"


@dataclass
class Call:
    step: str
    method: str
    path: str
    body: str                       # exactly as sent (http.request's JSON encoding)
    status: int | None = None
    seconds: float = 0.0
    response: str | None = None
    error: str | None = None


class C3Client(PD.Client):
    """pastdev.Client that records every call, refuses an outgoing body that holds a needle (the tripwire) and
    enforces the budget guard. Also reads the request log (GET /requests, /requests/{id})."""

    def __init__(self, cfg: PD.Config, guard: Guard, needles: Needles, *, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__(cfg, sleep=sleep)
        self.guard, self.needles, self.clock = guard, needles, clock
        self.calls: list[Call] = []
        self.blocked: list[str] = []
        self.step = ""
        self.accepted: dict[str, float] = {}     # ingestion id -> when its receipt arrived
        self.completed: dict[str, float] = {}    # ingestion id -> when a poll first said completed
        self.last_status: dict[str, Any] = {}

    def _call(self, method: str, path: str, body: Any = None, *, ok: Callable[[int], bool] | None = None,
              retries: int | None = None) -> http.Response:
        text = "" if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        hits, _ = self.needles.scan(f"{path}\n{text}", outgoing=True)
        if hits:
            self.blocked.append(f"{method} {path}: {', '.join(sorted(set(hits))[:5])}")
            self.guard.stopped = self.guard.stopped or f"leak tripwire: {self.blocked[-1]}"
            raise Stop(self.guard.stopped)
        cost = self.guard.cost(method, path, body)
        if cost > 0 and self.guard.baseline is not None and not self.guard.stopped:
            self.observe_usage()                    # free: the meter, read before every spending call
        self.guard.before(cost)
        call = Call(self.step, method, path, text)
        t0 = self.clock()
        try:
            resp = super()._call(method, path, body, ok=ok, retries=retries)
        except http.HttpError as exc:
            call.status, call.error, call.seconds = exc.status, str(exc)[:300], self.clock() - t0
            self.calls.append(call)
            raise
        t1 = self.clock()
        call.status, call.seconds, call.response = resp.status, t1 - t0, resp.text()
        self.calls.append(call)
        self._track(method, path, body, resp, t1)
        return resp

    def _track(self, method: str, path: str, body: Any, resp: http.Response, at: float) -> None:
        try:
            res = resp.json()
        except ValueError:
            return
        self.guard.charge(method, path, body, res)
        if not isinstance(res, Mapping):
            return
        if method == "POST" and path == "/ingest/batch" and res.get("ingestionId"):
            self.accepted.setdefault(str(res["ingestionId"]), at)
        elif method == "GET" and path.startswith("/ingest/") and not path.endswith(PD.NIL_INGESTION):
            self.last_status = dict(res)
            if res.get("status") == PD.COMPLETED:
                self.completed.setdefault(path.rsplit("/", 1)[-1], at)

    def observe_usage(self) -> float:
        """GET /usage (free): project.credits, fed to the budget guard."""
        c = _num(_project(self.usage()).get("credits"))
        if c is None:
            raise ValueError("GET /usage: no project.credits")
        self.guard.observe(c)
        return c

    def request_log(self) -> http.Response:
        return self._call("GET", "/requests")

    def request_entry(self, rid: str) -> http.Response:
        return self._call("GET", "/requests/" + urllib.parse.quote(rid, safe=""))

    def since(self, n: int, method: str, path: str) -> list[Call]:
        return [c for c in self.calls[n:] if c.method == method and c.path == path]


def _json(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _source_ids(call: Call | None) -> list[str]:
    res = _json(call.response) if call else None
    out: list[str] = []
    for r in (res.get("results") if isinstance(res, Mapping) else None) or []:
        for s in (r.get("sources") if isinstance(r, Mapping) else None) or []:
            sid = s.get("sourceId") if isinstance(s, Mapping) else None
            sid = PD.unwire(sid) if isinstance(sid, str) else sid     # wired ids (2026-10-24) back to refs
            if isinstance(sid, str) and sid not in out:
                out.append(sid)
    return out


def _log_entries(page: Any) -> tuple[list[Mapping[str, Any]], list[str]]:
    """(entries, the page's keys): the request-log list under any of the usual keys. The shape of GET /requests is
    not in the repo's notes, so this is tolerant, and an empty result fails the check loudly."""
    if isinstance(page, list):
        return [e for e in page if isinstance(e, Mapping)], []
    if isinstance(page, Mapping):
        for k in ("requests", "items", "data", "results", "entries"):
            if isinstance(page.get(k), list):
                return [e for e in page[k] if isinstance(e, Mapping)], sorted(page)
        return [], sorted(page)
    return [], []


def _entry_id(e: Mapping[str, Any]) -> str | None:
    for k in ("id", "requestId", "request_id"):
        v = e.get(k)
        if isinstance(v, str | int) and not isinstance(v, bool) and str(v):
            return str(v)
    return None


# ---------------------------------------------------------------- the fake transport of the cap check


class _CapServer:
    """A loopback past.dev stand-in that answers every send with 429 project-cap-reached (health: 404, ok)."""

    def __enter__(self) -> _CapServer:
        hits: list[tuple[str, str]] = []
        self.hits = hits

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _go(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                hits.append((self.command, self.path))
                if self.command == "POST" and self.path.endswith("/ingest/batch"):
                    status, body = 429, {"code": "project-cap-reached", "status": 429,
                                         "message": "the project's monthly credit cap is reached"}
                else:
                    status, body = 404, {"code": "not-found", "status": 404}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _go

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/api/v1"

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


class _CapCheckSink(PD.PastDevSink):
    name = "c3-capcheck"         # its own cursor: the real past.dev cursor and links are never touched


# ---------------------------------------------------------------- the run


def _config(environ: Mapping[str, str] | None) -> PD.Config | None:
    """The key as `amem sync run` reads it: env files load first (when reading the process env)."""
    if environ is None:
        amem_config.load_env()
    return PD.config(environ, base_url=BASE_URL)


def _queries(gold: Mapping[str, Any]) -> list[tuple[str, list[str]]]:
    send = set(gold["pastdev"]["send"])

    def one(prefix: str) -> str:
        found = [r for r in send if r.startswith(prefix)]
        if len(found) != 1:
            raise ValueError(f"gold send has {len(found)} {prefix} refs, expected 1")
        return found[0]

    names = {"proposal": one("amem:proposal/"), "decision": one("amem:decision/"),
             "experiment:c2": f"amem:experiment/{synth.EXP_C2}", "experiment:ncm": f"amem:experiment/{synth.EXP_NCM}",
             "lesson:accept": f"amem:run/{synth.uid(f'run:{synth.HARVEST}:9')}",
             "edit:r1": f"amem:edit/{synth.uid('edit:r1')}"}
    missing = sorted(r for r in names.values() if r not in send)
    if missing:
        raise ValueError(f"gold send lacks the story refs {missing}: gold and synth.py disagree")
    return [(q, [names[n] for n in want]) for q, want in RECALL_QUERIES]


def _changed_ref(gold: Mapping[str, Any]) -> tuple[str, str]:
    eid = str(synth.uid("edit:r2"))
    ref = f"amem:edit/{eid}"
    if ref not in gold["pastdev"]["send"]:
        raise ValueError(f"gold send lacks {ref} (the record the duplicate check changes)")
    return eid, ref


def _planned(sink: PD.PastDevSink, conn: psycopg.Connection) -> list[dict[str, Any]]:
    """The items the next run would send (read-only, nothing sent)."""
    conn.rollback()
    conn.read_only = True
    try:
        return [row for b in sink.batches(conn, {}, replay=False) if b.table not in (PD.RETRACT, PD.CHECK)
                for row in b.rows]
    finally:
        conn.rollback()
        conn.read_only = False


def _scrub(report: dict[str, Any], key: str | None) -> dict[str, Any]:
    if not key:
        return report
    text = json.dumps(report, default=str)
    return json.loads(text.replace(key, "[key]")) if key in text else report


def run_c3(db_url: str, gold: Mapping[str, Any], *, environ: Mapping[str, str] | None = None,
           cfg: PD.Config | None = None, connect: Callable[..., psycopg.Connection] = psycopg.connect,
           bad_url: str = BAD_URL, sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic, max_credits: float = MAX_RUN_CREDITS,
           allow_prior_use: bool = False, usage_wait_s: float = USAGE_WAIT_S) -> dict[str, Any]:
    """Run every step. A failed preflight returns at once; a failed plan or a budget stop skips the steps that
    would send (leak_scan still scans what was sent); any other failing step never stops the others."""
    if not 0 < max_credits <= MAX_RUN_CREDITS:
        raise ValueError(f"max_credits must be in (0, {MAX_RUN_CREDITS}]")
    started = datetime.now(UTC)
    t0 = clock()
    cfg = cfg if cfg is not None else _config(environ)
    env = dict(environ) if environ is not None else None
    pd_gold = gold["pastdev"]
    send = list(pd_gold["send"])
    team, private = list(pd_gold["refuse_team"]), list(pd_gold["refuse_private"])
    needles = Needles.from_gold(gold, cfg.key if cfg else None)
    guard = Guard(max_credits)
    client = C3Client(cfg, guard, needles, sleep=sleep, clock=clock) if cfg else None
    state: dict[str, Any] = {"plan_credits": 0, "change_credits": 0, "iids": set()}
    report: dict[str, Any] = {
        "c3": "pastdev", "synthetic": True, "dataset": gold.get("dataset"), "anchor": gold.get("anchor"),
        "started_at": started.isoformat(timespec="seconds"), "db_host": synth.db_host(db_url),
        "pastdev": {"host": _url_host(cfg.base_url) if cfg else _url_host(BASE_URL), "key_set": cfg is not None},
        "limits": {"credit_cap_max": CREDIT_CAP_MAX, "ingestion_cap_max": INGESTION_CAP_MAX,
                   "run_credits_max": max_credits, "batch_max": BATCH_MAX},
        "steps": []}

    def finish() -> dict[str, Any]:
        report["ok"] = all(s["ok"] for s in report["steps"]) and len(report["steps"]) == len(STEPS)
        report["credits"] = {"baseline": guard.baseline, "used": round(guard.used, 3),
                             "local_tally": round(guard.spent, 3), "max": max_credits}
        if guard.stopped:
            report["stopped"] = guard.stopped
        if state.get("refused"):
            report["refused"] = state["refused"]
        report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        report["seconds"] = round(clock() - t0, 3)
        return _scrub(report, cfg.key if cfg else None)

    try:
        synth.check_local(db_url)
    except synth.UnsafeTarget as exc:
        report["steps"].append({"name": "preflight", "ok": False, "seconds": 0.0, "detail": str(exc)})
        state["refused"] = "unsafe database"
        return finish()

    with connect(db_url) as conn:
        def halted() -> str | None:
            return guard.stopped or state.get("halt")

        def usage_credits() -> float:
            assert client is not None
            return client.observe_usage()

        def usage_wait(start: float, done: Callable[[float], bool]) -> float:
            """/usage until done(delta since `start`) (the meter may lag), at most usage_wait_s."""
            deadline = clock() + usage_wait_s
            while True:
                now = usage_credits()
                if done(now - start) or clock() >= deadline or guard.stopped:
                    return now
                sleep(USAGE_POLL_S)

        def exactly(expect: float) -> Callable[[float], bool]:
            return lambda delta: abs(delta - expect) < EPS

        def sink() -> PD.PastDevSink:
            return PD.PastDevSink(cfg, environ=env, client=client, batch_max=BATCH_MAX, poll_s=COMPLETE_S, sleep=sleep,
                                  clock=clock)

        def run_sync(s: PD.PastDevSink, **kw: Any) -> sync.SyncReport:
            return sync.run(s, conn, budget_s=SYNC_BUDGET_S, environ=env, clock=clock, **kw)

        def step(name: str, fn: Callable[[], tuple[bool, str, dict[str, Any]]], *, local: bool = False) -> bool:
            why = halted()
            if why and not local:
                report["steps"].append({"name": name, "ok": False, "seconds": 0.0, "detail": f"skipped: {why}"})
                return False
            if client is not None:
                client.step = name
            s0 = clock()
            try:
                ok, detail, data = fn()
            except Stop as exc:
                ok, detail, data = False, str(exc), {}
            except (http.HttpError, psycopg.Error, ValueError, KeyError, RuntimeError, TypeError) as exc:
                ok, detail, data = False, f"{type(exc).__name__}: {str(exc)[:300]}", {}
            conn.rollback()          # every write commits itself; this ends any read (or failed) transaction
            if name != "preflight" and guard.baseline is not None and not guard.stopped:
                try:
                    usage_credits()
                    data = {**data, "credits_used_after": round(guard.used, 3)}
                except (http.HttpError, ValueError) as exc:
                    ok, detail = False, f"{detail}; usage: {type(exc).__name__}: {str(exc)[:120]}"
            if guard.stopped and not state.get("stop_noted"):    # the step during which the run stopped fails
                state["stop_noted"] = True
                ok, detail = False, detail if guard.stopped in detail else f"{detail}; {guard.stopped}"
            report["steps"].append({"name": name, "ok": ok, "seconds": round(clock() - s0, 3), "detail": detail,
                                    **({"data": data} if data else {})})
            return ok

        # ------------------------------------------------------------ preflight
        def preflight() -> tuple[bool, str, dict[str, Any]]:
            problems: list[str] = []
            sent = synth._sentinel(conn)
            if not sent or sent.get("anchor") != gold.get("anchor"):
                problems.append(f"the database was not seeded with this gold (sentinel anchor "
                                f"{(sent or {}).get('anchor')}, gold {gold.get('anchor')})")
            have = synth.db_counts(conn)
            if have != gold.get("counts"):
                problems.append(f"row counts differ from gold: {have} vs {gold.get('counts')}")
            links = int(conn.execute("select count(*) from rsi_evidence where system = 'pastdev'").fetchone()[0])
            cursor = bool(sync.load(conn, "pastdev").get("watermarks"))
            if links or cursor:
                problems.append(f"the database already holds past.dev state ({links} links, cursor {cursor}): "
                                "seed a fresh one")
            data: dict[str, Any] = {"send": len(send), "refuse_team": len(team), "refuse_private": len(private),
                                    "links": links, "cursor": cursor}
            if client is None:
                problems.append(f"past.dev is not configured ({PD.KEY_ENV})")
                return False, "; ".join(problems), data
            u = client.usage()
            p = _project(u)
            data["usage"] = {k: p.get(k) for k in ("slug", "credits", "ingestions", "recalls", "creditCap",
                                                   "creditCapLeft", "ingestionCap", "ingestionCapLeft")}
            bal = u.get("balance") if isinstance(u, Mapping) else None
            data["usage"]["balance_left"] = bal.get("left") if isinstance(bal, Mapping) else None
            caps = []
            for name_, limit in (("creditCap", CREDIT_CAP_MAX), ("ingestionCap", INGESTION_CAP_MAX)):
                v = _num(p.get(name_))
                if v is None or v > limit:
                    caps.append(f"project.{name_} is {p.get(name_)!r}: it must be set and <= {limit}")
            if caps:
                state["refused"] = "; ".join(caps)
                return False, "REFUSED (owner's stop rule): " + "; ".join(caps) + "; nothing was sent", data
            credits = _num(p.get("credits"))
            if credits is None:
                problems.append("GET /usage has no project.credits: the budget guard cannot measure the run")
            prior = _num(p.get("ingestions")) or 0
            if prior and not allow_prior_use:
                problems.append(f"the project already has {prior:g} ingestion(s) this month: the synthetic ids are "
                                "fixed and past.dev refuses a deleted id, so use a fresh project (or --allow-prior-use)")
            left = _num(p.get("ingestionCapLeft"))
            if left is not None and left < len(send) + 1:
                problems.append(f"ingestionCapLeft {left:g} < {len(send) + 1} data points this run needs")
            if not problems:
                guard.baseline = credits
            return not problems, "; ".join(problems) or "ok", data

        if not step("preflight", preflight):
            return finish()

        # ------------------------------------------------------------ plan (check 1)
        def plan() -> tuple[bool, str, dict[str, Any]]:
            s = sink()
            conn.rollback()
            out = sync.plan(s, conn, environ=env)            # read-only; health is not called, nothing is sent
            refused = {ref: list(reasons) for ref, reasons in s.refused}
            want = {**{r: ["team"] for r in team}, **{r: ["private"] for r in private}}
            items = _planned(s, conn)
            ids = sorted(PD.unwire(it["id"]) for it in items)
            local = PD.local_credits(items)
            masked = out.get("masked") or {}
            problems = []
            if ids != sorted(send):
                problems.append(f"send set differs from gold: extra {sorted(set(ids) - set(send))}, "
                                f"missing {sorted(set(send) - set(ids))}")
            if refused != want:
                problems.append(f"refused differs from gold: {refused} vs {want}")
            if out.get("rows") != len(send) or out.get("cost") != local or out.get("refused") != len(want):
                problems.append(f"plan rows/cost/refused {out.get('rows')}/{out.get('cost')}/{out.get('refused')}, "
                                f"expected {len(send)}/{local}/{len(want)}")
            if (masked.get("team"), masked.get("private")) != (len(team), len(private)):
                problems.append(f"masked team/private {masked.get('team')}/{masked.get('private')}, "
                                f"expected {len(team)}/{len(private)}")
            data: dict[str, Any] = {"plan": {k: out.get(k) for k in ("rows", "requests", "cost", "refused", "retired")},
                                    "masked": {k: masked.get(k) for k in ("team", "private")}, "send": len(ids),
                                    "local_credits": local, "bytes": sum(len(it["content"].encode()) for it in items)}
            if problems:                                     # the send set is wrong: nothing may leave
                state["halt"] = "plan failed: nothing was sent"
                return False, "; ".join(problems) + "; the estimate was not sent", data
            est = client.estimate(items)                     # free; only the planned sanitized set
            data["estimate"] = {k: est.get(k) for k in ("dataPoints", "bytes", "credits", "fits")}
            if _num(est.get("credits")) != local:
                problems.append(f"estimate {est.get('credits')} credits, local rule {local}")
            if "dataPoints" in est and est.get("dataPoints") != len(items):
                problems.append(f"estimate {est.get('dataPoints')} data points, planned {len(items)}")
            if est.get("fits") is False:
                problems.append("estimate: the send does not fit the balance or the caps")
            if problems:
                state["halt"] = "plan failed: nothing was ingested"
                return False, "; ".join(problems), data
            state["plan_credits"] = local
            return True, f"{len(ids)} to send, {len(refused)} refused (team {len(team)}, private {len(private)}); " \
                         f"estimate {local} credits = local rule", data

        # ------------------------------------------------------------ sync_run (check 2)
        def sync_run() -> tuple[bool, str, dict[str, Any]]:
            assert client is not None
            n0 = len(client.calls)
            rep = run_sync(sink())
            calls = client.since(n0, "POST", "/ingest/batch")
            receipts: dict[str, str] = {}
            mism: list[str] = []
            for c in calls:
                req, res = _json(c.body) or {}, _json(c.response)
                if not isinstance(res, Mapping):
                    continue
                by_ord = {it.get("ordinal"): it for it in res.get("items") or [] if isinstance(it, Mapping)}
                for i, it in enumerate(req.get("items") or []):
                    got = (by_ord.get(i) or {}).get("sourceId")
                    if not (isinstance(got, str) and got.encode("utf-8") == it["id"].encode("utf-8")):
                        mism.append(f"{it['id']} -> {got!r}")
                    receipts[PD.unwire(it["id"])] = str(res.get("ingestionId") or "")
            iids = {i for i in receipts.values() if i}
            state["iids"] = iids
            secs = {i: client.completed[i] - client.accepted[i] for i in iids
                    if i in client.completed and i in client.accepted}
            slow = sorted(i for i in iids if i not in secs or secs[i] > COMPLETE_S)
            links = PD._links(conn)
            bad_links = [ref for ref in send if (links.get(ref) or {}).get("status") != PD.COMPLETED
                         or (links.get(ref) or {}).get("ingestion_id") != receipts.get(ref)]
            statuses = Counter(c.status for c in calls)
            st = client.last_status
            if iids:                                          # free polls until the project settles
                deadline = clock() + SETTLE_S
                while not st.get("settled") and clock() < deadline and not guard.stopped:
                    sleep(SETTLE_POLL_S)
                    st = client.ingestion(sorted(iids)[-1])
            failed_points = (st.get("readiness") or {}).get("failed") if isinstance(st.get("readiness"), Mapping) else None
            ok = (rep.status == "ok" and rep.failed == 0 and rep.dead_letters == 0 and rep.rows == len(send)
                  and len(calls) == len(send) and statuses == Counter({202: len(calls)}) and not mism and not slow
                  and not bad_links and len(links) == len(send) and not failed_points)
            detail = (f"{rep.line()}; receipts {dict(statuses)}; completed {len(secs)}/{len(iids)} within "
                      f"{COMPLETE_S:.0f}s; links {len(links)}; settled {bool(st.get('settled'))}")
            if mism:
                detail += f"; sourceId differs: {mism[:3]}"
            if bad_links:
                detail += f"; links without a completed ingestionId: {bad_links[:3]}"
            if failed_points:
                detail += f"; readiness.failed {failed_points}"
            return ok, detail, {"status": rep.status, "rows": rep.rows, "ingestions": len(iids),
                                "receipts": {str(k): v for k, v in statuses.items()}, "links": len(links),
                                "completion_s": _lat(list(secs.values())), "slow": slow[:10],
                                "settled": bool(st.get("settled")), "readiness": st.get("readiness")}

        send_set = set(send)

        def queries() -> list[tuple[str, list[str]]]:
            if "queries" not in state:
                state["queries"] = _queries(gold)            # a gold that disagrees with synth.py fails the step
            return state["queries"]

        def retriever() -> PD.PastDevRetriever:
            return PD.PastDevRetriever(lambda: connect(db_url), cfg, environ=env, client=client)

        def recall_raw(r: PD.PastDevRetriever, q: str) -> tuple[E.QueryResult, Call | None]:
            assert client is not None
            n = len(client.calls)
            res = E.run_query(r, "recall", {"query": q, "limit": str(RECALL_LIMIT)})
            raw = next((c for c in reversed(client.since(n, "POST", "/recall")) if c.status == 200), None)
            return res, raw

        # ------------------------------------------------------------ recall (check 3)
        def recall() -> tuple[bool, str, dict[str, Any]]:
            r = retriever()
            out, failed = [], 0
            for q, want in queries():
                res, raw = recall_raw(r, q)
                sids = _source_ids(raw)
                foreign = [s for s in sids if s not in send_set]
                stale = [s for s in sids if s in send_set and not E.exists(conn, s)]
                mapped = [ev.subject_refs[0] for ev in res.refs]
                good = res.status == "ok" and raw is not None and bool(sids) and not foreign and not stale
                failed += not good
                out.append({"query": q, "status": res.status, "detail": res.detail or None, "source_ids": len(sids),
                            "refs": len(mapped), "foreign": foreign[:5], "stale": stale[:5],
                            "hit_at_5": any(w in mapped[:5] for w in want), "ok": good})
            hits = sum(o["hit_at_5"] for o in out)
            return not failed, f"{len(out) - failed}/{len(out)} queries ok (only gold refs, all re-read); " \
                               f"expected ref in top 5: {hits}/{len(out)}", {"queries": out}

        # ------------------------------------------------------------ duplicates (check 4)
        def duplicates() -> tuple[bool, str, dict[str, Any]]:
            assert client is not None
            problems: list[str] = []
            # a. a second run sends nothing
            n0 = len(client.calls)
            rep = run_sync(sink())
            sends = client.since(n0, "POST", "/ingest/batch") + client.since(n0, "POST", "/ingest/estimate")
            if rep.status != "ok" or rep.rows or sends:
                problems.append(f"second run: {rep.line()}, {len(sends)} send(s)")
            # b. --replay: the same keys, the stored receipts, no credits
            u0 = usage_credits()
            n1 = len(client.calls)
            rep_r = run_sync(sink(), replay=True)
            replays = client.since(n1, "POST", "/ingest/batch")
            new = []
            for c in replays:
                res = _json(c.response)
                if not (isinstance(res, Mapping) and (res.get("unchanged") is True
                                                      or str(res.get("ingestionId")) in state["iids"])):
                    new.append(c.status)
            u1 = usage_wait(u0, exactly(0.0))
            if rep_r.status != "ok" or len(replays) != len(send) or new or abs(u1 - u0) > EPS:
                problems.append(f"replay: {rep_r.line()}, {len(replays)} request(s), {len(new)} new ingestion(s), "
                                f"{u1 - u0:+.1f} credits")
            # c. one changed record: sent alone, charged exactly its estimate
            eid, ref = _changed_ref(gold)
            conn.execute("update edits set summary = %s where id = %s", (CHANGED_SUMMARY, eid))
            conn.commit()
            items = _planned(sink(), conn)
            change: dict[str, Any] = {"ref": ref, "planned": [PD.unwire(it["id"]) for it in items]}
            if change["planned"] != [ref]:
                problems.append(f"changed record: planned {change['planned']}, expected [{ref}]; not sent")
            else:
                local = PD.local_credits(items)
                est = client.estimate(items)
                u2 = usage_credits()
                n2 = len(client.calls)
                rep_c = run_sync(sink())
                sent = client.since(n2, "POST", "/ingest/batch")
                u3 = usage_wait(u2, exactly(float(local)))
                link = PD._links(conn).get(ref) or {}
                change.update({"local_credits": local, "estimate": est.get("credits"), "credits": round(u3 - u2, 3),
                               "requests": len(sent), "status": link.get("status")})
                state["change_credits"] = local
                if (_num(est.get("credits")) != local or rep_c.rows != 1 or len(sent) != 1
                        or abs((u3 - u2) - local) > EPS or link.get("status") != PD.COMPLETED
                        or link.get("sha256") != PD._sha(items[0]["content"])):
                    problems.append(f"changed record: estimate {est.get('credits')}, local {local}, charged "
                                    f"{u3 - u2:+.1f}, {len(sent)} request(s), {rep_c.line()}, link {link.get('status')}")
            data = {"second_run": {"rows": rep.rows, "sends": len(sends)},
                    "replay": {"requests": len(replays), "new_ingestions": len(new), "credits": round(u1 - u0, 3)},
                    "change": change}
            return not problems, "; ".join(problems) or \
                f"second run 0 sends; replay {len(replays)} stored receipts, 0 credits; changed record " \
                f"{change.get('credits')} credits = estimate", data

        # ------------------------------------------------------------ failure_path
        def failure_path() -> tuple[bool, str, dict[str, Any]]:
            problems: list[str] = []
            if cfg is not None and cfg.key == DUMMY_KEY:
                raise ValueError("the configured key is the runner's dummy key")
            before = sync.load(conn, "pastdev").get("watermarks")
            bad = PD.PastDevSink(PD.Config(DUMMY_KEY, bad_url), environ=env, sleep=sleep, clock=clock)
            rep = sync.run(bad, conn, budget_s=60.0, environ=env, clock=clock)
            after = sync.load(conn, "pastdev").get("watermarks")
            if not (rep.status == "unavailable" and before == after and before):
                problems.append(f"bad host: status={rep.status}; cursor {'unchanged' if before == after else 'MOVED'}")
            snap = PD._links(conn)
            slept: list[float] = []
            with _CapServer() as srv:
                fake = PD.Client(PD.Config(DUMMY_KEY, srv.url), sleep=slept.append)
                cap = sync.run(_CapCheckSink(PD.Config(DUMMY_KEY, srv.url), environ=env, client=fake, batch_max=BATCH_MAX,
                                             poll_s=0.0, sleep=slept.append, clock=clock),
                               conn, replay=True, budget_s=60.0, environ=env, clock=clock)
                posts = [h for h in srv.hits if h[0] == "POST"]
            conn.execute("delete from rsi_checkpoints where job = %s", (sync.job(_CapCheckSink.name),))
            conn.commit()
            same = PD._links(conn) == snap
            if not (cap.status == "capped" and "project-cap-reached" in cap.detail and len(posts) == 1 and not slept
                    and same):
                problems.append(f"cap: status={cap.status}, {len(posts)} send(s), backoff {slept}, links "
                                f"{'unchanged' if same else 'CHANGED'}")
            return not problems, "; ".join(problems) or \
                f"bad host: {rep.status} ({rep.detail}), cursor unchanged; 429 project-cap-reached: capped after " \
                f"{len(posts)} request, no retry", \
                {"bad_host": {"status": rep.status, "detail": rep.detail[:120], "key": "dummy"},
                 "cap": {"status": cap.status, "sends": len(posts), "backoff": slept, "links_unchanged": same}}

        # ------------------------------------------------------------ deletion (check 5)
        def deletion() -> tuple[bool, str, dict[str, Any]]:
            assert client is not None
            problems: list[str] = []
            live = sorted(sid for sid, m in PD._links(conn).items() if not m.get("deleted"))
            n0 = len(client.calls)
            out = PD.delete_all(conn, client)
            statuses = [c.status for c in client.since(n0, "POST", "/data-points/delete")]
            if out["deleted"] != len(live) or sorted(live) != sorted(send) or out["gone"] or not statuses \
                    or any(s != 202 for s in statuses):
                problems.append(f"delete_all {out}, statuses {statuses}, live before {len(live)}")
            r = retriever()
            gone, still, rounds, errors = set(live), set(), 0, []
            for i in range(DELETE_RECALL_ROUNDS):
                if i:
                    sleep(DELETE_RECALL_WAIT_S)
                rounds, still, errors = i + 1, set(), []
                for q, _ in queries():
                    res, raw = recall_raw(r, q)
                    if res.status != "ok" or raw is None:
                        errors.append(f"{res.status} {res.detail}")
                    still |= set(_source_ids(raw)) & gone
                if not still and not errors:
                    break
            if still or errors:
                problems.append(f"recall after delete: still returns {sorted(still)[:3]}, errors {errors[:2]}")
            links = PD._links(conn)
            unmarked = [sid for sid in live if not (links.get(sid) or {}).get("deleted")]
            n1 = len(client.calls)
            s = sink()
            rep = run_sync(s)
            sends = client.since(n1, "POST", "/ingest/batch")
            if unmarked or rep.status != "ok" or rep.rows or sends or sorted(s.retired) != live or rep.dead_letters:
                problems.append(f"after delete: {len(unmarked)} links not marked, {rep.line()}, {len(sends)} send(s), "
                                f"retired {len(s.retired)}/{len(live)}")
            return not problems, "; ".join(problems) or \
                f"{out['deleted']} deleted in {out['calls']} call(s) (202), gone from recall after {rounds} round(s), " \
                f"retired {len(s.retired)}, a later run sent nothing", \
                {"delete_all": out, "statuses": statuses, "recall_rounds": rounds, "still_returned": sorted(still)[:10],
                 "retired": len(s.retired), "later_run": {"rows": rep.rows, "sends": len(sends),
                                                           "dead_letters": rep.dead_letters}}

        # ------------------------------------------------------------ metrics (check 6)
        def metrics() -> tuple[bool, str, dict[str, Any]]:
            assert client is not None
            ingest = [c.seconds for c in client.calls if c.method == "POST" and c.path == "/ingest/batch"
                      and c.status in (200, 202)]
            recalls = [c.seconds for c in client.calls if c.method == "POST" and c.path == "/recall" and c.status == 200]
            done = [client.completed[i] - client.accepted[i] for i in state["iids"]
                    if i in client.completed and i in client.accepted]
            est = state["plan_credits"] + state["change_credits"] + RECALL_CREDITS * len(recalls)

            def close(delta: float) -> bool:
                return est > 0 and abs(delta - est) <= TOLERANCE * est + EPS

            usage_wait(guard.baseline or 0.0, close)
            used = guard.used
            within = close(used)
            ok = within and used <= max_credits + EPS and bool(ingest) and bool(recalls)
            data = {"ingest_s": _lat(ingest), "completion_s": _lat(done), "recall_s": _lat(recalls),
                    "credits": {"used": round(used, 3), "local_estimate": round(est, 3),
                                "plan": state["plan_credits"], "change": state["change_credits"],
                                "recalls": len(recalls), "within_10pct": within}}
            return ok, (f"credits used {used:.1f} vs local estimate {est:.1f} ({'within' if within else 'OUTSIDE'} "
                        f"10%); ingest p50/p95 {data['ingest_s']['p50']}/{data['ingest_s']['p95']}s, recall p50/p95 "
                        f"{data['recall_s']['p50']}/{data['recall_s']['p95']}s"), data

        # ------------------------------------------------------------ leak_scan (check 7)
        def leak_scan() -> tuple[bool, str, dict[str, Any]]:
            problems: list[str] = []
            calls = client.calls if client else []
            a_hits: list[str] = []
            a_allowed = 0
            for c in calls:
                h, n = needles.scan(f"{c.path}\n{c.body}")
                a_hits += h
                a_allowed += n
            c_hits: list[str] = []
            c_allowed = 0
            answers = [c.response for c in calls if c.method == "POST" and c.path == "/recall" and c.response]
            for t in answers:
                h, n = needles.scan(t)
                c_hits += h
                c_allowed += n
            blocked = list(client.blocked) if client else []
            data: dict[str, Any] = {
                "bodies": {"scanned": sum(bool(c.body) for c in calls), "hits": sorted(set(a_hits))[:10],
                           "decision_text_markers": a_allowed, "blocked_by_tripwire": blocked[:5]},
                "recall_answers": {"scanned": len(answers), "hits": sorted(set(c_hits))[:10],
                                   "decision_text_markers": c_allowed}}
            if a_hits or blocked:
                problems.append(f"request bodies: {len(a_hits)} hit(s), {len(blocked)} blocked")
            if c_hits:
                problems.append(f"recall answers: {len(c_hits)} hit(s)")
            if not any(c.body for c in calls):
                problems.append("no request body was scanned")
            if halted() or client is None:
                data["request_log"] = {"read": False}
                problems.append(f"request log not read: {halted() or 'not configured'}")
            else:
                keys: set[str] = set()
                for c in calls:
                    body = _json(c.body) if c.method == "POST" and c.path == "/ingest/batch" else None
                    if isinstance(body, Mapping) and body.get("idempotencyKey"):
                        keys.add(str(body["idempotencyKey"]))
                page = client.request_log()
                entries, shape = _log_entries(_json(page.text()))
                ids = [i for e in entries if (i := _entry_id(e))][:REQUEST_LOG_MAX]
                texts = [client.request_entry(i).text() for i in ids]
                b_hits: list[str] = []
                b_allowed = 0
                for t in [page.text(), *texts]:
                    h, n = needles.scan(t)
                    b_hits += h
                    b_allowed += n
                found = sum(any(k in t for k in keys) for t in texts)
                covered = sum(any(k in t for t in texts) for k in keys)
                data["request_log"] = {"read": True, "entries": len(entries), "payloads_read": len(texts),
                                       "page_keys": shape, "ingest_payloads_found": found,
                                       "ingest_keys_covered": f"{covered}/{len(keys)}", "hits": sorted(set(b_hits))[:10],
                                       "decision_text_markers": b_allowed}
                if b_hits:
                    problems.append(f"stored payloads: {len(b_hits)} hit(s)")
                if not found:
                    problems.append(f"no stored ingest payload found in {len(texts)} request-log entries "
                                    f"(page keys {shape})")
            return not problems, "; ".join(problems) or \
                (f"0 hits in {data['bodies']['scanned']} bodies, {data['request_log'].get('payloads_read')} stored "
                 f"payloads ({data['request_log'].get('ingest_keys_covered')} ingest keys found) and "
                 f"{len(answers)} recall answers; decision_text markers seen (sent by design): {a_allowed}"), data

        step("plan", plan)
        for name, fn in (("sync_run", sync_run), ("recall", recall), ("duplicates", duplicates),
                         ("failure_path", failure_path), ("deletion", deletion), ("metrics", metrics)):
            step(name, fn)
        step("leak_scan", leak_scan, local=True)
    return finish()


# ---------------------------------------------------------------- dry run and CLI


def dry_run(db_url: str, gold: Mapping[str, Any], gold_path: Path, *,
            environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The plan only: no database connection, no past.dev request (the env is read, never printed)."""
    cfg = _config(environ)
    try:
        synth.check_local(db_url)
        target = "ok"
    except synth.UnsafeTarget as exc:
        target = f"REFUSED: {exc}"
    pd = gold["pastdev"]
    strict = sum(1 for m in gold.get("markers", []) if m.get("policy") != synth.DECISION)
    upper = len(pd["send"]) * PD.local_credits([{"content": "x" * 1536}]) \
        + PD.local_credits([{"content": "x" * 1536}]) \
        + RECALL_CREDITS * len(RECALL_QUERIES) * (1 + DELETE_RECALL_ROUNDS)
    return {"c3": "pastdev", "dry_run": True, "synthetic": True, "db_host": synth.db_host(db_url), "db_target": target,
            "gold": str(gold_path), "anchor": gold.get("anchor"), "send": len(pd["send"]),
            "refuse_team": len(pd["refuse_team"]), "refuse_private": len(pd["refuse_private"]),
            "markers": {"strict": strict, "decision_text": len(gold.get("markers", [])) - strict},
            "pastdev": {"host": _url_host(cfg.base_url if cfg else BASE_URL), "key_set": cfg is not None},
            "limits": {"credit_cap_max": CREDIT_CAP_MAX, "ingestion_cap_max": INGESTION_CAP_MAX,
                       "run_credits_max": MAX_RUN_CREDITS, "batch_max": BATCH_MAX},
            "credits_upper_bound": round(upper, 1), "queries": [q for q, _ in RECALL_QUERIES],
            "steps": [{"name": s, "does": PLAN[s]} for s in STEPS]}


def _lines(report: Mapping[str, Any]) -> list[str]:
    if report.get("dry_run"):
        out = [f"C3 dry run (SYNTHETIC dataset, anchor {report['anchor']}): nothing will be touched",
               f"  database host {report['db_host']}: {report['db_target']}",
               f"  past.dev host {report['pastdev']['host']}; key set {report['pastdev']['key_set']}",
               f"  gold {report['gold']}: {report['send']} to send, {report['refuse_team']} team + "
               f"{report['refuse_private']} private refused, {report['markers']['strict']} strict markers",
               f"  caps required: credit <= {report['limits']['credit_cap_max']}, ingestion <= "
               f"{report['limits']['ingestion_cap_max']}; run budget {report['limits']['run_credits_max']} credits; "
               f"at most {report['credits_upper_bound']} credits by the local rule"]
        return out + [f"  {i + 1:2d}. {s['name']}: {s['does']}" for i, s in enumerate(report["steps"])]
    verdict = "REFUSED" if report.get("refused") else "PASS" if report["ok"] else "FAIL"
    out = [f"C3 past.dev on SYNTHETIC data (anchor {report['anchor']}): {verdict} in {report.get('seconds', 0):.1f}s; "
           f"credits used {report.get('credits', {}).get('used')}"]
    out += [f"  {'ok  ' if s['ok'] else 'FAIL'} {s['name']:<13} {s['seconds']:7.2f}s  {s['detail']}"
            for s in report["steps"]]
    if report.get("stopped"):
        out.append(f"  STOPPED: {report['stopped']} (run pastdev.delete_all by hand if anything was sent)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C3: past.dev verification on the synthetic dataset")
    ap.add_argument("--db-url", required=True, help="the disposable local Postgres `amem synth seed` filled")
    ap.add_argument("--gold", required=True, type=Path, help="gold.json from `amem synth seed`")
    ap.add_argument("--out", type=Path, default=None, help="report path (default: c3-report.json next to gold)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")
    ap.add_argument("--allow-prior-use", action="store_true",
                    help="run although the project already has ingestions this month")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        gold = json.loads(args.gold.read_text())
        if not isinstance(gold.get("pastdev"), Mapping):
            raise ValueError("no pastdev section")
    except (OSError, ValueError, AttributeError) as exc:
        print(f"c3: cannot read gold: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.dry_run:
        rep = dry_run(args.db_url, gold, args.gold)
        print(json.dumps(rep, indent=1, sort_keys=True) if args.json else "\n".join(_lines(rep)))
        return 0 if rep["db_target"] == "ok" else 2
    try:
        synth.check_local(args.db_url)
    except synth.UnsafeTarget as exc:
        print(f"c3: {exc}", file=sys.stderr)
        return 2
    rep = run_c3(args.db_url, gold, allow_prior_use=args.allow_prior_use)
    out = args.out or args.gold.with_name("c3-report.json")
    out.write_text(json.dumps(rep, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(rep, indent=1, sort_keys=True, default=str) if args.json else "\n".join(_lines(rep) + [f"report: {out}"]))
    return 2 if rep.get("refused") else 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
