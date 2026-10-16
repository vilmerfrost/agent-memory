"""`amem sync ...` and `amem evidence query ...` (Mission 004A; blueprint Appendix 2).

    amem sync run --sink clickhouse|langfuse|pastdev [--budget S] [--replay] [--tables T1,T2] [--since DATE] [--json]
    amem sync status [--json]
    amem sync verify --sink clickhouse [--days N] [--json]
    amem sync schema --sink clickhouse [--cloud] [--db NAME] [--part all|admin|writer] [--hash-from-env]
                                                                (prints SQL for the owner; sends nothing)
    amem sync grants --sink clickhouse [--json]   (as the writer: probes read/insert allowed, DDL/users denied)
    amem sync plan --sink clickhouse|langfuse|pastdev [--replay] [--tables T1,T2] [--since DATE] [--json]
                                                                (dry run; read-only, sends nothing)
    amem evidence query --source sb|ch|lf|pd <named-query> [--param k=v ...] [--json]
    amem evidence ask <question> [--param k=v ...] [--record] [--json]     (questions.py: the six questions)
    amem evidence questions
    amem evidence record-ncm <results.json> [--dry-run] [--json]          (one NCM research run -> rsi_evidence)

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
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from agent_memory import store
from agent_memory.integrations import evidence as E, sync

SOURCE_ADAPTER = {"ch": "clickhouse", "lf": "langfuse", "pd": "pastdev"}


def sink_for(name: str, tables: list[str] | None = None, since: datetime | None = None) -> sync.Sink:
    """The sink, narrowed to `tables` and to rows since `since` when given (Mission 004C, F4). Raises
    ValueError for a table the sink does not have."""
    if name == "clickhouse":
        from agent_memory.integrations import clickhouse
        return clickhouse.ClickHouseSink(tables=clickhouse.tables_named(tables) if tables else clickhouse.TABLES,
                                         since=since)
    if name == "langfuse":
        from agent_memory.integrations import langfuse
        if tables and set(tables) != {langfuse.TABLE}:
            raise ValueError(f"unknown table(s) for langfuse: {', '.join(sorted(set(tables) - {langfuse.TABLE}))} "
                             f"(known: {langfuse.TABLE})")
        return langfuse.LangfuseSink(langfuse.LangfuseClient.from_env(), os.environ.get("RSI_SANDBOX_DIR") or None,
                                     since=since)
    if name == "pastdev":
        from agent_memory.integrations import pastdev
        return pastdev.PastDevSink(tables=tables, since=since)
    raise KeyError(name)


def _tables_arg(text: str) -> list[str]:
    names = [t.strip() for t in text.split(",") if t.strip()]
    if not names:
        raise argparse.ArgumentTypeError("--tables takes table names separated by commas")
    return names


def _since_arg(text: str) -> datetime:
    try:
        d = date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--since takes a date YYYY-MM-DD, got {text!r}") from None
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _scoped_sink(args: argparse.Namespace, cmd: str) -> sync.Sink | None:
    try:
        return sink_for(args.sink, args.tables, args.since)
    except ValueError as exc:
        print(f"amem: sync {cmd}: {exc}", file=sys.stderr)
    except (ImportError, AttributeError, KeyError) as exc:
        print(f"amem: sync {cmd}: adapter {args.sink} not available: {type(exc).__name__}", file=sys.stderr)
    return None


def _scope(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.tables:
        out["tables"] = args.tables
    if args.since:
        out["since"] = args.since.date().isoformat()
    return out


def retriever_for(name: str) -> E.Retriever:
    if name == "clickhouse":
        from agent_memory.integrations import clickhouse
        return clickhouse.ClickHouseRetriever()
    if name == "langfuse":
        from agent_memory.integrations import langfuse
        return langfuse.LangfuseRetriever(langfuse.LangfuseClient.from_env())
    if name == "pastdev":
        from agent_memory.integrations import pastdev
        return pastdev.PastDevRetriever(store.connect)       # re-reads recalled refs from Supabase
    raise KeyError(name)


def _out(args: argparse.Namespace, obj: Any, line: str, code: int = 0) -> int:
    print(json.dumps(obj, sort_keys=True, default=str) if getattr(args, "json", False) else line)
    return code


def cmd_sync_run(args: argparse.Namespace) -> int:
    sink = _scoped_sink(args, "run")
    if sink is None:
        return 2
    try:
        with store.connect() as conn:
            rep = sync.run(sink, conn, budget_s=args.budget, replay=args.replay)
    except psycopg.Error as exc:
        print(f"amem: sync run failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    scope = _scope(args)
    obj = {**rep.__dict__, **({"scope": scope} if scope else {})}
    return _out(args, obj, rep.line(), 1 if rep.status == "failed" else 0)


def cmd_sync_status(args: argparse.Namespace) -> int:
    try:
        with store.connect() as conn:
            rows = sync.status(conn)
    except psycopg.Error as exc:
        print(f"amem: sync status failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    lines = [f"{r['sink']}: {r['status']} last_ok={r['last_ok_at'] or '-'} tables={len(r['tables'])}"
             + (f" dead={','.join(r['dead'])}" if r["dead"] else "") + (f" failing={','.join(r['failing'])}" if r["failing"] else "")
             + (f" dead_letters={r['dead_letters']}" if r.get("dead_letters") else "")
             + (f" today={r['usage'].get('rows', 0)}rows/{r['usage'].get('requests', 0)}req" if r.get("usage") else "")
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


def cmd_sync_plan(args: argparse.Namespace) -> int:
    """What a run (or a --replay backfill) would send. Read-only connection; the provider is never called."""
    sink = _scoped_sink(args, "plan")
    if sink is None:
        return 2
    try:
        with store.connect() as conn:
            out = sync.plan(sink, conn, replay=args.replay)
    except psycopg.Error as exc:
        print(f"amem: sync plan failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    scope = _scope(args)
    if scope:
        out["scope"] = scope
    lines = [f"plan {out['sink']}{' (replay)' if out['replay'] else ''}: {out['rows']} rows in {out['requests']} "
             f"requests, {out['days_at_limit']} day(s) at the client-side limit"
             + (f", ~{out['cost']} credits" if "cost" in out else "")
             + (f" [scope {json.dumps(scope, sort_keys=True)}]" if scope else "")]
    lines += [f"  {t}: {v['rows']} rows, {v['batches']} batches, {v['bytes']} bytes" for t, v in sorted(out["tables"].items())]
    return _out(args, out, "\n".join(lines))


def cmd_sync_schema(args: argparse.Namespace) -> int:
    """The DDL and the least-privilege user, for the owner to run once in the provider's SQL console."""
    from agent_memory.integrations import clickhouse
    try:
        admin = clickhouse.schema_sql(args.db, cloud=args.cloud) if args.part in ("all", "admin") else []
        sha = clickhouse.password_sha256() if args.hash_from_env else None
        writer = clickhouse.writer_sql(args.db, sha256_hex=sha) if args.part in ("all", "writer") else []
    except ValueError as exc:
        print(f"amem: sync schema: {exc}", file=sys.stderr)
        return 2
    print(";\n\n".join(admin + writer) + ";")
    return 0


