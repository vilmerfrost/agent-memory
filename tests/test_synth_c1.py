"""Mission 004C: the C1 runner (scripts/c1_clickhouse.py) against a fake ClickHouse writer (synth_support.C1Fake)
and the synthetic dataset in a fresh database: every step passes on a correct sink; a planted leak, a wrong gold
value, a mis-masked team row and a bad host each fail the right step; --dry-run touches nothing."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import synth
from agent_memory.integrations import clickhouse as CH, sync
from integrations_mock import MockService
from synth_support import C1Fake, fresh_db, load_script

c1 = load_script("c1_clickhouse")


@pytest.fixture(scope="module")
def seeded(pg_url: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    with fresh_db(pg_url) as url:
        rep = synth.seed(url, datetime.now(UTC).date(), out=tmp_path_factory.mktemp("c1-gold"))
        yield url, Path(rep["gold"])


@pytest.fixture
def fresh_cursor(seeded: tuple[str, Path], monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict[str, Any]]:
    url, gold_path = seeded
    monkeypatch.setenv(CH.PSEUDONYM_ENV, "c" * 40)
    with psycopg.connect(url) as conn:                      # every C1 run starts like the real one: no cursor yet
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
    return url, json.loads(gold_path.read_text())


def _client(svc: MockService) -> CH.Client:
    return CH.Client(CH.Config(svc.url, "amem_writer", "writer-key-not-real"), sleep=lambda s: None)


def _steps(rep: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in rep["steps"]}


def test_c1_passes_every_step_on_a_correct_sink(fresh_cursor: tuple[str, dict[str, Any]]) -> None:
    url, gold = fresh_cursor
    with MockService() as svc:
        C1Fake(svc)
        rep = c1.run_c1(url, gold, client=_client(svc), environ={}, bad_url="http://127.0.0.1:1")
        wire = "\n".join(r.body.decode("utf-8", "replace") + json.dumps(r.query) for r in svc.requests)
    steps = _steps(rep)
    assert list(steps) == list(c1.STEPS) and rep["ok"] is True, {k: v["detail"] for k, v in steps.items() if not v["ok"]}
    assert steps["sync_run"]["data"]["rows"] == sum(gold["clickhouse"]["tables"].values())
    assert steps["sync_rerun"]["data"]["final"] == gold["clickhouse"]["tables"]
    assert all(q["ok"] and q["rows"] == q["gold_rows"] for q in steps["queries"]["data"]["queries"])
    assert len(steps["grants"]["data"]["probes"]) == 11
    assert steps["failure_path"]["data"]["status"] == "unavailable"
    assert steps["leak_scan"]["data"]["hits"] == {t.name: 0 for t in CH.TABLES}
    assert all(isinstance(s["seconds"], float) for s in rep["steps"]) and rep["synthetic"] is True
    assert "ZZLEAK" not in wire and "writer-key-not-real" not in json.dumps(rep)


def test_c1_catches_a_leak_a_wrong_value_and_a_masking_slip(fresh_cursor: tuple[str, dict[str, Any]]) -> None:
    url, gold = fresh_cursor
    bad_gold = json.loads(json.dumps(gold))
    bad_gold["clickhouse"]["queries"][0]["rows"][0]["metrics"]["n_deviations"] += 1
    with MockService() as svc:
        fake = C1Fake(svc)
        client = _client(svc)
        with psycopg.connect(url) as conn:
            sync.run(CH.ClickHouseSink(client), conn, environ={})
        team_sid = gold["team"]["sessions"][0]
        row = dict(fake.by_id("sessions")[team_sid])
        fake.rows["sessions"].append({**row, "project": "pipeline-poc-raw", "version": row["version"] + 1})
        fake.rows["deviations"].append({**fake.rows["deviations"][0], "rule_id": gold["markers"][0]["marker"]})
        rep = c1.run_c1(url, bad_gold, client=client, environ={}, bad_url="http://127.0.0.1:1")
    steps = _steps(rep)
    assert rep["ok"] is False
    assert not steps["leak_scan"]["ok"] and steps["leak_scan"]["data"]["hits"]["deviations"] == 1
    assert not steps["team_masking"]["ok"] and "team row" in steps["team_masking"]["detail"]
    q0 = steps["queries"]["data"]["queries"][0]
    assert not q0["ok"] and any("n_deviations" in d for d in q0["diffs"])
    assert steps["grants"]["ok"] and steps["failure_path"]["ok"]                # the others still ran and passed


def test_c1_preflight_refuses_a_remote_db_a_wrong_gold_and_an_unconfigured_clickhouse(
        fresh_cursor: tuple[str, dict[str, Any]]) -> None:
    url, gold = fresh_cursor

    def no(*a: object, **k: object) -> psycopg.Connection:
        raise AssertionError("connected")

    remote = c1.run_c1("postgresql://u:p@db.example.com/x", gold, client=CH.Client(CH.Config()), connect=no)
    assert remote["ok"] is False and [s["name"] for s in remote["steps"]] == ["preflight"]
    off = c1.run_c1(url, gold, client=CH.Client(CH.Config()), environ={})
    (pre,) = off["steps"]
    assert not pre["ok"] and "not configured" in pre["detail"]
    other = json.loads(json.dumps(gold))
    other["anchor"] = "2026-01-01"
    with MockService() as svc:
        C1Fake(svc)
        (pre,) = c1.run_c1(url, other, client=_client(svc), environ={})["steps"]
    assert not pre["ok"] and "not seeded with this gold" in pre["detail"]


def test_c1_dry_run_touches_nothing(seeded: tuple[str, Path], capsys: pytest.CaptureFixture[str],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    url, gold_path = seeded
    with MockService() as svc:
        monkeypatch.setenv("AMEM_CLICKHOUSE_URL", svc.url)
        monkeypatch.setenv("AMEM_CLICKHOUSE_USER", "amem_writer")
        monkeypatch.setenv("AMEM_CLICKHOUSE_KEY", "dry-run-key-not-real")
        monkeypatch.setattr(c1.psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("db")))
        assert c1.main(["--db-url", url, "--gold", str(gold_path), "--dry-run"]) == 0
        text = capsys.readouterr().out
        assert c1.main(["--db-url", url, "--gold", str(gold_path), "--dry-run", "--json"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert svc.requests == []
    assert [s["name"] for s in plan["steps"]] == list(c1.STEPS) and plan["clickhouse"]["key_set"] is True
    assert "dry-run-key-not-real" not in text + json.dumps(plan) and "postgres:postgres" not in text
    assert "nothing will be touched" in text
    assert c1.main(["--db-url", "postgresql://u:p@db.example.com/x", "--gold", str(gold_path), "--dry-run"]) == 2
    assert not (gold_path.parent / "c1-report.json").exists()
