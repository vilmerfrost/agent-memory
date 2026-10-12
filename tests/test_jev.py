from __future__ import annotations

import pytest

from agent_memory import jev

# This state + question set has a recorded fixture in tests/fixtures/jev/.
STATE = "Routine morning-briefing finished. 3 of 3 sections written, report saved."
QUESTIONS = [
    jev.choice(
        "outcome",
        "Did the routine do its job?",
        {"pass": "All work done", "partial": "Some work done", "fail": "Nothing useful done"},
    ),
    jev.score(
        "quality",
        "How useful is the report?",
        ["Useless", "Somewhat useful", "Useful", "Very useful"],
    ),
    jev.noul("needs_human", "Does a human need to look at this run?"),
]


def test_mode_is_fixture_by_default() -> None:
    assert jev.mode() == "fixture"


def test_key_without_live_flag_stays_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-not-a-real-key")
    assert jev.mode() == "fixture"
    monkeypatch.setenv("AMEM_JEV_LIVE", "0")
    assert jev.mode() == "fixture"


def test_live_flag_without_key_stays_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "   ")
    assert jev.mode() == "fixture"


def test_live_needs_key_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only checks the mode rule; the SDK client is a tripwire in tests, so no call is made.
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-not-a-real-key")
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    assert jev.mode() == "live"
    with pytest.raises(AssertionError, match="live Jev call attempted"):
        jev.ask(STATE, QUESTIONS)


def test_fixture_key_is_deterministic() -> None:
    assert jev.fixture_key(STATE, QUESTIONS) == jev.fixture_key(STATE, list(QUESTIONS))
    assert jev.fixture_key(STATE, QUESTIONS) != jev.fixture_key(STATE + " ", QUESTIONS)


def test_fixture_hit_returns_typed_results() -> None:
    results = jev.ask(STATE, QUESTIONS)
    assert list(results) == ["outcome", "quality", "needs_human"]

    outcome = results["outcome"]
    assert outcome.kind == "choice"
    assert outcome.value == "pass"
    assert outcome.confidence == pytest.approx(0.92)
    assert set(outcome.probabilities) == {"pass", "partial", "fail"}

    quality = results["quality"]
    assert quality.kind == "score"
    assert quality.value == pytest.approx(2.6)
    assert quality.confidence == pytest.approx(0.81)
    assert set(quality.probabilities) == {"0", "1", "2", "3"}

    needs_human = results["needs_human"]
    assert needs_human.kind == "noul"
    assert needs_human.value == pytest.approx(0.07)
    assert needs_human.confidence is None  # the API returns no confidence for noul

    assert jev.ask(STATE, QUESTIONS) == results  # deterministic


def test_fixture_miss_raises_clear_error() -> None:
    with pytest.raises(jev.JevFixtureMissing, match="no Jev fixture") as info:
        jev.ask("A state nobody recorded.", QUESTIONS)
    assert jev.fixture_key("A state nobody recorded.", QUESTIONS) in str(info.value)


def test_rejects_empty_questions_and_duplicate_names() -> None:
    with pytest.raises(jev.JevError):
        jev.ask(STATE, [])
    with pytest.raises(jev.JevError, match="unique"):
        jev.ask(STATE, [jev.noul("a", "x?"), jev.noul("a", "y?")])
