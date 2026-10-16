"""`amem synth ...`: the synthetic RSI dataset (Mission 004C; see synth.py).

    amem synth seed --db-url URL [--anchor YYYY-MM-DD] [--out DIR] [--json]

Local disposable Postgres only (localhost, 127.0.0.1, ::1). The URL is never printed, only its host.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from agent_memory import synth


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {value!r}") from exc


def cmd_seed(args: argparse.Namespace) -> int:
    try:
        rep = synth.seed(args.db_url, args.anchor, out=args.out)
    except synth.UnsafeTarget as exc:
        print(f"amem: synth seed refused: {exc}", file=sys.stderr)
        return 2
    except synth.SeedError as exc:
        print(f"amem: synth seed: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"amem: synth seed failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rep, sort_keys=True))
    else:
        rows = sum(rep["counts"].values())
        print(f"synth {rep['status']}: SYNTHETIC dataset {rep['dataset']} v{rep['version']} on {rep['host']}, "
              f"anchor {rep['anchor']}, {rows} rows, {rep['markers']} markers; gold: {rep['gold']}")
    return 0


def register(sub: Any) -> None:
    p = sub.add_parser("synth", help="synthetic RSI dataset for the C1 check and the evidence demo (local DB only)")
    s = p.add_subparsers(dest="synth_command", required=True)
    q = s.add_parser("seed", help="seed a migrated, empty, local Postgres with the synthetic story; write gold.json")
    q.add_argument("--db-url", required=True, help="a disposable local database (localhost / 127.0.0.1 only)")
    q.add_argument("--anchor", type=_date, default=None,
                   help="UTC date the story ends before (default: today; reuse keeps the seeded one)")
    q.add_argument("--out", type=Path, default=Path("synth-out"), help="directory for gold.json (default synth-out)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_seed)
