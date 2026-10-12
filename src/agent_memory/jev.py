"""Thin wrapper over the TypeSafe (Jev) SDK `typesafe-sdk==0.7.4`.

Mode rule:
- `live` only when TYPESAFE_API_KEY is non-empty AND AMEM_JEV_LIVE=1.
- `fixture` otherwise. Fixture mode reads `tests/fixtures/jev/<key>.json`, where
  `<key>` is a hash of the state and the questions. A missing fixture raises
  `JevFixtureMissing`; it never falls back to a live call.

Fixture files hold the API response body (`{"model", "usage", "answers"}`), so
fixture and live answers are parsed by the same SDK answer models.

SDK facts (checked in the installed source, `typesafe_sdk/_schemas/models.py`):
- choice answer: `choice`, `confidence`, `probabilities`
- score answer: `score`, `confidence`, `legend`, `probabilities`
- noul answer: `noul` only (probability of yes). No confidence field.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import TypeAdapter

from agent_memory import config

Kind = Literal["choice", "score", "noul"]
Mode = Literal["fixture", "live"]


class JevError(RuntimeError):
    """Base error for this wrapper."""


class JevFixtureMissing(JevError):
    """Fixture mode found no recorded response for this state + questions."""


@dataclass(frozen=True)
class Question:
    """One typed question. Build it with `choice()`, `score()` or `noul()`."""

    name: str
    kind: Kind
    instructions: Any = None
    criteria: Any = None

    def wire(self) -> dict[str, Any]:
        """The question as the API dictionary form (unset fields left out)."""
        body: dict[str, Any] = {"type": self.kind}
        if self.instructions is not None:
            body["instructions"] = self.instructions
        if self.criteria is not None:
            body["criteria"] = self.criteria
        return body

    def to_sdk(self) -> Any:
        from typesafe_sdk import Choice, Noul, Score

        cls = {"choice": Choice, "score": Score, "noul": Noul}[self.kind]
        kwargs: dict[str, Any] = {}
        if self.instructions is not None:
            kwargs["instructions"] = self.instructions
        if self.criteria is not None:
            kwargs["criteria"] = self.criteria
        return cls(**kwargs)


def choice(name: str, instructions: Any, criteria: dict[str, Any]) -> Question:
    """Pick one label. `criteria` maps label -> description (or None)."""
    return Question(name, "choice", instructions, dict(criteria))


def score(name: str, instructions: Any, criteria: list[Any]) -> Question:
    """Rate on an ordered rubric. `criteria[i]` describes score level i."""
    return Question(name, "score", instructions, list(criteria))


def noul(name: str, instructions: Any = None, criteria: dict[str, Any] | None = None) -> Question:
    """Yes/no. `criteria` may describe the `true` and `false` outcomes."""
    return Question(name, "noul", instructions, dict(criteria) if criteria is not None else None)


@dataclass(frozen=True)
class JevResult:
    """One typed answer.

    value: the label (choice), the expected score (score) or P(yes) (noul).
    confidence: from the API for choice and score; None for noul (the API has none).
    """

    name: str
    kind: Kind
    value: str | float
    confidence: float | None
    probabilities: dict[str, float] = field(default_factory=dict)


def mode() -> Mode:
    return "live" if config.typesafe_api_key() and config.jev_live_flag() else "fixture"


def fixture_key(state: str, questions: list[Question]) -> str:
    """Deterministic key: sha256 of canonical JSON of state + questions (in given order)."""
    payload = {"state": state, "questions": [[q.name, q.wire()] for q in questions]}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _validate(state: str, questions: list[Question]) -> None:
    if not isinstance(state, str) or not state:
        raise JevError("state must be a non-empty string")
    if not questions:
        raise JevError("at least one question is required")
    names = [q.name for q in questions]
    if len(set(names)) != len(names):
        raise JevError(f"question names must be unique: {names}")
    for q in questions:
        if q.kind not in ("choice", "score", "noul"):
            raise JevError(f"unknown question kind {q.kind!r} for {q.name!r}")


_ANSWER_ADAPTER: TypeAdapter[Any] | None = None


def _answer_adapter() -> TypeAdapter[Any]:
    global _ANSWER_ADAPTER
    if _ANSWER_ADAPTER is None:
        from typesafe_sdk import Answer

        _ANSWER_ADAPTER = TypeAdapter(Answer)
    return _ANSWER_ADAPTER


def _to_result(name: str, answer: Any) -> JevResult:
    from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer

    if isinstance(answer, ChoiceAnswer):
        return JevResult(name, "choice", answer.choice, answer.confidence, dict(answer.probabilities))
    if isinstance(answer, ScoreAnswer):
        probs = {str(k): v for k, v in answer.probabilities.items()}
        return JevResult(name, "score", answer.score, answer.confidence, probs)
    if isinstance(answer, NoulAnswer):
        return JevResult(name, "noul", answer.noul, None, {})
    raise JevError(f"unsupported answer type for {name!r}: {type(answer).__name__}")


def _collect(questions: list[Question], answers: dict[str, Any]) -> dict[str, JevResult]:
    results: dict[str, JevResult] = {}
    for q in questions:
        if q.name not in answers:
            raise JevError(f"no answer for question {q.name!r}")
        result = _to_result(q.name, answers[q.name])
        if result.kind != q.kind:
            raise JevError(f"answer for {q.name!r} is {result.kind}, expected {q.kind}")
        results[q.name] = result
    return results


def _ask_fixture(state: str, questions: list[Question]) -> dict[str, JevResult]:
    key = fixture_key(state, questions)
    path = config.jev_fixtures_dir() / f"{key}.json"
    if not path.is_file():
        raise JevFixtureMissing(
            f"no Jev fixture for this state+questions (key {key}); expected file {path}. "
            "Fixture mode never calls the API: add the fixture file."
        )
    body = json.loads(path.read_text(encoding="utf-8"))
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, dict):
        raise JevError(f"fixture {path} has no 'answers' object")
    adapter = _answer_adapter()
    answers = {name: adapter.validate_json(json.dumps(raw)) for name, raw in raw_answers.items()}
    return _collect(questions, answers)


def _ask_live(state: str, questions: list[Question]) -> dict[str, JevResult]:
    from typesafe_sdk import TypeSafeClient

    with TypeSafeClient(api_key=config.typesafe_api_key()) as client:
        response = client.system_one(state=state, questions={q.name: q.to_sdk() for q in questions})
    return _collect(questions, dict(response.answers))


def ask(state: str, questions: list[Question]) -> dict[str, JevResult]:
    """Ask Jev typed questions about `state`. Returns results keyed by question name."""
    _validate(state, questions)
    if mode() == "live":
        return _ask_live(state, questions)
    return _ask_fixture(state, questions)
