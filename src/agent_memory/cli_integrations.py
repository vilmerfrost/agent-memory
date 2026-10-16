"""`amem sync ...` and `amem evidence query ...` (Mission 004A; blueprint Appendix 2).

    amem sync run --sink clickhouse|langfuse|pastdev [--budget S] [--replay] [--json]
    amem sync status [--json]
    amem sync verify --sink clickhouse [--days N] [--json]
    amem evidence query --source sb|ch|lf|pd <named-query> [--param k=v ...] [--json]

Not wired into any routine. An adapter without its settings (URL, keys) answers `unavailable`, so these
commands cannot send anything before activation (docs/integrations/activation.md).

Adapters load lazily (the factories below import on use), so one missing or broken adapter never breaks the
others. Each factory builds the adapter from its env settings; unset settings mean `unavailable`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg

from agent_memory import store
from agent_memory.integrations import evidence as E, sync

SOURCE_ADAPTER = {"ch": "clickhouse", "lf": "langfuse", "pd": "pastdev"}


def sink_for(name: str) -> sync.Sink:
    if name == "clickhouse":
        from agent_memory.integrations import clickhouse
        return clickhouse.ClickHouseSink()
    if name == "langfuse":
        from agent_memory.integrations import langfuse
        return langfuse.LangfuseSink(langfuse.LangfuseClient.from_env(), os.environ.get("RSI_SANDBOX_DIR") or None)
    if name == "pastdev":
        from agent_memory.integrations import pastdev
        return pastdev.sink_from_env()
    raise KeyError(name)


def retriever_for(name: str) -> E.Retriever:
    if name == "clickhouse":
        from agent_memory.integrations import clickhouse
        return clickhouse.ClickHouseRetriever()
    if name == "langfuse":
        from agent_memory.integrations import langfuse
        return langfuse.LangfuseRetriever(langfuse.LangfuseClient.from_env())
    if name == "pastdev":
        from agent_memory.integrations import pastdev
        return pastdev.retriever_from_env()
    raise KeyError(name)


def _out(args: argparse.Namespace, obj: Any, line: str, code: int = 0) -> int:
    print(json.dumps(obj, sort_keys=True, default=str) if getattr(args, "json", False) else line)
    return code


def cmd_sync_run(args: argparse.Namespace) -> int:
    try:
        sink = sink_for(args.sink)
    except (ImportError, AttributeError, KeyError) as exc:
        print(f"amem: sync run: adapter {args.sink} not available: {type(exc).__name__}", file=sys.stderr)
        return 2
    try:
        with store.connect() as conn:
            rep = sync.run(sink, conn, budget_s=args.budget, replay=args.replay)
    except psycopg.Error as exc:
        print(f"amem: sync run failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return _out(args, rep.__dict__, rep.line(), 1 if rep.status == "failed" else 0)


def cmd_sync_status(args: argparse.Namespace) -> int:
    try:
        with store.connect() as conn:
            rows = sync.status(conn)
    except psycopg.Error as exc:
        print(f"amem: sync status failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    lines = [f"{r['sink']}: {r['status']} last_ok={r['last_ok_at'] or '-'} tables={len(r['tables'])}"
             + (f" dead={','.join(r['dead'])}" if r["dead"] else "") + (f" failing={','.join(r['failing'])}" if r["failing"] else "")
             for r in rows] or ["no sync has run"]
    return _out(args, rows, "\n".join(lines))


def cmd_sync_verify(args: argparse.Namespace) -> int:
    try:
        from agent_memory.integrations import clickhouse
    except ImportError:
        print(f"amem: sync verify: no verify for {args.sink}", file=sys.stderr)
        return 2
    from agent_memory.integrations import http
    if not clickhouse.ClickHouseSink().health().ok:
        print(f"amem: sync verify: {args.sink} unavailable", file=sys.stderr)
        return 1
    try:
        with store.connect() as conn:
            rep = clickhouse.verify(conn, args.days)
    except (psycopg.Error, http.HttpError, ValueError) as exc:
        print(f"amem: sync verify failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    obj = rep.to_json()
    return _out(args, obj, f"sync verify {args.sink}: parity={obj.get('parity')} days={args.days} "
                           f"mismatches={len(rep.mismatches())}", 0 if rep.parity else 1)


def _params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep or not k:
            raise ValueError(f"--param takes k=v, got {p!r}")
        out[k] = v
    return out


def cmd_evidence_query(args: argparse.Namespace) -> int:
    try:
        params = _params(args.param)
    except ValueError as exc:
        print(f"amem: evidence query: {exc}", file=sys.stderr)
        return 2
    if args.source == "sb":
        r: Any = E.SupabaseRetriever(store.connect)
    else:
        try:
            r = retriever_for(SOURCE_ADAPTER[args.source])
        except (ImportError, AttributeError, KeyError) as exc:
            print(f"amem: evidence query: source {args.source} not available: {type(exc).__name__}", file=sys.stderr)
            return 2
    res = E.run_query(r, args.query, params)
    lines = [f"{res.source}: {res.status}" + (f" ({res.detail})" if res.detail else "") + f" {len(res.refs)} ref(s)"]
    lines += [f"  {e.ref} {json.dumps(e.metrics, sort_keys=True, default=str)[:200]}" for e in res.refs]
    code = 0 if res.status in ("ok", "unavailable") else 2 if res.status in ("bad-params", "unknown-query") else 1
    return _out(args, res.to_json(), "\n".join(lines), code)


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("sync", help="push Supabase evidence to ClickHouse / Langfuse / past.dev (docs/integrations/)")
    s = p.add_subparsers(dest="sync_command", required=True)
    q = s.add_parser("run", help="one bounded sync pass for one sink (unconfigured = unavailable, exit 0)")
    q.add_argument("--sink", required=True, choices=sync.SINKS)
    q.add_argument("--budget", type=float, default=60.0, help="seconds; stops between batches")
    q.add_argument("--replay", action="store_true", help="start from no cursor (backfill; pushes are idempotent)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_sync_run)
    q = s.add_parser("status", help="each sink's cursor, last success, failing and dead tables")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_sync_status)
    q = s.add_parser("verify", help="per-day parity between Supabase and the sink")
    q.add_argument("--sink", required=True, choices=["clickhouse"])
    q.add_argument("--days", type=int, default=30)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_sync_verify)

    p = sub.add_parser("evidence", help="the evidence facade (named queries only)")
    e = p.add_subparsers(dest="evidence_command", required=True)
    q = e.add_parser("query", help="run one named query against one source")
    q.add_argument("--source", required=True, choices=["sb", "ch", "lf", "pd"])
    q.add_argument("query")
    q.add_argument("--param", action="append", default=[], metavar="K=V")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_evidence_query)
