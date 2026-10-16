#!/usr/bin/env python3
"""C1: verify ClickHouse (Cloud) against the SYNTHETIC dataset (Mission 004C). Prepared; not yet run against Cloud.

    uv run python scripts/c1_clickhouse.py --db-url URL --gold synth-out/gold.json [--days 30] [--out FILE] [--json]
    uv run python scripts/c1_clickhouse.py --db-url URL --gold synth-out/gold.json --dry-run

Before: a disposable local Postgres, migrated with the Keychain tripwire (AMEM_ADMIN_SECURITY=<script that exits
44>, AMEM_NO_DOTENV=1), then `amem synth seed --db-url URL --out synth-out` (writes gold.json).

The database comes from --db-url only (never AMEM_DB_URL or an env file) and must be localhost / 127.0.0.1.
ClickHouse comes from the env, as for `amem sync run`: AMEM_CLICKHOUSE_URL / _USER (the writer) / _KEY / _DB,
and AMEM_PSEUDONYM_KEY for the project pseudonyms. Nothing here prints a key or the database password.

Steps, each timed, into one JSON report (default: next to gold.json, c1-report.json):
  preflight     the database is local and holds the seeded dataset (counts and anchor equal gold); the anchor
                is recent enough for the gold query results; ClickHouse is configured and answers /ping
  sync_run      `amem sync run --sink clickhouse`: status ok, nothing failed or dead-lettered
  verify        `amem sync verify --sink clickhouse --days N`: parity, and Supabase's per-day counts equal gold
  final_counts  SELECT count() FROM <table> FINAL equals gold for every table
  queries       every named query in gold, via ClickHouseRetriever: the same rows and metrics (1e-3), and every
                amem: subject re-reads in Supabase
  sync_rerun    a second run: status ok, and the FINAL counts are unchanged (re-sent rows deduplicate)
  grants        `amem sync grants --sink clickhouse`: read and insert allowed; DDL, users, grants denied
  failure_path  a run against a bad host (no credentials sent): `unavailable`, the cursor unchanged
  team_masking  sessions and skill_calls: project/repo is 'team' exactly on the gold team rows, else a
                pseudonym (p_<16 hex>) or null
  leak_scan     as the writer, per table: rows whose toString(tuple(*)) holds a ZZLEAK marker, an sk-ant- key
                or an @example.com address: 0

`--dry-run` prints the plan and touches nothing (no database connection, no ClickHouse request).
Exit: 0 all steps ok, 1 a step failed, 2 bad arguments or an unsafe target.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import psycopg

from agent_memory import synth
from agent_memory.integrations import clickhouse as CH, evidence as E, http, sync

BAD_URL = "https://amem-c1-bad-host.invalid:8443"
NEEDLES = ("zzleak", "sk-ant-", "@example.com")
TOLERANCE = 1e-3
PSEUDO_RE = re.compile(r"p_[0-9a-f]{16}")
STEPS = ("preflight", "sync_run", "verify", "final_counts", "queries", "sync_rerun", "grants", "failure_path",
         "team_masking", "leak_scan")
PLAN = {
    "preflight": "database local + seeded (counts and anchor equal gold); anchor recent; ClickHouse configured, /ping ok",
    "sync_run": "amem sync run --sink clickhouse: status ok, 0 failed, 0 dead letters",
    "verify": "amem sync verify --sink clickhouse --days N: parity=yes; Supabase per-day counts equal gold",
    "final_counts": "SELECT count() FROM amem.<table> FINAL equals gold, every table",
    "queries": "each gold named query via ClickHouseRetriever: rows + metrics equal gold; amem: subjects re-read",
    "sync_rerun": "a second sync run: status ok; FINAL counts unchanged",
    "grants": "amem sync grants --sink clickhouse: 11 probes ok (read/insert allowed, DDL/users/grants denied)",
    "failure_path": f"sync run against {BAD_URL} (no credentials): unavailable, cursor unchanged",
    "team_masking": "sessions/skill_calls project ('team' only on gold team rows, else p_<hex> or null)",
    "leak_scan": "per table: count rows whose toString(tuple(*)) contains " + ", ".join(NEEDLES) + " -> 0",
}


class Abort(Exception):
    pass


def _url_host(url: str) -> str | None:
    """The host of a URL only (never a user or key that might ride in it)."""
    return urlsplit(url).hostname if url else None


def _final_counts(client: CH.Client) -> dict[str, int]:
    out = {}
    for t in CH.TABLES:
        rows = client.select(f"SELECT count() AS n FROM {client.cfg.db}.{t.name} FINAL FORMAT JSONEachRow", {})
        out[t.name] = int(rows[0]["n"]) if rows else 0
    return out


def _supabase_counts(conn: psycopg.Connection) -> dict[str, int]:
    out = {}
    for t in CH.TABLES:
        where = f" where {t.where}" if t.where else ""
        out[t.name] = int(conn.execute(f"select count(*) from {t.src}{where}").fetchone()[0])
    return out


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), abs_tol=TOLERANCE)
    return a == b


def compare_query(expected: list[Mapping[str, Any]], got: list[E.EvidenceRef]) -> list[str]:
    """Differences between gold rows and the retriever's refs (order-insensitive; gold keys only)."""
    diffs: list[str] = []
    by_ref = {ev.ref: ev for ev in got}
    want = {r["ref"]: r for r in expected}
    for ref in sorted(set(want) - set(by_ref)):
        diffs.append(f"missing {ref}")
    for ref in sorted(set(by_ref) - set(want)):
        diffs.append(f"unexpected {ref}")
    for ref in sorted(set(want) & set(by_ref)):
        ev, row = by_ref[ref], want[ref]
        for k, v in row["metrics"].items():
            if not _same(ev.metrics.get(k), v):
                diffs.append(f"{ref}: {k}={ev.metrics.get(k)!r}, gold {v!r}")
        if "subjects" in row and sorted(ev.subject_refs) != sorted(row["subjects"]):
            diffs.append(f"{ref}: subjects {ev.subject_refs}, gold {row['subjects']}")
        if "sessions" in row:
            extra = [s for s in ev.subject_refs if s.split("/", 1)[1] not in row["sessions"]]
            if extra:
                diffs.append(f"{ref}: subjects outside the gold sessions: {extra}")
    return diffs


