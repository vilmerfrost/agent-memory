"""`amem db dedupe-runs [--apply]`: one logical run = one `runs` row.

Before canonical keys (see `routines_log`), a cloud run was stored twice: once
from `ingest git-ref` (the published line) and once from `ingest routines-log`
(routine-sync's copy, `report=` rewritten). The two rows had different
`source_key`s. This command finds such rows and merges them. Postgres only.

How a stored row is matched to its source line (rows hold no raw line):

- The line is rebuilt from the row: `<ts> <routine> <STATUS> <summary>`
  (STATUS = `meta.raw_status`, else the upper-case status), or
  `<ts> <routine>-fail <meta.failures[0]>` for an orphan-detail row.
- The ts offset is not stored, so every offset from -12:00 to +14:00 (15 min
  steps, `+HHMM`, `+HH:MM` and `Z`) is tried, with occurrence n = 1..k
  (k = rows with the same routine and started_at).
- A row matches when its stored key equals the old key (sha256 of the raw line)
  or the new canonical key of a rebuilt line. Its canonical key is then exactly
  what `ingest` computes today.
- A row that matches nothing is left alone and counted as unmatched.

Merge rule, per group of rows with the same canonical key:

- Keep the oldest row (created_at, then id). Every other row is a duplicate.
- Move references from each duplicate to the kept row: `scores` (same question
  on both: the newer score wins), `labels` (both labeled: the newer label wins),
  `lesson_uses.run_id`, any other foreign key to `runs.id`, and
  `lessons.source_run_ids` (order kept, no repeats). Then delete the duplicate.
- Rewrite `source_key` of every kept row to its canonical key, so later
  ingests find it.
- `--apply` does all of it in one transaction under a table lock on `runs`.
  Without `--apply` it only prints the plan.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from agent_memory import config, routines_log, store

REMOTE_REQUIRED_EXIT = 3
_OFFSETS_MIN = [0, 60, 120] + [m for m in range(-12 * 60, 14 * 60 + 1, 15) if m not in (0, 60, 120)]


@dataclass
class Row:
    id: uuid.UUID
    routine: str
    started_at: datetime | None
    status: str
    log_source: str | None
    source_key: str
    created_at: datetime
    new_key: str | None = None  # canonical key; None = unmatched


@dataclass
class Group:
    keeper: Row
    duplicates: list[Row]


@dataclass
class Plan:
    groups: list[Group] = field(default_factory=list)
    rekeys: list[Row] = field(default_factory=list)  # kept rows whose source_key changes
    unmatched: list[Row] = field(default_factory=list)

    @property
    def duplicate_ids(self) -> list[uuid.UUID]:
        return [d.id for g in self.groups for d in g.duplicates]


# ---------------------------------------------------------------- key recovery


def _ts_candidates(started_at: datetime) -> Iterator[str]:
    for minutes in _OFFSETS_MIN:
        local = started_at.astimezone(timezone(timedelta(minutes=minutes)))
        base = local.strftime("%Y-%m-%dT%H:%M:%S")
        if local.microsecond:
            base += f".{local.microsecond:06d}"
        sign = "+" if minutes >= 0 else "-"
        hh, mm = divmod(abs(minutes), 60)
        yield f"{base}{sign}{hh:02d}{mm:02d}"
        yield f"{base}{sign}{hh:02d}:{mm:02d}"
        if minutes == 0:
            yield f"{base}Z"


def _tails(row: dict[str, Any]) -> list[str]:
    """Everything after the ts, for each shape the row could have come from."""
    meta = row["meta"] or {}
    routine = row["routine"]
    if meta.get("orphan_detail"):
        failures = meta.get("failures") or [""]
        first = failures[0]
        tails = [f" {routine}{routines_log.DETAIL_SUFFIX} {first}"]
        if first == "":
            tails.append(f" {routine}{routines_log.DETAIL_SUFFIX}")
        return tails
    word = meta.get("raw_status") or str(row["status"]).upper()
    summary = row["summary"]
    if summary:
        return [f" {routine} {word} {summary}"]
    return [f" {routine} {word}", f" {routine} {word} "]


def canonical_key(row: dict[str, Any], max_occurrence: int) -> str | None:
    """The canonical source_key of a stored row, or None if no rebuilt line matches its key."""
    stored, routine, started_at = row["source_key"], row["routine"], row["started_at"]
    if started_at is None:
        return None
    tails = _tails(row)
    for ts in _ts_candidates(started_at):
        for tail in tails:
            line = ts + tail
            for n in range(1, max_occurrence + 1):
                new = routines_log.source_key(routine, line, n)
                if new == stored or routines_log.legacy_source_key(routine, line, n) == stored:
                    return new
    return None


# ---------------------------------------------------------------- plan


_ROWS_SQL = """
select id, routine, started_at, status, summary, meta, source_key, created_at
from runs where source_key is not null
order by created_at, id
"""


def build_plan(conn: psycopg.Connection) -> Plan:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_ROWS_SQL)
        raw = cur.fetchall()
    per_slot = Counter((r["routine"], r["started_at"]) for r in raw)
    rows: list[Row] = []
    for r in raw:
        row = Row(id=r["id"], routine=r["routine"], started_at=r["started_at"], status=r["status"],
                  log_source=(r["meta"] or {}).get("log_source"), source_key=r["source_key"],
                  created_at=r["created_at"])
        row.new_key = canonical_key(r, per_slot[(r["routine"], r["started_at"])])
        rows.append(row)

    plan = Plan()
    by_key: dict[str, list[Row]] = {}
    for row in rows:
        if row.new_key is not None:
            by_key.setdefault(row.new_key, []).append(row)
    for row in rows:
        if row.new_key is None:
            if row.source_key in by_key:  # already holds a canonical key of a group
                row.new_key = row.source_key
                by_key[row.source_key].append(row)
            else:
                plan.unmatched.append(row)
    for members in by_key.values():
        members.sort(key=lambda r: (r.created_at, str(r.id)))
        keeper, dups = members[0], members[1:]
        if dups:
            plan.groups.append(Group(keeper, dups))
        if keeper.new_key != keeper.source_key:
            plan.rekeys.append(keeper)
    plan.groups.sort(key=lambda g: (g.keeper.started_at or datetime.min.replace(tzinfo=UTC),
                                    g.keeper.routine))
    return plan


# ---------------------------------------------------------------- references


def _table_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("select to_regclass(%s) is not null", (f"public.{name}",)).fetchone()[0]


def _has_source_run_ids(conn: psycopg.Connection) -> bool:
    return conn.execute(
        """select exists (select 1 from information_schema.columns where table_schema = 'public'
           and table_name = 'lessons' and column_name = 'source_run_ids')"""
    ).fetchone()[0]


def _foreign_keys(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    """(schema, table, column) of every single-column foreign key to runs.id."""
    return [tuple(r) for r in conn.execute(
        """
        select n.nspname, t.relname, a.attname
        from pg_constraint c
        join pg_class t on t.oid = c.conrelid
        join pg_namespace n on n.oid = t.relnamespace
        join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
        where c.contype = 'f' and c.confrelid = 'public.runs'::regclass
          and cardinality(c.conkey) = 1
        order by 1, 2, 3
        """
    ).fetchall()]


def reference_counts(conn: psycopg.Connection, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """Per run id: how many scores / labels / lesson_uses rows point at it."""
    counts: dict[uuid.UUID, dict[str, int]] = {i: {} for i in ids}
    if not ids:
        return counts
    for table in ("scores", "labels", "lesson_uses"):
        if not _table_exists(conn, table):
            continue
        query = sql.SQL("select run_id, count(*) from {} where run_id = any(%s) group by run_id").format(
            sql.Identifier("public", table))
        for run_id, n in conn.execute(query, (ids,)).fetchall():
            counts[run_id][table] = n
    if _has_source_run_ids(conn):
        for i in ids:
            n = conn.execute("select count(*) from lessons where %s = any(source_run_ids)", (i,)).fetchone()[0]
            if n:
                counts[i]["lessons"] = n
    return counts


@dataclass
class Applied:
    deleted: int = 0
    moved: Counter[str] = field(default_factory=Counter)
    rekeyed: int = 0


def _merge_into(conn: psycopg.Connection, dup: uuid.UUID, keep: uuid.UUID,
                fks: list[tuple[str, str, str]], lessons_array: bool, applied: Applied) -> None:
    p = {"d": dup, "k": keep}
    tables = {(s, t) for s, t, _ in fks}
    if ("public", "scores") in tables:  # unique (run_id, question): the newer score wins
        conn.execute("""delete from scores s using scores d where s.run_id = %(k)s and d.run_id = %(d)s
                        and s.question = d.question and s.created_at < d.created_at""", p)
        conn.execute("""delete from scores d using scores s where d.run_id = %(d)s and s.run_id = %(k)s
                        and s.question = d.question""", p)
    if ("public", "labels") in tables:  # unique (run_id): the newer label wins
        conn.execute("""delete from labels l using labels d where l.run_id = %(k)s and d.run_id = %(d)s
                        and l.labeled_at < d.labeled_at""", p)
        conn.execute("""delete from labels d using labels l where d.run_id = %(d)s and l.run_id = %(k)s""", p)
    for schema, table, column in fks:
        query = sql.SQL("update {} set {col} = %(k)s where {col} = %(d)s").format(
            sql.Identifier(schema, table), col=sql.Identifier(column))
        moved = conn.execute(query, p).rowcount
        if moved > 0:
            applied.moved[table if schema == "public" else f"{schema}.{table}"] += moved
    if lessons_array:
        moved = conn.execute(
            """
            update lessons set source_run_ids = (
                select coalesce(array_agg(u order by i), '{}')
                from (select u, min(i) as i
                      from unnest(array_replace(source_run_ids, %(d)s::uuid, %(k)s::uuid))
                           with ordinality as t(u, i)
                      group by u) as s)
            where %(d)s::uuid = any(source_run_ids)
            """, p).rowcount
        if moved > 0:
            applied.moved["lessons.source_run_ids"] += moved
    applied.deleted += conn.execute("delete from runs where id = %s", (dup,)).rowcount


def apply_plan(conn: psycopg.Connection) -> tuple[Plan, Applied]:
    """Lock runs, plan again under the lock, merge, delete and rekey. Caller owns the transaction."""
    conn.execute("lock table runs in share row exclusive mode")
    plan = build_plan(conn)
    fks = _foreign_keys(conn)
    lessons_array = _has_source_run_ids(conn)
    applied = Applied()
    for group in plan.groups:
        for dup in group.duplicates:
            _merge_into(conn, dup.id, group.keeper.id, fks, lessons_array, applied)
    for row in plan.rekeys:
        applied.rekeyed += conn.execute(
            "update runs set source_key = %s where id = %s", (row.new_key, row.id)).rowcount
    return plan, applied


# ---------------------------------------------------------------- CLI


def _when(row: Row) -> str:
    return row.started_at.astimezone(UTC).isoformat() if row.started_at else "-"


def _refs(counts: dict[str, int]) -> str:
    parts = [f"{k} {v}" for k, v in sorted(counts.items()) if v]
    return f" [{', '.join(parts)}]" if parts else ""


def print_plan(plan: Plan, counts: dict[uuid.UUID, dict[str, int]]) -> None:
    dups = plan.duplicate_ids
    print(f"duplicate runs: {len(plan.groups)} groups, {len(dups)} rows to delete")
    for g in plan.groups:
        k = g.keeper
        drops = ", ".join(f"{d.id} ({d.log_source}){_refs(counts.get(d.id, {}))}" for d in g.duplicates)
        print(f"  {k.routine} {_when(k)} {k.status}: keep {k.id} ({k.log_source}){_refs(counts.get(k.id, {}))}; "
              f"delete {drops}")
    print(f"source_keys to rewrite to the canonical form: {len(plan.rekeys)}")
    print(f"unmatched rows (key not recognised, left alone): {len(plan.unmatched)}")
    for row in plan.unmatched[:20]:
        print(f"  {row.id} {row.routine} {_when(row)}")


def cmd_dedupe_runs(args: argparse.Namespace) -> int:
    error = config.remote_write_error("postgres")
    if error is not None:
        print(error, file=sys.stderr)
        return REMOTE_REQUIRED_EXIT
    try:
        backend = config.backend()
    except config.BackendError as exc:
        print(f"amem: dedupe-runs failed: {exc}", file=sys.stderr)
        return 1
    if backend != "postgres":
        print("amem: dedupe-runs needs the Postgres backend: set AMEM_DB_URL "
              "(the REST backend cannot run it in one transaction). Nothing was changed.", file=sys.stderr)
        return 1
    try:
        with store.connect() as conn:
            if not args.apply:
                plan = build_plan(conn)
                ids = [r.id for g in plan.groups for r in (g.keeper, *g.duplicates)]
                print_plan(plan, reference_counts(conn, ids))
                print("dry run: nothing changed. Run with --apply to merge the duplicates and rewrite the keys.")
                return 0
            with conn.transaction():
                plan, applied = apply_plan(conn)
    except psycopg.Error as exc:
        print(f"amem: dedupe-runs failed (nothing changed): {type(exc).__name__}: {exc}".strip(),
              file=sys.stderr)
        return 1
    moved = ", ".join(f"{k} {v}" for k, v in sorted(applied.moved.items())) or "none"
    print(f"deleted {applied.deleted} duplicate runs in {len(plan.groups)} groups; "
          f"moved references: {moved}; rewrote {applied.rekeyed} source_keys; "
          f"{len(plan.unmatched)} unmatched rows left alone")
    return 0


def register(db_sub: Any) -> None:
    """Add `db dedupe-runs` to the `db` subparsers action."""
    p = db_sub.add_parser(
        "dedupe-runs",
        help="merge runs rows that are one logical run (old source_keys); dry run unless --apply",
    )
    p.add_argument("--apply", action="store_true",
                   help="merge and delete the duplicates and rewrite source_keys, in one transaction")
    p.set_defaults(func=cmd_dedupe_runs)
