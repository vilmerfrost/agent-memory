"""`amem rsi supersede-morning`: retire the proposals the old morning edits step staged (2026-10-26).

amem-daily used to run `edits run` in stage mode at 09:20 and create one rsi_proposals row per edit. That step no
longer runs (daily.edits_in_daily): the RSI night has its own writer. Its proposals that still wait (status
`staged`) would stay in Night Desk's waiting list until they expire, so this command moves them to `superseded`.

- A morning proposal is a staged proposal that an `edits` row in stage mode points to (`meta.proposal_id`,
  `meta.mode = stage`), or whose experiment id has the edits step's shape (`exp-YYYYMMDD-edit-<8 hex>`). The night's
  proposals (`exp-YYYYMMDD-<8 hex>`) are never touched.
- Nothing is deleted. The row keeps everything; `result` says why and `meta.superseded` records when and by what.
  The decision functions answer `not-staged` for it, so an old Apply / Reject button does nothing.
- `--dry-run` lists them and changes nothing. Running it twice changes nothing the second time.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import store

MORNING_EXPERIMENT_RE = r"^exp-[0-9]{8}-edit-[0-9a-f]{8}$"
REASON = "superseded: the morning edits step no longer runs; the RSI night writes and stages its own proposals"

_MORNING_SQL = """
select p.id::text as id, p.title, p.experiment_id, p.created_at, p.expires_at
from rsi_proposals p
where p.status = 'staged'
  and (coalesce(p.experiment_id, '') ~ %(exp_re)s
       or exists (select 1 from edits e where e.meta->>'proposal_id' = p.id::text and e.meta->>'mode' = 'stage'))
order by p.created_at, p.id
"""


def morning_proposals(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_MORNING_SQL, {"exp_re": MORNING_EXPERIMENT_RE})
        return cur.fetchall()


def supersede_morning(*, db_url: str | None = None, dry_run: bool = False,
                      now: datetime | None = None) -> list[dict[str, Any]]:
    """The morning proposals that were (or, with dry_run, would be) moved from staged to superseded."""
    now = now or datetime.now(UTC)
    with store.connect(db_url) as conn:
        rows = morning_proposals(conn)
        if dry_run or not rows:
            return rows
        conn.execute(
            "update rsi_proposals set status = 'superseded', result = %s, meta = meta || %s "
            "where id = any(%s::uuid[]) and status = 'staged'",
            (REASON, Jsonb({"superseded": {"at": now.isoformat(timespec="seconds"), "by": "amem rsi supersede-morning",
                                           "reason": "morning edits step retired"}}),
             [r["id"] for r in rows]))
    return rows


def cmd_supersede_morning(args: argparse.Namespace) -> int:
    from agent_memory.cli_deviations import _read_guard, _write_guard
    guard = _read_guard if args.dry_run else _write_guard
    if (code := guard("rsi supersede-morning")) is not None:
        return code
    try:
        rows = supersede_morning(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001 - a DB error is the command's failure
        print(f"amem: rsi supersede-morning: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"dry_run": args.dry_run, "count": len(rows), "proposals": rows}, sort_keys=True, default=str))
        return 0
    verb = "would supersede" if args.dry_run else "superseded"
    print(f"rsi supersede-morning: {verb} {len(rows)} morning proposal(s)")
    for r in rows:
        print(f"  {r['id']}  {r['experiment_id'] or '-'}  {str(r['title'])[:80]}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("supersede-morning",
                       help="move the old morning edits step's waiting proposals (staged) to superseded; "
                            "deletes nothing (--dry-run lists them)")
    p.add_argument("--dry-run", action="store_true", help="list them, change nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_supersede_morning)
