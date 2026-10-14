"""`amem handoffs log|resume|list` on Postgres (throwaway container) and on the REST backend
(an in-process fake of `backend_rest._request`, no network)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
import pytest

from agent_memory import backend_rest, handoffs
from conftest import base_env, run_amem


@pytest.fixture
def clean(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from handoffs")
    return pg_url


def test_log_resume_list_cli(clean: str) -> None:
    env = base_env(AMEM_DB_URL=clean)
    res = run_amem("handoffs", "log", "--session", "s-old", "--path", "/tmp/h/HANDOFF.md", "--project",
                   "agent-memory", "--reason", "auto", "--tokens", "180000", "--pct", "90.5", "--title",
                   "Sequel: the funny title", env=env)
    assert res.returncode == 0, res.stderr
    hid = uuid.UUID(res.stdout.strip())

    listed = run_amem("handoffs", "list", "--days", "1", env=env)
    assert listed.returncode == 0, listed.stderr
    rows = [json.loads(line) for line in listed.stdout.splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == str(hid)
    assert {k: row[k] for k in ("session_id", "project", "handoff_path", "reason", "context_tokens", "context_pct",
                                "title", "resumed_session_id", "resumed_at")} == {
        "session_id": "s-old", "project": "agent-memory", "handoff_path": "/tmp/h/HANDOFF.md", "reason": "auto",
        "context_tokens": 180000, "context_pct": 90.5, "title": "Sequel: the funny title",
        "resumed_session_id": None, "resumed_at": None}
    assert row["created_at"]

    res = run_amem("handoffs", "resume", "--id", str(hid), "--session", "s-new", env=env)
    assert res.returncode == 0, res.stderr
    again = run_amem("handoffs", "resume", "--id", str(hid), "--session", "s-new", env=env)
    assert again.returncode == 0, again.stderr  # idempotent for the same session
    other = run_amem("handoffs", "resume", "--id", str(hid), "--session", "s-other", env=env)
    assert other.returncode == 1 and "already resumed by session s-new" in other.stderr

    row = json.loads(run_amem("handoffs", "list", env=env).stdout.splitlines()[0])
    assert row["resumed_session_id"] == "s-new" and row["resumed_at"]


def test_defaults_and_minimal_log(clean: str) -> None:
    hid = handoffs.log("s1", "/x.md", db_url=clean)
    (row,) = handoffs.recent(1, db_url=clean)
    assert row["id"] == hid and row["reason"] == "manual"
    assert row["project"] is None and row["context_tokens"] is None and row["title"] is None


def test_list_window_and_order(clean: str) -> None:
    old = handoffs.log("s-old", "/a.md", db_url=clean)
    new = handoffs.log("s-new", "/b.md", db_url=clean)
    with psycopg.connect(clean) as conn:
        conn.execute("update handoffs set created_at = now() - interval '10 days' where id = %s", (old,))
    assert [r["id"] for r in handoffs.recent(7, db_url=clean)] == [new]
    assert [r["id"] for r in handoffs.recent(30, db_url=clean)] == [new, old]


def test_resume_unknown_id_exits_1(clean: str) -> None:
    res = run_amem("handoffs", "resume", "--id", str(uuid.uuid4()), "--session", "s", env=base_env(AMEM_DB_URL=clean))
    assert res.returncode == 1 and "no handoff with id" in res.stderr


@pytest.mark.parametrize("args", [
    ("log", "--session", "s", "--path", "/x", "--reason", "crash"),
    ("log", "--session", "s", "--path", "/x", "--pct", "120"),
    ("log", "--session", "s", "--path", "/x", "--tokens", "-1"),
    ("log", "--session", " ", "--path", "/x"),
    ("resume", "--id", "not-a-uuid", "--session", "s"),
    ("list", "--days", "0"),
])
def test_bad_arguments_are_usage_errors(args: tuple[str, ...]) -> None:
    res = run_amem("handoffs", *args, env=base_env(AMEM_DB_URL="postgresql://127.0.0.1:9/x"))
    assert res.returncode == 2, res.stderr


def test_db_checks_reject_bad_values(clean: str) -> None:
    with psycopg.connect(clean) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into handoffs (session_id, handoff_path, reason) values ('s', '/x', 'crash')")


def test_rls_on_handoffs(pg_url: str) -> None:
    with psycopg.connect(pg_url) as conn:
        got = conn.execute("select relrowsecurity from pg_class where relname = 'handoffs'").fetchone()
        cols = [r[0] for r in conn.execute(
            "select column_name from information_schema.columns where table_name = 'handoffs'").fetchall()]
    assert got == (True,)
    # p1-harvest reads these: keep them stable.
    assert {"session_id", "project", "handoff_path", "reason", "created_at"} <= set(cols)


def test_require_remote_refuses_local_write(pg_url: str) -> None:
    env = base_env(AMEM_DB_URL=pg_url, AMEM_REQUIRE_REMOTE="1")
    res = run_amem("handoffs", "log", "--session", "s", "--path", "/x", env=env)
    assert res.returncode == 3


# ------------------------------------------------------------------ REST backend


class FakeRest:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, list[tuple[str, str]], Any, str | None, str]] = []

    def __call__(self, method: str, query: list[tuple[str, str]] | None = None, body: Any = None,
                 prefer: str | None = None, *, table: str = "runs") -> Any:
        q = dict(query or [])
        self.calls.append((method, list(query or []), body, prefer, table))
        assert table == "handoffs"
        if method == "POST":
            row = {"id": str(uuid.uuid4()), "created_at": "2026-10-14T00:00:00+00:00", "resumed_session_id": None,
                   "resumed_at": None, **body[0]}
            self.rows.append(row)
            return [{"id": row["id"]}]
        match = [r for r in self.rows if "id" not in q or r["id"] == q["id"].removeprefix("eq.")]
        if method == "PATCH":
            sid = body["resumed_session_id"]
            hit = [r for r in match if r["resumed_session_id"] in (None, sid)]
            for r in hit:
                r.update(body)
            return [{"id": r["id"]} for r in hit]
        return [{k: r.get(k) for k in q["select"].split(",")} for r in match]


@pytest.fixture
def rest(monkeypatch: pytest.MonkeyPatch) -> FakeRest:
    fake = FakeRest()
    monkeypatch.setenv("AMEM_BACKEND", "rest")
    monkeypatch.setenv("AMEM_SUPABASE_URL", "https://project.example.com")
    monkeypatch.setattr(backend_rest, "_request", fake)
    return fake


def test_rest_log_resume_list(rest: FakeRest) -> None:
    hid = handoffs.log("s-old", "/h.md", project="p", reason="auto", context_pct=88.0)
    method, _q, body, prefer, _t = rest.calls[0]
    assert method == "POST" and prefer == "return=representation"
    assert body == [{"session_id": "s-old", "project": "p", "handoff_path": "/h.md", "reason": "auto",
                     "context_tokens": None, "context_pct": 88.0, "title": None}]
    assert handoffs.resume(hid, "s-new").status == "ok"
    assert handoffs.resume(hid, "s-other") == handoffs.Resume("taken", "s-new")
    assert handoffs.resume(uuid.uuid4(), "s").status == "missing"
    (row,) = handoffs.recent(7)
    assert row["id"] == str(hid) and row["resumed_session_id"] == "s-new"
    get = [c for c in rest.calls if c[0] == "GET"][-1]
    q = dict(get[1])
    assert q["created_at"].startswith("gte.") and q["order"] == "created_at.desc"