def run_c1(db_url: str, gold: Mapping[str, Any], *, environ: Mapping[str, str] | None = None, days: int = 30,
           client: CH.Client | None = None, connect: Callable[..., psycopg.Connection] = psycopg.connect,
           bad_url: str = BAD_URL, today: date | None = None) -> dict[str, Any]:
    """Run every step; one failing step never stops the others (only a failed preflight does)."""
    started = datetime.now(UTC)
    t0 = time.monotonic()
    today = today or started.date()
    report: dict[str, Any] = {"c1": "clickhouse", "synthetic": True, "dataset": gold.get("dataset"),
                              "anchor": gold.get("anchor"), "started_at": started.isoformat(timespec="seconds"),
                              "db_host": synth.db_host(db_url), "steps": []}
    client = client if client is not None else CH.Client(CH.config(environ))
    cfg = client.cfg
    report["clickhouse"] = {"host": _url_host(cfg.url), "db": cfg.db, "user": cfg.user or None,
                            "key_set": bool(cfg.key), "cloud": cfg.cloud}
    env = dict(environ) if environ is not None else None
    ch_gold = gold["clickhouse"]

    def step(name: str, fn: Callable[[], tuple[bool, str, dict[str, Any]]]) -> bool:
        s0 = time.monotonic()
        try:
            ok, detail, data = fn()
        except Abort as exc:
            ok, detail, data = False, str(exc), {}
        except (http.HttpError, psycopg.Error, ValueError, KeyError, RuntimeError) as exc:
            ok, detail, data = False, f"{type(exc).__name__}: {str(exc)[:300]}", {}
        report["steps"].append({"name": name, "ok": ok, "seconds": round(time.monotonic() - s0, 3), "detail": detail,
                                **({"data": data} if data else {})})
        return ok

    try:
        synth.check_local(db_url)
    except synth.UnsafeTarget as exc:
        report["steps"].append({"name": "preflight", "ok": False, "seconds": 0.0, "detail": str(exc)})
        report["ok"] = False
        return report

    with connect(db_url) as conn:
        def preflight() -> tuple[bool, str, dict[str, Any]]:
            problems = []
            sent = synth._sentinel(conn)
            if not sent or sent.get("anchor") != gold.get("anchor"):
                problems.append(f"the database was not seeded with this gold (sentinel anchor "
                                f"{(sent or {}).get('anchor')}, gold {gold.get('anchor')})")
            have = _supabase_counts(conn)
            if have != ch_gold["tables"]:
                problems.append(f"Supabase rows per ClickHouse table differ from gold: {have} vs {ch_gold['tables']}")
            until = gold.get("queries_valid_until")
            if until and today > date.fromisoformat(until):
                problems.append(f"the anchor {gold['anchor']} is too old for the gold query results "
                                f"(valid until {until}): seed a fresh database")
            if cfg.problem:
                problems.append(f"ClickHouse: {cfg.problem}")
            data: dict[str, Any] = {"supabase_rows": have, "cursor": bool(sync.load(conn, "clickhouse").get("watermarks"))}
            if not cfg.problem:
                client.ping()
                pre = _final_counts(client)
                data["clickhouse_rows_before"] = pre
                if any(pre.values()) and pre != ch_gold["tables"]:
                    problems.append(f"ClickHouse already holds other rows: {pre}")
            return not problems, "; ".join(problems) or "ok", data

        if not step("preflight", preflight):
            report["ok"] = False
            report["seconds"] = round(time.monotonic() - t0, 3)
            return report

        def sync_once() -> tuple[bool, str, dict[str, Any]]:
            rep = sync.run(CH.ClickHouseSink(client), conn, budget_s=600.0, environ=env)
            ok = rep.status == "ok" and rep.failed == 0 and rep.dead_letters == 0
            return ok, rep.line(), {"status": rep.status, "rows": rep.rows, "batches": rep.batches,
                                    "dead_letters": rep.dead_letters}

        def verify() -> tuple[bool, str, dict[str, Any]]:
            rep = CH.verify(conn, days, client=client, today=today)
            if rep.status != "ok":
                return False, rep.line(), rep.to_json()
            off_gold = []
            for t, per_day in ch_gold["verify"].items():
                for d, n in per_day.items():
                    if d >= rep.since and rep.counts.get(t, {}).get(d, [0, 0])[0] != n:
                        off_gold.append(f"{t}@{d} supabase={rep.counts.get(t, {}).get(d, [0, 0])[0]} gold={n}")
            ok = rep.parity and not off_gold
            return ok, rep.line() + (f"; off gold: {', '.join(off_gold[:5])}" if off_gold else ""), \
                {"since": rep.since, "mismatches": [list(m) for m in rep.mismatches()], "off_gold": off_gold}

        def final_counts() -> tuple[bool, str, dict[str, Any]]:
            got = _final_counts(client)
            bad = {t: (got.get(t), n) for t, n in ch_gold["tables"].items() if got.get(t) != n}
            return not bad, "ok" if not bad else f"differs (got, gold): {bad}", {"final": got}

        def queries() -> tuple[bool, str, dict[str, Any]]:
            r = CH.ClickHouseRetriever(client)
            out, failed = [], 0
            for q in ch_gold["queries"]:
                res = E.run_query(r, q["name"], q["params"])
                diffs = compare_query(q["rows"], res.refs) if res.status == "ok" else [f"status {res.status}: {res.detail}"]
                stale = [ev.ref for ev in res.refs if not E.usable(conn, ev)]
                diffs += [f"{ref}: an amem: subject does not re-read" for ref in stale]
                failed += bool(diffs)
                out.append({"name": q["name"], "params": q["params"], "status": res.status, "gold_rows": len(q["rows"]),
                            "rows": len(res.refs), "ok": not diffs, "diffs": diffs[:10]})
            return not failed, f"{len(out) - failed}/{len(out)} queries equal gold", {"queries": out}

        def rerun() -> tuple[bool, str, dict[str, Any]]:
            ok, line, data = sync_once()
            got = _final_counts(client)
            same = got == ch_gold["tables"]
            return ok and same, f"{line}; FINAL counts {'unchanged' if same else 'CHANGED'}", \
                {**data, "rows_resent": data.get("rows"), "final": got}

        def grants() -> tuple[bool, str, dict[str, Any]]:
            rows = CH.check_grants(client)
            ok = all(r["ok"] for r in rows)
            return ok, f"{sum(r['ok'] for r in rows)}/{len(rows)} probes ok", {"probes": rows}

        def failure_path() -> tuple[bool, str, dict[str, Any]]:
            before = sync.load(conn, "clickhouse").get("watermarks")
            bad = CH.Client(CH.Config(bad_url, db=cfg.db), retries=0, timeout=5.0)    # never the real credentials
            rep = sync.run(CH.ClickHouseSink(bad), conn, budget_s=60.0, environ=env)
            after = sync.load(conn, "clickhouse").get("watermarks")
            ok = rep.status == "unavailable" and before == after and bool(before)
            return ok, f"status={rep.status}; cursor {'unchanged' if before == after else 'MOVED'}", \
                {"status": rep.status, "detail": rep.detail[:200]}

        def team_masking() -> tuple[bool, str, dict[str, Any]]:
            team = set(gold["team"].get("sessions", []))
            team_calls = set(gold["team"].get("skill_calls", []))
            bad = []
            sess = client.select(f"SELECT id, project, repo FROM {cfg.db}.sessions FINAL FORMAT JSONEachRow", {})
            for r in sess:
                for col in ("project", "repo"):
                    v = r.get(col)
                    if r["id"] in team and v != "team":
                        bad.append(f"sessions {r['id']}.{col}={v!r} (team row)")
                    elif r["id"] not in team and not (v is None or PSEUDO_RE.fullmatch(str(v))):
                        bad.append(f"sessions {r['id']}.{col} is neither a pseudonym nor null")
            calls = client.select(f"SELECT id, project FROM {cfg.db}.skill_calls FINAL FORMAT JSONEachRow", {})
            for r in calls:
                v = r.get("project")
                if r["id"] in team_calls and v != "team":
                    bad.append(f"skill_calls {r['id']}.project={v!r} (team row)")
                elif r["id"] not in team_calls and not (v is None or PSEUDO_RE.fullmatch(str(v))):
                    bad.append(f"skill_calls {r['id']}.project is neither a pseudonym nor null")
            n_team = sum(r.get("project") == "team" for r in sess)
            n_calls = sum(r.get("project") == "team" for r in calls)
            ok = not bad and n_team == ch_gold["team"]["sessions"] and n_calls == ch_gold["team"]["skill_calls"]
            pseudo = sum(1 for r in sess if r.get("project") and PSEUDO_RE.fullmatch(str(r["project"])))
            return ok, f"team sessions {n_team}, team skill_calls {n_calls}, pseudonymous sessions {pseudo}" + \
                (f"; {bad[:3]}" if bad else ""), {"problems": bad[:20]}

        def leak_scan() -> tuple[bool, str, dict[str, Any]]:
            needles = "[" + ", ".join(f"'{n}'" for n in NEEDLES) + "]"
            hits: dict[str, int] = {}
            how = "tuple(*)"
            for t in CH.TABLES:
                stmt = (f"SELECT count() AS n FROM {cfg.db}.{t.name} WHERE "
                        f"multiSearchAnyCaseInsensitive(toString(tuple(*)), {needles}) FORMAT JSONEachRow")
                try:
                    rows = client.select(stmt, {})
                except http.HttpError:
                    how = "allowlist columns (tuple(*) refused)"
                    cols = ", ".join([*t.allowlist, "version"])
                    rows = client.select(f"SELECT count() AS n FROM {cfg.db}.{t.name} WHERE multiSearchAnyCaseInsensitive("
                                         f"toString(tuple({cols})), {needles}) FORMAT JSONEachRow", {})
                hits[t.name] = int(rows[0]["n"]) if rows else 0
            bad = {t: n for t, n in hits.items() if n}
            return not bad, f"{len(hits)} tables scanned via {how}; rows with a marker: {sum(hits.values())}", \
                {"hits": hits, "markers": len(gold.get("markers", []))}

        for name, fn in (("sync_run", sync_once), ("verify", verify), ("final_counts", final_counts),
                         ("queries", queries), ("sync_rerun", rerun), ("grants", grants),
                         ("failure_path", failure_path), ("team_masking", team_masking), ("leak_scan", leak_scan)):
            step(name, fn)
    report["ok"] = all(s["ok"] for s in report["steps"]) and len(report["steps"]) == len(STEPS)
    report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    report["seconds"] = round(time.monotonic() - t0, 3)
    return report


