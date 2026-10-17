"""Budgets for the nightly RSI investigator (Mission 005, docs/rsi/m005-plan.md 2.4; brief Part D).

One `Budget` per night. It holds the caps from `config/rsi-night.json` ("budget") and records every model call
and every sandbox run (model, tokens, USD, duration), so the night report and each experiment's meta carry the
real cost. A cap that is used up raises `BudgetExhausted(kind)`; the orchestrator stops cleanly on it. It is
never retried.

Caps:
- per-model call caps (`model_calls`); a model the config does not name gets `default_model_calls` (0 = refused),
  except free stand-ins (stub / fixture / none), which get `free_model_calls`
- max hypotheses (hard ceiling 2), max implemented experiments, max sandbox runs
- a wall clock (`wall_clock_s`) for the whole night
- a USD cap, checked before a call with the model's estimate (`cost_estimate_usd`) and after it with the real cost
- bounded retries (`model_retries`, hard ceiling 2): a retry is a new call and counts against the model's cap

The investigator never writes this config: the caps are data a human changes.
`sandbox/rsi_sandbox.py` replays (rsi_replay.run_replay) duck-type the same object: take_sandbox_run /
record_run / remaining_s / take_call / record_call.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

HARD_MAX_HYPOTHESES = 2          # contract 2.4: at most two hypotheses, whatever the config says
HARD_MAX_EXPERIMENTS = 3
HARD_MAX_RETRIES = 2
HARD_MAX_WALL_CLOCK_S = 4 * 3600  # the sandbox's own MAX_TIMEOUT_S
FREE_MODELS = ("stub", "fixture", "none")
_EPS = 1e-9

T = TypeVar("T")


class BudgetExhausted(RuntimeError):
    """A cap is used up. `kind`: wall_clock | usd | sandbox_runs | experiments | calls:<model>."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"budget exhausted: {kind}" + (f" ({detail})" if detail else ""))
        self.kind = kind


class BudgetConfigError(ValueError):
    pass


def is_free_model(model: str) -> bool:
    m = model.lower()
    return m in FREE_MODELS or any(m.endswith("-" + f) or m.startswith(f + "-") for f in FREE_MODELS)


