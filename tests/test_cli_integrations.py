"""Mission 004A CLI: `amem sync ...` and `amem evidence query ...` before activation (nothing configured)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from agent_memory import cli

APP_PW = "cli-int-pw-0123456789"


@pytest.fixture
def app(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env) values "
                     "('cli-int-s1', 'chat', '/x', 1, 'prod') on conflict do nothing")
    url = pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)
    monkeypatch.setenv("AMEM_DB_URL", url)
    yield url
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("delete from rsi_checkpoints where job like 'sync:%'")
        conn.execute("delete from sessions where session_id = 'cli-int-s1'")


def test_status_before_any_sync(app: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sync", "status"]) == 0
    assert capsys.readouterr().out.strip() == "no sync has run"


@pytest.mark.parametrize("sink", ["clickhouse", "langfuse", "pastdev"])
def test_unconfigured_sinks_are_unavailable_and_move_nothing(app: str, sink: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sync", "run", "--sink", sink, "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["status"] == "unavailable" and rep["rows"] == 0
    with psycopg.connect(app) as conn:
        (meta,) = conn.execute("select meta from rsi_checkpoints where job = %s", (f"sync:{sink}",)).fetchone()
    assert "watermarks" not in meta and meta["last_error"].startswith("unavailable")


def test_kill_switch_wins(app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AMEM_PASTDEV", "off")
    assert cli.main(["sync", "run", "--sink", "pastdev"]) == 0
    assert "off" in capsys.readouterr().out


def test_evidence_query_supabase_and_param_checks(app: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["evidence", "query", "--source", "sb", "ref", "--param", "ref=amem:session/cli-int-s1", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["refs"][0]["ref"] == "amem:session/cli-int-s1"
    assert cli.main(["evidence", "query", "--source", "sb", "ref", "--param", "ref=amem:session/x;drop"]) == 2
    assert cli.main(["evidence", "query", "--source", "sb", "nope"]) == 2
    assert cli.main(["evidence", "query", "--source", "sb", "ref", "--param", "noequals"]) == 2
    for src, q in (("ch", "rule_gap"), ("lf", "experiment_scores"), ("pd", "recall")):
        capsys.readouterr()
        cli.main(["evidence", "query", "--source", src, q, "--json"])
        assert json.loads(capsys.readouterr().out)["status"] in ("unavailable", "bad-params"), src


def test_verify_without_clickhouse_fails_closed(app: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sync", "verify", "--sink", "clickhouse"]) == 1
    assert "unavailable" in capsys.readouterr().err


def _plan(capsys: pytest.CaptureFixture[str], *args: str) -> dict[str, Any]:
    capsys.readouterr()
    assert cli.main(["sync", "plan", "--replay", "--json", *args]) == 0
    return json.loads(capsys.readouterr().out)


def test_tables_and_since_narrow_plan_and_run(app: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    """Mission 004C (F4): --tables and --since on `sync plan` and `sync run`."""
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path))
    from agent_memory.integrations import http
    monkeypatch.setattr(http, "request", lambda *a, **k: pytest.fail("plan must not call the provider"))
    out = _plan(capsys, "--sink", "clickhouse", "--tables", "sessions, runs")
    assert set(out["tables"]) <= {"sessions", "runs"} and out["tables"]["sessions"]["rows"] >= 1   # cli-int-s1
    assert out["scope"] == {"tables": ["sessions", "runs"]}
    out = _plan(capsys, "--sink", "clickhouse", "--tables", "sessions", "--since", "2999-01-01")
    assert out["rows"] == 0 and out["scope"] == {"tables": ["sessions"], "since": "2999-01-01"}
    assert "scope" not in _plan(capsys, "--sink", "clickhouse")
    assert _plan(capsys, "--sink", "pastdev", "--tables", "edits", "--since", "2999-01-01")["rows"] == 0
    assert _plan(capsys, "--sink", "langfuse", "--tables", "sandbox_runs", "--since", "2026-10-01")["rows"] == 0
    for sink, bad in (("clickhouse", "sessions,nope"), ("pastdev", "sessions"), ("langfuse", "runs")):
        for cmd in ("plan", "run"):
            capsys.readouterr()
            assert cli.main(["sync", cmd, "--sink", sink, "--tables", bad]) == 2, (sink, cmd)
            err = capsys.readouterr().err
            assert "unknown table(s)" in err and "known:" in err, err
    for flag, value in (("--since", "yesterday"), ("--since", "2026-13-01"), ("--tables", " , ")):
        with pytest.raises(SystemExit) as exc:
            cli.main(["sync", "run", "--sink", "clickhouse", flag, value])
        assert exc.value.code == 2
    capsys.readouterr()
    assert cli.main(["sync", "run", "--sink", "clickhouse", "--tables", "runs", "--since", "2026-10-01", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["status"] == "unavailable" and rep["scope"] == {"tables": ["runs"], "since": "2026-10-01"}


def test_plan_counts_rows_masked_as_team_and_private(app: str, pg_url: str, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """Mission 004C (F8): the owner reviews team vs private masking before a backfill."""
    from agent_memory.integrations import http
    monkeypatch.setattr(http, "request", lambda *a, **k: pytest.fail("plan must not call the provider"))
    with psycopg.connect(pg_url, autocommit=True) as conn:
        for sid, meta in (("cli-int-own", '{"team": false, "remote_owner": "vilmerfrost"}'),
                          ("cli-int-team", '{"team": true}')):
            conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env, meta) values "
                         "(%s, 'chat', '/x', 1, 'prod', %s::jsonb) on conflict do nothing", (sid, meta))
    try:
        out = _plan(capsys, "--sink", "clickhouse", "--tables", "sessions")
        n = out["masked"]["tables"]["sessions"]
        assert n["own"] >= 1 and n["team"] >= 1 and n["private"] >= 1          # cli-int-s1: no ownership stored
        assert out["masked"]["owner_accounts"] == ["vilmerfrost"] and out["masked"]["private"] == n["private"]
        monkeypatch.setenv("AMEM_OWNER_ACCOUNTS", "someone-else")
        assert _plan(capsys, "--sink", "clickhouse", "--tables", "sessions")["masked"]["tables"]["sessions"]["own"] == 0
        assert "team" in _plan(capsys, "--sink", "pastdev")["masked"]
        capsys.readouterr()
        assert cli.main(["sync", "plan", "--sink", "clickhouse", "--replay", "--tables", "sessions"]) == 0
        text = capsys.readouterr().out
        assert "masked: team=" in text and "private=" in text and "(owner accounts: someone-else)" in text
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("delete from sessions where session_id in ('cli-int-own', 'cli-int-team')")


@pytest.mark.parametrize("sink", ["clickhouse", "langfuse", "pastdev"])
def test_plan_is_a_read_only_dry_run(app: str, sink: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path))
    from agent_memory.integrations import http
    monkeypatch.setattr(http, "request", lambda *a, **k: pytest.fail("plan must not call the provider"))
    assert cli.main(["sync", "plan", "--sink", sink, "--replay", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sink"] == sink and out["replay"] is True and out["rows"] >= 0
