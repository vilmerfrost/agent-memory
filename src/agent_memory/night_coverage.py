"""amem-rsi-night's coverage snapshots and consumption records (data-use plan R4, docs/data-use/coverage.md).

The night (rsi_night.Night) feeds a `Ledger` while it runs and writes it once, in `_finish`, through
`coverage.record_best_effort` (one savepoint per record, never raises, off with AMEM_COVERAGE_WRITE=0):

    S1 delta     one consumption row per delta dataset: queried = rows the delta read for the night window,
                 considered = rows the night looked at, used = rows that went into a sighting or a candidate's history
                 (or that it measured and found below its thresholds), skipped = reason counts, findings = sightings
    S4 packs     one consumption row per provider the night asked (S1 provider steps and every S4 evidence pack,
                 summed for the night): rows returned, rows that reached a pack, ok rows used as supporting evidence
    snapshots    one per S1 dataset and per asked provider, for the night window (the night is the job that knows
                 these denominators: the stored rows its delta query selects, and the rows a provider returned)

A provider that did not answer is `analysis_not_run` (unavailable) or `analysis_failed` (error, timeout) with the
reason the report already carries, and its snapshot is `measurement_unavailable`: never `used_no_finding`. Rows
returned but not analysed are `analysis_not_run` or `read_excluded`. The all-clear guard is coverage.verdict,
unchanged; `requirements` says which datasets the night's conclusions need.
"""

from __future__ import annotations

import functools
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg

from agent_memory import coverage as CV
from agent_memory.integrations import refs as REFS

JOB = "amem-rsi-night"          # the routine name (launchd, routines.log); rsi_checkpoints keeps its own "rsi-night"

SESSIONS = CV.dataset_key("sessions", "sessions")
DEVIATIONS = CV.dataset_key("deviations", "deviations")
EVAL_FAILURES = CV.dataset_key("experiments", "rsi_eval_results")
ROUTINE_FAILURES = CV.dataset_key("routine_runs", "runs")
EXPERIMENTS = CV.dataset_key("experiments", "rsi_experiments")
EVIDENCE = CV.dataset_key("evidence_links", "rsi_evidence")
SKILLS = CV.dataset_key("skills", "skill_scores")
PROVIDERS = ("supabase", "clickhouse", "langfuse", "pastdev")
PROVIDER_DATASET = {p: CV.dataset_key("evidence_links", p) for p in PROVIDERS}
RETRIEVER = {"sb": "supabase", "ch": "clickhouse", "lf": "langfuse", "pd": "pastdev"}
_SOURCE_PROVIDER = {"supabase": "supabase", "jev": "supabase", "experiments": "supabase", "clickhouse": "clickhouse",
                    "langfuse": "langfuse", "pastdev": "pastdev"}       # an evidence-pack finding source -> provider

# S1 delta kind (rsi_evidence_pack.Delta, DELTA_SQL) -> its dataset and the denominator of its snapshot
DELTA: dict[str, tuple[str, str]] = {
    "sessions": (SESSIONS, "prod sessions created, finished or given a deviation in the night window"),
    "deviations": (DEVIATIONS, "prod deviations created in the night window"),
    "eval_failures": (EVAL_FAILURES, "fail or worse eval results created in the night window"),
    "routine_failures": (ROUTINE_FAILURES, "fail or error routine runs created in the night window"),
    "experiments": (EXPERIMENTS, "experiments created or finished in the night window"),
    "findings": (EVIDENCE, "evidence links (not answers) created in the night window"),
}
S1_DATASETS = (*(d for d, _ in DELTA.values()), SKILLS)

# What the night's conclusions depend on (docs/data-use/coverage.md, "The night"). Sessions are only counted (the
# night reads them through their deviations) and skills are scored by rsi-nightly's refresh, so neither is required.
# An empty window passes for each (allow_no_eligible): the night runs only after amem-daily passed for it
# (rsi_schedule's prerequisite gate), so an empty window is "nothing new to check", and the brief names it.
BASE_REQUIRED = (DEVIATIONS, ROUTINE_FAILURES, EVAL_FAILURES, EXPERIMENTS, EVIDENCE)

