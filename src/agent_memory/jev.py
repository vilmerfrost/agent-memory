"""Thin wrapper over the TypeSafe (Jev) SDK `typesafe-sdk==0.7.4`.

Mode rule:
- `live` only when TYPESAFE_API_KEY is non-empty AND AMEM_JEV_LIVE=1.
- `fixture` otherwise. Fixture mode reads `tests/fixtures/jev/<key>.json`, where
  `<key>` is a hash of the state and the questions. A missing fixture raises
  `JevFixtureMissing`; it never falls back to a live call.

Errors: every failure of one `ask` raises `JevError` (SDK HTTP/rate-limit/connection
errors, network errors, an invalid response, an unreadable or invalid fixture), so a
caller can count it and go on. A missing fixture raises its subclass `JevFixtureMissing`.
Error text never holds the API key.

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
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from agent_memory import config

if TYPE_CHECKING:
    from pydantic import TypeAdapter

# Every live call names the model. The SDK's own default is "jev-latest", but the
# TYPESAFE_DEFAULT_MODEL env var can replace it silently; an explicit argument wins over both.
# A versioned ID, not an alias: aliases move without a code change (docs.typesafe.ai/models,
# 2026-10-16: jev-latest -> jev-1.13.0, jev-preview -> jev-1.14.0).
MODEL = "jev-1.14.0"

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
        # Commands that never validate an answer do not need Pydantic at startup.
        from pydantic import TypeAdapter
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
        _check_range(result)
        results[q.name] = result
    return results


def _is_unit(x: Any) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and 0 <= x <= 1


def _check_range(result: JevResult) -> None:
    """The SDK models accept any float; a confidence or P(yes) outside 0..1 (or a
    non-finite score) is an invalid answer, never something to store."""
    if result.confidence is not None and not _is_unit(result.confidence):
        raise JevError(f"answer for {result.name!r} has confidence {result.confidence!r} outside 0..1")
    if result.kind == "noul" and not _is_unit(result.value):
        raise JevError(f"answer for {result.name!r} has P(yes) {result.value!r} outside 0..1")
    if result.kind == "score" and not (isinstance(result.value, (int, float)) and math.isfinite(result.value)):
        raise JevError(f"answer for {result.name!r} has a non-finite score {result.value!r}")


_WS_RE = re.compile(r"\s+")
_DESCRIBE_MAX = 300


def _describe(exc: BaseException) -> str:
    """One line `<Type>: <message>`, at most 300 chars, with the API key masked.

    The SDK already keeps credentials out of its messages; the mask is a second guard
    in case a server ever echoes the key back in an error body.
    """
    text = _WS_RE.sub(" ", f"{type(exc).__name__}: {exc}").strip()
    key = config.typesafe_api_key()
    if key:
        text = text.replace(key, "[redacted]")
    if len(text) > _DESCRIBE_MAX:
        text = text[: _DESCRIBE_MAX - 3] + "..."
    return text


def _ask_fixture(state: str, questions: list[Question]) -> dict[str, JevResult]:
    key = fixture_key(state, questions)
    path = config.jev_fixtures_dir() / f"{key}.json"
    if not path.is_file():
        raise JevFixtureMissing(
            f"no Jev fixture for this state+questions (key {key}); expected file {path}. "
            "Fixture mode never calls the API: add the fixture file."
        )
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # unreadable, not UTF-8 or not JSON
        raise JevError(f"fixture {path} is unreadable or not JSON: {_describe(exc)}") from exc
    raw_answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(raw_answers, dict):
        raise JevError(f"fixture {path} has no 'answers' object")
    adapter = _answer_adapter()
    try:
        answers = {name: adapter.validate_json(json.dumps(raw)) for name, raw in raw_answers.items()}
    except ValueError as exc:  # pydantic ValidationError: an answer the SDK models reject
        raise JevError(f"fixture {path} has an invalid answer: {_describe(exc)}") from exc
    return _collect(questions, answers)


def _ask_live(state: str, questions: list[Question]) -> dict[str, JevResult]:
    """One System One call. Every SDK, network or response error becomes a JevError."""
    from typesafe_sdk import TypeSafeClient, TypeSafeError

    try:
        with TypeSafeClient(api_key=config.typesafe_api_key()) as client:
            response = client.system_one(state=state, questions={q.name: q.to_sdk() for q in questions},
                                         model=MODEL)
        answers = dict(response.answers)
    except TypeSafeError as exc:  # HTTP 4xx/5xx, rate limit, connection, timeout, invalid response
        raise JevError(f"Jev API error: {_describe(exc)}") from exc
    except (OSError, ValueError) as exc:  # network below the SDK, or a response the models reject
        raise JevError(f"Jev call failed: {_describe(exc)}") from exc
    return _collect(questions, answers)


def ask(state: str, questions: list[Question]) -> dict[str, JevResult]:
    """Ask Jev typed questions about `state`. Returns results keyed by question name."""
    _validate(state, questions)
    if mode() == "live":
        return _ask_live(state, questions)
    return _ask_fixture(state, questions)
