"""`amem coverage families|links` (docs/data-use/). Both read-only.

    amem coverage families [--json | --markdown]   the data-family registry and its migration check
    amem coverage links [--json]                   linked / unlinked / unresolvable counts per relationship
"""

from __future__ import annotations

import argparse
import json
import sys

import psycopg

from agent_memory import config


def cmd_families(args: argparse.Namespace) -> int:
    from agent_memory import data_families as DF

    problems = DF.check()
    if args.markdown:
        sys.stdout.write(DF.render_markdown())
    elif args.json:
        print(json.dumps({**DF.as_json(), "problems": problems}, indent=2, sort_keys=True))
    else:
        for f in DF.FAMILIES:
            kinds = ",".join(sorted(f.consumer_kinds())) or DF.NONE_FOUND
            gap = "  [no analysis consumer]" if f in DF.gaps() else ""
            store = len(f.tables) + len(f.views)
            print(f"{f.key:<20} {store:>2} tables/views  consumers: {kinds}{gap}")
        print(f"{len(DF.FAMILIES)} families, {len(DF.EXCLUDED)} excluded tables, {len(DF.gaps())} Stage E gaps")
        for p in problems:
            print(f"problem: {p}")
    return 1 if problems else 0


def cmd_links(args: argparse.Namespace) -> int:
    from agent_memory import coverage_links

    if config.backend() != "postgres":
        print("amem: coverage links needs the Postgres backend", file=sys.stderr)
        return 3
    try:
        with psycopg.connect(config.db_url(), autocommit=True) as conn:
            rows = coverage_links.counts(conn)
    except (OSError, ValueError, psycopg.Error, config.BackendError) as exc:
        # Database errors can contain connection details; only expose the class.
        print(f"amem coverage links failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for r in rows:
        if r["status"] == "missing_table":
            print(f"{r['key']:<28} missing table: {', '.join(r['missing'])}")
            continue
        extra = {k: v for k, v in r.items() if k in ("runs_missing_cases", "outcome_pending")}
        tail = "".join(f" {k}={v}" for k, v in extra.items())
        print(f"{r['key']:<28} eligible={r['eligible']} linked={r['linked']} unlinked={r['unlinked']} "
              f"unresolvable={r['unresolvable']} enforced={r['enforced']}{tail}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("coverage", help="data-use contracts: data families and relationship links (read-only)")
    actions = parser.add_subparsers(dest="coverage_action", required=True)
    p = actions.add_parser("families", help="the data-family registry; exit 1 when a migration table is unassigned")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--markdown", action="store_true", help="print docs/data-use/data-families.md")
    p.set_defaults(func=cmd_families)
    p = actions.add_parser("links", help="linked / unlinked / unresolvable counts per relationship")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_links)
