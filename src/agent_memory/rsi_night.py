"""The nightly RSI investigator: `amem rsi night` (Mission 005, docs/rsi/m005-plan.md 2.4 and 2.5).

A deterministic Python orchestrator. Models are called only at fixed stages, through adapters:

    S0 preflight    lock (one night at a time), config and budget, database, sandbox
    S1 delta        new or changed evidence since the last successful checkpoint (rsi_evidence_pack.delta_since)
    S2 candidates   deviation groups, low skill scores, eval failures, routine failures, prior experiments and
                    provider findings -> durable rsi_candidates rows (upsert by dedupe_key)
    S3 triage       Jev (fixture by default) answers typed questions; deterministic code ranks and selects
    S4 evidence     a bounded evidence pack for each selected candidate (untrusted-data envelope)
    S5 hypotheses   at most two typed hypotheses (what / why / target from the allowlist); anything else rejected
    S6 implement    the candidate file content (stub by default; Claude / Codex inside the sandbox)
                    staged with agent-config `improve stage` into $RSI_SANDBOX_DIR
    S7 replay       baseline vs candidate in the sandbox (rsi_replay.run_replay); the verdict is deterministic code
    S8 staging      a proposal row, only for IMPROVES with the exact artifact hash and a bound eval record
    S9 checkpoint   advanced only when S1-S8 finished for the night

The investigator never approves, promotes or decides anything, never writes its own config, criteria or
questions, and never runs a command or writes a file because retrieved text or model output says so: model
output is parsed into strict typed structures and everything else is rejected. Files it writes: the night
report and temporary stage inputs, both under $RSI_SANDBOX_DIR.

Candidates that are not selected stay `deferred` for later nights. Experiments, verdicts and rejected
hypotheses stay in rsi_experiments / rsi_candidates as learning history.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import re
import signal
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import config, edits_targets as ET, rsi_approval as AP, rsi_artifact as RA, rsi_targets
from agent_memory.rsi_budget import Budget, BudgetConfig, BudgetConfigError, BudgetExhausted, Usage

FORMAT = "rsi-night/1"
CONFIG_FORMAT = "rsi-night-config/1"
JOB = "rsi-night"
LOCK_NAME = "rsi-night"
CONFIG_PATH = config.REPO_ROOT / "config" / "rsi-night.json"
TRIAGE_FILE = "rsi-triage"
HYPOTHESES_FIXTURES = config.REPO_ROOT / "tests" / "fixtures" / "rsi-night" / "hypotheses"

STATUSES = ("ok", "partial", "aborted", "budget_exhausted", "preflight_failed")
SKIP_REASONS = ("prerequisites", "credentials")     # report["skipped"]["reason"]: the night job did not start it
MAX_PREREQ_WARNINGS = 10
EXP_STATUSES = ("staged", "not_promotable", "failed", "interrupted", "skipped_budget")
VERDICTS = ("IMPROVES", "NO_EFFECT", "WORSE", "INCONCLUSIVE")
RISKS = ("low", "medium", "high")
PROVIDERS = ("supabase", "clickhouse", "langfuse", "pastdev")
HEALTH_KEYS = (*PROVIDERS, "jev", "sandbox")
HEALTH_VALUES = ("ok", "unavailable", "error", "fixture", "live", "fixture-missing", "skipped", "stale",
                 "truncated", "unlinked")
_SEVERITY = {"ok": 0, "fixture": 0, "live": 0, "skipped": 0, "stale": 1, "truncated": 1, "unlinked": 1,
             "fixture-missing": 1, "error": 2, "unavailable": 2}

# The investigator's own rules: never a hypothesis target, whatever the allowlist says.
SELF_FILES = ("rsi-night.json", "rsi-night.first.json", "rsi-triage.toml", "rsi-verdict-policy.json", "rsi-prices.json",
              "rsi-evidence-providers.json", "criteria.json", "fence.txt", "pins.json")
TARGET_KINDS = ("instruction", "lessons", "skill")
NEW_TEXT_MAX = 200_000

# Names this module must never call (tests grep for them): the investigator cannot approve its own work.
FORBIDDEN_CALLS = ("record_cli_decision", "record_phone_decision", "decide_poll", "issue_tokens",
                   "push_proposals", "amem_rsi_record", "rsi_insert_decision")


class NightAborted(BaseException):
    """SIGINT / SIGTERM. A BaseException, so an adapter's `except Exception` cannot swallow it."""


class PreflightFailed(Exception):
    pass


class ConfigError(ValueError):
    pass


class ExperimentFailed(RuntimeError):
    pass


class HypothesisParseError(ValueError):
    """The whole model reply is unusable (not JSON, wrong shape)."""


# =========================================================================== config


@dataclass(frozen=True)
class NightConfig:
    budget: BudgetConfig
    stages: dict[str, str]
    candidates: dict[str, Any]
    weights: dict[str, float]
    min_rank: float
    hypothesizer: dict[str, Any]
    implementer: dict[str, Any]
    replay: dict[str, Any]
    staging: dict[str, Any]
    path: str
    sha256: str


_STAGE_MODES = {"triage": ("fixture", "live"), "hypothesizer": ("fixture", "claude-cli"),
                "implementer": ("stub", "claude", "codex"), "replay_agent": ("stub", "claude", "codex"),
                "judge": ("stub", "codex", "claude", "none"), "replay_jev": ("fixture", "live")}
_CAND_DEFAULTS = {"max_considered": 20, "max_attempts": 2, "window_days_max": 14, "skill_score_below": 0.5,
                  "skill_min_scored": 3, "finding_min_n": 5, "max_finding_candidates": 10}
_WEIGHT_KEYS = ("impact", "recurrence", "testability", "risk", "history")


def load_config(path: Path | None = None) -> NightConfig:
    p = Path(path or CONFIG_PATH).expanduser()
    try:
        raw = p.read_bytes()
        d = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{p}: unreadable ({type(exc).__name__})") from None
    if not isinstance(d, dict) or d.get("format") != CONFIG_FORMAT:
        raise ConfigError(f"{p}: not an {CONFIG_FORMAT} file")
    try:
        budget = BudgetConfig.from_dict(d.get("budget") or {})
    except BudgetConfigError as exc:
        raise ConfigError(f"{p}: {exc}") from None
    stages = {k: v[0] for k, v in _STAGE_MODES.items()}
    for k, v in (d.get("stages") or {}).items():
        if k.startswith("_"):
            continue
        if k not in _STAGE_MODES or v not in _STAGE_MODES[k]:
            raise ConfigError(f"{p}: stages.{k} must be one of {_STAGE_MODES.get(k, ())}, got {v!r}")
        stages[k] = v
    cands = {**_CAND_DEFAULTS}
    for k, v in (d.get("candidates") or {}).items():
        if k.startswith("_"):
            continue
        if k not in _CAND_DEFAULTS or isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"{p}: candidates.{k} is invalid: {v!r}")
        cands[k] = v
    tri = d.get("triage") or {}
    weights = {k: 0.2 for k in _WEIGHT_KEYS}
    for k, v in (tri.get("weights") or {}).items():
        if k not in _WEIGHT_KEYS or isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise ConfigError(f"{p}: triage.weights.{k} is invalid: {v!r}")
        weights[k] = float(v)
    min_rank = tri.get("min_rank", 0.0)
    if isinstance(min_rank, bool) or not isinstance(min_rank, (int, float)):
        raise ConfigError(f"{p}: triage.min_rank must be a number")

    def section(name: str) -> dict[str, Any]:
        v = d.get(name) or {}
        if not isinstance(v, dict):
            raise ConfigError(f"{p}: {name} must be an object")
        return {k: x for k, x in v.items() if not k.startswith("_")}

    return NightConfig(budget=budget, stages=stages, candidates=cands, weights=weights, min_rank=float(min_rank),
                       hypothesizer=section("hypothesizer"), implementer=section("implementer"),
                       replay=section("replay"), staging=section("staging"), path=str(p),
                       sha256=hashlib.sha256(raw).hexdigest())


# =========================================================================== targets and typed hypotheses


def instruction_targets() -> list[str]:
    """The allowlist: instruction, lessons and skill markdown files the RSI loop may target (rsi_targets.discover),
    as ~-keys. Settings, hooks, schedules and the investigator's own rules are never in it."""
    out = set()
    for t in rsi_targets.describe(rsi_targets.discover(include_schedules=False), with_git=False):
        if t.kind in TARGET_KINDS and t.key.startswith("~/") and t.real.suffix == ".md" \
                and Path(t.key).name not in SELF_FILES and ".." not in t.key.split("/"):
            out.add(t.key)
    return sorted(out)


@dataclass(frozen=True)
class Hypothesis:
    what: str
    why: str
    target: str
    expected: str = ""
    risk: str = "medium"
    confidence: float = 0.5

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


HYP_FIELDS = {"what": 200, "why": 400, "target": 300, "expected": 200, "risk": 10, "confidence": 0}
HYP_REQUIRED = ("what", "why", "target")
# Text that an instruction-edit hypothesis never needs: commands, code, links, secrets, approval verbs and the
# usual prompt-injection phrasing. A false positive only drops one hypothesis; nothing runs because of it.
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\b.{0,40}\b(instruction|rule|prompt|previous|above|system)"
    r"|\b(approve|approved|promote|promoted|self-approve|decide-poll)\b"
    r"|\bsystem\s+prompt\b|\byou\s+are\s+now\b|\bdeveloper\s+mode\b|\bjailbreak"
    r"|\bamem\b|\bimprove\s+(stage|promote|apply|revert|pin)"
    r"|\b(curl|wget|sudo|chmod|chown|eval|exec|powershell|osascript)\b|\brm\s+-|\bgit\s+(push|merge|reset|commit)"
    r"|\$\(|`|<\s*/?\s*[a-z_-]+\s*>|https?://|www\.|\b(api[_-]?key|password|passwd|secret|credential)s?\b",
    re.I)
_TARGET_RE = re.compile(r"~/[A-Za-z0-9._/-]+\.md")


