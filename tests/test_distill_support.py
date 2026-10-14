"""Shared helpers for tests/test_distill_*.py. Holds no tests.

- `add_dispatch` inserts a run in the dispatch ingest shape (routine `dispatch`,
  source `manual`, so this does not depend on the sibling migration that adds the
  `dispatch` source; the pipeline selects on routine only).
- `jev_dir` points AMEM_JEV_FIXTURES_DIR at an empty tmp dir; `answer` writes the
  write-gate answer for one run there (fixture mode, no live Jev).
- `FakeWriter` replaces `distill.call_writer`: canned envelopes, no LLM call.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_memory import distill, jev, lessons, store

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)

WORKER = {
    "name": "v12-web",
    "final_status": "PARTIAL",
    "reasons": ["tests failed on base too", "no base-branch proof in report"],
    "denied_shapes": {"cd && git": 2, "heredoc": 1},
    "brief_gaps": ["brief did not name the dev port"],
    "errors": {"pytest": "3 failed"},
    "checkpoint_excerpt": "### 10:02 — tests\nDid: ran pytest, 3 failed.\nNext: compare with integration.",
}


def add_dispatch(url: str, *, repo: str | None = "simplitics", status: str = "fail",
                 summary: str = "3 workers, 1 partial", started_at: datetime = T0,
                 workers: list[dict[str, Any]] | None = None, routine: str = "dispatch",
                 **meta: Any) -> uuid.UUID:
    body: dict[str, Any] = {"task_id": "v12", "dod_first_pass": False, "follow_ups": 1, "denials": 3,
                            "workers": [WORKER] if workers is None else workers}
    if repo is not None:
        body["repo"] = repo
    body.update(meta)
    return store.insert_run(routine, status, source="manual", summary=summary, started_at=started_at,
                            meta=body, db_url=url)


def later(n: int) -> datetime:
    return T0 + timedelta(hours=n)


@pytest.fixture(autouse=True)
def no_real_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tripwire: the real writer would run a missing file, never `claude`.
    Import it into every distill test module (autouse there)."""
    monkeypatch.setenv(distill.CLAUDE_ENV, str(tmp_path / "no-claude-here"))


@pytest.fixture
def jev_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "jev"
    out.mkdir()
    monkeypatch.setenv("AMEM_JEV_FIXTURES_DIR", str(out))
    return out


def answer(directory: Path, url: str, run_id: uuid.UUID, reusable: float, general: float,
           confidence: float = 0.8) -> None:
    """Write the write-gate fixture for this run's state."""
    run = lessons.get_run(run_id, db_url=url)
    assert run is not None
    key = jev.fixture_key(distill.gate_state(run), lessons.PROPOSE_QUESTIONS)
    body = {
        "_note": "Hand-written by tests/test_distill_support.py.",
        "model": "jev-latest",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "answers": {
            "reusable": {"type": "noul", "noul": reusable},
            "generalizable": {
                "type": "score", "score": general, "confidence": confidence,
                "legend": {"0": "Specific to this one run", "1": "Applies to many future runs"},
                "probabilities": {"0": round(1 - general, 4), "1": general},
            },
        },
    }
    (directory / f"{key}.json").write_text(json.dumps(body), encoding="utf-8")


ACCEPT = (0.9, 0.8)
REJECT = (0.2, 0.1)


def envelope(model_reply: Any, **over: Any) -> str:
    """The `claude -p --output-format json` envelope around a model reply."""
    text = model_reply if isinstance(model_reply, str) else json.dumps(model_reply)
    body = {"type": "result", "subtype": "success", "is_error": False, "result": text,
            "session_id": "00000000-0000-0000-0000-000000000000", "total_cost_usd": 0.01}
    body.update(over)
    return json.dumps(body)


NEW = {
    "action": "new", "merge_slug": None,
    "title": "Prove test failures pre-exist on the base branch",
    "rule": "When tests fail in a worker, run them on the base branch, report the proof, avoid DONE without it.",
    "scope_task": None,
    "evidence": "v12-web reported PARTIAL: 3 pytest failures and no base-branch proof.",
}


@dataclass
class FakeWriter:
    """Stands in for distill.call_writer. Each reply is an envelope string, an
    Exception to raise, or a callable(prompt) -> envelope."""

    replies: list[Any] = field(default_factory=list)
    default: Any = None
    prompts: list[str] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def __call__(self, prompt: str, *, timeout: float = distill.DEFAULT_TIMEOUT) -> str:
        self.prompts.append(prompt)
        self.timeouts.append(timeout)
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(prompt)
        assert isinstance(reply, str), "FakeWriter has no reply left"
        return reply


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeWriter]:
    def use(*replies: Any, default: Any = None) -> FakeWriter:
        fake = FakeWriter(list(replies), default)
        monkeypatch.setattr(distill, "call_writer", fake)
        return fake
    return use


def test_run_cli_turns_the_session_hook_off_for_the_child(tmp_path, monkeypatch):
    """A headless model call must not show up as its own `sessions` row (the hook skips it)."""
    from agent_memory import distill

    monkeypatch.delenv("AMEM_HOOK_OFF", raising=False)
    script = tmp_path / "fake-claude"
    script.write_text('#!/bin/sh\ncat >/dev/null\nprintf "%s" "${AMEM_HOOK_OFF:-unset}"\n')
    script.chmod(0o755)
    assert distill.run_cli([str(script)], "prompt") == "1"