STOPPED = ("preflight_failed", "aborted", "budget_exhausted")      # night statuses that stop work without a fault
_COUNT_ALL = 2_147_483_647


def requirements(required: Sequence[str]) -> list[CV.Requirement]:
    """coverage.verdict requirements: BASE_REQUIRED always, plus the provider datasets the night says it asked."""
    names = list(BASE_REQUIRED) + [d for d in required if d in PROVIDER_DATASET.values() and d not in BASE_REQUIRED]
    return [CV.Requirement(d, allow_no_eligible=True) for d in dict.fromkeys(names)]


def _safe(fn: Callable[..., None]) -> Callable[..., None]:
    """A ledger feed never raises into the night: a bug here is kept and the night records no coverage at all
    (the brief then says analysis incomplete), rather than rows with wrong counts."""
    @functools.wraps(fn)
    def wrapper(self: Ledger, *args: Any, **kw: Any) -> None:
        try:
            fn(self, *args, **kw)
        except Exception as exc:  # noqa: BLE001 - reported in the night's notes, never raised
            self.errors.append(f"{fn.__name__}: {type(exc).__name__}: {str(exc)[:120]}")
    return wrapper


def _dget(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _line(x: Any, n: int) -> str:
    s = " ".join(str(x).split())
    return s if len(s) <= n else s[: n - 3] + "..."


@dataclass
class Use:
    """One dataset's funnel for the night."""
    queried: int = 0
    considered: int = 0
    used: int = 0
    findings: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    refs: list[str] = field(default_factory=list)
    done: bool = False                  # its analysis finished
    failed: str | None = None           # why its analysis failed
    status: str | None = None           # explicit status (else derived from the counts)
    detail: str | None = None

    def ref(self, *rs: Any) -> None:
        for r in rs:
            if isinstance(r, str) and r not in self.refs and len(self.refs) < CV.MAX_EVIDENCE_REFS and REFS.is_ref(r):
                self.refs.append(r)


@dataclass
class Provider(Use):
    """A provider's rows for the night, plus the steps that asked it."""
    rows: int = 0                       # rows its ok steps returned
    ok_steps: set[str] = field(default_factory=set)
    failed_steps: dict[str, str] = field(default_factory=dict)      # step -> status
    reasons: list[str] = field(default_factory=list)
    owner_off: bool = False

    @property
    def asked(self) -> bool:
        return bool(self.ok_steps or self.failed_steps)

    def step(self, name: str, status: str, found: Any = 0, detail: Any = "") -> None:
        if status == "ok":
            self.ok_steps.add(name)
            self.rows += found if isinstance(found, int) and not isinstance(found, bool) and found > 0 else 0
            return
        self.failed_steps.setdefault(name, status)
        why = _line(f"{name}: {status}" + (f" ({detail})" if detail else ""), 160)
        if why not in self.reasons and len(self.reasons) < 3:
            self.reasons.append(why)


class Ledger:
    """What the night read, used and skipped, per dataset. Feeding it never raises into the night's logic for a
    malformed input: unknown shapes count as nothing."""

    def __init__(self, night: str, analysis_version: str) -> None:
        self.night, self.version = night, analysis_version[:80]
        self.window: tuple[datetime, datetime] | None = None
        self.checkpoint: str | None = None
        self.delta_seen = False
        self.uses: dict[str, Use] = {d: Use() for d in S1_DATASETS}
        self.snaps: dict[str, dict[str, Any]] = {}
        self.providers: dict[str, Provider] = {p: Provider() for p in PROVIDERS}
        self.errors: list[str] = []

    # -- S1 ------------------------------------------------------------------------------------------
    @_safe
    def delta(self, delta: Any, *, since: datetime, until: datetime, before: str | None,
              next_checkpoint: str | None) -> None:
        self.window = (since, until) if until >= since else (until, until)
        self.checkpoint = _line(f"rsi-night checkpoint {before or 'first'} -> {next_checkpoint or '?'}", 300)
        trunc = _dget(delta, "truncated") or {}
        findings = list(_dget(delta, "findings") or [])
        for kind, (ds, denom) in DELTA.items():
            rows = [f for f in findings if self._delta_provider(f) is None] if kind == "findings" \
                else list(_dget(delta, kind) or [])
            self.uses[ds].queried = len(rows)
            self.snaps[ds] = {"kind": kind, "denominator": denom, "collected": len(rows),
                              "truncated": bool(isinstance(trunc, Mapping) and trunc.get(kind))}
        s = self.uses[SESSIONS]
        s.status, s.done = "analysis_not_run", True
        s.detail = "counted only: the night analyses sessions through their deviations"
        for st in _dget(delta, "steps") or []:
            self._step(st, "s1")
        self._owner_off(_dget(delta, "health"))
        self.delta_seen = True

    @staticmethod
    def _delta_provider(f: Any) -> str | None:
        """The provider of a live S1 finding (ClickHouse, Langfuse, past.dev); None for a stored rsi_evidence row."""
        via = str(_dget(f, "via") or "")
        return RETRIEVER.get(via.split(".", 1)[0]) if "." in via and not via.startswith("sb.") else None

    @_safe
    def finding(self, f: Any, outcome: str) -> None:
        """One S1 finding's outcome in candidate discovery: sighted | below_min_n | over_cap | <its status>."""
        prov = self._delta_provider(f)
        u = self.uses[EVIDENCE] if prov is None else self.providers[prov]
        u.considered += 1
        if outcome in ("sighted", "below_min_n"):
            u.used += 1
            if outcome == "sighted":
                u.findings += 1
                u.ref(_dget(f, "ref"))
        else:
            u.skipped[outcome] += 1

    @_safe
    def deviations(self, *, used: int, team: int, other: int, sightings: Sequence[Any]) -> None:
        u = self.uses[DEVIATIONS]
        u.considered = u.queried
        u.used, u.findings = used, len(sightings)
        if team:
            u.skipped["team_rule"] += team
        if other:
            u.skipped["not_a_current_rule_in_lookback"] += other
        for s in sightings:
            u.ref(*list(_dget(s, "evidence_refs") or [])[:3])
        u.done = True

    @_safe
    def routine_failures(self, *, used: int, no_routine: int, sightings: Sequence[Any], messenger: int = 0) -> None:
        """`messenger`: failures of a messenger routine (rsi_night.MESSENGER_ROUTINES), skipped by design."""
        u = self.uses[ROUTINE_FAILURES]
        u.considered, u.used, u.findings = u.queried, used, len(sightings)
        if no_routine:
            u.skipped["no_routine_name"] += no_routine
        if messenger:
            u.skipped["messenger"] += messenger
        for s in sightings:
            u.ref(*list(_dget(s, "evidence_refs") or [])[:3])
        u.done = True

    @_safe
    def skills(self, *, rows: int, skipped_by: str | None, sightings: Sequence[Any]) -> None:
        u = self.uses[SKILLS]
        u.queried = rows
        if skipped_by:
            if rows:
                u.skipped[skipped_by] += rows
        else:
            u.considered = u.used = rows
            u.findings = len(sightings)
            for s in sightings:
                u.ref(*list(_dget(s, "evidence_refs") or [])[:1])
        u.detail = "low skill scores refreshed in the window (rsi-nightly's refresh owns the snapshot)"
        u.done = True

    @_safe
    def history(self, kind: str, *, ref: Any, outcome: str) -> None:
        """An eval failure or experiment from the delta: attached | no_candidate | no_experiment_id | malformed."""
        u = self.uses[EVAL_FAILURES if kind == "eval_failures" else EXPERIMENTS]
        if outcome in ("attached", "no_candidate"):
            u.considered += 1
        if outcome == "attached":
            u.used += 1
            if kind == "eval_failures":          # a failed eval the night carried to its candidate: a finding
                u.findings += 1
                u.ref(ref)
        else:
            u.skipped[outcome] += 1

    @_safe
    def history_done(self) -> None:
        self.uses[EVAL_FAILURES].done = self.uses[EXPERIMENTS].done = True

    @_safe
    def finished(self, dataset: str) -> None:
        self.uses[dataset].done = True

    @_safe
    def failed(self, dataset: str, why: str) -> None:
        self.uses[dataset].failed = _line(why, 300)
        if dataset == EVIDENCE:                  # the live S1 findings were in the same analysis
            for p in self.providers.values():
                if p.considered:
                    p.step("s1:findings", "error", detail=why)

    # -- S4 ------------------------------------------------------------------------------------------
    @_safe
    def pack(self, pack: Any, *, candidate: str = "") -> None:
        """One candidate's evidence pack: the providers it asked, their rows, which rows the night used."""
        steps = _dget(pack, "steps")
        per: dict[str, int] = Counter()
        for f in _dget(pack, "findings") or []:
            kind, status = _dget(f, "kind"), str(_dget(f, "status") or "")
            if kind == "dropped":
                for src, n in (_dget(f, "measurements") or {}).items():
                    prov = _SOURCE_PROVIDER.get(str(src))
                    if prov and isinstance(n, int):
                        self.providers[prov].skipped["pack_limit"] += n
                continue
            if kind in ("query", "health") or (steps is None and status in ("unavailable", "error")):
                continue                          # a status finding: says a step failed (the steps carry it)
            prov = self._pack_provider(f)
            if prov is None:
                continue
            p = self.providers[prov]
            p.considered += 1
            per[prov] += 1
            if status == "ok":
                p.used += 1
                p.findings += 1               # supporting evidence for the candidate under investigation
                p.ref(_dget(f, "ref"))
            else:
                p.skipped[status or "unknown"] += 1
        if steps is None:                         # a pack without step records: its health says who answered
            for prov, h in (_dget(pack, "health") or {}).items():
                if prov in self.providers and h != "skipped":
                    ok = h in ("ok", "stale", "unlinked", "truncated", "partial")
                    self.providers[prov].step(f"s4:{prov}.pack", "ok" if ok else str(h), found=per.get(prov, 0),
                                              detail="" if ok else f"evidence pack for {candidate}")
        else:
            for st in steps:
                self._step(st, "s4")
        self._owner_off(_dget(pack, "health"))

    @_safe
    def pack_failed(self, why: str) -> None:
        for p in self.providers.values():
            p.step("s4:pack", "error", detail=why)

    @staticmethod
    def _pack_provider(f: Any) -> str | None:
        via = str(_dget(f, "via") or "")
        if via == "candidate.evidence_refs":
            return "supabase"
        if "." in via and via.split(".", 1)[0] in RETRIEVER:
            return RETRIEVER[via.split(".", 1)[0]]
        return _SOURCE_PROVIDER.get(str(_dget(f, "source") or ""))

    def _step(self, st: Any, stage: str) -> None:
        """One step record ({step, status, found, detail}). A health check of an idle provider is not a request for
        data: a provider no step asked lost no evidence (a query step of an unhealthy one answers unavailable)."""
        name = str(_dget(st, "step") or "")
        prov = RETRIEVER.get(name.split(".", 1)[0])
        if prov is None or name.endswith(".health"):
            return
        self.providers[prov].step(f"{stage}:{name}", str(_dget(st, "status") or "error"), _dget(st, "found", 0),
                                  _dget(st, "detail") or "")

    def _owner_off(self, health: Any) -> None:
        for prov, h in (health or {}).items():
            if prov in self.providers and h == "skipped":
                self.providers[prov].owner_off = True

    # -- records -------------------------------------------------------------------------------------
    def _count_truncated(self, conn: psycopg.Connection) -> dict[str, int | None]:
        """The full denominator of a kind the delta cut at its page limit (None when the count fails)."""
        out: dict[str, int | None] = {}
        if self.window is None:
            return out
        from agent_memory.rsi_evidence_pack import DELTA_SQL
        for ds, s in self.snaps.items():
            if not s["truncated"]:
                continue
            try:
                with conn.transaction():
                    row = conn.execute(f"select count(*) from ({DELTA_SQL[s['kind']]}) q",
                                       {"since": self.window[0], "until": self.window[1], "limit": _COUNT_ALL}).fetchone()
                out[ds] = int(row[0]) if row else None
            except (psycopg.Error, KeyError):
                out[ds] = None
        return out

    def _consumption(self, ds: str, u: Use, *, status: str | None = None, detail: str | None = None,
                     windowed: bool = True) -> CV.Consumption:
        st = status or u.status
        considered, used, findings = u.considered, u.used, u.findings
        if st == "analysis_not_run":
            considered = used = findings = 0
        elif st == "analysis_failed":
            findings = 0
        queried = max(u.queried, considered)
        used = min(used, considered)
        w = self.window if windowed else None
        return CV.Consumption(job=JOB, run_key=self.night, dataset=ds, analysis_version=self.version,
                              window_start=w[0] if w else None, window_end=w[1] if w else None,
                              source_checkpoint=self.checkpoint if w else None, queried=queried, considered=considered,
                              used=used, skipped=dict(u.skipped), findings=findings,
                              evidence_refs=list(u.refs) if findings else [], status=st,
                              detail=_line(detail if detail is not None else (u.detail or ""), 500) or None)

    def records(self, *, status: str, reason: str, conn: psycopg.Connection | None = None
                ) -> tuple[list[CV.Snapshot | CV.Consumption], list[str]]:
        """(records, required provider datasets) for the night's final `status`; `reason` says why it stopped."""
        out: list[CV.Snapshot | CV.Consumption] = []
        stop = "analysis_not_run" if status in STOPPED else "analysis_failed"
        why = _line(f"night {status}: {reason}", 400)
        if not self.delta_seen:
            for ds in S1_DATASETS:
                out.append(self._consumption(ds, Use(), status=stop, detail=f"S1 did not run ({why})",
                                             windowed=False))
            return out, []
        counts = self._count_truncated(conn) if conn is not None else {}
        assert self.window is not None
        for ds in S1_DATASETS:
            u = self.uses[ds]
            if u.failed:
                out.append(self._consumption(ds, u, status="analysis_failed", detail=u.failed))
            elif not u.done:
                out.append(self._consumption(ds, u, status=stop, detail=f"not analysed ({why})"))
            else:
                out.append(self._consumption(ds, u))
            s = self.snaps.get(ds)
            if s is None:
                continue
            eligible = counts.get(ds) if s["truncated"] else s["collected"]
            collected = s["collected"] if eligible is None else min(s["collected"], eligible)
            detail = None
            if s["truncated"]:
                detail = (f"the delta read {collected} of {eligible} rows (page limit); the rest is read next night"
                          if eligible is not None else "the delta hit its page limit and the full count failed")
            out.append(CV.Snapshot(dataset=ds, window_start=self.window[0], window_end=self.window[1],
                                   denominator=s["denominator"], eligible=eligible, collected=collected,
                                   checkpoints={"rsi-night": self.checkpoint or ""}, detail=detail))
        required: list[str] = []
        for prov, p in self.providers.items():
            ds = PROVIDER_DATASET[prov]
            if p.owner_off and not p.ok_steps:
                if p.asked:
                    out.append(self._consumption(ds, Provider(), status="analysis_not_run",
                                                 detail="switched off by the owner: not asked"))
                continue
            if not p.asked:
                continue
            required.append(ds)
            if not p.ok_steps:
                bad = "analysis_not_run" if set(p.failed_steps.values()) <= {"unavailable"} else "analysis_failed"
                out.append(self._consumption(ds, p, status=bad, detail="; ".join(p.reasons) or bad))
            else:
                missing = f"; not answered: {'; '.join(p.reasons)}" if p.failed_steps else ""
                p.queried = max(p.rows, p.considered)
                out.append(self._consumption(ds, p, detail=f"{len(p.ok_steps)} step(s) answered{missing}"))
            unmeasured = tuple(sorted(p.failed_steps))[: CV.MAX_FIELDS]
            eligible = max(p.rows, p.considered) if p.ok_steps else None
            st = "measurement_unavailable" if unmeasured or eligible is None else \
                ("no_eligible_data" if eligible == 0 else "complete")
            out.append(CV.Snapshot(dataset=ds, window_start=self.window[0], window_end=self.window[1],
                                   denominator=f"rows {prov} returned to the night's evidence queries (S1 and S4)",
                                   eligible=eligible, collected=eligible,
                                   measured_fields=tuple(sorted(p.ok_steps))[: CV.MAX_FIELDS],
                                   unmeasured_fields=unmeasured, status=st,
                                   checkpoints={"rsi-night": self.checkpoint or ""},
                                   detail=_line("; ".join(p.reasons), 500) or None))
        return out, required

    def write(self, conn: psycopg.Connection, *, status: str, reason: str,
              environ: Mapping[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
        """Record the night (record_best_effort) and return (report["coverage"], problem lines for the notes)."""
        enabled = CV.writes_enabled(environ)
        summary: dict[str, Any] = {"job": JOB, "run_key": self.night, "enabled": enabled,
                                   "analysis_version": self.version, "consumption": {}, "snapshots": {},
                                   "datasets": {}, "required": [], "failed": 0}
        if not enabled:
            return summary, []
        if self.errors:                    # the counts are not trustworthy: record nothing (the verdict stays open)
            summary["failed"] = len(self.errors)
            return summary, [f"coverage not recorded: ledger {e}" for e in self.errors[:3]]
        records, provs = self.records(status=status, reason=reason, conn=conn)
        problems: list[str] = []
        cons: Counter[str] = Counter()
        snaps: Counter[str] = Counter()
        for r in records:
            failed = CV.record_best_effort(conn, [r], environ=environ)    # its own savepoint; never raises
            if failed:
                kind = "consumption" if isinstance(r, CV.Consumption) else "snapshot"
                problems += [f"coverage {kind} {x.removeprefix('coverage ')}" for x in failed]
                continue
            if isinstance(r, CV.Consumption):
                cons[r.resolved_status()] += 1
                summary["datasets"][r.dataset] = r.resolved_status()
            else:
                snaps[r.resolved_status()] += 1
        summary.update(consumption=dict(sorted(cons.items())), snapshots=dict(sorted(snaps.items())),
                       required=[*BASE_REQUIRED, *provs], failed=len(problems))
        return summary, problems


def report_problems(cov: Any) -> list[str]:
    """Problems with a night report's optional `coverage` object (rsi_night.validate_report)."""
    def count(x: Any) -> bool:
        return isinstance(x, int) and not isinstance(x, bool) and x >= 0
    if not isinstance(cov, dict):
        return ["coverage must be an object"]
    errs = []
    allowed = {"job", "run_key", "enabled", "analysis_version", "consumption", "snapshots", "datasets", "required",
               "failed"}
    errs += [f"coverage: unknown key {k}" for k in sorted(set(cov) - allowed)]
    if cov.get("job") != JOB:
        errs.append(f"coverage.job must be {JOB}")
    if not isinstance(cov.get("run_key"), str) or not cov["run_key"]:
        errs.append("coverage.run_key must be a non-empty string")
    if not isinstance(cov.get("enabled"), bool):
        errs.append("coverage.enabled must be a bool")
    if not isinstance(cov.get("analysis_version"), str):
        errs.append("coverage.analysis_version must be a string")
    for k, statuses in (("consumption", CV.CONSUMPTION_STATUSES), ("snapshots", CV.SNAPSHOT_STATUSES)):
        v = cov.get(k)
        if not (isinstance(v, dict) and all(s in statuses and count(n) for s, n in v.items())):
            errs.append(f"coverage.{k} must map {statuses} to counts")
    ds = cov.get("datasets")
    if not (isinstance(ds, dict) and all(isinstance(d, str) and CV.DATASET_RE.fullmatch(d)
                                         and s in CV.CONSUMPTION_STATUSES for d, s in ds.items())):
        errs.append("coverage.datasets must map dataset keys to consumption statuses")
    elif isinstance(cov.get("consumption"), dict) and sum(n for n in cov["consumption"].values()
                                                          if count(n)) != len(ds):
        errs.append("coverage.consumption must add up to the number of coverage.datasets")
    rq = cov.get("required")
    if not (isinstance(rq, list) and all(isinstance(d, str) and CV.DATASET_RE.fullmatch(d) for d in rq)):
        errs.append("coverage.required must be a list of dataset keys")
    if not count(cov.get("failed")):
        errs.append("coverage.failed must be a count")
    return errs
