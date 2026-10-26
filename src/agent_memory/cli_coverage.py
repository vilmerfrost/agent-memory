"""`amem coverage families|links|repair-links` (docs/data-use/). families and links are read-only.

    amem coverage families [--json | --markdown]   the data-family registry and its migration check
    amem coverage links [--json]                   linked / unlinked / unresolvable counts per relationship
    amem coverage links --classify [--sandbox DIR] [--json]
                                                   experiment attempts by class (attempt_links.classify)
    amem coverage repair-links [--apply | --undo MANIFEST] [--sandbox DIR] [--manifest-dir DIR] [--hosted-ok]
                                                   dry run by default; --apply writes rsi_attempts with an undo manifest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


EXTRA_COLUMNS = ("runs_missing_cases", "outcome_pending", "excluded_eval_harness", "excluded_eval_other")


def _sandbox_root(args: argparse.Namespace) -> Path:
    from agent_memory.sessions_ingest import rsi_sandbox_dir
    return Path(args.sandbox).expanduser() if args.sandbox else rsi_sandbox_dir()


def cmd_links(args: argparse.Namespace) -> int:
    from agent_memory import attempt_links, coverage_links

    if config.backend() != "postgres":
        print("amem: coverage links needs the Postgres backend", file=sys.stderr)
        return 3
    try:
        with psycopg.connect(config.db_url(), autocommit=True) as conn:
            if args.classify:
                out = attempt_links.classify(conn, _sandbox_root(args))
            else:
                rows = coverage_links.counts(conn)
    except (OSError, ValueError, psycopg.Error, config.BackendError) as exc:
        # Database errors can contain connection details; only expose the class.
        print(f"amem coverage links failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.classify:
        if args.json:
            print(json.dumps(out, indent=2, sort_keys=True))
        else:
            _print_classify(out)
        return 0
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    for r in rows:
        if r["status"] == "missing_table":
            print(f"{r['key']:<28} missing table: {', '.join(r['missing'])}")
            continue
        extra = {k: v for k, v in r.items() if k in EXTRA_COLUMNS}
        tail = "".join(f" {k}={v}" for k, v in extra.items())
        print(f"{r['key']:<28} eligible={r['eligible']} linked={r['linked']} unlinked={r['unlinked']} "
              f"unresolvable={r['unresolvable']} enforced={r['enforced']}{tail}")
        if r.get("excluded"):
            print(f"{'':<28} excluded: {r['excluded']}")
    return 0


def _print_classify(out: dict) -> None:
    print(f"sandbox root: {out['sandbox_root']}" + ("" if out["sandbox_root_found"] else " (not found)"))
    old = out["old_eligible"]
    print("old eligible set (sessions with env sandbox/eval or an experiment id): "
          + " ".join(f"{k}={v}" for k, v in old.items()))
    print("classes: " + " ".join(f"{k}={v}" for k, v in out["classes"].items()))
    print("sandbox attempt sources: " + " ".join(f"{k}={v}" for k, v in out["sandbox_attempt_sources"].items()))
    print("excluded runs (not attempts): " + (" ".join(f"{k.replace(' ', '_')}={v}"
                                                         for k, v in out["excluded_runs"].items()) or "none"))
    print("divergences: " + " ".join(f"{k}={v}" for k, v in out["divergences"].items()))
    for exp, n in out["attempts_per_experiment"].items():
        print(f"  attempts {exp}: {n}")
    for e in out["experiments_without_attempts"]:
        print(f"  experiment without attempts: {e}")
    for u in out["unresolved"]:
        print(f"  unresolved {u['id']}: {u['reason']}")
    for a in out["ambiguous"]:
        print(f"  ambiguous session {a['session_id']}: {a['reason']}")


def cmd_repair_links(args: argparse.Namespace) -> int:
    from agent_memory import attempt_links as AL

    if config.backend() != "postgres":
        print("amem: coverage repair-links needs the Postgres backend", file=sys.stderr)
        return 3
    try:
        url = config.db_url()
        writes = args.apply or args.undo
        if writes and not AL.is_local(url) and not args.hosted_ok:
            print(f"amem coverage repair-links: {AL.db_label(url)} is not a local database; a hosted write needs "
                  "Vilmer's yes and --hosted-ok", file=sys.stderr)
            return 2
        with psycopg.connect(url, autocommit=True) as conn:
            if args.undo:
                out = AL.undo(conn, Path(args.undo).expanduser(), db=AL.db_label(url))
            else:
                out = AL.repair(conn, _sandbox_root(args), apply=args.apply, db=AL.db_label(url),
                                manifest_dir=Path(args.manifest_dir).expanduser() if args.manifest_dir else None)
    except AL.RepairError as exc:
        print(f"amem coverage repair-links refused: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, psycopg.Error, config.BackendError) as exc:
        print(f"amem coverage repair-links failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    if out["mode"] == "undo":
        print(f"undo: {out['status']} ({out['restored']} row(s))")
        return 0
    p = out["planned"]
    print(f"{out['mode']}: insert={p['insert']} update={p['update']} delete={p['delete']} unchanged={p['unchanged']} "
          f"kept_runner={p['kept_runner']} (linked={p['linked']} unresolved={p['unresolved']})")
    if out["manifest"]:
        print(f"undo manifest: {out['manifest']}")
    elif out["mode"] == "apply":
        print("no change: nothing to undo")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("coverage", help="data-use contracts: data families, relationship links, attempt-link repair")
    actions = parser.add_subparsers(dest="coverage_action", required=True)
    p = actions.add_parser("families", help="the data-family registry; exit 1 when a migration table is unassigned")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--markdown", action="store_true", help="print docs/data-use/data-families.md")
    p.set_defaults(func=cmd_families)
    p = actions.add_parser("links", help="linked / unlinked / unresolvable counts per relationship")
    p.add_argument("--json", action="store_true")
    p.add_argument("--classify", action="store_true",
                   help="experiment attempts by class: eval-harness session, sandbox attempt, experiment without "
                        "attempts, ambiguous, unresolved (reads the RSI sandbox dir and the database, writes nothing)")
    p.add_argument("--sandbox", help="RSI sandbox root for --classify (default RSI_SANDBOX_DIR or ~/sandbox)")
    p.set_defaults(func=cmd_links)
    p = actions.add_parser("repair-links", help="rebuild experiment-attempt links from explicit identifiers "
                                                "(dry run unless --apply; --undo restores a manifest)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write the plan; saves an undo manifest first")
    mode.add_argument("--undo", metavar="MANIFEST", help="restore the rows a repair changed, exactly")
    p.add_argument("--sandbox", help="RSI sandbox root (default RSI_SANDBOX_DIR or ~/sandbox); read only")
    p.add_argument("--manifest-dir", help="where --apply writes its undo manifest "
                                          "(default AMEM_REPAIR_LINKS_DIR or ~/.local/state/amem/repair-links)")
    p.add_argument("--hosted-ok", action="store_true", help="allow --apply/--undo on a non-local database")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_repair_links)
