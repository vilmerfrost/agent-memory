#!/usr/bin/env python3
"""`amem`, read-only against the database and with every provider switched off (backfill review, Mission 004C).

    uv run python scripts/ro_amem.py probe          prove it: two UPDATEs must be refused
    uv run python scripts/ro_amem.py <amem args>    e.g. sync plan --sink pastdev --contents --class own --json

Why: the Supabase session pooler silently ignores PGOPTIONS (`-c default_transaction_read_only=on` left
`default_transaction_read_only` off, verified 2026-10-17), and `env -u AMEM_PASTDEV_KEY` does not keep a key out
because amem loads its env files itself.

What it does, before the CLI runs:
1. amem's own env loading runs first; then every variable whose name holds KEY, SECRET, TOKEN or PASSWORD, and
   every AMEM_CLICKHOUSE_*, AMEM_LANGFUSE_* and LANGFUSE_* variable, is removed. AMEM_PASTDEV=off and
   AMEM_SYNC_<SINK>=off for every sink, so no provider can be reached. The database URL stays.
2. Every psycopg connection runs `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` (covers autocommit) and
   gets `read_only = True` (every BEGIN is READ ONLY): any write fails with ReadOnlySqlTransaction.
Nothing here prints a value.
"""
import os
import re
import sys

import psycopg

from agent_memory import config

config.load_env()
_DROP = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|^AMEM_CLICKHOUSE_|^AMEM_LANGFUSE_|^LANGFUSE_")
for _name in [n for n in os.environ if _DROP.search(n)]:
    del os.environ[_name]
os.environ["AMEM_PASTDEV"] = "off"
for _sink in ("CLICKHOUSE", "LANGFUSE", "PASTDEV"):
    os.environ[f"AMEM_SYNC_{_sink}"] = "off"

_orig = psycopg.Connection.connect.__func__


def _ro_connect(cls, *a, **k):
    conn = _orig(cls, *a, **k)
    ac = conn.autocommit
    conn.autocommit = True
    conn.execute("set session characteristics as transaction read only")
    conn.autocommit = ac
    conn.read_only = True
    return conn


psycopg.Connection.connect = classmethod(_ro_connect)
psycopg.connect = psycopg.Connection.connect

if sys.argv[1:] == ["probe"]:
    url = config.db_url()
    with psycopg.connect(url) as c:
        print("transaction_read_only =", c.execute("show transaction_read_only").fetchone()[0])
        try:
            c.execute("update sessions set meta = meta where false")
            print("UPDATE ALLOWED")
            sys.exit(1)
        except psycopg.errors.ReadOnlySqlTransaction:
            print("update refused: read-only transaction")
    with psycopg.connect(url, autocommit=True) as c:
        try:
            c.execute("update sessions set meta = meta where false")
            print("AUTOCOMMIT UPDATE ALLOWED")
            sys.exit(1)
        except psycopg.errors.ReadOnlySqlTransaction:
            print("update refused: read-only session (autocommit)")
    keys = sorted(n for n in os.environ if _DROP.search(n))
    print("provider keys in env:", len(keys))
    sys.exit(0)

from agent_memory.cli import main  # noqa: E402  (after the patch, so every module sees it)

sys.exit(main(sys.argv[1:]))
