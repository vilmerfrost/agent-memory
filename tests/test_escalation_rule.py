"""The escalation rule (no DB): `escalate_on` in question files and the verdict confidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_memory import questions
from agent_memory.questions import QuestionConfigError, load_file

BASE = """
threshold = 0.7
pass_question = "verdict"
pass_options = ["pass"]
{extra}

[[questions]]
name = "verdict"
kind = "choice"
prompt = "Did it work?"
[questions.options]
pass = "yes"
regression = "no, behavior"
harness = "no, harness"

[[questions]]
name = "severity"
kind = "score"
prompt = "How bad?"
levels = ["fine", "minor", "major", "critical"]

[[questions]]
name = "flag"
kind = "noul"
prompt = "Flag it?"
"""


def _load(tmp_path: Path, extra: str = "") -> questions.QuestionSet:
    path = tmp_path / "r.toml"
    path.write_text(BASE.format(extra=extra), encoding="utf-8")
    return load_file(path)


def _score(question: str, kind: str, value: str, conf: float | None, probs: dict, value_num=None) -> dict:
    return {"question": question, "kind": kind, "value": value, "value_num": value_num,
            "confidence": conf, "probabilities": probs}


def test_escalate_on_defaults_to_the_pass_question(tmp_path: Path) -> None:
    assert _load(tmp_path).escalate_on == ["verdict"]
    assert _load(tmp_path, 'escalate_on = ["verdict", "severity"]').escalate_on == ["verdict", "severity"]


@pytest.mark.parametrize(("extra", "message"), [
    ('escalate_on = ["nope"]', "escalate_on must be a non-empty list"),
    ("escalate_on = []", "escalate_on must be a non-empty list"),
    ('escalate_on = "verdict"', "escalate_on must be a non-empty list"),
    ('escalate_on = ["flag"]', "noul has no confidence"),
    ('escalate_on = ["verdict", "verdict"]', "twice"),
])
def test_bad_escalate_on_is_rejected(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(QuestionConfigError, match=message):
        _load(tmp_path, extra)


def test_shipped_files_escalate_only_on_the_verdict() -> None:
    cache = questions.QuestionCache(questions.QUESTIONS_DIR)
    for routine in cache.routines():
        qset = cache.get(routine)
        assert qset is not None and qset.escalate_on == [qset.pass_question] == ["verdict"], routine


def test_verdict_confidence_sums_labels_on_the_same_side(tmp_path: Path) -> None:
    qset = _load(tmp_path)
    # Real shape: regression 0.61 vs harness 0.37 split the label, but "down" is 0.98 sure.
    probs = {"pass": 0.02, "regression": 0.61, "harness": 0.37}
    assert qset.verdict_confidence("regression", None, 0.41, probs) == pytest.approx(0.96)
    # Up vs down itself is unsure: Jev's own confidence stays (it is the larger number).
    probs = {"pass": 0.53, "regression": 0.14, "harness": 0.33}
    assert qset.verdict_confidence("pass", None, 0.30, probs) == pytest.approx(0.30)
    # The label is "pass" but most mass is on the down side: clamp at 0, keep Jev's confidence.
    probs = {"pass": 0.47, "regression": 0.10, "harness": 0.43}
    assert qset.verdict_confidence("pass", None, 0.21, probs) == pytest.approx(0.21)
    # No probabilities (old rows): Jev's confidence; no confidence either: the binary value.
    assert qset.verdict_confidence("pass", None, 0.5, {}) == 0.5
    assert qset.verdict_confidence("pass", None, None, {"pass": 0.9, "regression": 0.1}) == pytest.approx(0.8)


def test_two_option_verdict_equals_jev_confidence(tmp_path: Path) -> None:
    text = BASE.format(extra="").replace('regression = "no, behavior"\nharness = "no, harness"', 'bad = "no"')
    path = tmp_path / "two.toml"
    path.write_text(text, encoding="utf-8")
    qset = load_file(path)
    # Jev's confidence for n options is (n * p_top - 1) / (n - 1); for n = 2 that is 2p - 1.
    assert qset.verdict_confidence("bad", None, 0.38, {"pass": 0.31, "bad": 0.69}) == pytest.approx(0.38)


def test_score_pass_question_sides_use_pass_min(tmp_path: Path) -> None:
    text = BASE.format(extra="").replace('pass_question = "verdict"\npass_options = ["pass"]',
                                         'pass_question = "severity"\npass_min = 2')
    path = tmp_path / "s.toml"
    path.write_text(text, encoding="utf-8")
    qset = load_file(path)
    assert qset.escalate_on == ["severity"]
    probs = {"0": 0.0, "1": 0.05, "2": 0.5, "3": 0.45}  # up = levels 2 and 3 = 0.95
    assert qset.verdict_confidence("2.4", 2.4, 0.3, probs) == pytest.approx(0.9)
    assert qset.verdict_confidence("1.2", 1.2, 0.3, probs) == pytest.approx(0.3)  # down side holds 0.05


def test_low_answers_follow_escalate_on_and_the_verdict_confidence(tmp_path: Path) -> None:
    scores = [
        _score("verdict", "choice", "regression", 0.41, {"pass": 0.02, "regression": 0.61, "harness": 0.37}),
        _score("severity", "score", "2.5", 0.5, {"2": 0.5, "3": 0.5}, 2.5),
        _score("flag", "noul", "0.4", None, {}, 0.4),
    ]
    # Default (verdict only): the verdict is sure of "down" and severity is not listed.
    assert _load(tmp_path).low_answers(scores) == []
    # Listing severity makes its low confidence escalate; the noul never does.
    both = _load(tmp_path, 'escalate_on = ["verdict", "severity"]')
    assert both.low_answers(scores) == [
        {"question": "severity", "kind": "score", "value": "2.5", "confidence": 0.5, "jev_confidence": 0.5}]
    # A verdict unsure of up vs down escalates, with both numbers reported.
    unsure = [_score("verdict", "choice", "pass", 0.3, {"pass": 0.53, "regression": 0.14, "harness": 0.33})]
    assert both.low_answers(unsure) == [
        {"question": "verdict", "kind": "choice", "value": "pass", "confidence": 0.3, "jev_confidence": 0.3}]
    # Strictly below: equal to the threshold does not escalate.
    edge = [_score("verdict", "choice", "pass", 0.7, {})]
    assert both.low_answers(edge) == []
