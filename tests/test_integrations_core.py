"""Mission 004A shared core: refs and ids, the evidence facade, the sink loop, the HTTP client."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agent_memory.integrations import evidence as E, http, refs, sync
from integrations_mock import MockService

APP_PW = "core-test-pw-0123456789"


# ---------------------------------------------------------------- refs and ids


@pytest.mark.parametrize("text", [
    "amem:session/0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b", "amem:session/codex:019a-thread",
    "amem:session/0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b/agent-a3d6", "amem:proposal/0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b",
    "ch:deviations/0b3e7d2a-8c1f-4f6e-9a2b-1c3d4e5f6a7b", "ch:q.rule_gap/parent-md:dod-gate",
    "lf:trace/0123456789abcdef0123456789abcdef", "past:amem:proposal/x1", "ncm:run/ncm-20261020-sudoku-easy",
])
def test_refs_round_trip(text: str) -> None:
    r = refs.parse(text)
    assert str(r) == text and refs.is_ref(text)


@pytest.mark.parametrize("text", [
    "", "amem:nosuch/1", "amem:session/", "ch:Deviations/1", "lf:trace/x y", "http://x", "ncm:job/1",
    "amem:session/../etc", "amem:session/a b", "past:", 123,
])
def test_refs_reject(text: object) -> None:
    assert not refs.is_ref(text) if isinstance(text, str) else True
    with pytest.raises(refs.RefError):
        refs.parse(text)  # type: ignore[arg-type]


def test_ids_are_deterministic_and_well_formed() -> None:
    key = refs.run_key("exp-20261017-01", "A", "a-split", 0)
    assert key == "exp-20261017-01/A/a-split/0"
    t1, t2 = refs.trace_id(key), refs.trace_id(key)
    assert t1 == t2 and refs.TRACE_RE.fullmatch(t1)
    assert refs.trace_id(refs.run_key("exp-20261017-01", "B", "a-split", 0)) != t1
    s = refs.session_uuid(key)
    assert uuid.UUID(s).version == 5 and s == refs.session_uuid(key)
    assert refs.run_key("exp-20261016-edit-1a2b3c4d", "B", "case.1", 2)
    for bad in (("exp-1", "A", "c", 0), ("exp-20261017-01", "ab", "c", 0), ("exp-20261017-01", "A", "c d", 0),
                ("exp-20261017-01", "A", "c", -1)):
        with pytest.raises(refs.RefError):
            refs.run_key(*bad)


def test_evidence_ref_validates_subjects() -> None:
    E.EvidenceRef("ch:q.rule_gap/x", "ch", "metric", ["amem:deviation/1"])
    with pytest.raises(refs.RefError):
        E.EvidenceRef("ch:q.rule_gap/x", "ch", "metric", ["lf:trace/0123456789abcdef0123456789abcdef"])
    with pytest.raises(refs.RefError):
        E.EvidenceRef("not-a-ref", "ch", "metric")


# ---------------------------------------------------------------- facade


class _Fake:
    def __init__(self, source: str, *, up: bool = True, boom: bool = False, health_boom: bool = False):
        self.source, self.up, self.boom, self.health_boom, self.calls = source, up, boom, health_boom, []

    def health(self) -> E.Health:
        if self.health_boom:
            raise RuntimeError("socket")
        return E.Health(self.up, "" if self.up else "connection refused")

    def named_queries(self) -> Mapping[str, E.NamedQuery]:
        return {"q": E.NamedQuery("q", {"rule": r"[a-z-]{1,20}", "days": r"[1-9][0-9]{0,2}"}, "test", ("rule",))}

    def query(self, name: str, params: Mapping[str, str]) -> list[E.EvidenceRef]:
        self.calls.append((name, dict(params)))
        if self.boom:
            raise ValueError("secret-looking detail must not leak")
        ref = {"sb": "amem:deviation/d1", "pd": "past:x"}.get(self.source, f"{self.source}:q/{params['rule']}")
        return [E.EvidenceRef(ref, self.source, "metric", ["amem:deviation/d1"])]


def test_gather_puts_supabase_first_and_never_blocks() -> None:
    ch, sb, lf, pd = _Fake("ch"), _Fake("sb"), _Fake("lf", up=False), _Fake("pd", boom=True)
    res = E.gather("q", {"rule": "dod-gate"}, [ch, lf, pd, sb])
    assert [r.source for r in res] == ["sb", "ch", "lf", "pd"]
    assert [r.status for r in res] == ["ok", "ok", "unavailable", "error"]
    assert res[2].detail == "connection refused" and lf.calls == []
    assert res[3].detail == "ValueError"                     # type only, never the message
    assert len(E.flatten(res)) == 2


def test_gather_checks_params_before_any_call() -> None:
    f = _Fake("ch")
    assert E.gather("q", {"rule": "x; drop table"}, [f])[0].status == "bad-params"
    assert E.gather("q", {"days": "3"}, [f])[0].status == "bad-params"       # rule is required
    assert E.gather("q", {"rule": "a", "sql": "select"}, [f])[0].status == "bad-params"
    assert E.gather("nope", {}, [f])[0].status == "unknown-query"
    assert E.gather("q", {"rule": "a"}, [_Fake("ch", health_boom=True)])[0].status == "unavailable"
    assert f.calls == []


# ---------------------------------------------------------------- Supabase authority + sync loop (disposable Postgres)


@pytest.fixture
def app(pg_url: str) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("delete from rsi_evidence where system in ('clickhouse', 'langfuse', 'pastdev')")
        conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env) values "
                     "('core-s1', 'chat', '/x', 1, 'prod') on conflict do nothing")
    yield pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("delete from rsi_evidence where system in ('clickhouse', 'langfuse', 'pastdev')")
        conn.execute("delete from sessions where session_id = 'core-s1'")


def test_exists_and_usable_reread_supabase(app: str) -> None:
    with psycopg.connect(app) as conn:
        assert E.exists(conn, "amem:session/core-s1")
        assert not E.exists(conn, "amem:session/nope")
        good = E.EvidenceRef("ch:sessions/core-s1", "ch", "row", ["amem:session/core-s1"])
        stale = E.EvidenceRef("ch:sessions/gone", "ch", "row", ["amem:session/core-s1", "amem:session/gone"])
        assert E.usable(conn, good) and not E.usable(conn, stale)
        with pytest.raises(refs.RefError):
            E.exists(conn, "ch:sessions/core-s1")


def test_supabase_retriever_and_record_are_idempotent(app: str) -> None:
    with psycopg.connect(app) as conn:
        ev = E.EvidenceRef("lf:trace/0123456789abcdef0123456789abcdef", "lf", "trace", ["amem:session/core-s1"],
                           {"spans": 3}, "sandbox trace")
        a, b = E.record(conn, ev), E.record(conn, ev)
        conn.commit()
        assert a == b
    sb = E.SupabaseRetriever(lambda: psycopg.connect(app))
    (res,) = E.gather("evidence_for", {"ref": "amem:session/core-s1"}, [sb])
    assert res.status == "ok" and [r.ref for r in res.refs] == ["lf:trace/0123456789abcdef0123456789abcdef"]
    assert res.refs[0].source == "lf" and res.refs[0].metrics == {"spans": 3}
    assert E.gather("ref", {"ref": "amem:session/core-s1"}, [sb])[0].refs[0].ref == "amem:session/core-s1"
    assert E.gather("ref", {"ref": "amem:session/nope"}, [sb])[0].refs == []


class _Sink:
    name = "clickhouse"

    def __init__(self, *, up: bool = True, fail_tables: tuple[str, ...] = ()):
        self.up, self.fail_tables, self.pushed, self.seen_marks = up, set(fail_tables), [], []

    def health(self) -> E.Health:
        return E.Health(self.up, "" if self.up else "down")

    def batches(self, conn: psycopg.Connection, watermarks: Mapping[str, str], *, replay: bool) -> Iterator[sync.Batch]:
        self.seen_marks.append(dict(watermarks))
        for t in ("runs", "sessions"):
            start = int(watermarks.get(t, "0"))
            for i in range(start + 1, 3):
                yield sync.Batch(t, [{"id": f"{t}-{i}"}], str(i),
                                 [E.EvidenceRef(f"ch:{t}/{t}-{i}", "ch", "row", ["amem:session/core-s1"])])

    def push(self, batch: sync.Batch) -> None:
        if batch.table in self.fail_tables:
            raise RuntimeError("HTTP 503")
        self.pushed.append((batch.table, batch.rows[0]["id"]))


def test_sync_advances_the_cursor_and_records_links(app: str) -> None:
    s = _Sink()
    with psycopg.connect(app) as conn:
        rep = sync.run(s, conn, environ={})
        assert (rep.status, rep.batches, rep.rows) == ("ok", 4, 4)
        assert sync.load(conn, "clickhouse")["watermarks"] == {"runs": "2", "sessions": "2"}
        n = conn.execute("select count(*) from rsi_evidence where system = 'clickhouse'").fetchone()[0]
        assert n == 4
        rep2 = sync.run(s, conn, environ={})                       # nothing new
        assert (rep2.status, rep2.batches) == ("ok", 0) and s.seen_marks[-1] == {"runs": "2", "sessions": "2"}
        rep3 = sync.run(s, conn, replay=True, environ={})          # replay = backfill from zero, idempotent links
        assert rep3.batches == 4 and s.seen_marks[-1] == {}
        assert conn.execute("select count(*) from rsi_evidence where system = 'clickhouse'").fetchone()[0] == 4
        (st,) = sync.status(conn)
        assert st["sink"] == "clickhouse" and st["status"] == "done" and st["tables"] == ["runs", "sessions"]


def test_sync_down_or_off_moves_nothing(app: str) -> None:
    with psycopg.connect(app) as conn:
        sync.run(_Sink(), conn, environ={})
        before = sync.load(conn, "clickhouse")["watermarks"]
        rep = sync.run(_Sink(up=False), conn, environ={})
        assert rep.status == "unavailable" and sync.load(conn, "clickhouse")["watermarks"] == before
        assert sync.run(_Sink(), conn, environ={"AMEM_SYNC_CLICKHOUSE": "off"}).status == "off"
        assert sync.switched_off("pastdev", {"AMEM_PASTDEV": "off"})


def test_sync_failure_backs_off_then_goes_dead(app: str) -> None:
    s = _Sink(fail_tables=("runs",))
    t0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
    with psycopg.connect(app) as conn:
        rep = sync.run(s, conn, now=t0, environ={})
        assert rep.status == "partial" and rep.failed == 1 and ("sessions", "sessions-1") in s.pushed
        meta = sync.load(conn, "clickhouse")
        assert "runs" not in meta["watermarks"] and meta["failures"]["runs"]["attempts"] == 1
        rep = sync.run(s, conn, now=t0 + timedelta(seconds=30), environ={})      # inside the 1-minute backoff
        assert rep.skipped_tables == ["runs"] and rep.failed == 0
        now = t0
        for _ in range(6):
            now += timedelta(hours=3)
            sync.run(s, conn, now=now, environ={})
        f = sync.load(conn, "clickhouse")["failures"]["runs"]
        assert f["dead"] and f["attempts"] == 6
        assert sync.status(conn)[0]["dead"] == ["runs"]
        s.fail_tables.clear()
        assert "runs" in sync.run(s, conn, now=now + timedelta(days=1), environ={}).skipped_tables   # dead stays dead


def test_sync_budget_stops_between_batches(app: str) -> None:
    ticks = iter([0.0, 0.0, 0.5, 2.0, 2.0, 2.0, 2.0, 2.0])
    with psycopg.connect(app) as conn:
        rep = sync.run(_Sink(), conn, budget_s=1.0, clock=lambda: next(ticks), environ={})
        assert rep.status == "partial" and rep.detail == "time budget" and rep.batches == 2


# ---------------------------------------------------------------- HTTP client


def test_http_retries_then_succeeds_and_hides_headers() -> None:
    with MockService() as svc:
        svc.route("POST", "/x", lambda r: (200, {"ok": True}))
        svc.fail_next(2, 503)
        slept: list[float] = []
        r = http.request("POST", svc.url + "/x", headers={"Authorization": "Bearer sk-secret-123456789"},
                         body={"a": 1}, sleep=slept.append, retries=2)
        assert r.json() == {"ok": True} and slept == [0.5, 1.0] and len(svc.requests) == 3
        assert svc.requests[-1].json == {"a": 1}
        svc.fail_next(3, 500)
        with pytest.raises(http.HttpError) as ei:
            http.request("GET", svc.url + "/x", headers={"Authorization": "Bearer sk-secret-123456789"},
                         sleep=lambda s: None, retries=2)
        assert ei.value.status == 500 and "sk-secret" not in str(ei.value)
        with pytest.raises(http.HttpError):
            http.request("GET", svc.url + "/missing", sleep=lambda s: None)          # 404: no retry
        assert len(svc.requests_to("GET", "/missing")) == 1


def test_http_refuses_plain_http_off_localhost() -> None:
    with pytest.raises(http.HttpError):
        http.request("GET", "http://example.com/")