def cmd_sync_grants(args: argparse.Namespace) -> int:
    """Probe, as the configured writer, that it can read and insert in its database and nothing else."""
    from agent_memory.integrations import clickhouse
    client = clickhouse.Client()
    if client.cfg.problem:
        print(f"amem: sync grants: {client.cfg.problem}", file=sys.stderr)
        return 2
    try:
        client.ping()
    except clickhouse.http.HttpError as exc:
        print(f"amem: sync grants: unavailable ({str(exc)[:120]})", file=sys.stderr)
        return 1
    rows = clickhouse.check_grants(client)
    ok = all(r["ok"] for r in rows)
    lines = [f"grants {client.cfg.user}@{client.cfg.db}: {'ok' if ok else 'FAILED'}"]
    lines += [f"  {'ok  ' if r['ok'] else 'FAIL'} {r['probe']}: expect {r['expect']}, got {r['got']}" for r in rows]
    return _out(args, {"ok": ok, "probes": rows}, "\n".join(lines), 0 if ok else 1)


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


def _retrievers() -> dict[str, E.Retriever | None]:
    out: dict[str, E.Retriever | None] = {"sb": E.SupabaseRetriever(store.connect)}
    for src, name in SOURCE_ADAPTER.items():
        try:
            out[src] = retriever_for(name)
        except (ImportError, AttributeError, KeyError):
            out[src] = None                       # the step answers unavailable
    return out


def cmd_evidence_questions(args: argparse.Namespace) -> int:
    from agent_memory.integrations import questions
    for q in questions.QUESTIONS.values():
        print(f"{q.name}: {q.text}  params: {', '.join(q.params)}")
    return 0


