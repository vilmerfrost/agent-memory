"""The sandbox-replay verdict: deterministic code over three evaluation layers (Mission 005, plan 2.1).

    machine  objective checks per case and arm (ArmPair), plus the run errors and refusals
    judge    a blind pairwise judge's tally (JudgeResult); it never knew which attempt was A or B
    jev      Jev's typed read of the structured results (JevResult; questions/rsi-replay.toml)

`decide` is a pure function of those numbers and a versioned policy (config/rsi-verdict-policy.json). No model
ever decides the verdict, and the investigator cannot change the policy: it is repo data, read here only.

The machine layer counts scores, not just passes. A run's check score is in 0..1: the share of the case's
scenarios it passed (`check_score`, or `check_scenarios` passed/total), or 1.0/0.0 from `check_passed` when the
run record has neither (records before scenario scores). A case's arm score is the sum over its complete pairs, so
5 pairs give a score in 0..5. The checks move only when the total delta, the sum over cases of
(b_score - a_score), reaches `policy.min_score_margin` (in summed score units, the same both ways), or when one
case alone regresses by the margin. The policy/2 margin is 2.0: two full runs' worth, so one run that flips
(A 5/5, B 4/5) is noise, not a regression and not an improvement. Totals let a multi-case plan (2+2+1 pairs) move.

Decision order (the first rule that fires wins):

    1. refusals (stale baseline, artifact not exact, ...)        -> INCONCLUSIVE
    2. budget stop, run errors or timeouts, missing runs          -> INCONCLUSIVE
    3. fewer complete pairs than policy.min_pairs, or a case
       without a pair                                             -> INCONCLUSIVE
    4. Jev says the harness is broken (P >= jev_harness_max)      -> INCONCLUSIVE
       both arms score 0 on every case (policy.
       harness_dead_inconclusive)                                 -> INCONCLUSIVE harness-dead:<cases>
    5. total delta <= -min_score_margin, or any one case's
       a_score - b_score >= min_score_margin                     -> WORSE   (a big regression anywhere blocks;
                                                                     no judge or Jev reading rescues it)
    6. total delta >= min_score_margin:
         judge missing / incomplete / B win rate < judge_min     -> INCONCLUSIVE
         Jev missing / not "improves" / confidence < min         -> INCONCLUSIVE
         else                                                     -> IMPROVES
    7. checks equal (total delta inside the margin):
         judge B win rate >= judge_strong or <= 1 - judge_strong -> INCONCLUSIVE judge-only-preference
         else                                                     -> NO_EFFECT

The judge alone never decides: it can block an IMPROVES the checks found, but it never makes WORSE or IMPROVES
when the checks are equal. Only IMPROVES maps to a passing rsi-eval/1 verdict (`improves`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from agent_memory import config

VERDICTS = ("IMPROVES", "NO_EFFECT", "WORSE", "INCONCLUSIVE")
EVAL_VERDICT = {"IMPROVES": "improves", "NO_EFFECT": "no-effect", "WORSE": "worse", "INCONCLUSIVE": "inconclusive"}
POLICY_FORMAT = "rsi-verdict-policy/2"
POLICY_PATH = config.REPO_ROOT / "config" / "rsi-verdict-policy.json"
JEV_EFFECTS = ("improves", "no_effect", "worse", "unclear")


class PolicyError(ValueError):
    """The policy file is missing, malformed or of another version."""


@dataclass(frozen=True)
class VerdictPolicy:
    version: str = POLICY_FORMAT
    min_pairs: int = 5
    max_error_runs: int = 0
    min_score_margin: float = 2.0  # summed check score (0..pairs per arm) a case must move, both ways
    require_judge: bool = True
    judge_min_b_winrate: float = 0.5
    judge_strong_winrate: float = 0.75
    require_jev: bool = True
    jev_min_confidence: float = 0.5
    jev_harness_max: float = 0.7
    harness_dead_inconclusive: bool = True   # every case A_pass=0 and B_pass=0 -> INCONCLUSIVE harness-dead
    sha256: str = ""              # of the policy file it came from ("" for the built-in default)

    def describe(self) -> dict[str, Any]:
        return asdict(self)


def load_policy(path: Path | None = None) -> VerdictPolicy:
    """The versioned policy file (default config/rsi-verdict-policy.json). Unknown keys or a bad value refuse.

    Only the current format loads. Records keep just the policy's version and sha256, and nothing reads an old
    policy file back, so a policy/1 file (pass-rate deltas) is refused rather than read with policy/2 rules."""
    path = path or POLICY_PATH
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise PolicyError(f"{path}: unreadable policy ({type(exc).__name__})") from None
    if not isinstance(data, dict) or data.get("version") != POLICY_FORMAT:
        raise PolicyError(f"{path}: version must be {POLICY_FORMAT}")
    known = {f.name: f for f in fields(VerdictPolicy)} | {"note": None}
    extra = sorted(set(data) - set(known) - {"sha256"})
    if extra or "sha256" in data:
        raise PolicyError(f"{path}: unknown keys {extra or ['sha256']}")
    kwargs: dict[str, Any] = {}
    for k, v in data.items():
        if k in ("note", "version"):
            continue
        default = getattr(VerdictPolicy, k)
        if isinstance(default, bool):
            ok = isinstance(v, bool)
        elif isinstance(default, int):
            ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
        elif k == "min_score_margin":
            # Summed score units, not a rate. Zero would let any difference decide.
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < float("inf")
        else:
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
        if not ok:
            raise PolicyError(f"{path}: bad value for {k}: {v!r}")
        kwargs[k] = float(v) if k == "min_score_margin" else v
    return VerdictPolicy(**kwargs, sha256=hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------- the three layers


def run_check_score(check_passed: bool | None, check_score: Any = None, check_scenarios: Any = None) -> float:
    """One run's check score in 0..1, from what its run record has.

    `check_score` (a number in 0..1) first, then `check_scenarios` ({"passed": int, "total": int}, total > 0), then
    1.0/0.0 from `check_passed`, so a record written before scenario scores scores exactly as its pass bit."""
    if isinstance(check_score, (int, float)) and not isinstance(check_score, bool) and 0 <= check_score <= 1:
        return float(check_score)
    if isinstance(check_scenarios, Mapping):
        passed, total = check_scenarios.get("passed"), check_scenarios.get("total")
        if (isinstance(passed, int) and isinstance(total, int) and not isinstance(passed, bool)
                and not isinstance(total, bool) and 0 <= passed <= total and total > 0):
            return passed / total
    return 1.0 if check_passed else 0.0


@dataclass(frozen=True)
class CaseStats:
    """One case: complete A/B pairs, how many runs of each arm passed every scenario of the objective check
    (a_pass/b_pass, for reporting), and each arm's summed check score (a_score/b_score, 0..pairs; see
    run_check_score). The verdict reads the scores. Without them, the scores are the pass counts."""
    case: str
    pairs: int
    a_pass: int
    b_pass: int
    a_score: float | None = None
    b_score: float | None = None

    def __post_init__(self) -> None:
        for arm in ("a", "b"):
            score = getattr(self, f"{arm}_score")
            score = float(getattr(self, f"{arm}_pass")) if score is None else float(score)
            if not (-1e-9 <= score <= self.pairs + 1e-9):
                raise ValueError(f"{self.case}: {arm}_score {score} outside 0..{self.pairs}")
            object.__setattr__(self, f"{arm}_score", round(score, 6))

    @property
    def a_rate(self) -> float:
        return self.a_pass / self.pairs if self.pairs else 0.0

    @property
    def b_rate(self) -> float:
        return self.b_pass / self.pairs if self.pairs else 0.0


@dataclass(frozen=True)
class ArmPair:
    """The machine layer: per-case check results of arm A (baseline) and arm B (candidate)."""
    cases: tuple[CaseStats, ...]
    runs_expected: int = 0
    runs_done: int = 0
    errors: tuple[str, ...] = ()          # run errors and timeouts ("<run_id>: <what>")
    refusals: tuple[str, ...] = ()        # preconditions that failed ("stale-baseline: ...")
    budget_stop: str | None = None        # the budget kind that stopped the replay

    @property
    def pairs(self) -> int:
        return sum(c.pairs for c in self.cases)


@dataclass(frozen=True)
class JudgeResult:
    """The blind pairwise judge's tally. A B win counts only when B used the change in that pair (or the change
    is always on, like an instruction file); other B wins are `unfired_b` and count as ties (nightly-trials)."""
    mode: str                              # stub | claude | codex | none
    judged: int = 0
    wins_a: int = 0
    wins_b: int = 0
    ties: int = 0
    unfired_b: int = 0
    errors: int = 0
    pairs: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def b_winrate(self) -> float | None:
        """Ties (and unfired B wins) count half, so an all-tie replay reads 0.5."""
        if not self.judged:
            return None
        return round((self.wins_b + 0.5 * (self.ties + self.unfired_b)) / self.judged, 4)

    def summary(self) -> dict[str, Any]:
        return {"mode": self.mode, "judged": self.judged, "wins_a": self.wins_a, "wins_b": self.wins_b,
                "ties": self.ties, "unfired_b": self.unfired_b, "errors": self.errors, "b_winrate": self.b_winrate}


@dataclass(frozen=True)
class JevResult:
    """Jev's typed answers on the replay's structured results (not jev.JevResult, which is one answer)."""
    mode: str                              # fixture | live | none
    status: str                            # ok | missing | unavailable | error
    effect: str | None = None              # one of JEV_EFFECTS
    confidence: float | None = None        # Jev's confidence in `effect`
    harness_problem: float | None = None   # P(yes)
    evidence_strength: float | None = None
    error: str | None = None
    state_key: str | None = None
    model: str | None = None               # the model that answered (Jev's response; a fixture's recording)
    tokens_in: int | None = None           # the response's input/output tokens (None when not reported)
    tokens_out: int | None = None

    def summary(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reasons: tuple[str, ...]
    inputs: dict                           # the exact numbers used

    @property
    def eval_verdict(self) -> str:
        return EVAL_VERDICT[self.verdict]

    @property
    def promotable(self) -> bool:
        return self.verdict == "IMPROVES"

    def to_json(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "eval_verdict": self.eval_verdict, "reasons": list(self.reasons),
                "inputs": self.inputs}


def _inputs(machine: ArmPair, judge: JudgeResult | None, jev: JevResult | None, policy: VerdictPolicy) -> dict:
    return {
        "policy": {"version": policy.version, "sha256": policy.sha256, "min_pairs": policy.min_pairs,
                   "min_score_margin": policy.min_score_margin},
        "machine": {"pairs": machine.pairs, "runs_expected": machine.runs_expected, "runs_done": machine.runs_done,
                    "errors": len(machine.errors), "refusals": list(machine.refusals),
                    "budget_stop": machine.budget_stop,
                    "cases": [{"case": c.case, "pairs": c.pairs, "a_pass": c.a_pass, "b_pass": c.b_pass,
                               "a_rate": round(c.a_rate, 4), "b_rate": round(c.b_rate, 4),
                               "a_score": c.a_score, "b_score": c.b_score} for c in machine.cases]},
        "judge": judge.summary() if judge else None,
        "jev": jev.summary() if jev else None,
    }


def _score(x: float) -> str:
    return f"{x:g}"


def _moved(c: CaseStats) -> str:
    return f"{c.case} A {_score(c.a_score)}/{c.pairs} -> B {_score(c.b_score)}/{c.pairs}"


def decide(machine: ArmPair, judge: JudgeResult | None, jev: JevResult | None,
           policy: VerdictPolicy | None = None) -> Verdict:
    """The verdict. Pure: the same inputs always give the same Verdict.

    The checks decide the direction, and only by a margin. Per case, delta = b_score - a_score (summed check
    scores over the case's complete pairs); total = the sum of the case deltas. total <= -min_score_margin, or
    any one case with delta <= -min_score_margin, is WORSE, whatever the judge or Jev say. total >=
    min_score_margin (and no case worse by the margin) is IMPROVES only when the blind judge (b_winrate >=
    judge_min_b_winrate) and Jev (`improves`, confidence >= jev_min_confidence) agree; otherwise INCONCLUSIVE.
    A total inside the margin counts as equal checks: NO_EFFECT, or INCONCLUSIVE `judge-only-preference` when the
    judge strongly prefers either arm. The judge alone never yields WORSE or IMPROVES. With policy/2 (5 pairs,
    margin 2.0) one flipped run is noise both ways: A 5/5 vs B 4/5 is NO_EFFECT, and so is A 4/5 vs B 5/5.
    Partial scores count: A 3.0 vs B 5.0 moves. A 2+2+1 plan with B +1 on every case (total +3) moves; +1, +1,
    -1 (total +1) does not; -2 on one case with +2, +2 elsewhere is WORSE. The full order is in the module
    docstring."""
    policy = policy or VerdictPolicy()
    inputs = _inputs(machine, judge, jev, policy)

    def out(verdict: str, *reasons: str) -> Verdict:
        return Verdict(verdict, tuple(reasons), inputs)

    if machine.refusals:
        return out("INCONCLUSIVE", *machine.refusals)
    if machine.budget_stop:
        return out("INCONCLUSIVE", f"budget-exhausted:{machine.budget_stop}")
    if len(machine.errors) > policy.max_error_runs:
        return out("INCONCLUSIVE", f"run-errors:{len(machine.errors)}>{policy.max_error_runs}", *machine.errors[:5])
    if machine.runs_done < machine.runs_expected:
        return out("INCONCLUSIVE", f"missing-runs:{machine.runs_done}/{machine.runs_expected}")
    if not machine.cases:
        return out("INCONCLUSIVE", "no-cases")
    empty = [c.case for c in machine.cases if c.pairs == 0]
    if empty:
        return out("INCONCLUSIVE", *(f"missing-arm:{c}" for c in empty))
    if machine.pairs < policy.min_pairs:
        return out("INCONCLUSIVE", f"too-few-pairs:{machine.pairs}<{policy.min_pairs}")
    if jev and jev.status == "ok" and jev.harness_problem is not None and jev.harness_problem >= policy.jev_harness_max:
        return out("INCONCLUSIVE", f"jev-harness-problem:{jev.harness_problem:.2f}>={policy.jev_harness_max}")
    # Code sees what Jev may miss (2026-10-26, codex: A 0/3, B 0/3, Jev improves 0.71, harness_problem 0.22): when
    # both arms fail every machine check on every case, the checks cannot tell the arms apart, and a judge
    # preference alone must not become a verdict. Scores, not pass bits: a run that passes some scenarios is alive.
    if policy.harness_dead_inconclusive and all(c.a_score == 0 and c.b_score == 0 for c in machine.cases):
        return out("INCONCLUSIVE", "harness-dead:" + ",".join(c.case for c in machine.cases))

    # The margin applies to the total over all cases (a plan of 2+2+1 pairs can still move), and a single case
    # that regresses by the margin blocks on its own, so a big loss on one case is never averaged away.
    margin = policy.min_score_margin
    delta = {c.case: round(c.b_score - c.a_score, 6) for c in machine.cases}
    total = round(sum(delta.values()), 6)
    total_why = f"total-delta:{total:+g} margin {_score(margin)}"
    up = [c for c in machine.cases if delta[c.case] > 0]
    down = [c for c in machine.cases if delta[c.case] < 0]
    if total <= -margin or any(-delta[c.case] >= margin for c in down):
        return out("WORSE", *(f"regressed:{_moved(c)}" for c in down), total_why)

    wr = judge.b_winrate if judge else None
    if total >= margin:
        why = ([f"improved:{_moved(c)}" for c in up] + [f"within-noise:{_moved(c)}" for c in down] + [total_why])
        if policy.require_judge:
            if judge is None or judge.mode == "none" or not judge.judged:
                return out("INCONCLUSIVE", *why, "judge-missing")
            if judge.errors or judge.judged < machine.pairs:
                return out("INCONCLUSIVE", *why, f"judge-incomplete:{judge.judged}/{machine.pairs}")
        if wr is not None and wr < policy.judge_min_b_winrate:
            return out("INCONCLUSIVE", *why, f"judge-disagrees:b_winrate {wr:.2f}<{policy.judge_min_b_winrate}")
        if policy.require_jev:
            if jev is None or jev.status != "ok":
                return out("INCONCLUSIVE", *why, f"jev-{'missing' if jev is None else jev.status}")
            if jev.effect != "improves":
                return out("INCONCLUSIVE", *why, f"jev-disagrees:{jev.effect}")
            if jev.confidence is None or jev.confidence < policy.jev_min_confidence:
                return out("INCONCLUSIVE", *why, f"jev-low-confidence:{jev.confidence}")
        tail = [f"judge b_winrate {wr:.2f}"] if wr is not None else []
        if jev and jev.status == "ok":
            tail.append(f"jev {jev.effect} {jev.confidence}")
        return out("IMPROVES", *why, *tail)

    # Equal checks (inside the margin). The judge may make the result unclear, never WORSE or IMPROVES.
    moved = [c for c in machine.cases if delta[c.case]]
    within = [f"within-noise:{_moved(c)}" for c in moved] + ([total_why] if moved else [])
    # b_winrate <= 1 - judge_strong: the judge prefers the baseline; >= judge_strong: the candidate.
    if wr is not None and (wr <= 1 - policy.judge_strong_winrate or wr >= policy.judge_strong_winrate):
        return out("INCONCLUSIVE", "checks-equal", *within, f"judge-only-preference:b_winrate {wr:.2f}")
    return out("NO_EFFECT", "checks-equal" + (f", judge b_winrate {wr:.2f}" if wr is not None else ""), *within)
