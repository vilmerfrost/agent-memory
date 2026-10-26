"""Bounded, version-bound production observations. Evidence, never application authority.

The outcome job keeps its latest-value projection in rsi_outcomes and appends an
idempotent observation to the existing rsi_evidence table. Consumers must check
freshness, exact instruction bytes and coverage again. A before/after association
is not a causal estimate or permission to edit, promote or undo anything.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory.integrations import refs

FORMAT = "production-outcome/1"
KIND = "production-outcome"
FRESH_DAYS = 7
MAX_READ = 100
MIN_NEW_SESSIONS = 5
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _at(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is not None else None


def _version(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


# All stored sessions in the rule's input window form the collection denominator.
# Single-file source versions can be verified against an intervention's exact
# before/after file hashes. A composite/missing/unresolved source is not guessed.
_COVERAGE_SQL = """
with s as (
 select s.session_id, s.scanned_at is not null as scanned,
        case when coalesce(s.started_at,s.created_at) < %(applied)s then 'before' else 'after' end as side,
        case when coalesce(s.started_at,s.created_at) < %(applied)s then %(before_sha)s else %(after_sha)s end as expected,
        (select count(*) from jsonb_array_elements(coalesce(s.instruction_sources,'[]'::jsonb)) e
          where e->>'name' = %(source)s) as source_entries,
        (select count(*) from jsonb_array_elements(coalesce(s.instruction_sources,'[]'::jsonb)) e
          where e->>'name' = %(source)s and e->>'path' = %(target)s
            and e->>'authority' in ('observed','reconstructed')
            and coalesce((e->>'changed_during_session')::boolean,false) = false
            and e->>'sha256' = case when coalesce(s.started_at,s.created_at) < %(applied)s
                                   then %(before_sha)s else %(after_sha)s end) as exact_entries
 from sessions s
 where coalesce(s.env,'prod') not in ('sandbox','eval') and s.kind <> 'eval'
   and s.instructions ? %(source)s
   and coalesce(s.started_at,s.created_at) >= %(start)s
   and coalesce(s.started_at,s.created_at) < %(end)s
), d as (
 select d.id::text, d.session_id, d.instruction_authority, d.instruction_sha
 from deviations d where d.instruction_source=%(source)s and d.rule_id=%(rule)s
)
select side, count(*) as eligible, count(*) filter(where scanned) as scanned,
       count(*) filter(where scanned and source_entries=1 and exact_entries=1) as version_verified,
       array_agg(session_id order by session_id) as session_ids,
       (select count(*) from d join s ds on ds.session_id=d.session_id where ds.scanned and ds.side=s.side) as deviations,
       (select count(*) from d join s ds on ds.session_id=d.session_id
         where ds.scanned and ds.side=s.side
           and d.instruction_authority in ('observed','reconstructed')
           and d.instruction_sha=ds.expected) as verified_deviations
