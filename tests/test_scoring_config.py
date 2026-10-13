"""Question files, the Jev state string, and the pure calibrate/drift helpers (no DB)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from agent_memory import jev, questions, scoring
from agent_memory.questions import QuestionConfigError, load_file

SHIPPED = ["canary-simplitics-selftest", "config-check", "dispatch", "evals-parent", "routine-audit"]

VALID = """
threshold = 0.7
pass_question = "verdict"
pass_options = ["ok"]

[[questions]]
name = "verdict"
kind = "choice"
prompt = "Did it work?"
[questions.options]
ok = "yes"
bad = "no"

[[questions]]
name = "severity"
kind = "score"
prompt = "How bad?"
levels = ["fine", "minor", "major"]

[[questions]]
name = "flag"
kind = "noul"
prompt = "Flag it?"
"""


def _write(tmp_path: Path, text: str, name: str = "r.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_shipped_question_files_load_and_fit_the_rules() -> None:
    cache = questions.QuestionCache(questions.QUESTIONS_DIR)
    assert cache.routines() == SHIPPED
    for routine in SHIPPED:
        qset = cache.get(routine)
        assert qset is not None
        assert 3 <= len(qset.questions) <= 5
        assert 0 < qset.threshold < 1
        assert qset.spec(qset.pass_question).kind in ("choice", "score")
        jqs = qset.jev_questions()
        assert [q.name for q in jqs] == [q.name for q in qset.questions]
        assert jev.fixture_key("x", jqs)  # builds the wire form without error


def test_routine_audit_has_the_triage_choice() -> None:
    qset = load_file(questions.QUESTIONS_DIR / "routine-audit.toml")
    triage = qset.spec("triage")
    assert triage.kind == "choice"
    assert list(triage.options) == ["real_failure", "environment_noise", "unclear"]


def test_valid_file_maps_to_jev_questions(tmp_path: Path) -> None:
    qset = load_file(_write(tmp_path, VALID))
    assert qset.routine == "r"
    jqs = qset.jev_questions()
    assert jqs == [
        jev.choice("verdict", "Did it work?", {"ok": "yes", "bad": "no"}),
        jev.score("severity", "How bad?", ["fine", "minor", "major"]),
        jev.noul("flag", "Flag it?"),
    ]


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('pass_options = ["ok"]', 'pass_options = ["maybe"]', "pass_options"),
        ("threshold = 0.7", "threshold = 1.5", "threshold"),
        ('kind = "noul"', 'kind = "yesno"', "kind must be"),
        ('pass_question = "verdict"', 'pass_question = "flag"', "choice or score"),
        ('levels = ["fine", "minor", "major"]', 'levels = ["fine"]', "levels"),
        ('prompt = "Flag it?"', 'prompt = "Flag it?"\noptions = {a = "b"}', "not allowed"),
    ],
)
def test_invalid_files_are_rejected(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in VALID
    with pytest.raises(QuestionConfigError, match=message):
        load_file(_write(tmp_path, VALID.replace(old, new, 1)))


def test_question_count_must_be_3_to_5(tmp_path: Path) -> None:
    two = VALID.split("[[questions]]\nname = \"flag\"")[0]
    with pytest.raises(QuestionConfigError, match="3 to 5"):
        load_file(_write(tmp_path, two))


def test_score_pass_question_uses_pass_min(tmp_path: Path) -> None:
    text = VALID.replace('pass_question = "verdict"\npass_options = ["ok"]',
                         'pass_question = "severity"\npass_min = 1.5')
    qset = load_file(_write(tmp_path, text))
    assert qset.jev_verdict("1.6", 1.6) == "up"
    assert qset.jev_verdict("1.5", 1.5) == "up"
    assert qset.jev_verdict("1.4", 1.4) == "down"
    assert qset.pass_signal(1.0, {}) == pytest.approx(0.5)  # 1.0 / top level 2


def test_choice_pass_signal_sums_pass_option_probabilities(tmp_path: Path) -> None:
    qset = load_file(_write(tmp_path, VALID))
    assert qset.jev_verdict("ok", None) == "up"
    assert qset.jev_verdict("bad", None) == "down"
    assert qset.pass_signal(None, {"ok": 0.8, "bad": 0.2}) == pytest.approx(0.8)


def test_missing_and_unsafe_routine_names_have_no_question_set(tmp_path: Path) -> None:
    cache = questions.QuestionCache(tmp_path)
    assert cache.get("no-such-routine") is None
    assert cache.get("../config-check") is None


def test_state_string_is_exact_and_deterministic() -> None:
    run = {
        "routine": "config-check",
        "status": "fail",
        "summary": "pass=18 fail=2 info=0 12s report=~/r.md",
        "failure_reason": None,
        "meta": {"failures": ["Orca LAN reach: answers on 192.168.50.2:6768", "repos in iCloud:\n 1"],
                 "duration_s": 12, "log_source": "routines.log"},
        "id": "ignored", "started_at": "ignored",
    }
    expected = (
        "routine: config-check\n"
        "status: fail\n"
        "summary: pass=18 fail=2 info=0 12s report=~/r.md\n"
        "failure_reason: (none)\n"
        "failures:\n"
        "- Orca LAN reach: answers on 192.168.50.2:6768\n"
        "- repos in iCloud: 1"
    )
    assert scoring.format_state(run) == expected
    other = dict(run, id="different", started_at="different", meta=dict(run["meta"], duration_s=99))
    assert scoring.format_state(other) == expected


def test_state_string_without_failures() -> None:
    state = scoring.format_state({"routine": "x", "status": "pass", "summary": None,
                                  "failure_reason": "  ", "meta": {}})
    assert state == "routine: x\nstatus: pass\nsummary: (none)\nfailure_reason: (none)\nfailures: (none)"


def test_suggest_threshold_needs_enough_labels() -> None:
    pairs = [(0.9, True)] * 9
    assert scoring.suggest_threshold(pairs) == (None, "not enough labels (9 < 10)")


def test_suggest_threshold_picks_lowest_confidence_reaching_target() -> None:
    confs = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.92, 0.95, 0.98]
    pairs = [(c, c not in (0.50, 0.55)) for c in confs]
    # >= 0.50: 10/12 = 0.83; >= 0.55: 10/11 = 0.909 -> 0.55
    assert scoring.suggest_threshold(pairs) == (0.55, None)
    # Too few runs left above any level that would reach the target.
    bad = [(c, i % 2 == 0) for i, c in enumerate(confs)]
    t, note = scoring.suggest_threshold(bad)
    assert t is None and "no confidence level" in note


def test_parse_duration() -> None:
    assert scoring.parse_duration("7d") == timedelta(days=7)
    assert scoring.parse_duration("12h") == timedelta(hours=12)
    for bad in ("0d", "7", "1w", "-1d"):
        with pytest.raises(ValueError):
            scoring.parse_duration(bad)