def dry_run(db_url: str, gold: Mapping[str, Any], gold_path: Path, *, environ: Mapping[str, str] | None = None,
            days: int = 30) -> dict[str, Any]:
    """The plan only: no database connection, no ClickHouse request (the env is read, never printed)."""
    cfg = CH.config(environ)                     # loads the env files as a real run would; sends nothing
    env = environ if environ is not None else os.environ
    try:
        synth.check_local(db_url)
        target = "ok"
    except synth.UnsafeTarget as exc:
        target = f"REFUSED: {exc}"
    return {"c1": "clickhouse", "dry_run": True, "synthetic": True, "db_host": synth.db_host(db_url), "db_target": target,
            "gold": str(gold_path), "anchor": gold.get("anchor"), "queries_valid_until": gold.get("queries_valid_until"),
            "rows": gold["clickhouse"]["tables"], "queries": [q["name"] for q in gold["clickhouse"]["queries"]],
            "markers": len(gold.get("markers", [])), "verify_days": days,
            "clickhouse": {"host": _url_host(cfg.url), "user_set": bool(cfg.user),
                           "key_set": bool(cfg.key), "cloud": cfg.cloud, "problem": cfg.problem or None,
                           "pseudonym_key_set": bool((env.get(CH.PSEUDONYM_ENV) or "").strip())},
            "steps": [{"name": s, "does": PLAN[s]} for s in STEPS]}