def _int(d: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise BudgetConfigError(f"budget.{key} must be an integer in {lo}..{hi}, got {v!r}")
    return v


def _num(d: Mapping[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    v = d.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise BudgetConfigError(f"budget.{key} must be a number in {lo}..{hi}, got {v!r}")
    return float(v)


def _caps(d: Mapping[str, Any], key: str, *, number: bool) -> dict[str, Any]:
    raw = d.get(key, {})
    if not isinstance(raw, dict):
        raise BudgetConfigError(f"budget.{key} must be an object")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        ok = (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0) if number \
            else (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 1000)
        if not isinstance(k, str) or not k or not ok:
            raise BudgetConfigError(f"budget.{key}.{k} is invalid: {v!r}")
        out[k] = float(v) if number else int(v)
    return out


@dataclass(frozen=True)
class BudgetConfig:
    max_hypotheses: int = 2
    max_experiments: int = 1
    max_sandbox_runs: int = 16
    wall_clock_s: float = 5400.0
    max_usd: float = 0.0
    model_calls: Mapping[str, int] = field(default_factory=dict)
    default_model_calls: int = 0
    free_model_calls: int = 200
    model_retries: int = 1
    cost_estimate_usd: Mapping[str, float] = field(default_factory=dict)
    sandbox_run_estimate_usd: float = 0.0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> BudgetConfig:
        if not isinstance(d, Mapping):
            raise BudgetConfigError("budget must be an object")
        known = {f for f in cls.__dataclass_fields__}
        extra = sorted(k for k in d if k not in known and not k.startswith("_"))
        if extra:
            raise BudgetConfigError(f"unknown budget keys: {extra}")
        return cls(
            max_hypotheses=_int(d, "max_hypotheses", 2, 1, HARD_MAX_HYPOTHESES),
            max_experiments=_int(d, "max_experiments", 1, 0, HARD_MAX_EXPERIMENTS),
            max_sandbox_runs=_int(d, "max_sandbox_runs", 16, 0, 200),
            wall_clock_s=_num(d, "wall_clock_s", 5400.0, 1.0, HARD_MAX_WALL_CLOCK_S),
            max_usd=_num(d, "max_usd", 0.0, 0.0, 100.0),
            model_calls=_caps(d, "model_calls", number=False),
            default_model_calls=_int(d, "default_model_calls", 0, 0, 1000),
            free_model_calls=_int(d, "free_model_calls", 200, 0, 1000),
            model_retries=_int(d, "model_retries", 1, 0, HARD_MAX_RETRIES),
            cost_estimate_usd=_caps(d, "cost_estimate_usd", number=True),
            sandbox_run_estimate_usd=_num(d, "sandbox_run_estimate_usd", 0.0, 0.0, 100.0),
        )

    def call_cap(self, model: str) -> int:
        if model in self.model_calls:
            return int(self.model_calls[model])
        return self.free_model_calls if is_free_model(model) else self.default_model_calls

    def estimate(self, model: str) -> float:
        if model in self.cost_estimate_usd:
            return float(self.cost_estimate_usd[model])
        return 0.0 if is_free_model(model) else float(self.cost_estimate_usd.get("default", 0.0))


@dataclass
class Usage:
    """What one call cost. Adapters return it with their value."""
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0


class Budget:
    def __init__(self, cfg: BudgetConfig, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._clock = clock
        self._start = clock()
        self.calls: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.provider_requests: dict[str, dict[str, float]] = {}
        self._taken_calls: dict[str, int] = {}
        self._taken_runs = 0
        self._experiments = 0
        self.exhausted: str | None = None

    # -- time and money ------------------------------------------------------------------------------
    def elapsed_s(self) -> float:
        return self._clock() - self._start

    def remaining_s(self) -> float:
        return max(0.0, self.cfg.wall_clock_s - self.elapsed_s())

    def spent_usd(self) -> float:
        return round(sum(c["usd"] for c in self.calls) + sum(r["usd"] for r in self.runs)
                     + sum(p["usd"] for p in self.provider_requests.values()), 6)

    def _stop(self, kind: str, detail: str = "") -> BudgetExhausted:
        if self.exhausted is None:
            self.exhausted = kind
        return BudgetExhausted(kind, detail)

    def check(self) -> None:
        """Raise when the wall clock or the USD cap is used up. Called at every stage boundary."""
        if self.remaining_s() <= 0:
            raise self._stop("wall_clock", f"{self.cfg.wall_clock_s:g}s")
        if self.spent_usd() > self.cfg.max_usd + _EPS:
            raise self._stop("usd", f"spent {self.spent_usd():.4f} > cap {self.cfg.max_usd:.4f}")

    # -- model calls ---------------------------------------------------------------------------------
    def take_call(self, model: str, stage: str) -> None:
        """Reserve one call of `model` before making it. Raises BudgetExhausted."""
        self.check()
        n = self._taken_calls.get(model, 0)
        if n >= self.cfg.call_cap(model):
            raise self._stop(f"calls:{model}", f"{stage}: cap {self.cfg.call_cap(model)}")
        est = self.cfg.estimate(model)
        if est > 0 and self.spent_usd() + est > self.cfg.max_usd + _EPS:
            raise self._stop("usd", f"{stage}: {model} estimate {est:.4f} over the cap {self.cfg.max_usd:.4f}")
        self._taken_calls[model] = n + 1

    def record_call(self, model: str, *, stage: str, tokens_in: int = 0, tokens_out: int = 0, usd: float = 0.0,
                    duration_s: float = 0.0, ok: bool = True, error: str | None = None, attempt: int = 1) -> None:
        self.calls.append({"model": model, "stage": stage, "tokens_in": int(tokens_in or 0),
                           "tokens_out": int(tokens_out or 0), "usd": float(usd or 0.0),
                           "duration_s": round(float(duration_s or 0.0), 3), "ok": bool(ok),
                           "error": (error or None) and str(error)[:200], "attempt": attempt})

    def call(self, model: str, stage: str, fn: Callable[[], tuple[T, Usage]], *,
             retry_on: tuple[type[BaseException], ...] = (), retries: int | None = None) -> T:
        """Run one model call through the budget: reserve, run, record. `fn` returns (value, Usage).
        Only `retry_on` errors are retried, at most `min(retries, cfg.model_retries)` times; every attempt is a
        new reserved call. BudgetExhausted and anything else propagate at once."""
        limit = self.cfg.model_retries if retries is None else min(int(retries), self.cfg.model_retries)
        attempt = 0
        while True:
            attempt += 1
            self.take_call(model, stage)
            t0 = self._clock()
            try:
                value, usage = fn()
            except BudgetExhausted:
                raise
            except BaseException as exc:
                usage = getattr(exc, "usage", None) or Usage()
                self.record_call(model, stage=stage, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                                 usd=usage.usd, duration_s=self._clock() - t0, ok=False,
                                 error=f"{type(exc).__name__}: {exc}", attempt=attempt)
                if isinstance(exc, retry_on) and attempt <= limit:
                    continue
                raise
            self.record_call(model, stage=stage, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                             usd=usage.usd, duration_s=self._clock() - t0, attempt=attempt)
            return value

    # -- sandbox runs, experiments, hypotheses ---------------------------------------------------------
    def take_sandbox_run(self, run_id: str) -> None:
        """Reserve one container run before starting it. Raises BudgetExhausted."""
        self.check()
        if self._taken_runs >= self.cfg.max_sandbox_runs:
            raise self._stop("sandbox_runs", f"cap {self.cfg.max_sandbox_runs} ({run_id})")
        est = self.cfg.sandbox_run_estimate_usd
        if est > 0 and self.spent_usd() + est > self.cfg.max_usd + _EPS:
            raise self._stop("usd", f"sandbox run estimate {est:.4f} over the cap")
        self._taken_runs += 1

    def record_run(self, run_id: str, *, duration_s: float = 0.0, tokens_in: int = 0, tokens_out: int = 0,
                   usd: float = 0.0, model: str | None = None, exit_code: int | None = None,
                   error: str | None = None) -> None:
        self.runs.append({"run_id": str(run_id), "duration_s": round(float(duration_s or 0.0), 3),
                          "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0),
                          "usd": float(usd or 0.0), "model": model, "exit_code": exit_code,
                          "error": (error or None) and str(error)[:200]})

    def take_experiment(self) -> None:
        self.check()
        if self._experiments >= self.cfg.max_experiments:
            raise self._stop("experiments", f"cap {self.cfg.max_experiments}")
        self._experiments += 1

    def experiments_left(self) -> int:
        return max(0, self.cfg.max_experiments - self._experiments)

    def hypothesis_cap(self) -> int:
        return min(self.cfg.max_hypotheses, HARD_MAX_HYPOTHESES)

    def record_provider(self, name: str, requests: int = 1, usd: float = 0.0) -> None:
        p = self.provider_requests.setdefault(name, {"requests": 0, "usd": 0.0})
        p["requests"] += int(requests)
        p["usd"] = round(p["usd"] + float(usd or 0.0), 6)

    # -- summaries -----------------------------------------------------------------------------------
    def mark(self) -> tuple[int, int]:
        return len(self.calls), len(self.runs)

    def since(self, mark: tuple[int, int]) -> dict[str, Any]:
        """Calls and runs after `mark`: the cost of one experiment (stored in its meta)."""
        calls, runs = self.calls[mark[0]:], self.runs[mark[1]:]
        usd = round(sum(c["usd"] for c in calls) + sum(r["usd"] for r in runs), 6)
        return {"calls": calls, "runs": runs, "sandbox_runs": len(runs), "usd": usd,
                "tokens_in": sum(c["tokens_in"] for c in calls) + sum(r["tokens_in"] for r in runs),
                "tokens_out": sum(c["tokens_out"] for c in calls) + sum(r["tokens_out"] for r in runs)}

    def costs(self) -> dict[str, Any]:
        """The report's `costs` block (contract 2.5)."""
        by_model: dict[str, dict[str, Any]] = {}
        for c in self.calls:
            m = by_model.setdefault(c["model"], {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0})
            m["calls"] += 1
            m["tokens_in"] += c["tokens_in"]
            m["tokens_out"] += c["tokens_out"]
            m["usd"] = round(m["usd"] + c["usd"], 6)
        for r in self.runs:
            if r.get("model") and (r["tokens_in"] or r["tokens_out"] or r["usd"]):
                m = by_model.setdefault(r["model"], {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0})
                m["tokens_in"] += r["tokens_in"]
                m["tokens_out"] += r["tokens_out"]
                m["usd"] = round(m["usd"] + r["usd"], 6)
        providers = {k: {"requests": int(v["requests"]), "usd": float(v["usd"])}
                     for k, v in sorted(self.provider_requests.items())}
        return {"by_model": dict(sorted(by_model.items())), "sandbox_runs": len(self.runs),
                "providers": providers, "total_usd": self.spent_usd()}

    def caps(self) -> dict[str, Any]:
        c = self.cfg
        return {"max_hypotheses": self.hypothesis_cap(), "max_experiments": c.max_experiments,
                "max_sandbox_runs": c.max_sandbox_runs, "wall_clock_s": c.wall_clock_s, "max_usd": c.max_usd,
                "model_calls": dict(c.model_calls), "model_retries": c.model_retries}
