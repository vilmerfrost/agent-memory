"""The sandbox-replay verdict: deterministic code over three evaluation layers (Mission 005, plan 2.1).

    machine  objective checks per case and arm (ArmPair), plus the run errors and refusals
    judge    a blind pairwise judge's tally (JudgeResult); it never knew which attempt was A or B
    jev      Jev's typed read of the structured results (JevResult; questions/rsi-replay.toml)

`decide` is a pure function of those numbers and a versioned policy (config/rsi-verdict-policy.json). No model
ever decides the verdict, and the investigator cannot change the policy: it is repo data, read here only.

Decision order (the first rule that fires wins):

    1. refusals (stale baseline, artifact not exact, ...)        -> INCONCLUSIVE
    2. budget stop, run errors or timeouts, missing runs          -> INCONCLUSIVE
    3. fewer complete pairs than policy.min_pairs, or a case
       without a pair                                             -> INCONCLUSIVE
    4. Jev says the harness is broken (P >= jev_harness_max)      -> INCONCLUSIVE
    5. B's pass rate drops on any case (>= min_worse_delta)       -> WORSE   (a regression anywhere blocks)
    6. B's pass rate rises on a case (>= min_improve_delta):
         judge missing / incomplete / B win rate < judge_min     -> INCONCLUSIVE
         Jev missing / not "improves" / confidence < min         -> INCONCLUSIVE
         else                                                     -> IMPROVES
    7. equal checks: judge B win rate <= 1 - judge_strong         -> WORSE
                     judge B win rate >= judge_strong             -> INCONCLUSIVE (a preference no check sees)
                     else                                         -> NO_EFFECT

Only IMPROVES maps to a passing rsi-eval/1 verdict (`improves`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from agent_memory import config

VERDICTS = ("IMPROVES", "NO_EFFECT", "WORSE", "INCONCLUSIVE")
EVAL_VERDICT = {"IMPROVES": "improves", "NO_EFFECT": "no-effect", "WORSE": "worse", "INCONCLUSIVE": "inconclusive"}
POLICY_FORMAT = "rsi-verdict-policy/1"
POLICY_PATH = config.REPO_ROOT / "config" / "rsi-verdict-policy.json"
JEV_EFFECTS = ("improves", "no_effect", "worse", "unclear")


class PolicyError(ValueError):
    """The policy file is missing, malformed or of another version."""


@dataclass(frozen=True)
class VerdictPolicy:
    version: str = POLICY_FORMAT
    min_pairs: int = 3
    max_error_runs: int = 0
    min_improve_delta: float = 0.01
    min_worse_delta: float = 0.01
    require_judge: bool = True
    judge_min_b_winrate: float = 0.5
    judge_strong_winrate: float = 0.75
    require_jev: bool = True
    jev_min_confidence: float = 0.5
    jev_harness_max: float = 0.7
    sha256: str = ""              # of the policy file it came from ("" for the built-in default)

    def describe(self) -> dict[str, Any]:
        return asdict(self)


def load_policy(path: Path | None = None) -> VerdictPolicy:
    """The versioned policy file (default config/rsi-verdict-policy.json). Unknown keys or a bad value refuse."""
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
        else:
            ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
        if not ok:
            raise PolicyError(f"{path}: bad value for {k}: {v!r}")
        kwargs[k] = v
    return VerdictPolicy(**kwargs, sha256=hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------- the three layers


@dataclass(frozen=True)
class CaseStats:
    """One case: complete A/B pairs and how many runs of each arm passed the objective check."""
    case: str
    pairs: int
    a_pass: int
    b_pass: int

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
        "policy": {"version": policy.version, "sha256": policy.sha256},
        "machine": {"pairs": machine.pairs, "runs_expected": machine.runs_expected, "runs_done": machine.runs_done,
                    "errors": len(machine.errors), "refusals": list(machine.refusals),
                    "budget_stop": machine.budget_stop,
                    "cases": [{"case": c.case, "pairs": c.pairs, "a_pass": c.a_pass, "b_pass": c.b_pass,
                               "a_rate": round(c.a_rate, 4), "b_rate": round(c.b_rate, 4)} for c in machine.cases]},
        "judge": judge.summary() if judge else None,
        "jev": jev.summary() if jev else None,
    }


def decide(machine: ArmPair, judge: JudgeResult | None, jev: JevResult | None,
           policy: VerdictPolicy | None = None) -> Verdict:
    """The verdict. Pure: the same inputs always give the same Verdict."""
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

    worse = [c for c in machine.cases if c.a_rate - c.b_rate >= policy.min_worse_delta]
    better = [c for c in machine.cases if c.b_rate - c.a_rate >= policy.min_improve_delta]
    if worse:
        return out("WORSE", *(f"regressed:{c.case} A {c.a_pass}/{c.pairs} -> B {c.b_pass}/{c.pairs}" for c in worse))

    wr = judge.b_winrate if judge else None
    if better:
        why = [f"improved:{c.case} A {c.a_pass}/{c.pairs} -> B {c.b_pass}/{c.pairs}" for c in better]
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

    if wr is not None and wr <= 1 - policy.judge_strong_winrate:
        return out("WORSE", "checks-equal", f"judge-prefers-baseline:b_winrate {wr:.2f}")
    if wr is not None and wr >= policy.judge_strong_winrate:
        return out("INCONCLUSIVE", "checks-equal", f"judge-only-preference:b_winrate {wr:.2f}")
    return out("NO_EFFECT", "checks-equal" + (f", judge b_winrate {wr:.2f}" if wr is not None else ""))
