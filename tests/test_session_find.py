"""`amem sessions find --agent-id`: a worker transcript to its sessions row, then the
incident-scan chain `find -> deviations scan --session <id> --dry-run`."""

from __future__ import annotations

import json

import psycopg
import pytest

from agent_memory import session_find
from conftest import base_env, run_amem
from test_deviations_support import Home, busy, setup_home, worker_events, write_transcript  # noqa: F401

AGENT = "afix0123456789abc"
PARENT = "parent-incident"


def _worker(home: Home, agent: str = AGENT) -> str:
    cwd = str(home.repo)
    sub = home.project("-work-demo-repo") / PARENT / "subagents"
    write_transcript(sub / f"agent-{agent}.jsonl", worker_events(cwd) + busy(cwd))
    (sub / f"agent-{agent}.meta.json").write_text(json.dumps({"agentType": "worker", "toolUseId": "a1"}))
    return f"{PARENT}/agent-{agent}"


def _env(home: Home, pg_url: str) -> dict[str, str]:
    return base_env(AMEM_DB_URL=pg_url, AMEM_CLAUDE_HOME=str(home.claude), AMEM_PROJECTS_DIR=str(home.projects),
                    AMEM_AGENT_CONFIG_DIR=str(home.agent_config))


@pytest.fixture
def clean(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from sessions")
    return pg_url


def test_not_ingested_exits_1_with_a_clear_message(setup_home: Home, clean: str) -> None:  # noqa: F811
    _worker(setup_home)
    res = run_amem("sessions", "find", "--agent-id", AGENT, env=_env(setup_home, clean))
    assert res.returncode == 1
    assert res.stdout == ""
    assert "not ingested" in res.stderr and "--ingest" in res.stderr


def test_ingest_then_find_prints_id_category_project(setup_home: Home, clean: str) -> None:  # noqa: F811
    sid = _worker(setup_home)
    env = _env(setup_home, clean)
    res = run_amem("sessions", "find", "--agent-id", AGENT, "--ingest", env=env)
    assert res.returncode == 0, res.stderr
    assert "1 new" in res.stderr
    session_id, category, project = res.stdout.strip().split("\t")
    assert session_id == sid and category == "worker" and project
    # Idempotent, and a plain find now succeeds without --ingest.
    again = run_amem("sessions", "find", "--agent-id", AGENT, "--ingest", env=env)
    assert again.returncode == 0 and "0 new" in again.stderr
    plain = run_amem("sessions", "find", "--agent-id", AGENT, "--json", env=env)
    assert plain.returncode == 0, plain.stderr
    row = json.loads(plain.stdout)
    assert row["session_id"] == sid and row["kind"] == "worker" and row["scanned_at"] is None


def test_ingest_without_a_transcript_exits_1(setup_home: Home, clean: str) -> None:  # noqa: F811
    res = run_amem("sessions", "find", "--agent-id", "nosuchagent", "--ingest", env=_env(setup_home, clean))
    assert res.returncode == 1 and "no transcript agent-nosuchagent.jsonl" in res.stderr


def test_suffix_match_is_exact(setup_home: Home, clean: str) -> None:  # noqa: F811
    _worker(setup_home, "abc")
    _worker(setup_home, "xabc")
    session_find.ingest("abc", setup_home.projects, db_url=clean)
    session_find.ingest("xabc", setup_home.projects, db_url=clean)
    assert [r["session_id"] for r in session_find.find("abc", db_url=clean)] == [f"{PARENT}/agent-abc"]


@pytest.mark.parametrize("bad", ["../x", "a/b", "a%b", "", "a_b*"])
def test_bad_agent_id_is_a_usage_error(setup_home: Home, bad: str) -> None:  # noqa: F811
    res = run_amem("sessions", "find", "--agent-id", bad, env=base_env(AMEM_DB_URL="postgresql://127.0.0.1:9/x"))
    assert res.returncode == 2 and "not an agent id" in res.stderr


def test_rest_backend_is_refused(setup_home: Home) -> None:  # noqa: F811
    env = base_env(AMEM_BACKEND="rest", AMEM_SUPABASE_URL="http://127.0.0.1:9")
    res = run_amem("sessions", "find", "--agent-id", AGENT, env=env)
    assert res.returncode == 1 and "Postgres backend" in res.stderr


def test_incident_chain_find_then_dry_run_scan_selects_the_worker(setup_home: Home, clean: str) -> None:  # noqa: F811
    sid = _worker(setup_home)
    env = _env(setup_home, clean)
    found = run_amem("sessions", "find", "--agent-id", AGENT, "--ingest", env=env)
    assert found.returncode == 0, found.stderr
    session_id = found.stdout.split("\t", 1)[0]
    scan = run_amem("deviations", "scan", "--session", session_id, "--dry-run", env=env)
    assert scan.returncode == 0, scan.stderr
    lines = scan.stdout.strip().splitlines()
    assert lines[0].startswith(f"dry-run {sid} kind=worker"), scan.stdout
    assert "agent:worker" in scan.stdout
