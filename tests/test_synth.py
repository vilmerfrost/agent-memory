"""Mission 004C: the synthetic RSI dataset (synth.py, `amem synth seed`).

Deterministic rows and gold; markers in Supabase only (never on the ClickHouse wire, never in a team record or a
strict field sent to past.dev); gold consistent with the fake ClickHouse (tests/test_integrations_clickhouse) and
with the Supabase question queries; the local-host guard. Each database is a fresh one inside the session's
throwaway Postgres (synth_support.fresh_db).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from agent_memory import synth
from agent_memory.integrations import clickhouse as CH, evidence as E, pastdev as PD, sb_queries, sync
from conftest import base_env, run_amem
from integrations_mock import MockService
from synth_support import C1Fake, fresh_db, load_script

ANCHOR = date(2026, 10, 17)


@pytest.fixture(scope="module")
def seeded(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, dict, Path]]:
    out = tmp_path_factory.mktemp("synth-gold")
    with fresh_db(pg_url) as url:
        rep = synth.seed(url, datetime.now(UTC).date(), out=out)
        yield url, rep, Path(rep["gold"])


def _gold(seeded: tuple[str, dict, Path]) -> dict:
    return json.loads(seeded[2].read_text())


# ---------------------------------------------------------------- pure: determinism and the guard


def test_build_is_deterministic() -> None:
    a, b = synth.build(ANCHOR), synth.build(ANCHOR)
    assert json.dumps(synth._jsonable(dict(a.rows)), sort_keys=True) == json.dumps(synth._jsonable(dict(b.rows)),
                                                                                   sort_keys=True)
    assert json.dumps(synth._jsonable(synth.gold(a)), sort_keys=True) == json.dumps(synth._jsonable(synth.gold(b)),
                                                                                    sort_keys=True)
    later = synth.build(ANCHOR + timedelta(days=3))
    assert [m["marker"] for m in later.markers] == [m["marker"] for m in a.markers]
    assert [r["id"] for r in later.rows["deviations"]] == [r["id"] for r in a.rows["deviations"]]
    assert later.rows["sessions"][0]["created_at"] - a.rows["sessions"][0]["created_at"] == timedelta(days=3)


def test_markers_are_unique_and_every_row_says_synthetic() -> None:
    ds = synth.build(ANCHOR)
    names = [m["marker"] for m in ds.markers]
    assert len(names) == len(set(names)) > 400 and all(n.startswith("ZZLEAK-") for n in names)
    assert {m["policy"] for m in ds.markers} == {synth.STRICT, synth.DECISION}
    assert not [m for m in ds.markers if m["team"] and m["policy"] != synth.STRICT]
    for t, rows in ds.rows.items():
        for r in rows:
            blob = json.dumps(synth._jsonable(r))
            assert "synth" in blob.lower(), (t, r)                # ids, labels or meta say synthetic
    # every column the dataset fills with a marker is one ClickHouse never receives (not in any allowlist)
    allow = {t.src: set(t.allowlist) for t in CH.TABLES}
    for m in ds.markers:
        col = m["column"].split(".", 1)[0]
        assert col not in allow.get(m["table"], set()) or col in ("project", "repo"), m   # project/repo: masked


@pytest.mark.parametrize("url, ok", [
    ("postgresql://postgres:pw@127.0.0.1:5432/x", True),
    ("postgresql://postgres:pw@localhost/x", True),
    ("host=127.0.0.1 port=5432 dbname=x user=u", True),
    ("postgresql://postgres:pw@[::1]:5432/x", True),
    ("postgresql://postgres:pw@db.uyolylfqjxoplpqmplpq.supabase.co:5432/postgres", False),
    ("postgresql://postgres:pw@127.0.0.1,db.example.com/x", False),
    ("postgresql:///x", False),
    ("host=127.0.0.1 hostaddr=10.0.0.5 dbname=x", False),
    ("not a url ===", False),
])
def test_check_local_refuses_any_remote_host(url: str, ok: bool) -> None:
    if ok:
        assert synth.check_local(url)
    else:
        with pytest.raises(synth.UnsafeTarget):
            synth.check_local(url)


def test_seed_refuses_a_remote_host_before_connecting() -> None:
    def no(*a: object, **k: object) -> psycopg.Connection:
        raise AssertionError("connected")

    with pytest.raises(synth.UnsafeTarget):
        synth.seed("postgresql://postgres:pw@aws-0-eu-north-1.pooler.supabase.com:6543/postgres", connect=no)


# ---------------------------------------------------------------- seeding a real database


def test_seed_counts_gold_and_reuse(seeded: tuple[str, dict, Path], tmp_path: Path) -> None:
    url, rep, gold_path = seeded
    gold = _gold(seeded)
    assert rep["status"] == "seeded" and rep["synthetic"] is True and rep["host"] == "127.0.0.1"
    with psycopg.connect(url) as conn:
        assert synth.db_counts(conn) == gold["counts"] == rep["counts"]
        assert synth._sentinel(conn)["anchor"] == gold["anchor"]
    again = run_amem("synth", "seed", "--db-url", url, "--json", "--out", str(tmp_path), env=base_env())
    assert again.returncode == 0, again.stderr
    out = json.loads(again.stdout)
    assert out["status"] == "reused" and out["anchor"] == gold["anchor"] and url not in again.stdout
    assert json.loads((tmp_path / "gold.json").read_text()) == gold            # same anchor, same gold
    other = run_amem("synth", "seed", "--db-url", url, "--anchor", "2026-01-01", "--out", str(tmp_path), env=base_env())
    assert other.returncode == 1 and "already seeded" in other.stderr
    remote = run_amem("synth", "seed", "--db-url", "postgresql://u:secretpw@db.example.com/x", env=base_env())
    assert remote.returncode == 2 and "secretpw" not in remote.stderr + remote.stdout


def test_seed_refuses_unmigrated_or_busy_databases(pg_url: str, tmp_path: Path) -> None:
    name = "amem_synth_unmigrated"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    try:
        with pytest.raises(synth.SeedError, match="not migrated"):
            synth.seed(pg_url.rsplit("/", 1)[0] + "/" + name, out=tmp_path)
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
    with fresh_db(pg_url) as url:
        with psycopg.connect(url) as conn:
            conn.execute("insert into sessions (session_id, kind, project_dir) values ('real-looking', 'chat', '/x')")
        with pytest.raises(synth.SeedError, match="holds data"):
            synth.seed(url, out=tmp_path)
        with psycopg.connect(url) as conn:
            assert synth.db_counts(conn)["sessions"] == 1 and synth._sentinel(conn) is None   # nothing written


def test_every_marker_is_in_supabase(seeded: tuple[str, dict, Path]) -> None:
    url, _, _ = seeded
    gold = _gold(seeded)
    with psycopg.connect(url) as conn:
        dump = "\n".join(r[0] for t in synth.TABLES
                         for r in conn.execute(sql.SQL("select t::text from {} t").format(sql.Identifier(t))).fetchall())
    missing = [m["marker"] for m in gold["markers"] if m["marker"] not in dump]
    assert missing == [] and len(gold["markers"]) > 400


# ---------------------------------------------------------------- gold vs ClickHouse (fake) and past.dev


@pytest.fixture
def pseudonym_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CH.PSEUDONYM_ENV, "s" * 40)


def test_gold_matches_a_clickhouse_sync(seeded: tuple[str, dict, Path], pseudonym_key: None) -> None:
    url, _, _ = seeded
    gold = _gold(seeded)
    c1 = load_script("c1_clickhouse")
    with MockService() as svc:
        fake = C1Fake(svc)
        client = CH.Client(CH.Config(svc.url, "amem_writer", "k"), sleep=lambda s: None)
        with psycopg.connect(url) as conn:
            rep = sync.run(CH.ClickHouseSink(client), conn, replay=True, environ={})
            assert rep.status == "ok" and rep.rows == sum(gold["clickhouse"]["tables"].values())
            ver = CH.verify(conn, 30, client=client)
            assert ver.parity, ver.mismatches()
            for t, per_day in gold["clickhouse"]["verify"].items():
                assert {d: v[0] for d, v in ver.counts[t].items()} == per_day, t
            assert {t: len(fake.final(t)) for t in gold["clickhouse"]["tables"]} == gold["clickhouse"]["tables"]
            r = CH.ClickHouseRetriever(client)
            for q in gold["clickhouse"]["queries"]:
                res = E.run_query(r, q["name"], q["params"])
                assert res.status == "ok", (q, res.detail)
                assert q["rows"] and c1.compare_query(q["rows"], res.refs) == [], q
                assert all(E.usable(conn, ev) for ev in res.refs)
        wire = "\n".join(f"{x.path} {json.dumps(x.query)} {x.body.decode('utf-8', 'replace')}" for x in svc.requests)
    assert "ZZLEAK" not in wire and "sk-ant-" not in wire and "@example.com" not in wire
    team = set(gold["team"]["sessions"])
    assert {i for i, s in fake.by_id("sessions").items() if s["project"] == "team"} == team
    assert all(s["project"].startswith("p_") for i, s in fake.by_id("sessions").items() if i not in team)


def test_pastdev_gets_only_sanitized_non_team_decision_text(seeded: tuple[str, dict, Path]) -> None:
    url, _, _ = seeded
    gold = _gold(seeded)
    with psycopg.connect(url) as conn:
        corpus = PD.corpus(conn)
    assert sorted(ref for ref, _ in corpus) == gold["pastdev"]["send"]
    assert not set(gold["pastdev"]["refuse_team"]) & {ref for ref, _ in corpus}
    text = "\n".join(c for _, c in corpus)
    strict = [m["marker"] for m in gold["markers"] if m["policy"] == synth.STRICT and m["marker"] in text]
    assert strict == [] and "sk-ant-" not in text and "@example.com" not in text and "/Users/" not in text
    allowed = [m["marker"] for m in gold["markers"] if m["policy"] == synth.DECISION]
    assert [m for m in allowed if m in text]                           # non-vacuous: decision text does go


def test_supabase_answers_match_the_story(seeded: tuple[str, dict, Path]) -> None:
    url, _, _ = seeded
    story = _gold(seeded)["questions"]["repeat_failures"]
    with psycopg.connect(url) as conn:
        pats = sb_queries.recurring_deviations(conn, {"days": "30", "min_n": "3"})
        got = {ev.metrics["rule_key"]: {k: ev.metrics[k] for k in ("n", "sessions", "claude_sessions", "codex_sessions")}
               for ev in pats}
        assert got == story["recurring"] and got[story["top_rule"]] == story["r1"]
        attempts = sb_queries.attempts_for(conn, {"rule": story["top_rule"]})
        assert sorted(ev.ref for ev in attempts) == story["attempts"]
        evals = sb_queries.evals_for(conn, {"experiment": story["experiments"][0]})
        assert {ev.ref.split("/", 1)[1]: ev.metrics["verdict"] for ev in evals} == \
            {k: v["verdict"] for k, v in story["evals"].items()}
        links = E.SupabaseRetriever(lambda: psycopg.connect(url)).query(
            "evidence_for", {"ref": f"amem:experiment/{story['experiments'][0]}", "limit": "50"})
    traces = {ev.metrics["arm"]: ev for ev in links if ev.ref.startswith("lf:trace/")}
    assert {arm: ev.ref for arm, ev in traces.items()} == story["trace_links"]
    assert traces["A"].metrics["check_passed"] is True and traces["B"].metrics["check_passed"] is False
    assert all("ZZLEAK" not in (ev.summary or "") for ev in links)
