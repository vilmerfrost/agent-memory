"""Small explicit verification contract for a pilot, not a new database table.
Binding and observed behavior are separate facts. A screenshot/navigation alone
cannot satisfy a criterion. Unknown context never becomes a passing result.
"""
from __future__ import annotations
from typing import Any, Mapping

FORMAT = "verification-pilot/1"
TARGET_FIELDS = ("repo_id", "revision", "worktree", "build_id", "server_instance", "origin", "route", "tab_id")


def assess(expected: Mapping[str, Any], observed: Mapping[str, Any], *, criterion: Mapping[str, Any] | None,
           evidence_ref: str | None) -> dict[str, Any]:
    missing = [name for name in TARGET_FIELDS if expected.get(name) is None or observed.get(name) is None]
    mismatch = [name for name in TARGET_FIELDS if expected.get(name) is not None and observed.get(name) is not None
                and expected[name] != observed[name]]
    if mismatch:
        result, binding, reason = "fail", "mismatch", "target_mismatch"
    elif missing:
        result, binding, reason = "unknown", "incomplete", "target_binding_incomplete"
    elif not criterion or not isinstance(criterion.get('key'), str) or 'expected' not in criterion or not evidence_ref:
        result, binding, reason = "unknown", "verified", "criterion_or_evidence_missing"
    elif 'actual' not in criterion:
        result, binding, reason = "unknown", "verified", "behavior_not_observed"
    else:
        result, binding, reason = ("pass" if criterion['actual'] == criterion['expected'] else "fail"), "verified", "observed_predicate"
    return {"format": FORMAT, "result": result, "binding_status": binding, "reason": reason,
            "missing": missing, "mismatch": mismatch, "criterion": dict(criterion) if criterion else None,
            "evidence_ref": evidence_ref, "acceptance_scope": "this_observed_criterion_only"}
