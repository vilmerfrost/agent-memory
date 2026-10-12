"""Shared helpers for tests/test_scoring_*.py. Holds no tests.

`sdb` gives each test its own fresh database inside the session's throwaway
Postgres container (conftest `pg_url`), migrated, and points AMEM_DB_URL and
AMEM_QUESTIONS_DIR at it and at the test question set. Scoring commands read
every routine in the database, so a private database keeps tests independent.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
import typesafe_sdk
from psycopg.types.json import Jsonb

from agent_memory import jev, scoring, store
from agent_memory.questions import QuestionCache
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


# --------------------------------------------------------------------------- Jev failures

JEV_FIXTURES = TESTS_DIR / "fixtures" / "jev"
DUMMY_KEY = "dummy-typesafe-key-never-print"


def t_routine_fixture_path(directory: Path, status: str, summary: str) -> Path:
    """Where fixture mode looks for the t-routine run with this status and summary."""
    run = {"routine": "t-routine", "status": status, "summary": summary, "failure_reason": None,
           "meta": {"failures": []}}
    qset = QuestionCache(TEST_QUESTIONS).get("t-routine")
    assert qset is not None
    return directory / f"{jev.fixture_key(scoring.format_state(run), qset.jev_questions())}.json"


def fixture_dir_with(tmp_path: Path, corrupt: dict[tuple[str, str], str]) -> Path:
    """A copy of tests/fixtures/jev plus one corrupt fixture per (status, summary) -> file text."""
    out = tmp_path / "jev-fixtures"
    shutil.copytree(JEV_FIXTURES, out)
    for (status, summary), text in corrupt.items():
        t_routine_fixture_path(out, status, summary).write_text(text, encoding="utf-8")
    return out


def sdk_headers() -> Any:
    import httpx2  # the SDK's own HTTP library; its errors take httpx2.Headers

    return httpx2.Headers({})


class FakeJevClient:
    """Stand-in for `typesafe_sdk.TypeSafeClient` in live mode (no network).

    `system_one` raises the error set in `errors` for a state whose summary matches;
    otherwise it answers with the recorded fixture whose `_state` equals the state,
    parsed by the real SDK response model. `calls` holds the summaries in call order.
    """

    errors: dict[str, BaseException] = {}
    calls: list[str] = []

    def __init__(self, api_key: str) -> None:
        assert api_key == DUMMY_KEY

    def __enter__(self) -> FakeJevClient:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def system_one(self, state: str, questions: dict[str, Any]) -> Any:
        summary = state.split("\nsummary: ", 1)[1].split("\n", 1)[0]
        FakeJevClient.calls.append(summary)
        if summary in FakeJevClient.errors:
            raise FakeJevClient.errors[summary]
        for path in sorted(JEV_FIXTURES.glob("*.json")):
            body = json.loads(path.read_text(encoding="utf-8"))
            if body.get("_state") == state:
                wire = {k: body[k] for k in ("model", "usage", "answers")}
                return typesafe_sdk.SystemOneResponse.model_validate_json(json.dumps(wire))
        raise AssertionError(f"FakeJevClient has no answer for state {state!r}")


@pytest.fixture
def live_jev(monkeypatch: pytest.MonkeyPatch) -> type[FakeJevClient]:
    """Live mode (dummy key + AMEM_JEV_LIVE=1) with the SDK client replaced by FakeJevClient."""
    monkeypatch.setenv("TYPESAFE_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    FakeJevClient.errors = {}
    FakeJevClient.calls = []
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", FakeJevClient)
    assert jev.mode() == "live"
    return FakeJevClient