from s group by side order by side
"""


def coverage(conn: Any, outcome: Any) -> dict[str, Any]:
    """Measurement authority for this outcome's exact window; never changes its old verdict."""
    change = outcome.change
    window = timedelta(days=outcome.window_days)
    before = _version(getattr(change, "before_sha256", None))
    after = _version(getattr(change, "after_sha256", None))
    target = getattr(change, "target", None)
    counts: dict[str, dict[str, Any]] = {
        side: {"eligible": 0, "scanned": 0, "version_verified": 0, "deviations": 0,
               "verified_deviations": 0, "session_set_sha256": _hash([])} for side in ("before", "after")}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_COVERAGE_SQL, {"source": change.instruction_source, "rule": change.rule_id,
                    "target": target, "before_sha": before, "after_sha": after,
                    "applied": change.applied_at, "start": change.applied_at-window,
                    "end": change.applied_at+window})
        for row in cur.fetchall():
            counts[row["side"]] = {k: int(row[k]) for k in ("eligible", "scanned", "version_verified",
                                                           "deviations", "verified_deviations")}
            counts[row["side"]]["session_set_sha256"] = _hash(row["session_ids"])
    gaps: list[str] = []
    if not target or before is None or after is None:
        gaps.append("intervention_version_unknown")
    if getattr(outcome, "other_changes", 0) or outcome.verdict == "mixed":
        gaps.append("other_instruction_changes")
    for side in ("before", "after"):
        group = counts[side]
        if group["eligible"] < 5:
            gaps.append(f"{side}_insufficient_sessions")
        if group["scanned"] != group["eligible"]:
            gaps.append(f"{side}_scan_incomplete")
        if group["version_verified"] != group["scanned"]:
            gaps.append(f"{side}_instruction_version_unverified")
        if group["verified_deviations"] != group["deviations"]:
            gaps.append(f"{side}_deviation_authority_unverified")
        if group["scanned"] != getattr(outcome, f"sessions_{side}") or group["deviations"] != getattr(
                outcome, f"deviations_{side}"):
            gaps.append(f"{side}_cohort_mismatch")
    return {"status": "complete" if not gaps else "incomplete", "gaps": gaps, "cohort": counts,
            "denominator": "stored production non-eval sessions naming the source in the exact outcome window",
            "scope": "stored_rows_only", "authority": "observed_or_reconstructed_exact_file_versions"}


def observation(outcome: Any, checked_at: datetime, evidence_coverage: Mapping[str, Any]) -> dict[str, Any]:
    from agent_memory import rsi_outcome as producer
    c = outcome.change
    if checked_at.tzinfo is None or c.applied_at.tzinfo is None:
        raise ValueError("production observation timestamps must be timezone-aware")
    checked_at = checked_at.astimezone(UTC)
    applied = c.applied_at.astimezone(UTC)
    w = timedelta(days=outcome.window_days)
    intervention = {"kind": c.kind, "id": c.id, "rule_key": c.rule_key, "target": getattr(c, "target", None),
                    "before_sha256": _version(getattr(c, "before_sha256", None)),
                    "after_sha256": _version(getattr(c, "after_sha256", None)),
                    "artifact_hash": _version(getattr(c, "artifact_hash", None)),
                    "applied_at": applied.isoformat(),
                    "applied_at_basis": "recorded_promotion" if c.kind == "proposal" else "edit_created_at_proxy",
                    "version_basis": "artifact_manifest" if c.kind == "proposal" else "recorded_apply_hashes"}
    metrics = {"format": FORMAT, "advisory_only": True, "design": "observational_before_after",
               "method": {"producer_format": producer.FORMAT, "test": "conditional_binomial_one_sided",
                          "max_window_days": producer.WINDOW_DAYS, "min_days": producer.MIN_DAYS,
                          "min_sessions": producer.MIN_SESSIONS, "alpha": producer.ALPHA,
                          "min_effect": producer.MIN_EFFECT},
               "intervention": intervention, "rule_key": c.rule_key, "verdict": outcome.verdict,
               "checked_at": checked_at.isoformat(),
               "window": {"before_start": (applied-w).isoformat(), "applied_at": applied.isoformat(),
                          "after_end": (applied+w).isoformat(), "days": outcome.window_days},
               "uncontrolled_confounders": ["model", "effort", "task_mix", "environment"],
               "measurements": {"sessions_before": outcome.sessions_before, "sessions_after": outcome.sessions_after,
                                "deviations_before": outcome.deviations_before,
                                "deviations_after": outcome.deviations_after,
                                "rate_before": outcome.rate_before, "rate_after": outcome.rate_after,
                                "p_value": outcome.p_value}, "coverage": dict(evidence_coverage)}
    # The generation deliberately excludes collection time: unchanged windows,
    # cohorts, intervention and measurements do not make an old idea new again.
    generation = _hash({k: metrics[k] for k in ("format", "method", "intervention", "window", "measurements", "coverage")})
    metrics["generation"] = generation
    key = f"{FORMAT}:{c.kind}:{c.id}:{checked_at.isoformat()}:{_hash(metrics)}"
    return {"external_ref": key, "subject_refs": [refs.amem(c.kind, c.id)], "metrics": metrics,
            "observed_at": checked_at, "valid_until": checked_at+timedelta(days=FRESH_DAYS),
            "summary": f"Advisory production observation: {c.rule_key}; {outcome.verdict}; "
                       f"{evidence_coverage.get('status', 'incomplete')} coverage; not causal"}