def cmd_evidence_ask(args: argparse.Namespace) -> int:
    from agent_memory.integrations import questions
    try:
        params = _params(args.param)
        questions.QUESTIONS[args.question].check(params)
    except (ValueError, KeyError) as exc:
        print(f"amem: evidence ask: {exc}", file=sys.stderr)
        return 2
    try:
        ans = questions.ask(args.question, params, _retrievers(), store.connect)
        recorded: list[str] = []
        if args.record:
            with store.connect() as conn:
                recorded = questions.record_findings(conn, ans)
    except psycopg.Error as exc:
        print(f"amem: evidence ask failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    obj = {**ans.to_json(), "recorded": len(recorded)}
    lines = [f"{ans.text}  ({ans.question})"]
    lines += [f"  step {s['source']}.{s['query']}: {s['status']}" + (f" ({s['detail']})" if s["detail"] else "")
              + f" {s['found']} found" for s in obj["steps"]]
    lines += [f"  [{f['status']}] {f['service']} {f['type']} {f['ref']} -> {f['supabase_ref'] or '-'} "
              f"{f['timestamp'] or ''} {f['measure']}" for f in ans.findings]
    if args.record:
        lines.append(f"  recorded {len(recorded)} linked external finding(s) in rsi_evidence")
    return _out(args, obj, "\n".join(lines))


def cmd_evidence_record_ncm(args: argparse.Namespace) -> int:
    """Validate one NCM results.json (the 004A contract) and store it as an ncm:run evidence row. The run must
    name a registered experiment (rsi_experiments), so the research run joins the shared experiment system."""
    from pathlib import Path

    from agent_memory.integrations import ncm
    try:
        run = ncm.load_results(Path(args.path))
    except ncm.ResultsError as exc:
        print(f"amem: evidence record-ncm: {exc}", file=sys.stderr)
        return 2
    ev = ncm.to_evidence(run)
    try:
        with store.connect() as conn:
            if run.experiment_id and not E.exists(conn, f"amem:experiment/{run.experiment_id}"):
                print(f"amem: evidence record-ncm: experiment {run.experiment_id} is not registered", file=sys.stderr)
                return 1
            eid = None
            if not args.dry_run:
                eid = E.record(conn, ev)
                conn.commit()
    except psycopg.Error as exc:
        print(f"amem: evidence record-ncm failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    obj = {"evidence_id": eid, "dry_run": args.dry_run, **ev.to_json()}
    return _out(args, obj, f"{'would record' if args.dry_run else 'recorded'} {ev.ref} status={run.status} "
                           f"metrics={json.dumps(run.metrics, sort_keys=True)}")


def _scope_args(q: argparse.ArgumentParser) -> None:
    q.add_argument("--tables", type=_tables_arg, default=None, metavar="T1,T2",
                   help="only these source tables (clickhouse: its tables; langfuse: sandbox_runs; pastdev: "
                        "rsi_proposals, rsi_decisions, rsi_experiments, edits, lesson_candidates)")
    q.add_argument("--since", type=_since_arg, default=None, metavar="YYYY-MM-DD",
                   help="only rows changed on or after this UTC date. clickhouse/langfuse: older rows fall behind "
                        "the cursor (a later --replay without --since sends them); pastdev: older records wait "
                        "for a run without --since. Retractions and ingestion checks are never narrowed")


def register(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("sync", help="push Supabase evidence to ClickHouse / Langfuse / past.dev (docs/integrations/)")
    s = p.add_subparsers(dest="sync_command", required=True)
    q = s.add_parser("run", help="one bounded sync pass for one sink (unconfigured = unavailable, exit 0)")
    q.add_argument("--sink", required=True, choices=sync.SINKS)
    q.add_argument("--budget", type=float, default=60.0, help="seconds; stops between batches")
    q.add_argument("--replay", action="store_true", help="start from no cursor (backfill; pushes are idempotent)")
    _scope_args(q)
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
    q = s.add_parser("plan", help="dry run: what a run or a --replay backfill would send (read-only)")
    q.add_argument("--sink", required=True, choices=sync.SINKS)
    q.add_argument("--replay", action="store_true")
    _scope_args(q)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_sync_plan)
    q = s.add_parser("schema", help="print the sink's DDL and least-privilege user SQL (sends nothing)")
    q.add_argument("--sink", required=True, choices=["clickhouse"])
    q.add_argument("--cloud", action="store_true", help="ClickHouse Cloud DDL (no non-replicated settings)")
    q.add_argument("--db", default="amem")
    q.add_argument("--part", choices=["all", "admin", "writer"], default="all",
                   help="admin: database and tables (run as the console admin); writer: the least-privilege user")
    q.add_argument("--hash-from-env", action="store_true",
                   help="identify the writer by the SHA-256 of AMEM_CLICKHOUSE_KEY (never printed)")
    q.set_defaults(func=cmd_sync_schema)
    q = s.add_parser("grants", help="probe as the ClickHouse writer: read and insert allowed, nothing else")
    q.add_argument("--sink", required=True, choices=["clickhouse"])
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_sync_grants)

    p = sub.add_parser("evidence", help="the evidence facade (named queries only)")
    e = p.add_subparsers(dest="evidence_command", required=True)
    q = e.add_parser("ask", help="one of the investigator's six questions, across every source (Supabase first)")
    q.add_argument("question")
    q.add_argument("--param", action="append", default=[], metavar="K=V")
    q.add_argument("--record", action="store_true", help="store linked external findings in rsi_evidence")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_evidence_ask)
    q = e.add_parser("record-ncm", help="store one NCM results.json as an ncm:run evidence row")
    q.add_argument("path")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_evidence_record_ncm)
    q = e.add_parser("questions", help="list the six questions and their params")
    q.set_defaults(func=cmd_evidence_questions)
    q = e.add_parser("query", help="run one named query against one source")
    q.add_argument("--source", required=True, choices=["sb", "ch", "lf", "pd"])
    q.add_argument("query")
    q.add_argument("--param", action="append", default=[], metavar="K=V")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_evidence_query)
