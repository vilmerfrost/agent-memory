#!/usr/bin/env python3
"""Shared-intelligence demo on SYNTHETIC data (Mission 004C): one question across Supabase, ClickHouse,
Langfuse and past.dev.

    uv run python scripts/demo_evidence_ask.py --docker [--keep] [--json] [--no-record]
    uv run python scripts/demo_evidence_ask.py --db-url postgresql://postgres:postgres@127.0.0.1:PORT/postgres

The question: "Why do certain Claude or Codex sessions repeatedly fail the same instruction, and has a similar
improvement already been tested?" It runs the real CLI, `amem evidence ask repeat_failures --record --json`:
1. Supabase: the rules several prod sessions keep failing, with Claude vs Codex sessions per rule;
   ClickHouse: per rule, the followed grade vs the objective outcome.
2. For the top rule: earlier proposals and improve edits (Supabase), similar past decisions (past.dev),
   the weekly rate (ClickHouse).
3. Per experiment those attempts name: eval verdicts and the stored sandbox trace links (Supabase), score
   aggregates per arm (Langfuse).
4. The failing run's trace, read live (Langfuse).
`--record` saves the structured finding as one rsi_evidence row (plus any linked finding a provider answered).

Database: `--docker` starts a throwaway pgvector Postgres on 127.0.0.1 (an OS-assigned port), migrates it with
the Keychain tripwire (AMEM_ADMIN_SECURITY=<script that exits 44>, AMEM_NO_DOTENV=1) and removes it after
(unless --keep). `--db-url` takes a migrated local database (localhost / 127.0.0.1 only). Either way the data
is seeded with `amem synth seed` (or reused when that database is already seeded).

Providers: whatever the environment configures (AMEM_CLICKHOUSE_*, AMEM_LANGFUSE_*, AMEM_PASTDEV_KEY). Env files
are not loaded unless --dotenv. An unconfigured provider answers `unavailable`, never an empty success.

Per finding the output carries: source, ref, its Supabase ref, sample size, timestamp, the provider's status
and the finding's status (authoritative | linked | stale | unlinked).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import synth

QUESTION = "repeat_failures"
ASKED = ("Why do certain Claude or Codex sessions repeatedly fail the same instruction, "
         "and has a similar improvement already been tested?")
BANNER = "SYNTHETIC DATA: every row, id and trace in this demo is made up (amem synth v1). Nothing here is real."
PG_IMAGE = "pgvector/pgvector:pg17"
_DROP_ENV = ("AMEM_DB_URL", "AMEM_BACKEND", "AMEM_SUPABASE_", "AMEM_REQUIRE_REMOTE", "AMEM_ADMIN_", "PG")


def _amem(args: Sequence[str], env: Mapping[str, str], timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "agent_memory.cli", *args], env=dict(env), capture_output=True,
                          text=True, timeout=timeout)


def child_env(db_url: str, *, dotenv: bool = False, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The caller's env (provider settings stay), but never its store selection: AMEM_DB_URL is ours."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_DROP_ENV)}
    env["AMEM_DB_URL"] = db_url
    if not dotenv:
        env["AMEM_NO_DOTENV"] = "1"
    env.update(extra or {})
    return env


class Throwaway:
    """A pgvector Postgres container on 127.0.0.1, migrated with the Keychain tripwire."""

    def __init__(self, keep: bool = False):
        self.keep, self.container, self.url, self._tmp = keep, "", "", Path(tempfile.mkdtemp(prefix="amem-demo-"))

    def __enter__(self) -> Throwaway:
        if shutil.which("docker") is None:
            raise RuntimeError("docker is not installed")
        self.container = subprocess.run(
            ["docker", "run", "-d", "--rm", "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD=postgres", PG_IMAGE],
            check=True, capture_output=True, text=True, timeout=300).stdout.strip()
        port = subprocess.run(["docker", "port", self.container, "5432/tcp"], check=True, capture_output=True,
                              text=True, timeout=60).stdout.splitlines()[0].rsplit(":", 1)[1]
        self.url = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(self.url, connect_timeout=2) as conn:
                    conn.execute("select 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        tripwire = self._tmp / "security"
        tripwire.write_text("#!/bin/sh\necho 'the demo never reads the Keychain' >&2\nexit 44\n")
        tripwire.chmod(0o755)
        res = _amem(["db", "migrate"], child_env(self.url, extra={"AMEM_ADMIN_SECURITY": str(tripwire)}))
        if res.returncode != 0:
            raise RuntimeError(f"migrate failed: {res.stderr.strip()[-300:]}")
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.container and not self.keep:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=60)
        shutil.rmtree(self._tmp, ignore_errors=True)


def _evidence_ids(db_url: str) -> set[str]:
    with psycopg.connect(db_url) as conn:
        return {str(r[0]) for r in conn.execute("select id from rsi_evidence").fetchall()}


def _sample(f: Mapping[str, Any]) -> int | None:
    n = f.get("measure", {}).get("n")
    if isinstance(n, int):
        return n
    return 1 if f.get("measure", {}).get("basis") in ("record", "eval", "trace") else None


def findings_view(answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    step_status = {f"{s['source']}.{s['query']}": s["status"] for s in answer["steps"]}
    return [{"source": f["source"], "service": f["service"], "step": f["step"], "type": f["type"], "ref": f["ref"],
             "supabase_ref": f["supabase_ref"], "sample_size": _sample(f), "timestamp": f["timestamp"],
             "provider_status": step_status.get(f["step"], "unknown"), "status": f["status"]}
            for f in answer["findings"]]


def headline(answer: Mapping[str, Any]) -> list[str]:
    """A short reading of the authoritative and linked findings (no free text from any row)."""
    by_step: dict[str, list[Mapping[str, Any]]] = {}
    for f in answer["findings"]:
        by_step.setdefault(f["step"], []).append(f)
    out = []
    pats = [f["metrics"] for f in by_step.get("sb.recurring_deviations", [])]
    if pats:
        top = min(pats, key=lambda m: (-m.get("sessions", 0), -m.get("n", 0), m.get("rule_key", "")))
        out.append(f"Most repeated rule: {top['rule_key']}: {top['n']} deviations in {top['sessions']} prod sessions "
                   f"({top.get('claude_sessions', 0)} Claude, {top.get('codex_sessions', 0)} Codex) "
                   f"in the last {top.get('window_days')} days; followed avg {top.get('followed_avg')}, "
                   f"objective avg {top.get('objective_avg')}.")
        for m in sorted(pats, key=lambda m: -m.get("n", 0)):
            if m is not top:
                out.append(f"Also recurring: {m['rule_key']}: {m['n']} deviations in {m['sessions']} sessions.")
    gaps = [f["metrics"] for f in by_step.get("ch.rule_gap", [])]
    for m in gaps[:3]:
        out.append(f"ClickHouse gap {m.get('rule_key')}: followed {m.get('followed_avg')} vs objective "
                   f"{m.get('objective_avg')} (gap {m.get('gap')}, flag {m.get('flag')}).")
    attempts = by_step.get("sb.attempts_for", [])
    if attempts:
        kinds = [f"{f['ref'].split(':', 1)[1].split('/', 1)[0]} {f['metrics'].get('status')}" for f in attempts]
        out.append(f"Tried before: {len(attempts)} attempt(s): " + ", ".join(kinds) + ".")
    evals = by_step.get("sb.evals_for", [])
    if evals:
        out.append("Eval verdicts: " + ", ".join(f"{f['metrics'].get('verdict')} on {f['metrics'].get('model')} "
                                                f"({f['metrics'].get('suite')})" for f in evals) + ".")
    traces = [f for f in by_step.get("sb.evidence_for", []) if f["ref"].startswith("lf:trace/")]
    if traces:
        out.append("Sandbox traces: " + ", ".join(
            f"arm {f['metrics'].get('arm')} check_passed={f['metrics'].get('check_passed')} "
            f"tool_errors={f['metrics'].get('tool_errors')} ({f['ref']})" for f in sorted(traces, key=lambda f: str(f["metrics"].get("arm")))) + ".")
    for f in by_step.get("lf.trace", []):
        out.append(f"Langfuse live trace {f['ref']}: {f['status']}, tool_errors={f['metrics'].get('tool_errors')}.")
    for f in by_step.get("pd.recall", [])[:3]:
        out.append(f"past.dev recalls {f['supabase_ref']} (rank {f['metrics'].get('rank')}).")
    down = sorted(svc for svc, st in answer.get("providers", {}).items() if st != "ok")
    if down:
        out.append("Not answered: " + ", ".join(f"{svc} ({answer['providers'][svc]})" for svc in down) + ".")
    return out


def run_demo(db_url: str, *, anchor: date | None = None, out: Path | str = "synth-out", record: bool = True,
             dotenv: bool = False, params: Sequence[str] = ()) -> dict[str, Any]:
    seeded = synth.seed(db_url, anchor, out=out)
    before = _evidence_ids(db_url)
    cmd = ["evidence", "ask", QUESTION, "--json", *(["--record"] if record else [])]
    for p in params:
        cmd += ["--param", p]
    res = _amem(cmd, child_env(db_url, dotenv=dotenv))
    if res.returncode != 0:
        raise RuntimeError(f"amem evidence ask exited {res.returncode}: {res.stderr.strip()[-300:]}")
    answer = json.loads(res.stdout)
    new = sorted(_evidence_ids(db_url) - before)
    rows = []
    if new:
        with psycopg.connect(db_url) as conn:
            rows = [{"id": str(i), "system": s, "kind": k, "external_ref": e, "subject_refs": len(sub)}
                    for i, s, k, e, sub in conn.execute(
                        "select id, system, kind, external_ref, subject_refs from rsi_evidence where id = any(%s::uuid[]) "
                        "order by system, kind", (new,)).fetchall()]
    return {"synthetic": True, "banner": BANNER, "asked": ASKED, "question": QUESTION, "text": answer["text"],
            "command": "amem " + " ".join(cmd), "dataset": {k: seeded[k] for k in ("status", "anchor", "gold", "markers")},
            "providers": answer.get("providers", {}),
            "steps": [{"step": f"{s['source']}.{s['query']}", "service": s["service"], "status": s["status"],
                       "detail": s["detail"], "found": s["found"]} for s in answer["steps"]],
            "findings": findings_view(answer), "summary": headline(answer),
            "recorded": {"requested": record, "cli_count": answer.get("recorded", 0), "new_rows": len(new), "rows": rows}}


def render(rep: Mapping[str, Any]) -> str:
    bar = "=" * len(rep["banner"])
    out = [bar, rep["banner"], bar, "", f"Q: {rep['asked']}", f"   -> {rep['command']}  ({rep['text']})",
           f"   dataset {rep['dataset']['status']}, anchor {rep['dataset']['anchor']}, gold {rep['dataset']['gold']}", "",
           "Providers:"]
    out += [f"  {svc:<11} {st}" for svc, st in rep["providers"].items()]
    out += ["", "Steps:"]
    out += [f"  {s['step']:<26} {s['status']:<12} found {s['found']}" + (f"  ({s['detail']})" if s["detail"] else "")
            for s in rep["steps"]]
    out += ["", "Findings (source, read by step | ref -> Supabase ref | n | timestamp | step status | finding status):"]
    out += [f"  {f['service']:<10} {'(' + f['step'] + ')':<26} {f['ref']} -> {f['supabase_ref'] or '-'} | "
            f"n={f['sample_size'] if f['sample_size'] is not None else '-'} | {f['timestamp'] or '-'} | "
            f"{f['provider_status']} | {f['status']}" for f in rep["findings"]]
    out += ["", "Answer:"] + [f"  - {line}" for line in rep["summary"]]
    rec = rep["recorded"]
    if rec["requested"]:
        out += ["", f"Recorded: {rec['new_rows']} new rsi_evidence row(s): "
                + ", ".join(f"{r['system']}/{r['kind']} {r['external_ref']}" for r in rec["rows"])]
    out += ["", bar, rep["banner"], bar]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="amem evidence ask on the synthetic dataset (demo)")
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--db-url", help="a migrated local Postgres (localhost / 127.0.0.1 only)")
    where.add_argument("--docker", action="store_true", help="start, migrate and (unless --keep) remove a throwaway one")
    ap.add_argument("--keep", action="store_true", help="--docker: keep the container and print its URL")
    ap.add_argument("--anchor", type=date.fromisoformat, default=None, help="story end date (default today, UTC)")
    ap.add_argument("--out", type=Path, default=Path("synth-out"), help="directory for gold.json")
    ap.add_argument("--no-record", action="store_true", help="do not save the finding")
    ap.add_argument("--dotenv", action="store_true", help="let amem load .env.local/.env for provider settings")
    ap.add_argument("--param", action="append", default=[], metavar="K=V", help="extra question params (rule=...)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.docker:
            with Throwaway(keep=args.keep) as db:
                rep = run_demo(db.url, anchor=args.anchor, out=args.out, record=not args.no_record,
                               dotenv=args.dotenv, params=args.param)
                if args.keep:
                    rep["db_url"] = db.url                  # a throwaway local container's default password
        else:
            synth.check_local(args.db_url)
            rep = run_demo(args.db_url, anchor=args.anchor, out=args.out, record=not args.no_record,
                           dotenv=args.dotenv, params=args.param)
    except synth.UnsafeTarget as exc:
        print(f"demo: {exc}", file=sys.stderr)
        return 2
    except (synth.SeedError, RuntimeError, psycopg.Error, subprocess.SubprocessError) as exc:
        print(f"demo: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(rep, indent=1, sort_keys=True) if args.json else render(rep))
    if rep.get("db_url"):
        print(f"kept: {rep['db_url']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
