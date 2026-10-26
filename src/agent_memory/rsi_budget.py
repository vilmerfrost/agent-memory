"""Budgets for the nightly RSI investigator (Mission 005, docs/rsi/m005-plan.md 2.4; brief Part D).

One `Budget` per night. It holds the caps from `config/rsi-night.json` ("budget") and records every model call
and every sandbox run (model, tokens, USD, duration), so the night report and each experiment's meta carry the
real cost. A cap that is used up raises `BudgetExhausted(kind)`; the orchestrator stops cleanly on it. It is
never retried.

Caps:
- per-model call caps (`model_calls`); a model the config does not name gets `default_model_calls` (0 = refused),
  except free stand-ins (stub / fixture / none), which get `free_model_calls`
- max hypotheses per hypothesizer reply (hard ceiling 2; one reply per selected candidate, so per candidate, not
  per night), max implemented experiments per night (hard ceiling 3; rsi_night runs at most one per candidate),
  max sandbox runs per night
- max A/B pairs per replay, so per experiment (`max_ab_pairs`); the night refuses a bigger replay
- a wall clock (`wall_clock_s`) for the whole night
- a USD cap, checked before a call with the model's estimate (`cost_estimate_usd`) and after it with the real cost
- bounded retries (`model_retries`, hard ceiling 2): a retry is a new call and counts against the model's cap

What a call costs (the USD the cap sees), fail-closed for paid models (`PriceTable`, config/rsi-prices.json):
- a cost the provider reports (Claude Code's total_cost_usd) is booked as reported
- else the versioned price table: per token (Codex models) or a flat price per call (Jev)
- a paid model the table does not name is priced with the table's high `default`; a paid call with neither a
  cost nor token counts books `unknown_usage_usd`. A paid call is never booked at $0. Free stand-ins book $0.
- the pre-call estimate of a paid model without a configured `cost_estimate_usd` comes from the table too.
Each cost is booked once: a container run whose model call was already recorded (the live judge) is recorded
with `cost_on_call=True` and carries no tokens or USD of its own.

The investigator never writes these configs: the caps and prices are data a human changes.
`sandbox/rsi_sandbox.py` replays (rsi_replay.run_replay) duck-type the same object: take_sandbox_run /
record_run / remaining_s / take_call / record_call.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from agent_memory import config

HARD_MAX_HYPOTHESES = 2          # contract 2.4: at most two hypotheses per hypothesizer reply (per candidate)
HARD_MAX_EXPERIMENTS = 3
HARD_MAX_RETRIES = 2
# The night starts at 01:30 and the brief reads it at 07:30. At this ceiling the night's own SIGALRM comes at 06:30
# and the watchdog's hard exit (+10 + 2 min, rsi_schedule) at 06:42. Each container keeps its own short timeout.
HARD_MAX_WALL_CLOCK_S = 5 * 3600
HARD_MAX_AB_PAIRS = 60           # rsi_replay: 3 cases x 20 reps
# 3 experiments x 2 providers x 6 subscription runs x (8 reserved tool containers + 1 check) = 324, plus a margin.
HARD_MAX_SANDBOX_RUNS = 400
FREE_MODELS = ("stub", "fixture", "none")
# What each price basis means for the human reading the costs (Mission 005.5: verified provider billing vs local
# estimates). "reported": the provider's own CLI reported the cost of the call (Claude Code's total_cost_usd); it is
# not reconciled with the provider's invoice. Every other paid basis is a local estimate from config/rsi-prices.json
# or a cap. Free stand-ins and costs booked on another record cost nothing here.
COST_KINDS = {"reported": "provider-reported", "table": "estimate", "default": "estimate", "call": "estimate",
              "unknown-usage": "estimate", "cli-cap": "estimate", "free": "free", "none": "free", "on-call": "free",
              "subscription": "subscription"}
PRICES_FORMAT = "rsi-prices/1"
PRICES_PATH = config.REPO_ROOT / "config" / "rsi-prices.json"
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


# ---------------------------------------------------------------- prices


class PriceConfigError(ValueError):
    """The price table is missing, malformed or of another format."""


@dataclass(frozen=True)
class Price:
    """One model's price. basis "tokens": USD per million tokens (cached input and cache writes default to the
    input price); basis "call": a flat USD price per call. `unknown_usage_usd`: what a paid call that reported
    neither a cost nor tokens books (None: the table default's)."""
    basis: str
    input_per_mtok: float = 0.0
    cached_input_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None
    output_per_mtok: float = 0.0
    per_call_usd: float = 0.0
    unknown_usage_usd: float | None = None

    @classmethod
    def from_dict(cls, d: Any, where: str) -> Price:
        if not isinstance(d, Mapping):
            raise PriceConfigError(f"{where} must be an object")
        meta = {"source"}
        rates = ("input_per_mtok", "cached_input_per_mtok", "cache_write_per_mtok", "output_per_mtok",
                 "per_call_usd", "unknown_usage_usd")
        extra = sorted(k for k in d if k not in {"basis", *rates, *meta} and not k.startswith("_"))
        if extra:
            raise PriceConfigError(f"{where}: unknown keys {extra}")
        vals: dict[str, float] = {}
        for k in rates:
            if k in d:
                v = d[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1000:
                    raise PriceConfigError(f"{where}.{k} must be a number in 0..1000, got {v!r}")
                vals[k] = float(v)
        basis = d.get("basis")
        if basis == "tokens":
            if vals.get("input_per_mtok", 0) <= 0 or "output_per_mtok" not in vals:
                raise PriceConfigError(f"{where}: a tokens price needs input_per_mtok > 0 and output_per_mtok")
            if "per_call_usd" in vals:
                raise PriceConfigError(f"{where}: per_call_usd belongs to basis 'call'")
        elif basis == "call":
            if vals.get("per_call_usd", 0) <= 0:
                raise PriceConfigError(f"{where}: a call price needs per_call_usd > 0")
            if set(vals) - {"per_call_usd", "unknown_usage_usd"}:
                raise PriceConfigError(f"{where}: token prices belong to basis 'tokens'")
        else:
            raise PriceConfigError(f"{where}.basis must be 'tokens' or 'call', got {basis!r}")
        if "unknown_usage_usd" in vals and vals["unknown_usage_usd"] <= 0:
            raise PriceConfigError(f"{where}.unknown_usage_usd must be > 0")
        return cls(basis=basis, **vals)

    def tokens_usd(self, tokens_in: int, tokens_out: int, cached: int = 0, cache_write: int = 0) -> float:
        """USD for one call's tokens. `tokens_in` is all input; `cached` and `cache_write` are parts of it."""
        cached = min(max(int(cached), 0), max(int(tokens_in), 0))
        cache_write = min(max(int(cache_write), 0), max(int(tokens_in), 0) - cached)
        plain = max(int(tokens_in), 0) - cached - cache_write
        cr = self.input_per_mtok if self.cached_input_per_mtok is None else self.cached_input_per_mtok
        cw = self.input_per_mtok if self.cache_write_per_mtok is None else self.cache_write_per_mtok
        return (plain * self.input_per_mtok + cached * cr + cache_write * cw
                + max(int(tokens_out), 0) * self.output_per_mtok) / 1_000_000


@dataclass(frozen=True)
class PriceTable:
    """config/rsi-prices.json (rsi-prices/1): what the budget books for a paid call that reports no cost."""
    models: Mapping[str, Price]
    default: Price
    version: str = ""
    sha256: str = ""

    @classmethod
    def from_dict(cls, d: Any, *, sha256: str = "") -> PriceTable:
        if not isinstance(d, Mapping) or d.get("format") != PRICES_FORMAT:
            raise PriceConfigError(f"price table format must be {PRICES_FORMAT}")
        extra = sorted(k for k in d if k not in ("format", "version", "models", "default") and not k.startswith("_"))
        if extra:
            raise PriceConfigError(f"price table: unknown keys {extra}")
        if not isinstance(d.get("version"), str) or not d["version"]:
            raise PriceConfigError("price table: version is a non-empty string")
        raw = d.get("models")
        if not isinstance(raw, Mapping):
            raise PriceConfigError("price table: models must be an object")
        models = {}
        for name, p in raw.items():
            if not isinstance(name, str) or not name or is_free_model(name):
                raise PriceConfigError(f"price table: bad model name {name!r} (free stand-ins are never priced)")
            models[name] = Price.from_dict(p, f"models.{name}")
        default = Price.from_dict(d.get("default"), "default")
        if default.basis != "tokens" or not default.unknown_usage_usd:
            raise PriceConfigError("price table: default must be a tokens price with unknown_usage_usd > 0")
        return cls(models=models, default=default, version=d["version"], sha256=sha256)

    def describe(self) -> dict[str, Any]:
        return {"format": PRICES_FORMAT, "version": self.version, "sha256": self.sha256}

    def price(self, model: str) -> tuple[Price, bool]:
        """(the model's price, whether the table names it)."""
        p = self.models.get(model)
        return (p, True) if p is not None else (self.default, False)

    def unknown_usage_usd(self, model: str) -> float:
        p, _ = self.price(model)
        return float(p.unknown_usage_usd or self.default.unknown_usage_usd or 0.0)

    def estimate(self, model: str) -> float:
        """A paid call's pre-call estimate when the budget config has none: its flat price, else what a call with
        unknown usage books."""
        p, _ = self.price(model)
        return p.per_call_usd if p.basis == "call" else self.unknown_usage_usd(model)

    def book(self, model: str | None, *, reported_usd: float = 0.0, tokens_in: int = 0, tokens_out: int = 0,
             tokens_cached: int = 0, tokens_cache_write: int = 0) -> tuple[float, str]:
        """(USD to book, basis). basis: reported | table | default | call | unknown-usage | free | none.
        A paid model (any name that is not a free stand-in) is never booked at 0."""
        reported = float(reported_usd or 0.0)
        if model is None:
            return reported, ("reported" if reported > 0 else "none")
        if is_free_model(model):
            return reported, "free"
        if reported > 0:
            return reported, "reported"
        p, named = self.price(model)
        if p.basis == "call":
            return p.per_call_usd, "call"
        usd = p.tokens_usd(tokens_in, tokens_out, tokens_cached, tokens_cache_write)
        if usd > 0:
            return round(usd, 10), ("table" if named else "default")
        return self.unknown_usage_usd(model), "unknown-usage"


def load_prices(path: Path | None = None) -> PriceTable:
    """The versioned price table (default config/rsi-prices.json). A bad file refuses (PriceConfigError)."""
    path = Path(path or PRICES_PATH)
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise PriceConfigError(f"{path}: unreadable price table ({type(exc).__name__})") from None
    return PriceTable.from_dict(data, sha256=hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------- the budget


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
    max_ab_pairs: int = HARD_MAX_AB_PAIRS     # cases x reps of one replay (the night refuses a bigger replay)
    subscription_models: tuple[str, ...] = ()
    max_subscription_tokens: int = 0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> BudgetConfig:
        if not isinstance(d, Mapping):
            raise BudgetConfigError("budget must be an object")
        known = {f for f in cls.__dataclass_fields__}
        extra = sorted(k for k in d if k not in known and not k.startswith("_"))
        if extra:
            raise BudgetConfigError(f"unknown budget keys: {extra}")
        subscription = d.get("subscription_models", [])
        if not isinstance(subscription, list) or any(not isinstance(m, str) or not m or is_free_model(m)
                                                   for m in subscription) or len(set(subscription)) != len(subscription):
            raise BudgetConfigError("budget.subscription_models must be unique non-free model names")
        token_cap = _int(d, "max_subscription_tokens", 0, 0, 20_000_000)
        if subscription and token_cap == 0:
            raise BudgetConfigError("subscription models require a positive max_subscription_tokens")
        return cls(
            max_hypotheses=_int(d, "max_hypotheses", 2, 1, HARD_MAX_HYPOTHESES),
            max_experiments=_int(d, "max_experiments", 1, 0, HARD_MAX_EXPERIMENTS),
            max_sandbox_runs=_int(d, "max_sandbox_runs", 16, 0, HARD_MAX_SANDBOX_RUNS),
            wall_clock_s=_num(d, "wall_clock_s", 5400.0, 1.0, HARD_MAX_WALL_CLOCK_S),
            max_usd=_num(d, "max_usd", 0.0, 0.0, 100.0),
            model_calls=_caps(d, "model_calls", number=False),
            default_model_calls=_int(d, "default_model_calls", 0, 0, 1000),
            free_model_calls=_int(d, "free_model_calls", 200, 0, 1000),
            model_retries=_int(d, "model_retries", 1, 0, HARD_MAX_RETRIES),
            cost_estimate_usd=_caps(d, "cost_estimate_usd", number=True),
            sandbox_run_estimate_usd=_num(d, "sandbox_run_estimate_usd", 0.0, 0.0, 100.0),
            max_ab_pairs=_int(d, "max_ab_pairs", HARD_MAX_AB_PAIRS, 1, HARD_MAX_AB_PAIRS),
            subscription_models=tuple(subscription), max_subscription_tokens=token_cap,
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
    """What one call cost. Adapters return it with their value. usd 0 = not reported: the price table books it.
    tokens_in is all input; tokens_cached and tokens_cache_write are the parts of it priced differently."""
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    tokens_cached: int = 0
    tokens_cache_write: int = 0


class Budget:
    def __init__(self, cfg: BudgetConfig, *, clock: Callable[[], float] = time.monotonic,
                 prices: PriceTable | None = None) -> None:
        self.cfg = cfg
        self.prices = prices if prices is not None else load_prices()
        self._clock = clock
        self._start = clock()
        self.calls: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.provider_requests: dict[str, dict[str, float]] = {}
        self._taken_calls: dict[str, int] = {}
        self._taken_runs = 0
        self._experiments = 0
        self.exhausted: str | None = None
        self._lock = threading.RLock()
        self._next_reservation = 0
        self._pending_calls: list[dict[str, Any]] = []
        self._pending_runs: dict[str, dict[str, Any]] = {}
        self.decisions: list[dict[str, Any]] = []

    # -- time and money ------------------------------------------------------------------------------
    def elapsed_s(self) -> float:
        return self._clock() - self._start

    def remaining_s(self) -> float:
        return max(0.0, self.cfg.wall_clock_s - self.elapsed_s())

    def spent_usd(self) -> float:
        with self._lock:
            return round(self._booked_amount(), 6)

    def _booked_amount(self) -> float:
        # Do not round before the admission check: many sub-microdollar charges must not disappear.
        return math.fsum([*(c["usd"] for c in self.calls), *(r["usd"] for r in self.runs),
                          *(p["usd"] for p in self.provider_requests.values())])

    def _reserved_amount(self) -> float:
        return math.fsum([*(r["estimate_usd"] for r in self._pending_calls),
                          *(r["estimate_usd"] for r in self._pending_runs.values())])

    def reserved_usd(self) -> float:
        """Outstanding pre-call estimates, not completed spend or provider billing."""
        with self._lock:
            return round(self._reserved_amount(), 6)

    def snapshot(self, *, resource: str | None = None) -> dict[str, Any]:
        """Reproducible decision inputs; USD is separate from call/experiment/token/time caps."""
        with self._lock:
            spent, reserved = self._booked_amount(), self._reserved_amount()
            return {"format": "rsi-budget-decision/1", "currency": "USD", "scope":
                    "api-only" if self.cfg.subscription_models else "all-recorded", "resource": resource,
                    "cap_usd": self.cfg.max_usd, "booked_usd": spent, "reserved_usd": reserved,
                    "remaining_usd": round(max(0.0, self.cfg.max_usd - spent - reserved), 6),
                    "experiments_taken": self._experiments, "experiment_cap": self.cfg.max_experiments,
                    "calls_taken": dict(self._taken_calls), "sandbox_runs_taken": self._taken_runs,
                    "sandbox_run_cap": self.cfg.max_sandbox_runs, "prices": self.prices.describe()}

    def _stop(self, kind: str, detail: str = "") -> BudgetExhausted:
        if self.exhausted is None:
            self.exhausted = kind
        self.decisions.append({**self.snapshot(resource=kind), "decision": "refused", "detail": detail})
        return BudgetExhausted(kind, detail)

    def check(self) -> None:
        """Raise when the wall clock or the USD cap is used up. Called at every stage boundary."""
        if self.remaining_s() <= 0:
            raise self._stop("wall_clock", f"{self.cfg.wall_clock_s:g}s")
        if self._booked_amount() > self.cfg.max_usd + _EPS:
            raise self._stop("usd", f"spent {self.spent_usd():.4f} > cap {self.cfg.max_usd:.4f}")
        if self.cfg.subscription_models:
            records = [r for r in [*self.calls, *self.runs] if r.get("price_basis") == "subscription"]
            if any(not r.get("tokens_in") and not r.get("tokens_out") for r in records):
                raise self._stop("subscription_usage_unknown", "cannot continue without usage accounting")
            used = sum(r["tokens_in"] + r["tokens_out"] for r in records)
            if used >= self.cfg.max_subscription_tokens:
                raise self._stop("subscription_tokens", f"used {used} of {self.cfg.max_subscription_tokens}")

    def estimate(self, model: str) -> float:
        """The pre-call USD estimate: the config's, else (a paid model) the price table's. Free stand-ins: 0."""
        if model in self.cfg.subscription_models:
            return 0.0
        est = self.cfg.estimate(model)
        price, named = self.prices.price(model)
        if named and price.basis == "call":
            # A stale configured estimate must not under-reserve a known fixed per-call price.
            return max(est, price.per_call_usd)
        if est > 0 or is_free_model(model):
            return est
        return self.prices.estimate(model)

    def book(self, model: str | None, *, usd: float = 0.0, tokens_in: int = 0, tokens_out: int = 0,
             tokens_cached: int = 0, tokens_cache_write: int = 0) -> tuple[float, str]:
        """(USD to book, basis) for one call or run: the reported cost, else the price table (never 0 for a paid
        model). The basis says which (PriceTable.book)."""
        if model in self.cfg.subscription_models:
            # Excluded from the API-dollar budget, not a claim that subscription usage is free.
            return 0.0, "subscription"
        return self.prices.book(model, reported_usd=usd, tokens_in=tokens_in, tokens_out=tokens_out,
                                tokens_cached=tokens_cached, tokens_cache_write=tokens_cache_write)

    # -- model calls ---------------------------------------------------------------------------------
    def take_call(self, model: str, stage: str) -> str:
        """Atomically reserve a call and its estimate. The slot is consumed even if the call fails.

        Returned identity is optional for legacy synchronous adapters; `call` uses it explicitly so concurrent
        calls can settle out of order. An unstarted operation must use cancel_call, not fake zero-cost usage.
        """
        with self._lock:
            self.check()
            n = self._taken_calls.get(model, 0)
            if n >= self.cfg.call_cap(model):
                raise self._stop(f"calls:{model}", f"{stage}: cap {self.cfg.call_cap(model)}")
            est = self.estimate(model)
            before = self.snapshot(resource="usd")
            if self._booked_amount() + self._reserved_amount() + est > self.cfg.max_usd + _EPS:
                raise self._stop("usd", f"{stage}: {model} estimate {est:.4f}; booked {before['booked_usd']:.4f}, "
                                 f"reserved {before['reserved_usd']:.4f}, remaining {before['remaining_usd']:.4f}, "
                                 f"cap {self.cfg.max_usd:.4f}")
            self._taken_calls[model] = n + 1
            self._next_reservation += 1
            rid = f"call-{self._next_reservation}"
            self._pending_calls.append({"id": rid, "model": model, "stage": stage, "estimate_usd": est,
                                        "before": before, "prices": self.prices})
            return rid

    def cancel_call(self, reservation_id: str) -> None:
        """Release money only when the adapter proves inference never started; retain the consumed call slot."""
        with self._lock:
            found = next((r for r in self._pending_calls if r["id"] == reservation_id), None)
            if found is None:
                raise BudgetConfigError("unknown or already settled call reservation")
            self._pending_calls.remove(found)
            self.decisions.append({**self.snapshot(resource="usd"), "decision": "cancelled_unstarted",
                                   "reservation_id": reservation_id, "released_usd": found["estimate_usd"]})

    def record_call(self, model: str, *, stage: str, tokens_in: int = 0, tokens_out: int = 0, usd: float = 0.0,
                    duration_s: float = 0.0, ok: bool = True, error: str | None = None, attempt: int = 1,
                    tokens_cached: int = 0, tokens_cache_write: int = 0,
                    reservation_id: str | None = None) -> None:
        """Record one model call and book its cost: `usd` as reported (0 = not reported), else the price table.
        A failed call is booked too: a call that died mid-turn was still billed."""
        with self._lock:
            pending = next((r for r in self._pending_calls if
                            (r["id"] == reservation_id if reservation_id else
                             (r["model"], r["stage"]) == (model, stage))), None)
            if reservation_id and (pending is None or (pending["model"], pending["stage"]) != (model, stage)):
                raise BudgetConfigError("unknown, mismatched or already settled call reservation")
            price_table = pending["prices"] if pending else self.prices
            if model in self.cfg.subscription_models:
                booked, basis = 0.0, "subscription"
            else:
                booked, basis = price_table.book(model, reported_usd=usd, tokens_in=tokens_in or 0,
                                                tokens_out=tokens_out or 0, tokens_cached=tokens_cached or 0,
                                                tokens_cache_write=tokens_cache_write or 0)
            if pending:
                self._pending_calls.remove(pending)
            self.calls.append({"model": model, "stage": stage, "tokens_in": int(tokens_in or 0),
                           "tokens_out": int(tokens_out or 0), "usd": float(booked), "price_basis": basis,
                           "tokens_cached": int(tokens_cached or 0), "tokens_cache_write": int(tokens_cache_write or 0),
                           "duration_s": round(float(duration_s or 0.0), 3), "ok": bool(ok),
                           "error": (error or None) and str(error)[:200], "attempt": attempt,
                           "reservation": ({"id": pending["id"], "estimate_usd": pending["estimate_usd"],
                                            "before": pending["before"], "released_usd": pending["estimate_usd"],
                                            "settled_booked_usd": booked, "prices": price_table.describe()}
                                           if pending else None)})

    def call(self, model: str, stage: str, fn: Callable[[], tuple[T, Usage]], *,
             retry_on: tuple[type[BaseException], ...] = (), retries: int | None = None) -> T:
        """Run one model call through the budget: reserve, run, record. `fn` returns (value, Usage).
        Only `retry_on` errors are retried, at most `min(retries, cfg.model_retries)` times; every attempt is a
        new reserved call. BudgetExhausted and anything else propagate at once."""
        limit = self.cfg.model_retries if retries is None else min(int(retries), self.cfg.model_retries)
        attempt = 0
        while True:
            attempt += 1
            reservation = self.take_call(model, stage)
            t0 = self._clock()
            try:
                value, usage = fn()
            except BudgetExhausted:
                # The callback may have started inference before a nested cap stopped it. Usage is unknown,
                # not free; conservatively settle it once and release its outstanding estimate.
                self.record_call(model, stage=stage, ok=False, error="nested budget stop",
                                 duration_s=self._clock() - t0, attempt=attempt, reservation_id=reservation)
                raise
            except BaseException as exc:
                usage = getattr(exc, "usage", None) or Usage()
                self.record_call(model, stage=stage, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                                 usd=usage.usd, duration_s=self._clock() - t0, ok=False,
                                 error=f"{type(exc).__name__}: {exc}", attempt=attempt,
                                 tokens_cached=usage.tokens_cached, tokens_cache_write=usage.tokens_cache_write,
                                 reservation_id=reservation)
                if isinstance(exc, retry_on) and attempt <= limit:
                    continue
                raise
            self.record_call(model, stage=stage, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                             usd=usage.usd, duration_s=self._clock() - t0, attempt=attempt,
                             tokens_cached=usage.tokens_cached, tokens_cache_write=usage.tokens_cache_write,
                             reservation_id=reservation)
            return value

    # -- sandbox runs, experiments, hypotheses ---------------------------------------------------------
    def take_sandbox_run(self, run_id: str) -> None:
        """Reserve one container run before starting it. Raises BudgetExhausted."""
        with self._lock:
            self.check()
            if run_id in self._pending_runs:
                raise BudgetConfigError("sandbox run already reserved")
            if self._taken_runs >= self.cfg.max_sandbox_runs:
                raise self._stop("sandbox_runs", f"cap {self.cfg.max_sandbox_runs} ({run_id})")
            est = self.cfg.sandbox_run_estimate_usd
            before = self.snapshot(resource="usd")
            if self._booked_amount() + self._reserved_amount() + est > self.cfg.max_usd + _EPS:
                raise self._stop("usd", f"sandbox run estimate {est:.4f}; remaining {before['remaining_usd']:.4f}")
            self._taken_runs += 1
            self._pending_runs[run_id] = {"estimate_usd": est, "before": before}

    def record_run(self, run_id: str, *, duration_s: float = 0.0, tokens_in: int = 0, tokens_out: int = 0,
                   usd: float = 0.0, model: str | None = None, exit_code: int | None = None,
                   error: str | None = None, tokens_cached: int = 0, tokens_cache_write: int = 0,
                   cost_on_call: bool = False, price_basis: str | None = None) -> None:
        """Record one container run. A run of a paid model books its cost like record_call. cost_on_call: the
        run's model call was recorded with record_call (the live judge), so the run books no tokens and no USD
        (one cost, booked once). price_basis: the caller already priced `usd` (for example a CLI's per-run cap)."""
        with self._lock:
            pending = self._pending_runs.pop(run_id, None)
            if cost_on_call:
                tokens_in = tokens_out = tokens_cached = tokens_cache_write = 0
                booked, basis = 0.0, "on-call"
            else:
                booked, basis = self.book(model, usd=usd, tokens_in=tokens_in or 0, tokens_out=tokens_out or 0,
                                          tokens_cached=tokens_cached or 0, tokens_cache_write=tokens_cache_write or 0)
                if price_basis and float(usd or 0.0) > 0:
                    basis = str(price_basis)[:40]
            self.runs.append({"run_id": str(run_id), "duration_s": round(float(duration_s or 0.0), 3),
                          "tokens_in": int(tokens_in or 0), "tokens_out": int(tokens_out or 0),
                          "usd": float(booked), "price_basis": basis, "model": model, "exit_code": exit_code,
                          "error": (error or None) and str(error)[:200],
                          "reservation": ({"estimate_usd": pending["estimate_usd"], "before": pending["before"],
                                           "released_usd": pending["estimate_usd"], "settled_booked_usd": booked}
                                          if pending else None)})

    def take_experiment(self) -> None:
        with self._lock:
            self.check()
            if self._experiments >= self.cfg.max_experiments:
                raise self._stop("experiments", f"cap {self.cfg.max_experiments}")
            self._experiments += 1

    def experiments_left(self) -> int:
        return max(0, self.cfg.max_experiments - self._experiments)

    def hypothesis_cap(self) -> int:
        return min(self.cfg.max_hypotheses, HARD_MAX_HYPOTHESES)

    def record_provider(self, name: str, requests: int = 1, usd: float = 0.0) -> None:
        with self._lock:
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
        subscriptions = [r for r in [*self.calls, *self.runs] if r.get("price_basis") == "subscription"]
        return {"by_model": dict(sorted(by_model.items())), "sandbox_runs": len(self.runs),
                "providers": providers, "total_usd": self.spent_usd(), "prices": self.prices.describe(),
                "accounting": self.snapshot(), "budget_decisions": list(self.decisions),
                **({"subscription": {"records": len(subscriptions),
                                 "tokens_in": sum(r["tokens_in"] for r in subscriptions),
                                 "tokens_out": sum(r["tokens_out"] for r in subscriptions),
                                 "billed_usd": None, "allowance_used": None,
                                 "usage_complete": all(r["tokens_in"] or r["tokens_out"] for r in subscriptions)}}
                   if self.cfg.subscription_models else {}),
                "total_usd_scope": "api-only" if self.cfg.subscription_models else "all-recorded",
                **self.cost_bases()}

    def cost_bases(self) -> dict[str, Any]:
        """Where each booked USD came from (COST_KINDS): `by_basis` {basis: {kind, records, usd}}, `model_bases`
        {model: {basis: usd}}, and the split `reported_usd` (provider-reported) vs `estimated_usd` (local
        estimates). Provider request costs are booked as `call` estimates."""
        by_basis: dict[str, dict[str, Any]] = {}
        model_bases: dict[str, dict[str, float]] = {}
        for rec in [*self.calls, *self.runs]:
            basis = str(rec.get("price_basis") or "none")
            b = by_basis.setdefault(basis, {"kind": COST_KINDS.get(basis, "estimate"), "records": 0, "usd": 0.0})
            b["records"] += 1
            b["usd"] = round(b["usd"] + float(rec["usd"]), 6)
            if rec.get("model") and (rec["usd"] or basis not in ("none", "on-call")):
                mb = model_bases.setdefault(str(rec["model"]), {})
                mb[basis] = round(mb.get(basis, 0.0) + float(rec["usd"]), 6)
        prov = round(sum(p["usd"] for p in self.provider_requests.values()), 6)
        if prov:
            b = by_basis.setdefault("call", {"kind": "estimate", "records": 0, "usd": 0.0})
            b["records"] += sum(int(p["requests"]) for p in self.provider_requests.values())
            b["usd"] = round(b["usd"] + prov, 6)
        reported = round(sum(v["usd"] for v in by_basis.values() if v["kind"] == "provider-reported"), 6)
        estimated = round(sum(v["usd"] for v in by_basis.values() if v["kind"] == "estimate"), 6)
        return {"by_basis": dict(sorted(by_basis.items())),
                "model_bases": {k: dict(sorted(v.items())) for k, v in sorted(model_bases.items())},
                "reported_usd": reported, "estimated_usd": estimated}

    def caps(self) -> dict[str, Any]:
        c = self.cfg
        return {"max_hypotheses": self.hypothesis_cap(), "max_experiments": c.max_experiments,
                "max_sandbox_runs": c.max_sandbox_runs, "wall_clock_s": c.wall_clock_s, "max_usd": c.max_usd,
                "model_calls": dict(c.model_calls), "model_retries": c.model_retries, "max_ab_pairs": c.max_ab_pairs,
                "subscription_models": list(c.subscription_models), "max_subscription_tokens": c.max_subscription_tokens}