def _clean_text(v: Any, name: str, limit: int) -> str:
    if not isinstance(v, str):
        raise ValueError(f"{name} must be a string")
    s = v.strip()
    if not s:
        raise ValueError(f"{name} is empty")
    if len(s) > limit:
        raise ValueError(f"{name} is longer than {limit} characters")
    if any(unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs") for ch in s):
        raise ValueError(f"{name} holds control or format characters")
    if _INJECTION.search(s):
        raise ValueError(f"{name} looks like a command or an injected instruction")
    return s


def parse_hypothesis(obj: Any, allowlist: Sequence[str]) -> Hypothesis:
    """One strict hypothesis. Raises ValueError with the reason."""
    if not isinstance(obj, dict):
        raise ValueError("a hypothesis must be an object")
    extra = sorted(set(obj) - set(HYP_FIELDS))
    if extra:
        raise ValueError(f"unknown fields {extra}")
    missing = [k for k in HYP_REQUIRED if k not in obj]
    if missing:
        raise ValueError(f"missing fields {missing}")
    target = obj["target"]
    if not isinstance(target, str) or not _TARGET_RE.fullmatch(target) or ".." in target.split("/"):
        raise ValueError("target is not a ~/ markdown path")
    if target not in set(allowlist) or Path(target).name in SELF_FILES:
        raise ValueError(f"target {target} is not in the allowlist of instruction targets")
    what = _clean_text(obj["what"], "what", HYP_FIELDS["what"])
    why = _clean_text(obj["why"], "why", HYP_FIELDS["why"])
    expected = _clean_text(obj["expected"], "expected", HYP_FIELDS["expected"]) if obj.get("expected") else ""
    risk = obj.get("risk", "medium")
    if risk not in RISKS:
        raise ValueError(f"risk must be one of {RISKS}")
    conf = obj.get("confidence", 0.5)
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
        raise ValueError("confidence must be a number in 0..1")
    return Hypothesis(what=what, why=why, target=target, expected=expected, risk=risk, confidence=float(conf))


_FENCE = re.compile(r"^```(?:json)?\s*\n(.*?)\n?```$", re.S)


def parse_hypotheses(reply: str, *, allowlist: Sequence[str], cap: int) -> tuple[list[Hypothesis], list[dict[str, Any]]]:
    """The model's reply -> (accepted, rejected). The reply must be exactly {"hypotheses": [...]} (optionally in one
    ```json fence). Each item is checked on its own; valid items past `cap` are rejected too."""
    if not isinstance(reply, str) or len(reply) > 50_000:
        raise HypothesisParseError("reply is not a string or is too long")
    text = reply.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
    except ValueError:
        raise HypothesisParseError("reply is not JSON") from None
    if not isinstance(obj, dict) or set(obj) != {"hypotheses"} or not isinstance(obj["hypotheses"], list):
        raise HypothesisParseError('reply must be exactly {"hypotheses": [...]}')
    ok: list[Hypothesis] = []
    rejected: list[dict[str, Any]] = []
    for i, item in enumerate(obj["hypotheses"][:10]):
        try:
            h = parse_hypothesis(item, allowlist)
        except ValueError as exc:
            rejected.append({"index": i, "reason": str(exc)[:200]})
            continue
        if len(ok) >= cap:
            rejected.append({"index": i, "reason": f"over the cap of {cap} hypotheses"})
            continue
        if any(h.target == o.target and h.what == o.what for o in ok):
            rejected.append({"index": i, "reason": "duplicate"})
            continue
        ok.append(h)
    if len(obj["hypotheses"]) > 10:
        rejected.append({"index": 10, "reason": f"{len(obj['hypotheses']) - 10} more items ignored"})
    return ok, rejected


# =========================================================================== triage


@dataclass(frozen=True)
class TriageAnswers:
    action: str            # experiment | wait
    p_experiment: float
    impact: float          # 0..2
    risk: float            # 0..2
    testability: float     # 0..2
    mode: str              # fixture | live | heuristic

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bucket(n: Any) -> str:
    if n is None:
        return "unknown"
    n = int(n)
    return "0" if n <= 0 else "1" if n == 1 else "2-4" if n < 5 else "5-9" if n < 10 else "10+"


def _grade(x: Any) -> str:
    return "unknown" if x is None else f"{round(float(x), 1):.1f}"


_TOKEN = re.compile(r"[^A-Za-z0-9 _.:/~@#-]")


def _token(v: Any, limit: int = 120) -> str:
    return _TOKEN.sub("", str(v if v is not None else "none"))[:limit] or "none"


def triage_state(c: Mapping[str, Any], history: Sequence[str], in_scope: bool) -> str:
    """Jev's state for one candidate: structured fields only, bucketed, so fixtures stay stable across nights."""
    m = c.get("metrics") or {}
    lines = ["rsi candidate for a nightly sandbox experiment",
             f"source: {_token(c.get('source'))}", f"kind: {_token(c.get('kind'))}",
             f"target: {_token(c.get('target'))}", f"target_in_scope: {'yes' if in_scope else 'no'}",
             f"rule: {_token(c.get('rule_key'))}", f"recurrence: {_bucket(c.get('seen_count'))}",
             f"sessions: {_bucket(m.get('sessions'))}", f"followed_grade: {_grade(m.get('followed_grade_mean'))}",
             f"objective_result: {_grade(m.get('objective_result_mean'))}",
             f"prior_experiments: {', '.join(_token(v, 20) for v in history) or 'none'}"]
    return "\n".join(lines)


def heuristic_answers(c: Mapping[str, Any], history: Sequence[str], in_scope: bool) -> TriageAnswers:
    """Deterministic stand-in when Jev has no answer (missing fixture, error, or a dry run)."""
    m = c.get("metrics") or {}
    rec = max(int(c.get("seen_count") or 1), int(m.get("sessions") or 0), int(m.get("count") or 0))
    impact = 2.0 if rec >= 5 else 1.0 if rec >= 2 else 0.0
    target = str(c.get("target") or "")
    risk = 0.0 if "/skills/" not in target and target.endswith(".md") else 1.0 if target.endswith(".md") else 2.0
    go = in_scope and rec >= 2 and "WORSE" not in history
    return TriageAnswers("experiment" if go else "wait", 1.0 if go else 0.0, impact, risk, 1.0, "heuristic")


def recurrence(c: Mapping[str, Any]) -> int:
    """How often a candidate recurs: nights it was sighted or sessions it touched, whichever is larger."""
    return max(int(c.get("seen_count") or 1), int(_num((c.get("metrics") or {}).get("sessions")) or 0))


# `recurrence` in SQL, for ordering the pool in the database.
RECURRENCE_SQL = ("greatest(seen_count, case when jsonb_typeof(metrics->'sessions') = 'number' "
                  "then floor((metrics->>'sessions')::numeric) else 0 end)")


def rank(a: TriageAnswers, recurrence: int, history: Sequence[str], weights: Mapping[str, float]) -> float:
    """The deterministic rank. Jev's answers are inputs; the formula and weights are fixed data."""
    def unit(x: float) -> float:
        return max(0.0, min(1.0, x / 2.0))
    rec = min(1.0, math.log1p(max(0, recurrence)) / math.log1p(10))
    hist = -min(1.0, sum({"WORSE": 1.0, "NO_EFFECT": 0.5, "INCONCLUSIVE": 0.25}.get(v, 0.0) for v in history))
    r = (weights["impact"] * unit(a.impact) + weights["recurrence"] * rec + weights["testability"] * unit(a.testability)
         - weights["risk"] * unit(a.risk) + weights["history"] * hist)
    return round(r, 4)


class JevTriage:
    """S3 through Jev with questions/rsi-triage.toml. Fixture mode never calls the API (jev._ask_fixture)."""

    def __init__(self, mode: str = "fixture") -> None:
        self.mode = mode
        self.health = "fixture" if mode == "fixture" else "live"
        self._qset: Any = None

    def _questions(self) -> Any:
        if self._qset is None:
            from agent_memory import questions
            self._qset = questions.load_file(questions.questions_dir() / f"{TRIAGE_FILE}.toml", TRIAGE_FILE)
        return self._qset

    def assess(self, c: Mapping[str, Any], *, state: str, budget: Budget) -> TriageAnswers | None:
        from agent_memory import jev
        qs = self._questions().jev_questions()
        live = self.mode == "live" and jev.mode() == "live"
        model = jev.MODEL if live else "jev-fixture"

        def ask() -> tuple[dict[str, Any], Usage]:
            if live:   # the wrapper reports no usage: the price table books its flat per-call price (an estimate,
                return jev.ask(state, qs), Usage()    # never labelled provider-reported)
            jev._validate(state, qs)
            return jev._ask_fixture(state, qs), Usage()
        try:
            res = budget.call(model, "triage", ask)
        except BudgetExhausted:
            raise
        except jev.JevFixtureMissing:
            self.health = "fixture-missing"
            return None
        except jev.JevError:
            self.health = "error"
            return None
        act = res["action"]
        return TriageAnswers(action=str(act.value), p_experiment=float(act.probabilities.get("experiment", 0.0)),
                             impact=float(res["impact"].value), risk=float(res["risk"].value),
                             testability=float(res["testability"].value), mode="live" if live else "fixture")


# =========================================================================== adapters (protocols + defaults)


class Triage(Protocol):
    health: str

    def assess(self, c: Mapping[str, Any], *, state: str, budget: Budget) -> TriageAnswers | None: ...


class EvidenceSource(Protocol):
    def delta(self, since: str | None, *, conn: psycopg.Connection) -> Any: ...

    def pack(self, candidate: Mapping[str, Any]) -> Any: ...

    def render(self, pack: Any) -> str: ...


class Hypothesizer(Protocol):
    def reply(self, candidate: Mapping[str, Any], *, evidence_text: str, targets: Sequence[str], cap: int,
              target_text: str, budget: Budget) -> str: ...


class Implementer(Protocol):
    def implement(self, hyp: Hypothesis, *, old_text: str, experiment_id: str, baseline_id: str,
                  budget: Budget) -> str: ...


@dataclass
class StageResult:
    status: str
    artifact_hash: str | None = None
    artifact_dir: str | None = None
    reason: str | None = None


class Stager(Protocol):
    def stage(self, *, experiment_id: str, baseline_dir: Path, key: str, new_text: str, rule_id: str,
              timeout: float) -> StageResult: ...


@dataclass
class ReplayOutcome:
    verdict: str
    reasons: list[str] = field(default_factory=list)
    arms: dict[str, Any] = field(default_factory=dict)
    artifact_hash: str | None = None
    baseline_id: str | None = None
    eval_path: str | None = None
    budget_stop: str | None = None
    runs: int = 0
    judge: dict[str, Any] | None = None      # rsi_verdict.JudgeResult.summary(): mode, judged, wins, b_winrate
    jev: dict[str, Any] | None = None        # rsi_verdict.JevResult.summary(): mode, status, effect, confidence


JUDGE_KEYS = ("mode", "judged", "wins_a", "wins_b", "ties", "unfired_b", "errors", "b_winrate")
JEV_KEYS = ("mode", "status", "effect", "confidence", "harness_problem", "evidence_strength", "error")


def _summary(obj: Any, keys: Sequence[str]) -> dict[str, Any] | None:
    """A judge or Jev summary reduced to its scalar fields (the report carries numbers and enums only)."""
    if obj is None:
        return None
    if not isinstance(obj, Mapping):
        obj = obj.summary() if callable(getattr(obj, "summary", None)) else {}
    out = {k: obj.get(k) for k in keys if k in obj}
    return {k: (str(v)[:200] if isinstance(v, str) else v) for k, v in out.items()
            if v is None or isinstance(v, (bool, int, float, str))} or None


class Replayer(Protocol):
    def preflight(self) -> str: ...

    def replay(self, *, artifact_hash: str, experiment_id: str, baseline_id: str, budget: Budget) -> ReplayOutcome: ...


class Proposer(Protocol):
    def propose(self, conn: psycopg.Connection, *, adir: Path, artifact_hash: str, candidate: Mapping[str, Any],
                hyp: Hypothesis, replay: ReplayOutcome, refs: list[str]) -> str: ...


def _dget(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


_RETRIEVER = {"sb": "supabase", "ch": "clickhouse", "lf": "langfuse", "pd": "pastdev"}   # evidence step prefix


def _one_line(x: Any, n: int) -> str:
    s = " ".join(str(x).split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _refused_by_improve(target: str) -> bool:
    """True when improve would refuse an edit to `target`: a pinned file (improve/pins.json) or a fenced path
    (improve/fence.txt), read the way the edits step reads them (edits_targets). An experiment on it always ends
    "improve stage refused" (night 2026-10-23: agents/worker.md, auto-pinned after a hand edit)."""
    try:
        path = rsi_targets.untilde(target)
        return ET.pinned_file(path) is not None or ET.fenced(path)
    except (OSError, RuntimeError, ValueError):
        return False


class PackEvidence:
    """rsi_evidence_pack (w-evidence, contract 2.3), imported lazily. The pack reads the night's own database
    (`db_url`, else the process default, as the night itself), never another one. `retrievers`: provider stand-ins
    (tests and the synthetic demo); None = the providers the environment configures."""

    db_url: str | None = None
    retrievers: Mapping[str, Any] | None = None

    def __init__(self, db_url: str | None = None, retrievers: Mapping[str, Any] | None = None) -> None:
        self.db_url, self.retrievers = db_url, retrievers

    def _connect(self) -> psycopg.Connection:
        from agent_memory import store
        return store.connect(self.db_url)

    def _mod(self) -> Any:
        try:
            from agent_memory import rsi_evidence_pack
        except ImportError as exc:
            raise PreflightFailed(f"evidence module unavailable: {exc}") from None
        return rsi_evidence_pack

    def delta(self, since: str | None, *, conn: psycopg.Connection) -> Any:
        mod = self._mod()
        if self.retrievers is not None:          # stand-ins (demo/tests): S1 stays Supabase-only, as before
            return mod.delta_since(since, conn=conn)
        return mod.delta_since(since, conn=conn,
                               retrievers=mod.default_retrievers(connect=self._connect if self.db_url else None))

    def pack(self, candidate: Mapping[str, Any]) -> Any:
        mod = self._mod()
        kw = {"connect": self._connect} if self.db_url else {}     # no db_url: the store default, as the night
        if self.retrievers is None:
            return mod.build_pack(candidate, **kw)
        rs = dict(self.retrievers)
        if "sb" not in rs:
            from agent_memory.integrations.evidence import SupabaseRetriever
            rs["sb"] = SupabaseRetriever(self._connect)
        return mod.build_pack(candidate, retrievers=rs, **kw)

    def render(self, pack: Any) -> str:
        return self._mod().render_for_model(pack)


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


class FixtureHypothesizer:
    """S5 without a model: <fixtures>/<sha16(dedupe_key)>.json (the raw reply) when present, else two template
    hypotheses built from the candidate's structured fields. Either way the reply goes through the strict parser."""

    def __init__(self, fixtures: Path | None = None) -> None:
        env = os.environ.get("AMEM_RSI_HYPOTHESES_DIR", "").strip()
        self.fixtures = Path(env) if env else (fixtures or HYPOTHESES_FIXTURES)

    def reply(self, candidate: Mapping[str, Any], *, evidence_text: str, targets: Sequence[str], cap: int,
              target_text: str, budget: Budget) -> str:
        key = str(candidate.get("dedupe_key") or "")
        p = self.fixtures / f"{_sha16(key)}.json"

        def run() -> tuple[str, Usage]:
            if p.is_file():
                return p.read_text(encoding="utf-8"), Usage()
            return json.dumps(template_hypotheses(candidate)), Usage()
        return budget.call("hypothesizer-fixture", "hypothesize", run)


def template_hypotheses(c: Mapping[str, Any]) -> dict[str, Any]:
    target = c.get("target")
    if not target:
        return {"hypotheses": []}
    rule = re.sub(r"[^a-z0-9:._/-]+", "-", str(c.get("rule_key") or c.get("dedupe_key") or "this rule").lower())[:80]
    m = c.get("metrics") or {}
    n, sessions = int(c.get("seen_count") or 1), int(m.get("sessions") or m.get("count") or 0)
    why = f"Seen {n} time(s) across {sessions} session(s): agents keep missing the rule {rule}."
    return {"hypotheses": [
        {"what": f"Rewrite the rule {rule} as one short explicit checklist item", "why": why, "target": target,
         "expected": "fewer deviations from this rule", "risk": "low", "confidence": 0.5},
        {"what": f"Move the rule {rule} to the top of its section with a one-line reason", "why": why,
         "target": target, "expected": "fewer deviations from this rule", "risk": "low", "confidence": 0.4}]}


HYPOTHESIS_PROMPT = """You improve instruction files for AI coding agents. A nightly investigator found the problem below.
Propose at most {cap} testable changes to ONE instruction file each. A sandbox replay will compare the changed file
with the unchanged one, so each change must be small, concrete and testable.

Reply with JSON only, exactly this shape and nothing else:
{{"hypotheses": [{{"what": "<the concrete change to the instruction text, one sentence, <= 200 chars>",
  "why": "<the evidence-based reason, <= 400 chars>", "target": "<one path from the list below>",
  "expected": "<the measurable effect, <= 200 chars>", "risk": "low|medium|high", "confidence": <0..1>}}]}}

Rules: "target" must be exactly one of: {targets}
Plain prose only: no commands, code, links, file paths other than the target, or secrets.

Problem (structured, from the investigator's own database):
{candidate}

Current text of {target} (the production file, trusted):
<<<FILE
{target_text}
FILE>>>

{evidence}
"""


def _envelope_usage(stdout: str) -> Usage:
    try:
        env = json.loads(stdout)
        u = env.get("usage") or {}
        return Usage(tokens_in=int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                     + int(u.get("cache_creation_input_tokens") or 0),
                     tokens_out=int(u.get("output_tokens") or 0), usd=float(env.get("total_cost_usd") or 0.0))
    except (ValueError, TypeError, AttributeError):
        return Usage()


# Env names the host-side model CLI never needs: stores, provider and cloud keys, anything secret-shaped. The night
# process may hold them (an amem routine loads .env unless AMEM_NO_DOTENV=1); the `claude -p` child does not get
# them. Its own login stays (CLAUDE_*, ANTHROPIC_*). Mission 005, K4 accounting (docs/rsi/m005-report.md).
_MODEL_ENV_DROP_PREFIXES = ("AMEM_", "PG", "SUPABASE", "TYPESAFE", "CLICKHOUSE", "LANGFUSE", "PASTDEV", "NTFY",
                            "AWS_", "AZURE_", "GOOGLE_", "GCP_", "OPENAI", "CODEX_", "GITHUB_", "GH_", "RSI_",
                            "DATABASE", "DOCKER_")
_MODEL_ENV_SECRET = re.compile(r"SECRET|PASSW|TOKEN|DB_URL|DATABASE_URL|PRIVATE_KEY|API_KEY|ACCESS_KEY|CREDENTIAL",
                               re.I)


def model_cli_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a host-side model CLI call (S5 `claude -p`): this process's env minus every store,
    provider or secret-shaped name; the CLI's own login (CLAUDE_*, ANTHROPIC_*) stays."""
    src = os.environ if environ is None else environ
    out = {}
    for k, v in src.items():
        own = k.startswith(("CLAUDE_", "ANTHROPIC_"))
        if not own and (k.upper().startswith(_MODEL_ENV_DROP_PREFIXES) or _MODEL_ENV_SECRET.search(k)):
            continue
        out[k] = v
    # The paid rehearsal runs the night under a temp HOME; the host CLI's own login lives in the real one
    # (Keychain + ~/.claude), so the rehearsal names it here. Unset in production (launchd has the real HOME).
    home = (src.get("AMEM_RSI_MODEL_HOME") or "").strip()
    if home:
        out["HOME"] = home
    return out


# Unattended `claude -p` (Mission 005.5): no host hook may run or stall it, and nothing may wait for a permission
# answer. The no-tools flags of distill.writer_argv stay; these are added on top.
UNATTENDED_CLAUDE_FLAGS = ("--settings", '{"disableAllHooks": true}', "--permission-prompts", "none")


class ClaudeCliHypothesizer:
    """S5 live (unverified in Mission 005): headless `claude -p` with the edit writer's configured model
    (edits_writer.writer_model, AMEM_EDITS_MODEL, default opus) and distill's no-tools flags (`--tools ""`: it
    cannot read or run anything), plus UNATTENDED_CLAUDE_FLAGS (host hooks off, permission prompts denied). Its
    environment is `model_cli_env()` (no store or provider credential) with AMEM_HOOK_OFF=1."""

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.cfg = dict(cfg)

    def reply(self, candidate: Mapping[str, Any], *, evidence_text: str, targets: Sequence[str], cap: int,
              target_text: str, budget: Budget) -> str:
        from agent_memory import distill, edits_writer
        model = str(self.cfg.get("model") or edits_writer.writer_model())
        argv = [*distill.writer_argv(edits_writer.CLAUDE_ENV, model=model), *UNATTENDED_CLAUDE_FLAGS]
        cand = {k: candidate.get(k) for k in ("dedupe_key", "source", "kind", "target", "rule_key", "seen_count")}
        cand["metrics"] = {k: v for k, v in (candidate.get("metrics") or {}).items() if isinstance(v, (int, float))}
        limit = int(self.cfg.get("max_prompt_chars") or 24_000)
        fields = dict(cap=cap, targets=", ".join(targets), candidate=json.dumps(cand, sort_keys=True, default=str),
                      target=candidate.get("target") or "(choose from the list)", evidence=evidence_text)
        # The target text gives way first, so a long prompt never cuts the nonce-delimited evidence block (its END
        # line) or the reply-shape instructions; the final cut stays as a backstop only.
        room = limit - len(HYPOTHESIS_PROMPT.format(target_text="", **fields))
        prompt = HYPOTHESIS_PROMPT.format(target_text=target_text[: max(0, min(12_000, room))], **fields)[:limit]
        timeout = float(self.cfg.get("timeout_s") or 600)

        def run() -> tuple[str, Usage]:
            out = distill.run_cli(argv, prompt, timeout=max(1.0, min(timeout, budget.remaining_s())),
                                  env={**model_cli_env(), "AMEM_HOOK_OFF": "1"})
            return distill.parse_envelope(out), _envelope_usage(out)
        return budget.call(model, "hypothesize", run, retry_on=(distill.WriterError,))


def stub_change(old_text: str, hyp: Hypothesis) -> str:
    """The stub implementer's deterministic new file content: the hypothesis as one appended rule line."""
    line = " ".join(hyp.what.split()).rstrip(".")
    return old_text.rstrip("\n") + "\n\n- " + line + ".\n"


class StubImplementer:
    def implement(self, hyp: Hypothesis, *, old_text: str, experiment_id: str, baseline_id: str,
                  budget: Budget) -> str:
        return budget.call("implementer-stub", "implement", lambda: (stub_change(old_text, hyp), Usage()))


def sandbox_module() -> Any:
    """sandbox/rsi_sandbox.py (stdlib only, not a package module), loaded once."""
    mod = sys.modules.get("rsi_sandbox")
    if mod is not None:
        return mod
    path = config.REPO_ROOT / "sandbox" / "rsi_sandbox.py"
    spec = importlib.util.spec_from_file_location("rsi_sandbox", path)
    if spec is None or spec.loader is None:
        raise PreflightFailed(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rsi_sandbox"] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop("rsi_sandbox", None)
        raise
    return mod


IMPLEMENT_PROMPT = """Edit exactly one file: /home/rsi/{rel}. Make this change and nothing else:
{what}
Reason: {why}
Keep every other rule. Do not create other files. Do not run commands other than reading and editing that file."""


class SandboxImplementer:
    """S6 live (unverified in Mission 005): Claude or Codex edits the target inside a fresh sandbox arm `I`
    (sanitized baseline home, internal network, dedicated sandbox key). The result is the edited file's bytes."""

    def __init__(self, agent: str, cfg: Mapping[str, Any]) -> None:
        self.agent, self.cfg = agent, dict(cfg)

    def implement(self, hyp: Hypothesis, *, old_text: str, experiment_id: str, baseline_id: str,
                  budget: Budget) -> str:
        mod = sandbox_module()
        box = mod.Sandbox(RA.sandbox_dir())
        rel = mod._home_rel(hyp.target)
        box.prepare_arm(experiment_id, baseline_id, "I", force=True)
        prompt = IMPLEMENT_PROMPT.format(rel=rel, what=hyp.what, why=hyp.why)
        if self.agent == "codex":
            model = str(self.cfg.get("model") or "codex")
            cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", prompt]
            creds, name = str(self.cfg.get("credentials") or "keychain:rsi-sandbox/openai"), "OPENAI_API_KEY"
        else:
            from agent_memory import edits_writer
            model = str(self.cfg.get("model") or edits_writer.writer_model())
            cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json", "--allowedTools", "Read,Edit",
                   "--permission-mode", "acceptEdits", "--no-session-persistence"]
            creds, name = str(self.cfg.get("credentials") or "keychain:rsi-sandbox/anthropic"), "ANTHROPIC_API_KEY"
        timeout = int(max(1, min(float(self.cfg.get("timeout_s") or 900), budget.remaining_s())))

        def run() -> tuple[str, Usage]:
            res = box.run(experiment_id, "I", cmd, timeout_s=timeout, credentials=creds, credential_name=name,
                          run_id=f"{experiment_id}-implement")
            if res.exit_code != 0 or res.timed_out:
                raise ExperimentFailed(f"implementer exit {res.exit_code} timed_out={res.timed_out}")
            new = (box.arm_home(experiment_id, "I") / rel).read_text(encoding="utf-8")
            # Claude reports its cost in the JSON envelope; Codex reports none here, so the price table books it
            # (an estimate, never labelled provider-reported).
            return new, _envelope_usage(res.stdout) if self.agent == "claude" else Usage()
        return budget.call(model, "implement", run)


class ImproveStager:
    """agent-config `improve stage` as a subprocess, the same call as edits._stage_edit: an immutable artifact +
    a bound `static` evaluation under $RSI_SANDBOX_DIR/artifacts. Never `apply`, never a promotion."""

    def stage(self, *, experiment_id: str, baseline_dir: Path, key: str, new_text: str, rule_id: str,
              timeout: float) -> StageResult:
        import subprocess

        from agent_memory import edits
        run = RA.sandbox_dir() / "run"
        run.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(prefix="rsi-night-", suffix=Path(key).suffix or ".md", dir=run)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            argv = [edits.improve_bin(), "stage", "--json", "--experiment", experiment_id, "--baseline",
                    str(baseline_dir), "--change", f"{key}={tmp}", "--rule-id", f"{key}={rule_id}",
                    "--kind", "edit_instruction"]
            try:
                proc = edits.run_command(argv, timeout=timeout)
            except subprocess.TimeoutExpired:
                return StageResult("refused", reason=f"improve stage timed out after {timeout:g}s")
            except OSError as exc:
                return StageResult("refused", reason=f"cannot run improve: {type(exc).__name__}")
        finally:
            Path(tmp).unlink(missing_ok=True)
        res: dict[str, Any] = {}
        for ln in reversed((proc.stdout or "").strip().splitlines()):
            if ln.startswith("{"):
                try:
                    res = json.loads(ln)
                    break
                except ValueError:
                    continue
        if res.get("status") != "staged" or not RA.HEX64.fullmatch(str(res.get("artifact_hash") or "")):
            return StageResult("refused", reason=str(res.get("reason") or f"improve exit {proc.returncode}")[:300])
        return StageResult("staged", artifact_hash=res["artifact_hash"], artifact_dir=res.get("artifact_dir"))


class SandboxReplayer:
    """rsi_replay.run_replay (w-replay, contract 2.2), imported lazily. Stub agent in the real Docker sandbox by
    default (no network, no credentials)."""

    def __init__(self, stages: Mapping[str, str], cfg: Mapping[str, Any]) -> None:
        self.stages, self.cfg = dict(stages), dict(cfg)

    def preflight(self) -> str:
        try:
            mod = sandbox_module()
            ok = mod.docker_available() and bool((mod.load_lock() or {}).get("worker"))
        except Exception:  # noqa: BLE001 - any failure means: no sandbox tonight
            return "unavailable"
        return "ok" if ok else "unavailable"

    def replay_config(self) -> Any:
        """The rsi_replay.ReplayConfig this night's config asks for (stages + the `replay` section). Live judge and
        agent credentials are references (keychain:<service>/<account>), never values."""
        from agent_memory import rsi_replay
        fields = {"agent": self.stages.get("replay_agent", "stub"), "judge": self.stages.get("judge", "stub"),
                  "jev": self.stages.get("replay_jev", "fixture"), "reps": int(self.cfg.get("reps") or 1),
                  "model": self.cfg.get("model"), "effort": self.cfg.get("effort"),
                  "credentials": self.cfg.get("credentials") or "none",
                  "credential_name": self.cfg.get("credential_name"), "judge_model": self.cfg.get("judge_model"),
                  "judge_credentials": self.cfg.get("judge_credentials") or "none",
                  "judge_credential_name": self.cfg.get("judge_credential_name")}
        if self.cfg.get("max_budget_usd") is not None:
            fields["max_budget_usd"] = float(self.cfg["max_budget_usd"])
        known = getattr(rsi_replay.ReplayConfig, "__dataclass_fields__", fields)
        return rsi_replay.ReplayConfig(**{k: v for k, v in fields.items() if k in known})

    def replay(self, *, artifact_hash: str, experiment_id: str, baseline_id: str, budget: Budget) -> ReplayOutcome:
        from agent_memory import rsi_replay
        cases = rsi_replay.load_cases(self.cfg.get("cases"))
        rc = self.replay_config()
        pairs = len(cases) * int(rc.reps)
        cap = int(getattr(budget.cfg, "max_ab_pairs", pairs))
        if pairs > cap:          # refused before any container: a bigger replay than the night allows
            raise ExperimentFailed(f"replay of {len(cases)} case(s) x {rc.reps} rep(s) = {pairs} A/B pairs is over "
                                   f"the cap of {cap}")
        # A live agent or judge needs rsi-internal + the egress proxy. `replay.ensure_network` (unattended nights)
        # brings them up for this replay and takes them down again if they were not up before.
        live = rc.agent != "stub" or rc.judge in ("codex", "claude")
        ensure = bool(self.cfg.get("ensure_network")) and live
        box = sandbox_module().Sandbox(RA.sandbox_dir()) if ensure else None
        was_up = bool((box.net_status() or {}).get("proxy_running")) if box is not None else True
        try:
            r = rsi_replay.run_replay(artifact_hash, cases, rc, root=RA.sandbox_dir(), experiment_id=experiment_id,
                                      budget=budget, baseline_id=baseline_id,
                                      **({"ensure_network": True} if ensure else {}))
        finally:
            if box is not None and not was_up:
                with contextlib.suppress(Exception):
                    box.net_down()
        v = _dget(r, "verdict")
        return ReplayOutcome(verdict=str(_dget(v, "verdict", v)), reasons=[str(x) for x in (_dget(v, "reasons") or [])],
                             arms=dict(_dget(r, "arms") or {}), artifact_hash=_dget(r, "artifact_hash"),
                             baseline_id=_dget(r, "baseline_id"),
                             eval_path=str(_dget(r, "eval_path")) if _dget(r, "eval_path") else None,
                             budget_stop=_dget(r, "budget_stop"), runs=len(_dget(r, "runs") or []),
                             judge=_summary(_dget(r, "judge"), JUDGE_KEYS), jev=_summary(_dget(r, "jev"), JEV_KEYS))


class ApprovalProposer:
    """S8: rsi_approval.create_proposal for the exact staged bytes, only with a bound `improves` sandbox-replay
    record. Records a staged proposal; deciding and promoting stay with Vilmer and the promotion worker."""

    def propose(self, conn: psycopg.Connection, *, adir: Path, artifact_hash: str, candidate: Mapping[str, Any],
                hyp: Hypothesis, replay: ReplayOutcome, refs: list[str]) -> str:
        try:
            _, h, _ = RA.load_artifact(adir)
        except RA.ArtifactError as exc:
            raise ExperimentFailed(f"artifact does not verify: {exc}") from None
        if h != artifact_hash or replay.artifact_hash != artifact_hash:
            raise ExperimentFailed("the replayed artifact hash differs from the staged one")
        recs = [r for r in RA.eval_records(adir) if r.get("suite") == "sandbox-replay" and r.get("artifact_hash") == h]
        if not any(r.get("verdict") == "improves" for r in recs):
            raise ExperimentFailed("no sandbox-replay `improves` record bound to the artifact")
        with conn.transaction():
            return AP.create_proposal(
                conn, adir, title=hyp.what[:120], summary=hyp.what,
                rationale={"why": hyp.why, "tests": f"sandbox replay {replay.verdict}: " + "; ".join(replay.reasons)[:300],
                           "kind": "edit_instruction", "hypothesis": hyp.as_dict(), "source": "rsi-night"},
                evidence_refs=refs[:20], risk={"level": hyp.risk}, confidence=hyp.confidence,
                expected={"effect": hyp.expected or "fewer deviations"},
                candidate_id=str(candidate["id"]) if candidate.get("id") else None)


class SandboxOps:
    """The night lock (rsi_sandbox.Sandbox.acquire_lock: O_EXCL file with pid, host and expiry; a stale lock is
    taken over) and the sandbox `recover` after an interruption."""

    def lock(self, ttl_s: float) -> Path:
        return sandbox_module().Sandbox(RA.sandbox_dir()).acquire_lock(LOCK_NAME, ttl_s)

    def release(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def recover(self) -> dict[str, Any]:
        try:
            return sandbox_module().Sandbox(RA.sandbox_dir()).recover()
        except Exception as exc:  # noqa: BLE001 - recovery is best effort; the report says what failed
            return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def reap(self, experiments: Sequence[str]) -> dict[str, Any]:
        """Remove every container of tonight's aborted experiments (Sandbox.reap_experiment): nothing may run them
        any more, and an interrupted `docker run` can leave one in Created state."""
        try:
            box = sandbox_module().Sandbox(RA.sandbox_dir())
            return {e: box.reap_experiment(e) for e in experiments}
        except Exception as exc:  # noqa: BLE001 - best effort; the report says what failed
            return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def create_baseline(key: str) -> tuple[str, Path]:
    return rsi_targets.create_baseline([rsi_targets.untilde(key)])


@dataclass
class Adapters:
    evidence: EvidenceSource
    triage: Triage
    hypothesizer: Hypothesizer
    implementer: Implementer
    stager: Stager
    replayer: Replayer
    proposer: Proposer
    sandbox: SandboxOps
    targets: Callable[[], list[str]] = instruction_targets
    baseline: Callable[[str], tuple[str, Path]] = create_baseline


def default_adapters(cfg: NightConfig, db_url: str | None = None) -> Adapters:
    st = cfg.stages
    hyp: Hypothesizer = ClaudeCliHypothesizer(cfg.hypothesizer) if st["hypothesizer"] == "claude-cli" \
        else FixtureHypothesizer()
    impl: Implementer = SandboxImplementer(st["implementer"], cfg.implementer) if st["implementer"] != "stub" \
        else StubImplementer()
    return Adapters(evidence=PackEvidence(db_url), triage=JevTriage(st["triage"]), hypothesizer=hyp, implementer=impl,
                    stager=ImproveStager(), replayer=SandboxReplayer(st, cfg.replay), proposer=ApprovalProposer(),
                    sandbox=SandboxOps())


# =========================================================================== candidates (S2)


@dataclass
class Sighting:
    dedupe_key: str
    source: str
    kind: str
    target: str | None = None
    rule_key: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    pattern: dict[str, Any] | None = None


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(UTC).isoformat(timespec="seconds")
    return str(v)


def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _ids(items: Any) -> list[str]:
    out = []
    for x in items or []:
        if isinstance(x, Mapping):
            x = x.get("id") or x.get("ref")
        if x is not None:
            out.append(str(x))
    return out


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# =========================================================================== the night


def report_path(night: str) -> Path:
    return RA.sandbox_dir() / "nights" / night / "report.json"


def _write_json(path: Path, obj: Any) -> None:
    """Atomic write. Only under $RSI_SANDBOX_DIR: the investigator writes nothing else."""
    root = RA.sandbox_dir().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    real = path.parent.resolve() / path.name
    if not (real == root or root in real.parents):
        raise RuntimeError(f"refusing to write outside the RSI sandbox: {path}")
    fd, tmp = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Night:
    def __init__(self, cfg: NightConfig, adapters: Adapters, *, night: str, db_url: str | None = None,
                 dry_run: bool = False, clock: Callable[[], float] = time.monotonic,
                 install_signals: bool = True, prerequisite_warnings: Sequence[str] = ()) -> None:
        self.cfg, self.ad, self.night, self.db_url, self.dry_run = cfg, adapters, night, db_url, dry_run
        self.budget = Budget(cfg.budget, clock=clock)
        self.install_signals = install_signals
        self.conn: psycopg.Connection | None = None
        self.lock: Path | None = None
        self.interrupted: str | None = None
        self.interruptible = False
        self.owns_night = False        # the night's checkpoint row and report belong to this run
        self.marked_running = False
        self.failed = False            # an experiment or stage failed: the checkpoint is not advanced
        self.degraded = False          # a provider was down or an experiment failed: partial, but S1-S8 finished
        self.health_reasons: dict[str, list[str]] = {}   # provider -> why it was not ok (redacted, one line each)
        self.pinned: list[str] = []    # allowlisted targets improve refuses (pins.json / fence.txt): never selected
        self.notes: list[str] = []
        self.health: dict[str, str] = {k: "skipped" for k in PROVIDERS}
        self.health["jev"] = "fixture" if cfg.stages["triage"] == "fixture" else "live"
        self.health["sandbox"] = "skipped"
        self.report: dict[str, Any] = {
            "format": FORMAT, "night": night, "started_at": _now_iso(), "finished_at": None, "status": "ok",
            "checkpoint": {"before": None, "after": None, "advanced": False},
            "delta": {"since": None, "sessions": 0, "deviations": 0, "eval_failures": 0, "routine_failures": 0},
            "patterns": [], "candidates": [], "experiments": [], "costs": {}, "health": {},
            "pending_decisions": [], "notes": []}
        if prerequisite_warnings:      # failed prerequisite steps the night does not read (rsi_schedule's gate)
            self.report["prerequisite_warnings"] = _prereq_warnings(prerequisite_warnings)
        self.allowlist: list[str] = []
        self.window_key = "first"
        self.since: str | None = None
        self.next_checkpoint: str | None = None
        self.selected_ids: list[str] = []
        self._pool_rows: dict[str, dict[str, Any]] = {}
        self._advance = False

    # -- signals, interruption ----------------------------------------------------------------------
    @contextlib.contextmanager
    def _signals(self) -> Iterator[None]:
        if not self.install_signals or threading.current_thread() is not threading.main_thread():
            yield
            return
        sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
        old = {s: signal.getsignal(s) for s in sigs}

        def on_stop(signum: int, _frame: Any) -> None:
            self.interrupted = self.interrupted or signal.Signals(signum).name
            if self.interruptible:
                self.interruptible = False
                raise NightAborted(self.interrupted)

        def on_alarm(_signum: int, _frame: Any) -> None:
            if self.budget.exhausted is None:
                self.budget.exhausted = "wall_clock"
            if self.interruptible:
                self.interruptible = False
                raise BudgetExhausted("wall_clock", "hard timeout")

        signal.signal(signal.SIGINT, on_stop)
        signal.signal(signal.SIGTERM, on_stop)
        signal.signal(signal.SIGALRM, on_alarm)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, self.budget.remaining_s()))
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for s, h in old.items():
                signal.signal(s, h)

    def _poll(self) -> None:
        if self.interrupted:
            raise NightAborted(self.interrupted)
        if self.budget.exhausted == "wall_clock":
            raise BudgetExhausted("wall_clock", "hard timeout")
        self.budget.check()

    @contextlib.contextmanager
    def _interruptible(self) -> Iterator[None]:
        """Long adapter calls (models, sandbox) may be cut by a signal; database writes never are."""
        self._poll()
        self.interruptible = True
        try:
            yield
        finally:
            self.interruptible = False

    # -- health --------------------------------------------------------------------------------------
    def _merge_health(self, h: Mapping[str, Any] | None, steps: Any = None) -> None:
        for k, v in (h or {}).items():
            if k not in PROVIDERS or not isinstance(v, str):
                continue
            if self.health.get(k) == "skipped" or _SEVERITY.get(v, 2) > _SEVERITY.get(self.health[k], 0):
                self.health[k] = v if v in HEALTH_VALUES else "error"
        # Why a provider was not ok: the failing steps' own detail (the 2026-10-23 night logged only "error").
        for st in steps or []:
            if not isinstance(st, Mapping) or st.get("status") in (None, "ok"):
                continue
            prov = _RETRIEVER.get(str(st.get("step") or "").split(".", 1)[0])
            if prov is None:
                continue
            why = _one_line(f"{st.get('step')}: {st.get('status')}" + (f" ({st['detail']})" if st.get("detail") else ""), 160)
            seen = self.health_reasons.setdefault(prov, [])
            if why not in seen and len(seen) < 3:
                seen.append(why)

    # -- the run -------------------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        status = "ok"
        try:
            with self._signals():
                self._preflight()
                self._collect()
                if not self.dry_run:
                    self._experiments()
                status = self._settle()
        except PreflightFailed as exc:
            status = "preflight_failed"
            self.notes.append(f"preflight: {exc}"[:300])
        except BudgetExhausted as exc:
            status = "budget_exhausted"
            self.notes.append(f"budget exhausted: {exc.kind}")
            self._unwind("budget")
            if exc.kind == "wall_clock":                  # the hard timeout can cut a container run short
                self._reap_tonight()
        except NightAborted as exc:
            status = "aborted"
            self.notes.append(f"interrupted by {exc}")
            self._unwind("aborted")
            rec = self.ad.sandbox.recover()
            self.notes.append("sandbox recover: " + json.dumps(rec, sort_keys=True, default=str)[:300])
            self._reap_tonight()
        except Exception as exc:  # noqa: BLE001 - the night ends partial with the reason; nothing advances
            status = "partial"
            self.failed = True
            self.notes.append(f"error: {type(exc).__name__}: {str(exc)[:200]}")
            self._unwind("error")
        finally:
            self._finish(status)
        return self.report

    # -- S0 -------------------------------------------------------------------------------------------
    def _preflight(self) -> None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.night):
            raise PreflightFailed(f"bad night key {self.night!r} (YYYY-MM-DD)")
        if not self.dry_run:
            try:
                self.lock = self.ad.sandbox.lock(self.cfg.budget.wall_clock_s + 900)
            except Exception as exc:  # noqa: BLE001 - SandboxError: another night holds the lock
                raise PreflightFailed(f"lock: {exc}") from None
        from agent_memory import store
        try:
            self.conn = store.connect(self.db_url, autocommit=True)
            self.conn.execute("select 1 from rsi_candidates limit 1")
            self.health["supabase"] = "ok"
        except psycopg.Error as exc:
            self.health["supabase"] = "unavailable"
            raise PreflightFailed(f"database unavailable ({type(exc).__name__})") from None
        row = self.conn.execute("select status from rsi_checkpoints where job = %s and run_key = %s",
                                (JOB, self.night)).fetchone()
        if row and row[0] == "done":
            raise PreflightFailed(f"night {self.night} is already done")
        self.owns_night = not self.dry_run
        self.health["sandbox"] = self.ad.replayer.preflight()
        if self.health["sandbox"] != "ok" and not self.dry_run:      # a dry run never reaches the sandbox
            raise PreflightFailed("sandbox unavailable (docker or the worker image)")
        self.allowlist = list(self.ad.targets())
        self.pinned = [t for t in self.allowlist if _refused_by_improve(t)]
        if self.pinned:                # improve refuses these: a candidate on one only burns triage and model money
            self.allowlist = [t for t in self.allowlist if t not in self.pinned]
        self.budget.check()
        if not self.dry_run:
            self._reconcile_orphans()
            AP.checkpoint(self.conn, JOB, self.night, "running",
                          meta={"started_at": self.report["started_at"], "config_sha256": self.cfg.sha256})
            self.marked_running = True

    # -- S1-S3 ----------------------------------------------------------------------------------------
    def _previous_checkpoint(self) -> str | None:
        assert self.conn is not None
        row = self.conn.execute(
            "select meta->>'next_checkpoint' from rsi_checkpoints where job = %s and status = 'done' and run_key <> %s "
            "and meta ? 'next_checkpoint' order by finished_at desc nulls last, run_key desc limit 1",
            (JOB, self.night)).fetchone()
        return row[0] if row else None

    def _collect(self) -> None:
        assert self.conn is not None
        self._poll()
        before = self._previous_checkpoint()
        self.report["checkpoint"]["before"] = before
        self.report["checkpoint"]["after"] = before
        try:
            delta = self.ad.evidence.delta(before, conn=self.conn)
        except psycopg.Error as exc:
            self.health["supabase"] = "unavailable"
            raise PreflightFailed(f"delta: database unavailable ({type(exc).__name__})") from None
        self._merge_health(_dget(delta, "health"), _dget(delta, "steps"))
        for src in PROVIDERS[1:]:
            if (_dget(delta, "health") or {}).get(src) in ("ok", "error", "stale", "truncated"):
                self.budget.record_provider(src)
        since = _iso(_dget(delta, "since", before))
        self.since = since
        self.window_key = before or "first"     # the same checkpoint means the same window (a re-read)
        self.next_checkpoint = _iso(_dget(delta, "next_checkpoint") or _dget(delta, "until"))
        counts = _dget(delta, "counts")
        counts = counts() if callable(counts) else {}
        self.report["delta"] = {"since": since, **{k: int(counts.get(k, len(_dget(delta, k) or [])))
                                                   for k in ("sessions", "deviations", "eval_failures", "routine_failures")}}
        self._poll()
        sightings = self._sightings(delta)
        self.report["patterns"] = [{"key": s.dedupe_key, **s.pattern} for s in sightings if s.pattern][:20]
        self._attach_history(delta)
        pool = self._pool(sightings)
        self._poll()
        self._triage(pool)

    def _sightings(self, delta: Any) -> list[Sighting]:
        out: list[Sighting] = []
        for fn in (self._deviation_sightings, self._skill_sightings, self._routine_sightings, self._finding_sightings):
            try:
                out += fn(delta)
            except psycopg.Error:
                raise
            except Exception as exc:  # noqa: BLE001 - one source failing must not hide the others
                self.degraded = True
                self.notes.append(f"candidates: {fn.__name__.strip('_')} failed: {type(exc).__name__}")
        return out

    def _window_days(self) -> int:
        since = _parse_ts(self.since)
        cap = int(self.cfg.candidates["window_days_max"])
        if since is None:
            return min(cap, 8)
        return max(1, min(cap, math.ceil((datetime.now(UTC) - since).total_seconds() / 86400) + 1))

    def _in_scope(self, target: str | None) -> bool:
        return self._scoped(target) is not None

    def _scoped(self, target: str | None) -> str | None:
        """The allowlist's spelling of `target`, or None when it is not in scope. The allowlist lists real paths;
        a candidate may name a symlink to one (~/.claude/agents/worker.md -> ~/agent-config/agents/worker.md)."""
        if not target:
            return None
        if target in self.allowlist:
            return target
        try:
            real = rsi_targets.tilde(rsi_targets.untilde(target).resolve())
        except (OSError, RuntimeError):       # unreadable path or a symlink loop
            return None
        return real if real in self.allowlist else None

    def _deviation_sightings(self, delta: Any) -> list[Sighting]:
        new = set(_ids(_dget(delta, "deviations")))
        if not new:
            return []
        from agent_memory import deviations
        out = []
        for g in deviations.candidates(days=self._window_days(), db_url=self.db_url):
            ids = [str(i) for i in g.get("deviation_ids") or []]
            fresh = [i for i in ids if i in new]
            if not fresh or g.get("team"):
                continue
            rule = g.get("rule_id") or g.get("instruction_ref")
            key = f"dev:{g['instruction_source']}:{rule}"
            target = next((s for s in map(self._scoped, g.get("target_paths") or []) if s),
                          (g.get("target_paths") or [None])[0])
            jv = g.get("jev") or {}
            metrics = {"count": g.get("count"), "sessions": g.get("sessions"), "new": len(fresh),
                       "followed_grade_mean": g.get("followed_grade_mean"), "result_grade_mean": g.get("result_grade_mean"),
                       "objective_result_mean": g.get("objective_result_mean"), "verdict": g.get("verdict"),
                       "jev_edit_instruction": jv.get("edit_instruction"), "jev_enforce": jv.get("enforce")}
            title = _token(g.get("rule_title") or rule, 80)
            out.append(Sighting(
                key, "deviations", "edit_instruction", target, f"{g['instruction_source']}:{rule}",
                [f"amem:deviation/{i}" for i in fresh][:20], metrics,
                {"summary": f"{_token(g['instruction_source'], 60)} / {title}: {g.get('count')} deviations in "
                            f"{g.get('sessions')} sessions ({len(fresh)} new)",
                 "n": int(g.get("count") or 0), "sessions": int(g.get("sessions") or 0),
                 "refs": [f"amem:deviation/{i}" for i in fresh][:5]}))
        return out

    def _skill_sightings(self, _delta: Any) -> list[Sighting]:
        assert self.conn is not None
        since = _parse_ts(self.since) or datetime.now(UTC) - timedelta(days=7)
        c = self.cfg.candidates
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("select skill, n_calls, n_scored, score from skill_scores where bucket = '*' and n_scored >= %s "
                        "and score < %s and refreshed_at > %s order by score, skill limit 20",
                        (int(c["skill_min_scored"]), float(c["skill_score_below"]), since))
            rows = cur.fetchall()
        out = []
        for r in rows:
            target = f"~/.agents/skills/{r['skill']}/SKILL.md"
            out.append(Sighting(f"skill:{r['skill']}", "skill_scores", "edit_skill",
                                self._scoped(target), f"skill:{r['skill']}",
                                [f"amem:skill/{r['skill']}"],
                                {"score": round(float(r["score"]), 3), "n_scored": r["n_scored"], "sessions": r["n_scored"]},
                                {"summary": f"skill {_token(r['skill'], 60)} scores {float(r['score']):.2f} over "
                                            f"{r['n_scored']} scored sessions", "n": int(r["n_calls"]),
                                 "sessions": int(r["n_scored"]), "refs": [f"amem:skill/{r['skill']}"]}))
        return out

    def _routine_sightings(self, delta: Any) -> list[Sighting]:
        groups: dict[str, list[Mapping[str, Any]]] = {}
        for f in _dget(delta, "routine_failures") or []:
            if isinstance(f, Mapping) and f.get("routine"):
                groups.setdefault(str(f["routine"]), []).append(f)
        out = []
        for routine, fails in sorted(groups.items()):
            r = _token(routine, 60).replace(" ", "-")
            target = f"~/.claude/scheduled-tasks/{r}/SKILL.md"
            refs = [str(f.get("ref") or f"amem:run/{f.get('run_id')}") for f in fails][:20]
            out.append(Sighting(f"routine:{r}", "routine_failures", "fix_routine",
                                self._scoped(target), f"routine:{r}", refs,
                                {"failures": len(fails), "count": len(fails), "sessions": len(fails)},
                                {"summary": f"routine {r} failed {len(fails)} time(s)", "n": len(fails),
                                 "sessions": 0, "refs": refs[:5]}))
        return out

    def _finding_sightings(self, delta: Any) -> list[Sighting]:
        c = self.cfg.candidates
        out = []
        for f in _dget(delta, "findings") or []:
            if _dget(f, "status") != "ok" or (_dget(f, "n") or 0) < int(c["finding_min_n"]):
                continue
            src, ref = _token(_dget(f, "source"), 30), _token(_dget(f, "ref"), 160)
            meas = {k: v for k, v in (_dget(f, "measurements") or {}).items() if _num(v) is not None}
            out.append(Sighting(f"finding:{src}:{ref}", f"provider:{src}", "investigate", None, None, [ref],
                                {**dict(list(meas.items())[:10]), "n": _dget(f, "n")}))
            if len(out) >= int(c["max_finding_candidates"]):
                break
        return out

    def _attach_history(self, delta: Any) -> None:
        """Eval failures and finished experiments from the delta: evidence on the candidate they tested (history,
        no new sighting, no status change)."""
        if self.dry_run:
            return
        assert self.conn is not None
        for item in list(_dget(delta, "eval_failures") or []) + list(_dget(delta, "experiments") or []):
            if not isinstance(item, Mapping):
                continue
            exp = item.get("experiment_id") or (item.get("id") if "suite" not in item else None)
            if not exp:
                continue
            ref = str(item.get("ref") or f"amem:experiment/{exp}")
            self.conn.execute(
                "update rsi_candidates set evidence_refs = (select array(select distinct unnest(evidence_refs || %s::text[]))) "
                "where dedupe_key = (select meta->>'candidate_key' from rsi_experiments where id = %s)", ([ref], str(exp)))

    def _sight(self, s: Sighting) -> None:
        """Upsert one sighting. Idempotent per delta window: re-reading the same window after a failed night adds
        evidence but does not count the sighting twice."""
        assert self.conn is not None
        row = self.conn.execute("select id, meta from rsi_candidates where dedupe_key = %s", (s.dedupe_key,)).fetchone()
        sighted = list((row[1] or {}).get("sighted") or []) if row else []
        if row and self.window_key in sighted:
            self.conn.execute(
                "update rsi_candidates set evidence_refs = (select array(select distinct unnest(evidence_refs || %s::text[]))), "
                "metrics = %s where id = %s", (s.evidence_refs, Jsonb(s.metrics), row[0]))
            return
        cid = AP.upsert_candidate(self.conn, dedupe_key=s.dedupe_key, source=s.source, kind=s.kind, target=s.target,
                                  rule_key=s.rule_key, evidence_refs=s.evidence_refs, metrics=s.metrics)
        self.conn.execute("update rsi_candidates set meta = meta || %s where id = %s",
                          (Jsonb({"sighted": (sighted + [self.window_key])[-10:]}), cid))

    def _pool(self, sightings: list[Sighting]) -> list[dict[str, Any]]:
        """Tonight's candidates for triage. Only candidates whose target is in the allowlist: one without can never
        be selected, so it must not take a slot (the 2026-10-18 night gave all 5 to routine failures and ran 0
        experiments). Unranked first, the most recurrent first (`recurrence`, the same measure `rank` uses), not
        the last sighted: sources are sighted in a fixed order, so `last_seen_at` favoured whichever came last."""
        assert self.conn is not None
        if not self.dry_run:
            for s in sightings:
                self._sight(s)
        limit = int(self.cfg.candidates["max_considered"])
        with self.conn.cursor(row_factory=dict_row) as cur:
            # open candidates stored under a symlink of an allowlisted file get the allowlist's spelling
            cur.execute("select distinct target from rsi_candidates where status in ('new', 'deferred') "
                        "and target is not null and not target = any(%s)", (list(self.allowlist),))
            aliases = {r["target"]: s for r in cur.fetchall() if (s := self._scoped(r["target"]))}
            if not self.dry_run:
                for alias, real in aliases.items():
                    cur.execute("update rsi_candidates set target = %s where target = %s and status in ('new', 'deferred')",
                                (real, alias))
            scope = list(self.allowlist) + list(aliases)
            cur.execute("select * from rsi_candidates where status in ('new', 'deferred') and target = any(%s) "
                        f"order by rank desc nulls first, {RECURRENCE_SQL} desc, last_seen_at desc, dedupe_key "
                        "limit %s", (scope, limit))
            pool = [{**r, "target": aliases.get(r["target"], r["target"])} for r in cur.fetchall()]
            cur.execute("select count(*) as n from rsi_candidates where status in ('new', 'deferred') "
                        "and not coalesce(target = any(%s), false)", (scope,))
            out_of_scope = int(cur.fetchone()["n"])
            pinned = 0
            if self.pinned:
                cur.execute("select count(*) as n from rsi_candidates where status in ('new', 'deferred') "
                            "and target = any(%s)", (list(self.pinned),))
                pinned = int(cur.fetchone()["n"])
        if out_of_scope - pinned:
            self.notes.append(f"candidates: {out_of_scope - pinned} without an in-scope target, not considered")
        if pinned:
            self.notes.append(f"candidates: {pinned} on a target improve refuses (pinned or fenced: "
                              f"{', '.join(sorted(self.pinned))[:200]}), not considered")
        if self.dry_run:                      # nothing was written: show tonight's sightings as they would be
            known = {c["dedupe_key"]: c for c in pool}
            for s in sightings:
                c = known.get(s.dedupe_key)
                if c is None and self._in_scope(s.target):
                    pool.append({"id": None, "dedupe_key": s.dedupe_key, "source": s.source, "kind": s.kind,
                                 "target": s.target, "rule_key": s.rule_key, "evidence_refs": s.evidence_refs,
                                 "metrics": s.metrics, "seen_count": 1, "status": "new", "meta": {}, "rank": None})
            pool.sort(key=lambda c: (c.get("rank") is not None, -(c.get("rank") or 0.0), -recurrence(c)))
        return pool[:limit]

    def _history(self, key: str) -> list[str]:
        assert self.conn is not None
        rows = self.conn.execute("select coalesce(verdict, status) from rsi_experiments where meta->>'candidate_key' = %s "
                                 "order by created_at, id", (key,)).fetchall()
        return [str(r[0]) for r in rows]

    def _triage(self, pool: list[dict[str, Any]]) -> None:
        assert self.conn is not None
        results = []
        for c in pool:
            self._poll()
            hist = self._history(c["dedupe_key"])
            in_scope = self._in_scope(c.get("target"))
            ans = None
            if not self.dry_run or self.cfg.stages["triage"] == "fixture":
                ans = self.ad.triage.assess(c, state=triage_state(c, hist, in_scope), budget=self.budget)
            if ans is None:
                ans = heuristic_answers(c, hist, in_scope)
            r = rank(ans, recurrence(c), hist, self.cfg.weights)
            eligible = ans.action == "experiment" and in_scope and r >= self.cfg.min_rank
            why = ("eligible" if eligible else "no in-scope target" if not in_scope
                   else "triage: wait" if ans.action != "experiment" else "rank below the minimum")
            results.append((c, ans, r, eligible, why, hist))
        self.health["jev"] = getattr(self.ad.triage, "health", self.health["jev"])
        order = sorted(results, key=lambda x: (not x[3], -x[2], x[0]["dedupe_key"]))
        slots = self.budget.experiments_left()
        for c, ans, r, eligible, why, hist in order:
            selected = eligible and slots > 0
            if selected:
                slots -= 1
                reason = f"selected: rank {r:.3f}"
            else:
                reason = f"deferred: {why}" if not eligible else f"deferred: rank {r:.3f} below tonight's experiment slots"
            c["rank"], c["jev"] = r, {**ans.as_dict(), "history": hist}
            if selected:
                self.selected_ids.append(str(c.get("id") or c["dedupe_key"]))
            if not self.dry_run:
                self.conn.execute(
                    "update rsi_candidates set status = %s, rank = %s, jev = %s, decided_reason = %s, "
                    "last_considered_at = now() where id = %s",
                    ("selected" if selected else "deferred", r, Jsonb(c["jev"]), reason[:300], c["id"]))
            self.report["candidates"].append({"id": str(c.get("id")) if c.get("id") else None, "key": c["dedupe_key"],
                                              "source": c["source"], "rank": r, "selected": selected, "reason": reason})
        self._pool_rows = {str(c.get("id") or c["dedupe_key"]): c for c, *_ in results}

    # -- S4-S8 -----------------------------------------------------------------------------------------
    def _experiments(self) -> None:
        for cid in list(self.selected_ids):
            self._poll()
            self._investigate(self._pool_rows[cid])

    def _investigate(self, c: dict[str, Any]) -> None:
        assert self.conn is not None
        cand = {k: c.get(k) for k in ("id", "dedupe_key", "source", "kind", "target", "rule_key", "evidence_refs",
                                      "metrics", "seen_count")}
        cand["id"] = str(c["id"]) if c.get("id") else None
        # S4: a bounded evidence pack (untrusted data; it only reaches the model inside its envelope)
        refs = list(c.get("evidence_refs") or [])[:20]
        evidence_text = ""
        try:
            with self._interruptible():
                pack = self.ad.evidence.pack(cand)
                evidence_text = self.ad.evidence.render(pack)
            self._merge_health(_dget(pack, "health"), _dget(pack, "steps"))
            for f in _dget(pack, "findings") or []:
                if _dget(f, "status") == "ok" and _dget(f, "ref"):
                    refs.append(str(_dget(f, "ref"))[:200])
            for src in PROVIDERS[1:]:
                if (_dget(pack, "health") or {}).get(src) in ("ok", "error", "stale", "truncated"):
                    self.budget.record_provider(src)
        except (NightAborted, BudgetExhausted):
            raise
        except Exception as exc:  # noqa: BLE001 - no evidence pack: hypotheses from the candidate's own data
            self.degraded = True
            self.notes.append(f"evidence pack for {c['dedupe_key']}: {type(exc).__name__}")
        refs = list(dict.fromkeys(refs))[:20]
        # S5: typed hypotheses (pending ones from an earlier night first: no new model call)
        meta = dict(c.get("meta") or {})
        cap = self.budget.hypothesis_cap()
        pending = [h for h in meta.get("pending_hypotheses") or [] if isinstance(h, dict)]
        if pending:
            reply = json.dumps({"hypotheses": pending[:cap]})
        else:
            target_text = ""
            if c.get("target") in self.allowlist:
                with contextlib.suppress(OSError, UnicodeDecodeError):
                    target_text = rsi_targets.untilde(str(c["target"])).read_text(encoding="utf-8")
            try:
                with self._interruptible():
                    reply = self.ad.hypothesizer.reply(cand, evidence_text=evidence_text, targets=self.allowlist,
                                                       cap=cap, target_text=target_text, budget=self.budget)
            except (NightAborted, BudgetExhausted):
                raise
            except Exception as exc:  # noqa: BLE001 - S5 failed for this candidate: it stays for a later night
                self.degraded = True       # the candidate is deferred in the database; the window need not be reread
                self.notes.append(f"hypothesizer failed for {c['dedupe_key']}: {type(exc).__name__}: {str(exc)[:150]}")
                self._update_candidate(c["id"], status="deferred", reason=f"hypothesizer failed: {type(exc).__name__}")
                return
        try:
            hyps, rejected = parse_hypotheses(reply, allowlist=self.allowlist, cap=cap)
        except HypothesisParseError as exc:
            hyps, rejected = [], [{"index": None, "reason": str(exc)}]
        if rejected:
            self.notes.append(f"{len(rejected)} hypothesis(es) rejected for {c['dedupe_key']}")
        self._update_candidate(c["id"], meta={"hypotheses_rejected": rejected[-10:]} if rejected else None)
        if not hyps:
            # Counted as an attempt: a candidate that never yields a usable hypothesis retires (it returns as
            # `new` when it is seen again) instead of costing a model call every night.
            attempts = int(meta.get("attempts") or 0) + 1
            final = attempts >= int(self.cfg.candidates["max_attempts"])
            self._update_candidate(c["id"], status="retired" if final else "deferred",
                                   reason="no valid hypothesis" + (" (retired until seen again)" if final else ""),
                                   meta={"pending_hypotheses": [], "attempts": attempts})
            return
        for i, h in enumerate(hyps):
            later = hyps[i + 1:]
            if self.budget.experiments_left() <= 0:
                self.report["experiments"].append(self._entry(None, c, h, refs, status="skipped_budget"))
                continue
            try:
                self.budget.take_experiment()
            except BudgetExhausted:
                for x in hyps[i:]:
                    self.report["experiments"].append(self._entry(None, c, x, refs, status="skipped_budget"))
                self._update_candidate(c["id"], status="deferred", reason="budget",
                                       meta={"pending_hypotheses": [x.as_dict() for x in hyps[i:]]})
                raise
            try:
                self._experiment(c, h, refs, later)
            except BudgetExhausted:
                for x in later:
                    self.report["experiments"].append(self._entry(None, c, x, refs, status="skipped_budget"))
                raise

    def _update_candidate(self, cid: Any, *, status: str | None = None, reason: str | None = None,
                          meta: dict[str, Any] | None = None) -> None:
        if cid is None or self.conn is None:
            return
        self.conn.execute(
            "update rsi_candidates set status = coalesce(%s, status), decided_reason = coalesce(%s, decided_reason), "
            "meta = meta || %s where id = %s", (status, reason[:300] if reason else None, Jsonb(meta or {}), cid))

    def _exp_id(self, key: str) -> str:
        assert self.conn is not None
        base = f"exp-{self.night.replace('-', '')}-{_sha16(key)[:8]}"
        taken = {r[0] for r in self.conn.execute("select id from rsi_experiments where id = %s or id like %s",
                                                  (base, base + "-r%")).fetchall()}
        if base not in taken:
            return base
        k = 2
        while f"{base}-r{k}" in taken:
            k += 1
        return f"{base}-r{k}"

    def _entry(self, exp_id: str | None, c: Mapping[str, Any], h: Hypothesis, refs: list[str], *,
               status: str) -> dict[str, Any]:
        return {"experiment_id": exp_id, "candidate_key": c["dedupe_key"], "hypothesis": {"what": h.what, "why": h.why},
                "target": h.target, "artifact_hash": None, "baseline_id": None, "verdict": None, "verdict_reasons": [],
                "arms": {"A": {}, "B": {}}, "risk": h.risk, "confidence": h.confidence, "cost_usd": 0.0,
                "status": status, "proposal_id": None, "evidence_refs": refs[:20], "judge": None, "jev": None}

    def _experiment(self, c: dict[str, Any], h: Hypothesis, refs: list[str], later: list[Hypothesis]) -> None:
        assert self.conn is not None
        exp_id = self._exp_id(c["dedupe_key"])
        entry = self._entry(exp_id, c, h, refs, status="failed")
        self.report["experiments"].append(entry)
        mark = self.budget.mark()
        base_meta = {"candidate_key": c["dedupe_key"], "candidate_id": str(c["id"]), "source": "rsi-night",
                     "evidence_refs": refs[:20], "config_sha256": self.cfg.sha256}
        self.conn.execute("insert into rsi_experiments (id, night, kind, hypothesis, status, meta) "
                          "values (%s, %s, 'edit_instruction', %s, 'running', %s)",
                          (exp_id, self.night, Jsonb(h.as_dict()), Jsonb(base_meta)))
        attempts = int((c.get("meta") or {}).get("attempts") or 0) + 1
        pending = [x.as_dict() for x in later]
        row: dict[str, Any] = {"status": "failed", "verdict": None, "meta": {}}
        cand_status, cand_reason = "deferred", "experiment failed"
        try:
            # S6: the candidate content, staged as an immutable artifact against a fresh baseline
            bid, bdir = self.ad.baseline(h.target)
            entry["baseline_id"] = bid
            old = (Path(bdir) / "files" / h.target[2:]).read_text(encoding="utf-8")
            with self._interruptible():
                new = self.ad.implementer.implement(h, old_text=old, experiment_id=exp_id, baseline_id=bid,
                                                    budget=self.budget)
            if not isinstance(new, str) or new == old or len(new) > NEW_TEXT_MAX:
                raise ExperimentFailed("implementer returned no usable change")
            self._poll()
            st = self.ad.stager.stage(experiment_id=exp_id, baseline_dir=Path(bdir), key=h.target, new_text=new,
                                      rule_id=f"rsi-night-{_sha16(c['dedupe_key'])[:10]}",
                                      timeout=max(1.0, min(float(self.cfg.staging.get("timeout_s") or 600),
                                                           self.budget.remaining_s())))
            if st.status != "staged" or not st.artifact_hash:
                raise ExperimentFailed(f"improve stage refused: {st.reason}")
            entry["artifact_hash"] = st.artifact_hash
            self.conn.execute("update rsi_experiments set baseline_id = %s, meta = meta || %s where id = %s",
                              (bid, Jsonb({"artifact_hash": st.artifact_hash}), exp_id))
            # S7: baseline vs candidate in the sandbox; the verdict comes from deterministic code
            with self._interruptible():
                rr = self.ad.replayer.replay(artifact_hash=st.artifact_hash, experiment_id=exp_id, baseline_id=bid,
                                             budget=self.budget)
            verdict = rr.verdict if rr.verdict in VERDICTS else "INCONCLUSIVE"
            entry.update(verdict=verdict, verdict_reasons=rr.reasons[:20],
                         arms={"A": dict(rr.arms.get("A") or {}), "B": dict(rr.arms.get("B") or {})},
                         judge=_summary(getattr(rr, "judge", None), JUDGE_KEYS),
                         jev=_summary(getattr(rr, "jev", None), JEV_KEYS))
            row = {"status": "evaluated", "verdict": verdict,
                   "meta": {"verdict_reasons": rr.reasons[:20], "arms": entry["arms"], "eval_path": rr.eval_path,
                            "replay_runs": rr.runs, "budget_stop": rr.budget_stop, "judge": entry["judge"],
                            "jev": entry["jev"]}}
            if rr.budget_stop:
                entry["status"] = "skipped_budget"
                cand_status, cand_reason = "deferred", f"replay stopped by the budget ({rr.budget_stop})"
                raise BudgetExhausted(str(rr.budget_stop), "replay")
            # S8: only IMPROVES with the exact artifact becomes a staged proposal
            if verdict == "IMPROVES":
                pid = self.ad.proposer.propose(self.conn, adir=Path(st.artifact_dir or RA.artifacts_dir() / st.artifact_hash),
                                               artifact_hash=st.artifact_hash, candidate=c, hyp=h, replay=rr, refs=refs)
                entry.update(status="staged", proposal_id=pid)
                row["meta"]["proposal_id"] = pid
                cand_status, cand_reason = "staged", f"staged as proposal {pid}"
                pending = []
            else:
                entry["status"] = "not_promotable"
                final = {"WORSE": "rejected", "NO_EFFECT": "tested", "INCONCLUSIVE": "tested"}[verdict]
                more = bool(pending) or (verdict == "INCONCLUSIVE" and attempts < int(self.cfg.candidates["max_attempts"]))
                if attempts >= int(self.cfg.candidates["max_attempts"]):
                    more = False
                cand_status = "deferred" if more else final
                cand_reason = f"{verdict}" + ("; another hypothesis pending" if pending and more else "")
        except NightAborted:
            entry["status"] = "interrupted"
            row = {**row, "status": "aborted", "meta": {**row["meta"], "reason": f"interrupted ({self.interrupted})"}}
            cand_status, cand_reason = "deferred", "interrupted"
            raise
        except BudgetExhausted as exc:
            entry["status"] = "skipped_budget"
            if row["status"] != "evaluated":
                row = {**row, "status": "aborted"}
            row["meta"] = {**row["meta"], "reason": f"budget: {exc.kind}"}
            cand_status, cand_reason = "deferred", f"budget: {exc.kind}"
            raise
        except Exception as exc:  # noqa: BLE001 - this experiment failed; the night records it and goes on
            self.degraded = True           # recorded on its row; the sightings are stored, so the checkpoint advances
            entry["status"] = "failed"
            msg = f"{type(exc).__name__}: {str(exc)[:240]}"
            row = {"status": "failed", "verdict": row.get("verdict"), "meta": {**row["meta"], "error": msg}}
            entry["verdict_reasons"] = [msg, *entry["verdict_reasons"]][:20]
            self.notes.append(f"experiment {exp_id} failed: {msg}"[:300])
            # A candidate whose experiments keep failing retires (it returns as `new` when it is seen again).
            if attempts >= int(self.cfg.candidates["max_attempts"]):
                cand_status, cand_reason = "retired", f"experiment failed {attempts} times; retired until seen again"
        finally:
            costs = self.budget.since(mark)
            entry["cost_usd"] = costs["usd"]
            self.conn.execute(
                "update rsi_experiments set status = %s, verdict = %s, finished_at = now(), meta = meta || %s where id = %s",
                (row["status"], row["verdict"], Jsonb({**row["meta"], "costs": costs, "report_status": entry["status"]}),
                 exp_id))
            hist = list((c.get("meta") or {}).get("history") or [])[-19:]
            hist.append({"night": self.night, "experiment_id": exp_id, "verdict": row["verdict"],
                         "status": entry["status"]})
            stopped = entry["status"] in ("interrupted", "skipped_budget")   # not the candidate's fault: retried as is
            counted = attempts - 1 if stopped else attempts
            if stopped:
                pending = [h.as_dict()] + pending
            self._update_candidate(c["id"], status=cand_status, reason=cand_reason,
                                   meta={"attempts": counted, "history": hist, "pending_hypotheses": pending,
                                         "last_experiment": exp_id})
            c["meta"] = {**(c.get("meta") or {}), "attempts": counted, "history": hist}

    # -- unwinding, S9, the report ----------------------------------------------------------------------
    def _reap_tonight(self) -> None:
        """After a stop that can cut a container run short: no container of tonight's experiments may survive."""
        ids = [e["experiment_id"] for e in self.report["experiments"] if e.get("experiment_id")]
        reap = getattr(self.ad.sandbox, "reap", None)
        if ids and callable(reap):
            self.notes.append("sandbox reap: " + json.dumps(reap(ids), sort_keys=True, default=str)[:300])

    def _reconcile_orphans(self) -> None:
        """Under the night lock: an earlier night that was hard-killed (watchdog exit 124, power loss) never ran
        _unwind. Its experiments still 'planned'/'running' are aborted and its 'selected' candidates go back to
        deferred, so tonight's pool can pick them again. Its pending hypotheses are not recovered."""
        assert self.conn is not None
        try:
            n_exp = self.conn.execute(
                "update rsi_experiments set status = 'aborted', finished_at = now(), meta = meta || %s "
                "where night <> %s and status in ('planned', 'running')",
                (Jsonb({"reason": "orphaned: an earlier night stopped without unwinding"}), self.night)).rowcount
            n_cand = self.conn.execute(
                "update rsi_candidates set status = 'deferred', decided_reason = %s where status = 'selected'",
                ("orphaned: an earlier night stopped without unwinding",)).rowcount
        except psycopg.Error as exc:
            raise PreflightFailed(f"orphan reconcile failed ({type(exc).__name__})") from None
        if n_exp or n_cand:
            self.notes.append(f"reconciled an earlier stopped night: experiments aborted={n_exp}, "
                              f"candidates deferred={n_cand}")

    def _unwind(self, why: str) -> None:
        """After a stop: experiments still running are aborted, candidates still selected go back to deferred."""
        if self.conn is None or self.dry_run:
            return
        try:
            self.conn.execute("update rsi_experiments set status = 'aborted', finished_at = now(), "
                              "meta = meta || %s where night = %s and status in ('planned', 'running')",
                              (Jsonb({"reason": why}), self.night))
            self.conn.execute("update rsi_candidates set status = 'deferred', decided_reason = %s "
                              "where status = 'selected'", (f"night stopped: {why}",))
        except psycopg.Error as exc:
            self.notes.append(f"unwind failed: {type(exc).__name__}")

    def _settle(self) -> str:
        if self.dry_run:
            return "partial" if self.degraded else "ok"
        if self.failed:
            return "partial"
        degraded = self.degraded or any(_SEVERITY.get(self.health.get(k, "ok"), 0) >= 2 for k in PROVIDERS)
        self._advance = True
        return "partial" if degraded else "ok"

    def _finish(self, status: str) -> None:
        advance = self._advance and status in ("ok", "partial") and not self.failed and not self.dry_run
        r = self.report
        r["status"] = status
        r["finished_at"] = _now_iso()
        r["costs"] = self.budget.costs()
        r["health"] = {k: self.health.get(k, "skipped") for k in HEALTH_KEYS}
        reasons = {k: v[:3] for k, v in self.health_reasons.items() if v and r["health"].get(k) not in ("ok", "skipped")}
        if reasons:
            r["health_reasons"] = reasons
        r["notes"] = self.notes[-30:]
        if advance:
            r["checkpoint"]["after"] = self.next_checkpoint
            r["checkpoint"]["advanced"] = True
        if self.conn is not None:
            try:
                with self.conn.cursor() as cur:
                    cur.execute("select id from rsi_proposals where status = 'staged' and expires_at > now() "
                                "order by created_at, id")
                    r["pending_decisions"] = [str(x[0]) for x in cur.fetchall()]
                if self.marked_running:
                    meta = {"status": status, "report": str(report_path(self.night)), "costs": r["costs"]["total_usd"]}
                    if advance:
                        meta.update(next_checkpoint=self.next_checkpoint, since=r["delta"]["since"])
                    AP.checkpoint(self.conn, JOB, self.night, "done" if advance else "failed", meta=meta)
            except psycopg.Error as exc:
                r["notes"].append(f"finish: database error {type(exc).__name__}")
                if advance:
                    r["checkpoint"]["advanced"] = False
                    r["checkpoint"]["after"] = r["checkpoint"]["before"]
        if self.owns_night:
            try:
                _write_json(report_path(self.night), r)
            except OSError as exc:
                r["notes"].append(f"report not written: {type(exc).__name__}")
        if self.lock is not None:
            self.ad.sandbox.release(self.lock)
            self.lock = None
        if self.conn is not None:
            with contextlib.suppress(psycopg.Error):
                self.conn.close()
            self.conn = None


def _prereq_warnings(warnings: Sequence[str]) -> list[str]:
    return [" ".join(str(w).split())[:300] for w in warnings][:MAX_PREREQ_WARNINGS]


def run_night(*, night: str | None = None, config_path: Path | None = None, dry_run: bool = False,
              db_url: str | None = None, adapters: Adapters | None = None,
              clock: Callable[[], float] = time.monotonic, install_signals: bool = True,
              prerequisite_warnings: Sequence[str] = ()) -> dict[str, Any]:
    """One night. Returns the report (rsi-night/1); writes it to $RSI_SANDBOX_DIR/nights/<night>/report.json
    unless dry_run (then nothing is written: no lock, no rows, no report file). `prerequisite_warnings` (the night
    job's gate: failed prerequisite steps the night does not read) go into the report as they are."""
    night = night or AP.night_key()
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        rep = {"format": FORMAT, "night": night, "started_at": _now_iso(), "finished_at": _now_iso(),
               "status": "preflight_failed", "checkpoint": {"before": None, "after": None, "advanced": False},
               "delta": {"since": None, "sessions": 0, "deviations": 0, "eval_failures": 0, "routine_failures": 0},
               "patterns": [], "candidates": [], "experiments": [],
               "costs": {"by_model": {}, "sandbox_runs": 0, "providers": {}, "total_usd": 0.0},
               "health": {k: "skipped" for k in HEALTH_KEYS}, "pending_decisions": [], "notes": [f"config: {exc}"[:300]]}
        if prerequisite_warnings:
            rep["prerequisite_warnings"] = _prereq_warnings(prerequisite_warnings)
        return rep
    return Night(cfg, adapters or default_adapters(cfg, db_url), night=night, db_url=db_url, dry_run=dry_run,
                 clock=clock, install_signals=install_signals, prerequisite_warnings=prerequisite_warnings).run()


# =========================================================================== the contract check

_EXP_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,60}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def validate_report(obj: Any) -> list[str]:
    """Problems with a night report against contract 2.5 (rsi-night/1). Empty list: valid."""
    errs: list[str] = []

    def need(d: Any, key: str, types: type | tuple[type, ...], where: str, nullable: bool = False) -> Any:
        if not isinstance(d, dict) or key not in d:
            errs.append(f"{where}.{key} missing")
            return None
        v = d[key]
        if v is None and nullable:
            return None
        if isinstance(v, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
            errs.append(f"{where}.{key} has type bool")
        elif not isinstance(v, types):
            errs.append(f"{where}.{key} has type {type(v).__name__}")
        return v

    if not isinstance(obj, dict):
        return ["report is not an object"]
    allowed = {"format", "night", "started_at", "finished_at", "status", "checkpoint", "delta", "patterns",
               "candidates", "experiments", "costs", "health", "pending_decisions", "notes",
               "skipped", "prerequisite_warnings",            # both optional (rsi_schedule's night job)
               "health_reasons"}                               # optional: provider -> why it was not ok
    errs += [f"unknown key {k}" for k in sorted(set(obj) - allowed)]
    if obj.get("format") != FORMAT:
        errs.append("format is not rsi-night/1")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(obj.get("night"))):
        errs.append("night is not YYYY-MM-DD")
    for k in ("started_at", "finished_at"):
        v = need(obj, k, str, "report")
        if isinstance(v, str) and _parse_ts(v) is None:
            errs.append(f"{k} is not an ISO timestamp")
    if obj.get("status") not in STATUSES:
        errs.append(f"status {obj.get('status')!r} not in {STATUSES}")
    cp = need(obj, "checkpoint", dict, "report")
    if isinstance(cp, dict):
        need(cp, "before", str, "checkpoint", True)
        need(cp, "after", str, "checkpoint", True)
        need(cp, "advanced", bool, "checkpoint")
    dl = need(obj, "delta", dict, "report")
    if isinstance(dl, dict):
        need(dl, "since", str, "delta", True)
        for k in ("sessions", "deviations", "eval_failures", "routine_failures"):
            need(dl, k, int, "delta")
    for i, p in enumerate(need(obj, "patterns", list, "report") or []):
        need(p, "key", str, f"patterns[{i}]")
        need(p, "summary", str, f"patterns[{i}]")
        need(p, "n", int, f"patterns[{i}]")
        need(p, "sessions", int, f"patterns[{i}]")
        for r in need(p, "refs", list, f"patterns[{i}]") or []:
            if not isinstance(r, str):
                errs.append(f"patterns[{i}].refs holds a non-string")
    for i, c in enumerate(need(obj, "candidates", list, "report") or []):
        w = f"candidates[{i}]"
        need(c, "id", str, w, True)
        need(c, "key", str, w)
        need(c, "source", str, w)
        need(c, "rank", (int, float), w, True)
        need(c, "selected", bool, w)
        need(c, "reason", str, w)
    for i, e in enumerate(need(obj, "experiments", list, "report") or []):
        w = f"experiments[{i}]"
        xid = need(e, "experiment_id", str, w, True)
        if isinstance(xid, str) and not _EXP_ID.fullmatch(xid):
            errs.append(f"{w}.experiment_id {xid!r} is not a valid rsi_experiments id")
        need(e, "candidate_key", str, w)
        hyp = need(e, "hypothesis", dict, w)
        if isinstance(hyp, dict):
            need(hyp, "what", str, f"{w}.hypothesis")
            need(hyp, "why", str, f"{w}.hypothesis")
        need(e, "target", str, w)
        ah = need(e, "artifact_hash", str, w, True)
        if isinstance(ah, str) and not RA.HEX64.fullmatch(ah):
            errs.append(f"{w}.artifact_hash is not 64 lowercase hex")
        need(e, "baseline_id", str, w, True)
        v = need(e, "verdict", str, w, True)
        if v is not None and v not in VERDICTS:
            errs.append(f"{w}.verdict {v!r} not in {VERDICTS}")
        need(e, "verdict_reasons", list, w)
        arms = need(e, "arms", dict, w)               # the writer always fills A and B; a reader accepts {}
        if isinstance(arms, dict) and any(k not in ("A", "B") or not isinstance(v, dict) for k, v in arms.items()):
            errs.append(f"{w}.arms may only hold the objects A and B")
        if e.get("risk") is not None and e.get("risk") not in RISKS:
            errs.append(f"{w}.risk not in {RISKS}")
        cf = need(e, "confidence", (int, float), w, True)
        if isinstance(cf, (int, float)) and not 0 <= cf <= 1:
            errs.append(f"{w}.confidence outside 0..1")
        cu = need(e, "cost_usd", (int, float), w, True)
        if isinstance(cu, (int, float)) and cu < 0:
            errs.append(f"{w}.cost_usd is negative")
        if e.get("status") not in EXP_STATUSES:
            errs.append(f"{w}.status {e.get('status')!r} not in {EXP_STATUSES}")
        pid = need(e, "proposal_id", str, w, True)
        if isinstance(pid, str) and not _UUID.fullmatch(pid):
            errs.append(f"{w}.proposal_id is not a lowercase uuid")
        need(e, "evidence_refs", list, w)
        for k in ("judge", "jev"):                    # optional (Mission 005.5): the replay's judge and Jev summaries
            if e.get(k) is not None and not isinstance(e[k], dict):
                errs.append(f"{w}.{k} must be an object or null")
        if e.get("status") == "staged" and not (e.get("proposal_id") and e.get("verdict") == "IMPROVES"
                                                and e.get("artifact_hash") and e.get("experiment_id")):
            errs.append(f"{w}: staged without an IMPROVES verdict, an experiment id, an artifact hash and a proposal")
    co = need(obj, "costs", dict, "report")
    if isinstance(co, dict):
        nums: list[Any] = []
        for m, v in (need(co, "by_model", dict, "costs") or {}).items():
            for k, t in (("calls", int), ("tokens_in", int), ("tokens_out", int), ("usd", (int, float))):
                nums.append(need(v, k, t, f"costs.by_model.{m}"))
        nums.append(need(co, "sandbox_runs", int, "costs"))
        for m, v in (need(co, "providers", dict, "costs") or {}).items():
            nums.append(need(v, "requests", int, f"costs.providers.{m}"))
            nums.append(need(v, "usd", (int, float), f"costs.providers.{m}"))
        nums.append(need(co, "total_usd", (int, float), "costs"))
        if any(isinstance(n, (int, float)) and n < 0 for n in nums):
            errs.append("costs hold a negative number")
    if isinstance(dl, dict) and any(isinstance(dl.get(k), int) and dl[k] < 0
                                    for k in ("sessions", "deviations", "eval_failures", "routine_failures")):
        errs.append("delta holds a negative count")
    he = need(obj, "health", dict, "report")
    if isinstance(he, dict):
        for k in HEALTH_KEYS:
            if he.get(k) not in HEALTH_VALUES:
                errs.append(f"health.{k} {he.get(k)!r} not in {HEALTH_VALUES}")
    for p in need(obj, "pending_decisions", list, "report") or []:
        if not isinstance(p, str) or not _UUID.fullmatch(p):
            errs.append("pending_decisions holds something that is not a lowercase uuid")
    if "notes" in obj and not (isinstance(obj["notes"], list) and all(isinstance(n, str) for n in obj["notes"])):
        errs.append("notes must be a list of strings")
    if "health_reasons" in obj:
        hr = obj["health_reasons"]
        if not (isinstance(hr, dict) and all(isinstance(v, list) and all(isinstance(x, str) for x in v)
                                             for v in hr.values())):
            errs.append("health_reasons must map a provider to a list of strings")
    if "skipped" in obj:
        sk = obj["skipped"]
        if not isinstance(sk, dict):
            errs.append("skipped must be an object")
        else:
            if obj.get("status") != "preflight_failed":
                errs.append("skipped is only valid with status preflight_failed")
            if sk.get("reason") not in SKIP_REASONS:
                errs.append(f"skipped.reason {sk.get('reason')!r} not in {SKIP_REASONS}")
            need(sk, "by", str, "skipped")
            need(sk, "summary", str, "skipped")
            items = need(sk, "items", list, "skipped")
            if isinstance(items, list) and not all(isinstance(i, str) for i in items):
                errs.append("skipped.items must be a list of strings")
    if "prerequisite_warnings" in obj and not (isinstance(obj["prerequisite_warnings"], list) and all(
            isinstance(w, str) for w in obj["prerequisite_warnings"])):
        errs.append("prerequisite_warnings must be a list of strings")
    return errs


