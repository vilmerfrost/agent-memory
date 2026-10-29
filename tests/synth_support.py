"""Helpers for the synthetic-dataset tests (Mission 004C).

- `fresh_db(pg_url)`: a new database inside the session's throwaway Postgres container, migrated with the
  Keychain tripwire (base_env), dropped after. The shared `postgres` database is never touched, so these tests
  cannot disturb (or be disturbed by) the suite's other fixtures.
- `load_script(name)`: imports scripts/<name>.py as a module (scripts/ is not a package).
- `C1Fake`: tests/test_integrations_clickhouse.FakeClickHouse plus what the C1 runner asks as the writer: the
  grant probes (DDL and user management answer ACCESS_DENIED), FINAL counts, the team-masking reads and the
  leak scan over every stored row version.
"""

from __future__ import annotations

import importlib.util
import json
import re
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import psycopg
from psycopg import sql

from conftest import base_env, run_amem
from integrations_mock import Request
from test_integrations_clickhouse import FakeClickHouse

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def fresh_db(pg_url: str) -> Iterator[str]:
    name = f"amem_synth_{secrets.token_hex(4)}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    try:
        res = run_amem("db", "migrate", env=base_env(AMEM_DB_URL=url))
        assert res.returncode == 0, res.stderr
        yield url
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


def load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"amem_script_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


_DENIED = "Code: 497. DB::Exception: amem_writer: Not enough privileges. (ACCESS_DENIED)"


@dataclass
class C1Fake(FakeClickHouse):
    user: str = "amem_writer"

    def _post(self, req: Request) -> tuple[int, str]:
        text = req.body.decode()
        if req.q("query") is None:
            if text.startswith("SHOW GRANTS"):
                return 200, _jsonl([{f"GRANTS FOR {self.user}": f"GRANT SELECT, INSERT ON amem.* TO {self.user}"}])
            if text.startswith("SELECT name FROM system.databases"):
                return 200, _jsonl([{"name": "amem"}, {"name": "system"}, {"name": "INFORMATION_SCHEMA"}])
            if ("system.users" in text or "system.query_log" in text or text.startswith(("ALTER ", "GRANT "))
                    or (text.startswith("CREATE ") and "probe" in text)):
                return 403, _DENIED
            if text.startswith("INSERT INTO amem.") and text.endswith(" WHERE 0"):
                return 200, ""
            m = re.fullmatch(r"SELECT count\(\) AS n FROM amem\.(\w+)( FINAL)?(?: WHERE _amem_deleted = 0)? FORMAT JSONEachRow", text)
            if m:
                return 200, _jsonl([{"n": str(len(self.final(m.group(1)) if m.group(2) else self.rows[m.group(1)]))}])
            m = re.fullmatch(r"SELECT count\(\) AS n FROM amem\.(\w+) WHERE multiSearchAnyCaseInsensitive\("
                             r"toString\(tuple\(\*\)\), \[(.*)\]\) FORMAT JSONEachRow", text)
            if m:
                needles = [n.lower() for n in re.findall(r"'([^']*)'", m.group(2))]
                hits = sum(any(n in json.dumps(r, ensure_ascii=False).lower() for n in needles)
                           for r in self.rows[m.group(1)])
                return 200, _jsonl([{"n": str(hits)}])
            m = re.fullmatch(r"SELECT (id, [a-z_, ]+) FROM amem\.(sessions|skill_calls) FINAL(?: WHERE _amem_deleted = 0)? FORMAT JSONEachRow", text)
            if m:
                cols = [c.strip() for c in m.group(1).split(",")]
                return 200, _jsonl([{c: r.get(c) for c in cols} for r in self.final(m.group(2))])
        return super()._post(req)
