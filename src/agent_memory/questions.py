"""Per-routine Jev question sets: `questions/<routine>.toml` (stdlib tomllib).

File format (full reference in docs/scoring.md):

    threshold = 0.7              # an escalate_on confidence below this escalates the run
    pass_question = "verdict"    # a choice or score question; maps Jev's answer to up/down
    pass_options = ["ok"]        # choice pass_question: these labels mean "up"
    # pass_min = 2.0             # score pass_question: score >= pass_min means "up"
    escalate_on = ["verdict"]    # optional; choice/score questions that escalate (default: the pass_question)

    [[questions]]                # 3 to 5 entries, in the order Jev gets them
    name = "verdict"
    kind = "choice"              # choice | score | noul
    prompt = "..."
    [questions.options]          # choice only: label -> description
    ok = "..."
    needs_action = "..."

    [[questions]]
    name = "severity"
    kind = "score"
    prompt = "..."
    levels = ["...", "...", "..."]   # score only: level i describes score i (range 0..len-1)

    [[questions]]
    name = "flag"
    kind = "noul"
    prompt = "..."
    # [questions.criteria] with optional `true` / `false` descriptions

`AMEM_QUESTIONS_DIR` overrides the directory (tests use it).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_memory import config, jev

QUESTIONS_DIR = config.REPO_ROOT / "questions"
MIN_QUESTIONS = 3
MAX_QUESTIONS = 5
# Question files Jev uses for something other than scoring routine runs. `amem score`
# and `amem daily` never treat them as routines (docs/deviations.md).
NOT_ROUTINES = frozenset({"deviation"})
_ROUTINE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class QuestionConfigError(ValueError):
    """A question file is malformed. The message names the file."""


@dataclass(frozen=True)
class QuestionSpec:
    name: str
    kind: str
    prompt: str
    options: dict[str, Any] = field(default_factory=dict)  # choice
    levels: list[Any] = field(default_factory=list)  # score
    criteria: dict[str, Any] | None = None  # noul

    def to_jev(self) -> jev.Question:
        if self.kind == "choice":
            return jev.choice(self.name, self.prompt, self.options)
        if self.kind == "score":
            return jev.score(self.name, self.prompt, self.levels)
        return jev.noul(self.name, self.prompt, self.criteria)


@dataclass(frozen=True)
class QuestionSet:
    routine: str
    path: Path
    threshold: float
    pass_question: str
    questions: list[QuestionSpec]
    pass_options: list[str] = field(default_factory=list)
    pass_min: float | None = None
    # Choice/score questions whose low confidence escalates a run. Default: the pass_question.
    escalate_on: list[str] = field(default_factory=list)

    def jev_questions(self) -> list[jev.Question]:
        return [q.to_jev() for q in self.questions]

    def spec(self, name: str) -> QuestionSpec:
        for q in self.questions:
            if q.name == name:
                return q
        raise KeyError(name)

    def jev_verdict(self, value: str, value_num: float | None) -> str:
        """Map Jev's pass_question answer to "up" (run fine) or "down" (needs action)."""
        spec = self.spec(self.pass_question)
        if spec.kind == "choice":
            return "up" if value in self.pass_options else "down"
        assert self.pass_min is not None
        return "up" if value_num is not None and value_num >= self.pass_min else "down"

    def pass_signal(self, value_num: float | None, probabilities: dict[str, Any]) -> float | None:
        """Jev's pass_question answer on a 0..1 scale (higher = better), for drift.

        choice: the summed probability of the pass_options.
        score: the expected score divided by the top level (len(levels) - 1).
        """
        spec = self.spec(self.pass_question)
        if spec.kind == "choice":
            return float(sum(float(probabilities.get(o, 0.0)) for o in self.pass_options))
        if value_num is None:
            return None
        return float(value_num) / (len(spec.levels) - 1)

    def _is_up_option(self, key: str) -> bool:
        """A probabilities key on the up side: a pass option (choice) or a level >= pass_min (score)."""
        if self.spec(self.pass_question).kind == "choice":
            return key in self.pass_options
        try:
            return float(key) >= float(self.pass_min)  # type: ignore[arg-type]
        except ValueError:
            return False

    def verdict_confidence(self, value: str, value_num: float | None, confidence: float | None,
                           probabilities: dict[str, Any] | None) -> float | None:
        """How sure Jev is of its up/down verdict on the pass_question, on Jev's own scale.

        Jev's `confidence` is for the exact label: (n * p_top - 1) / (n - 1) over n options.
        Two labels on the same side (e.g. `regression` vs `harness_problem`, both "down")
        split p_top and drag it down, although up vs down is clear. So the verdict
        confidence is max(Jev's confidence, 2 * p_side - 1), where p_side is the summed
        probability of the side Jev's answer is on: the same formula for a two-way
        up/down question. With two options it equals Jev's confidence. Without
        probabilities it is Jev's confidence. Rounded to 6 places.
        """
        if not probabilities:
            return confidence
        up = self.jev_verdict(value, value_num) == "up"
        p_side = sum(float(p) for k, p in probabilities.items() if self._is_up_option(str(k)) == up)
        binary = max(0.0, min(1.0, 2 * p_side - 1))
        return round(binary if confidence is None else max(float(confidence), binary), 6)

    def answer_confidence(self, score: dict[str, Any]) -> float | None:
        """The confidence escalation compares with `threshold` for one stored score row
        (`question`, `kind`, `value`, `value_num`, `confidence`, `probabilities`):
        the verdict confidence for the pass_question, Jev's confidence otherwise."""
        if score["question"] == self.pass_question:
            return self.verdict_confidence(score["value"], score.get("value_num"), score["confidence"],
                                           score.get("probabilities"))
        return score["confidence"]

    def low_answers(self, scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The `escalate_on` answers whose confidence is strictly below `threshold`."""
        low = []
        for s in scores:
            if s["question"] not in self.escalate_on or s["kind"] not in ("choice", "score"):
                continue
            conf = self.answer_confidence(s)
            if conf is not None and conf < self.threshold:
                low.append({"question": s["question"], "kind": s["kind"], "value": s["value"],
                            "confidence": conf, "jev_confidence": s["confidence"]})
        return low


def questions_dir() -> Path:
    config.load_env()
    override = os.environ.get("AMEM_QUESTIONS_DIR")
    return Path(override) if override else QUESTIONS_DIR


def _fail(path: Path, msg: str) -> QuestionConfigError:
    return QuestionConfigError(f"{path}: {msg}")


def _parse_question(path: Path, i: int, raw: Any) -> QuestionSpec:
    where = f"questions[{i}]"
    if not isinstance(raw, dict):
        raise _fail(path, f"{where} must be a table")
    name, kind, prompt = raw.get("name"), raw.get("kind"), raw.get("prompt")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise _fail(path, f"{where}.name must be an identifier, got {name!r}")
    if kind not in ("choice", "score", "noul"):
        raise _fail(path, f"{where} ({name}).kind must be choice, score or noul, got {kind!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _fail(path, f"{where} ({name}).prompt must be a non-empty string")
    allowed = {"name", "kind", "prompt", {"choice": "options", "score": "levels", "noul": "criteria"}[kind]}
    extra = sorted(set(raw) - allowed)
    if extra:
        raise _fail(path, f"{where} ({name}) has keys not allowed for {kind}: {extra}")
    if kind == "choice":
        options = raw.get("options")
        if not isinstance(options, dict) or len(options) < 2:
            raise _fail(path, f"{where} ({name}).options must be a table with at least 2 labels")
        return QuestionSpec(name, kind, prompt, options=dict(options))
    if kind == "score":
        levels = raw.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            raise _fail(path, f"{where} ({name}).levels must be a list with at least 2 levels")
        return QuestionSpec(name, kind, prompt, levels=list(levels))
    criteria = raw.get("criteria")
    if criteria is not None and (not isinstance(criteria, dict) or set(criteria) - {"true", "false"}):
        raise _fail(path, f"{where} ({name}).criteria may only hold `true` and `false`")
    return QuestionSpec(name, kind, prompt, criteria=dict(criteria) if criteria else None)


def load_file(path: Path, routine: str | None = None) -> QuestionSet:
    """Parse and validate one question file. Raises QuestionConfigError."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise _fail(path, f"invalid TOML: {exc}") from None
    allowed = {"threshold", "pass_question", "pass_options", "pass_min", "escalate_on", "questions"}
    extra = sorted(set(data) - allowed)
    if extra:
        raise _fail(path, f"unknown top-level keys: {extra}")

    threshold = data.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise _fail(path, f"threshold must be a number in 0..1, got {threshold!r}")

    raw_questions = data.get("questions")
    if not isinstance(raw_questions, list):
        raise _fail(path, "needs [[questions]] tables")
    if not MIN_QUESTIONS <= len(raw_questions) <= MAX_QUESTIONS:
        raise _fail(path, f"needs {MIN_QUESTIONS} to {MAX_QUESTIONS} questions, has {len(raw_questions)}")
    specs = [_parse_question(path, i, q) for i, q in enumerate(raw_questions)]
    names = [q.name for q in specs]
    if len(set(names)) != len(names):
        raise _fail(path, f"question names must be unique: {names}")

    pass_question = data.get("pass_question")
    by_name = {q.name: q for q in specs}
    if pass_question not in by_name:
        raise _fail(path, f"pass_question must name a question, got {pass_question!r}")
    pq = by_name[pass_question]
    pass_options: list[str] = []
    pass_min: float | None = None
    if pq.kind == "choice":
        opts = data.get("pass_options")
        if not isinstance(opts, list) or not opts or any(o not in pq.options for o in opts):
            raise _fail(path, f"pass_options must be a non-empty list of {pass_question} options "
                              f"{list(pq.options)}, got {opts!r}")
        if "pass_min" in data:
            raise _fail(path, "pass_min is only for a score pass_question")
        pass_options = list(opts)
    elif pq.kind == "score":
        pm = data.get("pass_min")
        top = len(pq.levels) - 1
        if isinstance(pm, bool) or not isinstance(pm, (int, float)) or not 0 <= pm <= top:
            raise _fail(path, f"pass_min must be a number in 0..{top}, got {pm!r}")
        if "pass_options" in data:
            raise _fail(path, "pass_options is only for a choice pass_question")
        pass_min = float(pm)
    else:
        raise _fail(path, "pass_question must be a choice or score question (noul has no confidence)")

    escalate_on = data.get("escalate_on", [pass_question])
    if (not isinstance(escalate_on, list) or not escalate_on
            or any(not isinstance(n, str) or n not in by_name for n in escalate_on)):
        raise _fail(path, f"escalate_on must be a non-empty list of question names {names}, got {escalate_on!r}")
    noul = [n for n in escalate_on if by_name[n].kind == "noul"]
    if noul:
        raise _fail(path, f"escalate_on may only name choice or score questions (noul has no confidence): {noul}")
    if len(set(escalate_on)) != len(escalate_on):
        raise _fail(path, f"escalate_on names a question twice: {escalate_on}")

    return QuestionSet(
        routine=routine or path.stem,
        path=path,
        threshold=float(threshold),
        pass_question=pass_question,
        questions=specs,
        pass_options=pass_options,
        pass_min=pass_min,
        escalate_on=list(escalate_on),
    )


def path_for(routine: str, directory: Path | None = None) -> Path | None:
    """The question file for a routine, or None when the name cannot be a file name."""
    if not _ROUTINE_RE.fullmatch(routine):
        return None
    return (directory or questions_dir()) / f"{routine}.toml"


class QuestionCache:
    """Loads each routine's question set once. Missing file -> None."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or questions_dir()
        self._sets: dict[str, QuestionSet | None] = {}

    def get(self, routine: str) -> QuestionSet | None:
        if routine not in self._sets:
            path = path_for(routine, self.directory)
            self._sets[routine] = load_file(path, routine) if path and path.is_file() else None
        return self._sets[routine]

    def routines(self) -> list[str]:
        """Every routine with a question file, sorted. Question files that are not
        about routine runs (`NOT_ROUTINES`, e.g. `deviation.toml`) are left out."""
        if not self.directory.is_dir():
            return []
        return sorted(p.stem for p in self.directory.glob("*.toml")
                      if p.is_file() and p.stem not in NOT_ROUTINES)