# =========================================================================== CLI (`amem rsi night`)


EXIT = {"ok": 0, "partial": 0, "budget_exhausted": 0, "preflight_failed": 2, "aborted": 130}


def cli(args: argparse.Namespace) -> int:
    from agent_memory.cli_deviations import _read_guard, _write_guard
    guard = _read_guard if args.dry_run else _write_guard
    if (code := guard("rsi night")) is not None:
        return code
    rep = run_night(night=args.night, config_path=args.config, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(rep, sort_keys=True, default=str))
    else:
        staged = [e for e in rep["experiments"] if e["status"] == "staged"]
        print(f"rsi night {rep['night']}: {rep['status']}"
              f"{' (dry run, nothing written)' if args.dry_run else ''}; delta sessions={rep['delta']['sessions']} "
              f"deviations={rep['delta']['deviations']}; candidates={len(rep['candidates'])} "
              f"selected={sum(c['selected'] for c in rep['candidates'])}; experiments={len(rep['experiments'])} "
              f"staged={len(staged)}; checkpoint {'advanced' if rep['checkpoint']['advanced'] else 'not advanced'}; "
              f"cost ${rep['costs'].get('total_usd', 0):.2f}" + (f"; {rep['notes'][-1]}" if rep.get("notes") else ""))
    return EXIT.get(rep["status"], 1)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("night", help="the nightly RSI investigator (S0-S9); stages proposals, never approves")
    p.add_argument("--night", default=None, help="the night key YYYY-MM-DD (default: the current night)")
    p.add_argument("--config", type=Path, default=None, help="config file (default: config/rsi-night.json)")
    p.add_argument("--dry-run", action="store_true", help="read and rank only; write nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cli)
