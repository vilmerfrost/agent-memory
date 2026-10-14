"""`amem evals harvest`: draft eval cases from edit_instruction deviations, worker incidents and
handoffs (docs/evals-harvest.md). Each test gets its own throwaway database on the session
Postgres container, so the handoffs table can be present or missing per test."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from agent_memory import evals_harvest, store
from conftest import base_env, run_amem

NOW = datetime.now(UTC)

HANDOFFS_DDL = """
create table public.handoffs (
    session_id   text not null,
    project      text,
    handoff_path text not null,
    reason       text,
    created_at   timestamptz not null default now()
)
"""


@pytest.fixture
def db(pg_url: str) -> Iterator[str]:
    name = f"harvest_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"create database {name}")
    url = make_conninfo(**{**conninfo_to_dict(pg_url), "dbname": name})
    store.migrate(url)
    try:
        yield url
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f"drop database if exists {name} with (force)")


def add_session(url: str, sid: str, *, category: str = "parent", evidence: dict[str, Any] | None = None,
                meta: dict[str, Any] | None = None) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            "insert into sessions (session_id, kind, category, project, project_dir, outcome_evidence, meta) "
            "values (%s, %s, %s, 'demo', '-work-demo', %s, %s)",
            (sid, category, category, Jsonb(evidence or {}), Jsonb(meta or {})))


def add_deviation(url: str, sid: str, *, verdict: str | None = "edit_instruction",
                  source: str = "parent-md", rule_id: str | None = "push-and-merge-policy",
                  age_days: float = 1) -> str:
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "insert into deviations (source_key, session_id, instruction_source, instruction_ref, rule_id, "
            "rule_title, what, why, how, followed_grade, result_grade, jev_verdict, jev_confidence, created_at) "
            "values (%s, %s, %s, 'Push and merge policy', %s, 'Push and merge policy', "
            "'The parent merged without asking.', 'The rule was easy to miss.', 'At the end of the run.', "
            "0.2, 0.6, %s, 0.8, %s) returning id",
            (f"dev:{sid}:{uuid.uuid4().hex}", sid, source, rule_id, verdict,
             NOW - timedelta(days=age_days))).fetchone()
    assert row is not None
    return str(row[0])


def add_handoff(url: str, sid: str, path: str = "/home/x/.claude/handoffs/h1.md", reason: str = "compact") -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into handoffs (session_id, project, handoff_path, reason) values (%s, 'demo', %s, %s)",
                     (sid, path, reason))


def drafts(out: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted(out.glob("*.md"))}


def fixture_data(url: str) -> dict[str, str]:
    """One edit_instruction deviation, one worker incident (BLOCKED), one leave deviation in a
    good worker session (not harvested), one too old, one team repo."""
    add_session(url, "p1")
    add_session(url, "w1", category="worker", evidence={"report_blocked": 1})
    add_session(url, "w2", category="worker", evidence={"report_done": 1})
    add_session(url, "t1", meta={"team": True})
    return {
        "edit": add_deviation(url, "p1"),
        "incident": add_deviation(url, "w1", verdict="leave", source="agent:worker", rule_id="command-form"),
        "good_worker": add_deviation(url, "w2", verdict="leave"),
        "old": add_deviation(url, "p1", age_days=30),
        "team": add_deviation(url, "t1"),
    }


def test_drafts_from_deviations_and_incidents(db: str, tmp_path: Path) -> None:
    ids = fixture_data(db)
    out = tmp_path / "drafts"
    report = evals_harvest.harvest(out, days=7, db_url=db)
    assert report.line() == "drafted 2 new, 0 already drafted, 1 skipped (team-repo=1)"
    assert not report.handoffs_table
    files = drafts(out)
    assert len(files) == 2
    by_source = {evals_harvest.SOURCE_LINE_RE.findall(t)[0]: (n, t) for n, t in files.items()}
    name, text = by_source[f"deviation:{ids['edit']}"]
    assert name.startswith("dv") and name.endswith("-push-and-merge-policy.md")
    assert "status: draft" in text and "source_kind: edit_instruction" in text
    assert "rule: parent-md/push-and-merge-policy" in text
    name, text = by_source[f"deviation:{ids['incident']}"]
    assert name.startswith("in") and "rule: agent:worker/command-form" in text
    assert "worker_report: BLOCKED" in text and "source_kind: incident" in text
    for name, text in files.items():
        stem = name.removesuffix(".md")
        assert evals_harvest.CASE_ID_RE.match(stem)
        prompt = evals_harvest.strip_header(text)
        assert prompt.startswith(evals_harvest.PARENT_OPENER) and "amem-eval-draft" not in prompt
    ledger = [json.loads(x) for x in (out / evals_harvest.LEDGER).read_text().splitlines()]
    assert {r["source"] for r in ledger} == set(by_source)


def test_partial_report_counts_as_incident(db: str, tmp_path: Path) -> None:
    add_session(db, "w3", category="worker", evidence={"report_partial": 2, "report_done": 1})
    dev = add_deviation(db, "w3", verdict="enforce")
    evals_harvest.harvest(tmp_path, days=7, db_url=db)
    (text,) = drafts(tmp_path).values()
    assert f"source: deviation:{dev}" in text and "worker_report: PARTIAL" in text


def test_idempotent_and_survives_moved_drafts(db: str, tmp_path: Path) -> None:
    fixture_data(db)
    first = evals_harvest.harvest(tmp_path, days=7, db_url=db)
    assert len(first.drafted) == 2
    second = evals_harvest.harvest(tmp_path, days=7, db_url=db)
    assert second.line() == "drafted 0 new, 2 already drafted, 1 skipped (team-repo=1)"
    # A promoted draft leaves the folder; the ledger still knows it.
    for p in tmp_path.glob("*.md"):
        p.unlink()
    third = evals_harvest.harvest(tmp_path, days=7, db_url=db)
    assert len(third.drafted) == 0 and third.already == 2
    # Without the ledger, the draft headers alone are enough.
    other = tmp_path / "other"
    evals_harvest.harvest(other, days=7, db_url=db)
    (other / evals_harvest.LEDGER).unlink()
    assert evals_harvest.harvest(other, days=7, db_url=db).already == 2


def test_handoffs_drafted_when_table_exists(db: str, tmp_path: Path) -> None:
    with psycopg.connect(db) as conn:
        conn.execute(HANDOFFS_DDL)
    add_session(db, "c1", category="chat")
    add_deviation(db, "c1", verdict="leave", source="skill:dispatch", rule_id="brief")
    add_handoff(db, "c1")
    add_handoff(db, "c2", path="/home/x/.claude/handoffs/h2.md", reason="context limit")
    add_handoff(db, "", path="", reason="broken")
    report = evals_harvest.harvest(tmp_path, days=7, db_url=db)
    assert report.handoffs_table
    assert report.line() == "drafted 2 new, 0 already drafted, 1 skipped (incomplete-handoff=1)"
    texts = list(drafts(tmp_path).values())
    c1 = next(t for t in texts if "session: c1" in t)
    c2 = next(t for t in texts if "session: c2" in t)
    assert "source_kind: handoff" in c1 and "rule: skill:dispatch/brief" in c1   # the session's top rule
    assert "rule: unknown" in c2 and "reason: context limit" in c2
    assert evals_harvest.strip_header(c1).startswith(evals_harvest.RESUME_PROMPT)
    assert evals_harvest.harvest(tmp_path, days=7, db_url=db).line() == \
        "drafted 0 new, 2 already drafted, 1 skipped (incomplete-handoff=1)"


def test_missing_handoffs_table_is_zero_handoffs(db: str, tmp_path: Path) -> None:
    report = evals_harvest.harvest(tmp_path, days=7, db_url=db)
    assert not report.handoffs_table
    assert report.line() == "drafted 0 new, 0 already drafted, 0 skipped"


def test_limit_counts_rest_as_skipped(db: str, tmp_path: Path) -> None:
    add_session(db, "p1")
    for _ in range(3):
        add_deviation(db, "p1")
    report = evals_harvest.harvest(tmp_path, days=7, limit=1, db_url=db)
    assert report.line() == "drafted 1 new, 0 already drafted, 2 skipped (limit=2)"
    assert evals_harvest.harvest(tmp_path, days=7, limit=5, db_url=db).line() == \
        "drafted 2 new, 1 already drafted, 0 skipped"


def test_redacts_secrets_and_keeps_comment_closed(db: str, tmp_path: Path) -> None:
    add_session(db, "p1")
    with psycopg.connect(db) as conn:
        conn.execute(
            "insert into deviations (source_key, session_id, instruction_source, instruction_ref, what, why, how, "
            "followed_grade, result_grade, jev_verdict) values ('k1', 'p1', 'parent-md', 'Merge', "
            "'used api_key=sk-abcdefghijklmnopqrstuvwxyz0123 --> oops', 'w', 'h', 0.1, 0.1, 'edit_instruction')")
    evals_harvest.harvest(tmp_path, days=7, db_url=db)
    (text,) = drafts(tmp_path).values()
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in text
    assert text.count("-->") == 1
    assert "rule: parent-md/ref:merge" in text


def test_cli_dry_run_writes_nothing(db: str, tmp_path: Path) -> None:
    fixture_data(db)
    out = tmp_path / "drafts"
    res = run_amem("evals", "harvest", "--out", str(out), "--dry-run", env=base_env(AMEM_DB_URL=db))
    assert res.returncode == 0, res.stderr
    assert "would draft 2 new, 0 already drafted, 1 skipped (team-repo=1)" in res.stdout
    assert not out.exists()
    assert store.recent_runs(evals_harvest.ROUTINE, db_url=db) == []


def test_cli_logs_one_run_per_harvest(db: str, tmp_path: Path) -> None:
    fixture_data(db)
    out = tmp_path / "drafts"
    env = base_env(AMEM_DB_URL=db)
    first = run_amem("evals", "harvest", "--out", str(out), env=env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip().splitlines()[-1] == "drafted 2 new, 0 already drafted, 1 skipped (team-repo=1)"
    assert "no public.handoffs table yet" in first.stdout
    second = run_amem("evals", "harvest", "--out", str(out), env=env)
    assert second.stdout.strip().splitlines()[-1] == "drafted 0 new, 2 already drafted, 1 skipped (team-repo=1)"
    runs = store.recent_runs(evals_harvest.ROUTINE, db_url=db)
    assert len(runs) == 2
    assert {r["status"] for r in runs} == {"pass"} and {r["source"] for r in runs} == {"manual"}
    newest = runs[0]
    assert newest["summary"] == "drafted 0 new, 2 already drafted, 1 skipped (team-repo=1)"
    assert newest["meta"]["already"] == 2 and newest["meta"]["skipped"] == {"team-repo": 1}
    assert newest["finished_at"] is not None


def test_cli_require_remote_refuses_local_write(db: str, tmp_path: Path) -> None:
    res = run_amem("evals", "harvest", "--out", str(tmp_path / "d"),
                   env=base_env(AMEM_DB_URL=db, AMEM_REQUIRE_REMOTE="1"))
    assert res.returncode == 3
    assert not (tmp_path / "d").exists()