def _lines(report: Mapping[str, Any]) -> list[str]:
    if report.get("dry_run"):
        ch = report["clickhouse"]
        out = [f"C1 dry run (SYNTHETIC dataset, anchor {report['anchor']}): nothing will be touched",
               f"  database host {report['db_host']}: {report['db_target']}",
               f"  ClickHouse host {ch['host'] or '(AMEM_CLICKHOUSE_URL unset)'}; user set {ch['user_set']}; "
               f"key set {ch['key_set']}; pseudonym key set {ch['pseudonym_key_set']}",
               f"  gold {report['gold']}: {sum(report['rows'].values())} rows, {len(report['queries'])} query cases, "
               f"{report['markers']} markers"]
        return out + [f"  {i + 1:2d}. {s['name']}: {s['does']}" for i, s in enumerate(report["steps"])]
    out = [f"C1 ClickHouse on SYNTHETIC data (anchor {report['anchor']}): {'PASS' if report['ok'] else 'FAIL'} "
           f"in {report.get('seconds', 0):.1f}s"]
    out += [f"  {'ok  ' if s['ok'] else 'FAIL'} {s['name']:<13} {s['seconds']:7.2f}s  {s['detail']}"
            for s in report["steps"]]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C1: ClickHouse verification on the synthetic dataset")
    ap.add_argument("--db-url", required=True, help="the disposable local Postgres `amem synth seed` filled")
    ap.add_argument("--gold", required=True, type=Path, help="gold.json from `amem synth seed`")
    ap.add_argument("--days", type=int, default=30, help="verify window (default 30)")
    ap.add_argument("--out", type=Path, default=None, help="report path (default: c1-report.json next to gold)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; touch nothing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        gold = json.loads(args.gold.read_text())
    except (OSError, ValueError) as exc:
        print(f"c1: cannot read gold: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.dry_run:
        rep = dry_run(args.db_url, gold, args.gold, days=args.days)
        print(json.dumps(rep, indent=1, sort_keys=True) if args.json else "\n".join(_lines(rep)))
        return 0 if rep["db_target"] == "ok" else 2
    try:
        synth.check_local(args.db_url)
    except synth.UnsafeTarget as exc:
        print(f"c1: {exc}", file=sys.stderr)
        return 2
    rep = run_c1(args.db_url, gold, days=args.days)
    out = args.out or args.gold.with_name("c1-report.json")
    out.write_text(json.dumps(rep, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps(rep, indent=1, sort_keys=True, default=str) if args.json else "\n".join(_lines(rep) + [f"report: {out}"]))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
