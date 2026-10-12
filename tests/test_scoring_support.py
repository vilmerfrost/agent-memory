"""Shared helpers for tests/test_scoring_*.py. Holds no tests.

`sdb` gives each test its own fresh database inside the session's throwaway
Postgres container (conftest `pg_url`), migrated, and points AMEM_DB_URL and
AMEM_QUESTIONS_DIR at it and at the test question set. Scoring commands read
every routine in the database, so a private database keeps tests independent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import store
from conftest import base_env

TESTS_DIR = Path(__file__).resolve().parent
TEST_QUESTIONS = TESTS_DIR / "fixtures" / "questions"
SAMPLE_LOG = TESTS_DIR / "fixtures" / "routines.sample.log"


@dataclass
class ScoringDb:
    url: str
    env: dict[str, str]


@pytest.fixture
def sdb(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[ScoringDb]:
    name = f"amem_t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    try:
        store.migrate(db_url=url)
        monkeypatch.setenv("AMEM_DB_URL", url)
        monkeypatch.setenv("AMEM_QUESTIONS_DIR", str(TEST_QUESTIONS))
        yield ScoringDb(url, base_env(AMEM_DB_URL=url, AMEM_QUESTIONS_DIR=str(TEST_QUESTIONS)))
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(f'drop database if exists "{name}" with (force)')


def add_run(
    url: str,
    routine: str,
    status: str,
    summary: str | None,
    started_at: datetime,
    failures: list[str] | None = None,
) -> uuid.UUID:
    return store.insert_run(routine, status, summary=summary, started_at=started_at,
                            meta={"failures": failures or []}, db_url=url)


def add_score(
    url: str,
    run_id: uuid.UUID,
    question: str,
    kind: str,
    value: str,
    value_num: float | None,
    confidence: float | None,
    probabilities: dict[str, Any] | None = None,
) -> None:
    """Insert a score row directly (calibrate/drift tests need many scored runs)."""
    with psycopg.connect(url) as conn:
        conn.execute(
            "insert into scores (run_id, question, kind, value, value_num, confidence, probabilities, jev_mode) "
            "values (%s, %s, %s, %s, %s, %s, %s, 'fixture')",
            (run_id, question, kind, value, value_num, confidence, Jsonb(probabilities or {})),
        )


def add_label(url: str, run_id: uuid.UUID, verdict: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into labels (run_id, verdict) values (%s, %s)", (run_id, verdict))