def store_observation(conn: Any, outcome: Any, checked_at: datetime) -> str:
    """Append exact snapshot; redelivery at the same instant is idempotent, never UPDATEs history."""
    cov = coverage(conn, outcome)
    outcome.feedback = {"format": FORMAT, "coverage": cov, "advisory_only": True}
    row = observation(outcome, checked_at, cov)
    got = conn.execute("""insert into rsi_evidence(system,kind,subject_refs,summary,metrics,external_ref,observed_at,valid_until)
        values('supabase',%s,%s,%s,%s,%s,%s,%s) on conflict(system,external_ref) do nothing returning id""",
        (KIND, row["subject_refs"], row["summary"], Jsonb(row["metrics"]), row["external_ref"],
         row["observed_at"], row["valid_until"])).fetchone()
    if got is None:
        got = conn.execute("select id from rsi_evidence where system='supabase' and external_ref=%s",
                           (row["external_ref"],)).fetchone()
    evidence_ref = refs.amem("evidence", str(got[0]))
    outcome.feedback["evidence_ref"] = evidence_ref
    return evidence_ref


def assess(row: Mapping[str, Any], current_sha256: str | None, now: datetime) -> dict[str, Any]:
    """Revalidate persisted evidence at consumption; caller supplies actual current target bytes' hash."""
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    reason = None
    if metrics.get("format") != FORMAT or metrics.get("advisory_only") is not True:
        reason = "unsupported_observation"
    observed, expires = _at(row.get("observed_at")), _at(row.get("valid_until"))
    if reason is None and (observed is None or expires is None or observed > now or expires <= now):
        reason = "stale_observation"
    window = metrics.get("window") if isinstance(metrics.get("window"), dict) else {}
    end = _at(window.get("after_end"))
    if reason is None and (end is None or end > now or end < now-timedelta(days=FRESH_DAYS)):
        reason = "stale_observation"
    inter = metrics.get("intervention") if isinstance(metrics.get("intervention"), dict) else {}
    if reason is None and (_version(current_sha256) is None or inter.get("after_sha256") != current_sha256):
        reason = "instruction_version_mismatch"
    if reason is None and not complete_coverage(metrics):
        reason = "coverage_incomplete"
    if reason is None and metrics.get("verdict") not in ("helped", "worse", "no-change"):
        reason = "outcome_not_conclusive"
    return {"ref": refs.amem("evidence", str(row["id"])), "status": "ok" if reason is None else "stale"
            if reason == "stale_observation" else "unlinked", "reason": reason or "validated_advisory_observation",
            "metrics": metrics, "observed_at": observed.isoformat() if observed else None,
            "valid_until": expires.isoformat() if expires else None}


def complete_coverage(metrics: Mapping[str, Any]) -> bool:
    """Validate the stored funnel, not only a self-reported word 'complete'."""
    cov = metrics.get("coverage") if isinstance(metrics.get("coverage"), dict) else {}
    measurements = metrics.get("measurements") if isinstance(metrics.get("measurements"), dict) else {}
    inter = metrics.get("intervention") if isinstance(metrics.get("intervention"), dict) else {}
    if not inter.get("target") or _version(inter.get("before_sha256")) is None or _version(inter.get("after_sha256")) is None:
        return False
    if cov.get("status") != "complete" or cov.get("gaps"):
        return False
    for side in ("before", "after"):
        cohort = cov.get("cohort") if isinstance(cov.get("cohort"), dict) else {}
        group = cohort.get(side) if isinstance(cohort.get(side), dict) else {}
        fields = ("eligible", "scanned", "version_verified", "deviations", "verified_deviations")
        if any(isinstance(group.get(k), bool) or not isinstance(group.get(k), int) or group[k] < 0 for k in fields):
            return False
        if not (group["eligible"] >= 5 and group["eligible"] == group["scanned"] == group["version_verified"]
                and group["deviations"] == group["verified_deviations"]
                and group["scanned"] == measurements.get(f"sessions_{side}")
                and group["deviations"] == measurements.get(f"deviations_{side}")):
            return False
    return True


def for_candidate(conn: Any, candidate: Mapping[str, Any], current_sha256: str | None, *,
                  now: datetime | None = None, limit: int = 6) -> list[dict[str, Any]]:
    """Bounded exact-rule read; stale/mismatched evidence remains visible, never supporting evidence."""
    rule = str(candidate.get("rule_key") or candidate.get("dedupe_key", "")).removeprefix("dev:")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""select id,metrics,observed_at,valid_until from (
                         select distinct on(metrics->>'generation') id,metrics,observed_at,valid_until
                         from rsi_evidence where system='supabase' and kind=%s and metrics->>'rule_key'=%s
                         order by metrics->>'generation',observed_at desc,id desc
                       ) observations order by observed_at desc,id desc limit %s""",
                    (KIND, rule, min(max(int(limit), 1), MAX_READ)))
        return [assess(r, current_sha256, now or datetime.now(UTC)) for r in cur.fetchall()]


@dataclass(frozen=True)
class Reconsideration:
    allowed: bool
    reason: str
    generation: str | None = None


def reconsider(candidate: Mapping[str, Any], finding: Mapping[str, Any], current_sha256: str | None, *,
               now: datetime | None = None) -> Reconsideration:
    """Explicit one-generation advisory reopening policy; never changes a candidate or its old verdicts."""
    metrics = finding.get("metrics") if isinstance(finding.get("metrics"), dict) else {}
    meta = candidate.get("meta") if isinstance(candidate.get("meta"), dict) else {}
    generation = metrics.get("generation")
    if finding.get("status") != "ok" or not isinstance(generation, str):
        return Reconsideration(False, "observation_not_validated")
    if not complete_coverage(metrics):
        return Reconsideration(False, "coverage_incomplete", generation)
    if candidate.get("rule_key") != metrics.get("rule_key"):
        return Reconsideration(False, "different_rule")
    if metrics.get("verdict") not in ("worse", "no-change"):
        return Reconsideration(False, "positive_observation_is_context_only", generation)
    if candidate.get("status") not in ("tested", "rejected"):
        return Reconsideration(False, "candidate_not_terminal", generation)
    if generation == meta.get("production_feedback_generation"):
        return Reconsideration(False, "generation_already_considered", generation)
    inter = metrics.get("intervention") if isinstance(metrics.get("intervention"), dict) else {}
    if _version(current_sha256) is None or inter.get("after_sha256") != current_sha256:
        return Reconsideration(False, "instruction_version_mismatch", generation)
    expires, observed = _at(finding.get("valid_until")), _at(finding.get("observed_at"))
    current = now or datetime.now(UTC)
    considered = _at(candidate.get("last_considered_at"))
    if expires is None or expires <= current or observed is None or observed > current:
        return Reconsideration(False, "stale_observation", generation)
    if considered is None or observed <= considered:
        return Reconsideration(False, "not_newer_than_candidate_consideration", generation)
    previous_version = _version(meta.get("last_tested_instruction_sha256"))
    if previous_version is not None and previous_version != current_sha256:
        return Reconsideration(True, "tested_instruction_version_changed", generation)
    previous_n = meta.get("production_feedback_sessions_after")
    measurements = metrics.get("measurements") if isinstance(metrics.get("measurements"), dict) else {}
    after = measurements.get("sessions_after")
    # Without a recorded prior cohort size, no claim that a daily rerun is
    # materially new can be made. Root records these fields when considering it.
    if (isinstance(previous_n, int) and not isinstance(previous_n, bool)
            and isinstance(after, int) and not isinstance(after, bool) and after >= previous_n + MIN_NEW_SESSIONS):
        return Reconsideration(True, "new_verified_production_cohort", generation)
    return Reconsideration(False, "unchanged_or_unmeasured_evidence_generation", generation)
