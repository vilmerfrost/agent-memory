"""Bounded, read-only comparison of recorded dispatch transcript outcomes.

No provider calls, database reads/writes or raw transcript export. This compares
classifiers and records result evidence; it does not manufacture human acceptance.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_memory import amem_digest as D, tasks_ingest as T

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class LegacyTracker(D.SignalTracker):
    """Version-five invocation semantics, with its proven commit detector intact."""
    def tool_call(self, call_id, name, inp):
        super().tool_call(call_id, name, inp)
        calls = self.operation_calls.pop(call_id, []) if call_id else []
        for call in calls:
            key = "pending_" + call["signal"]
            if self.signals.get(key, 0) <= 1:
                self.signals.pop(key, None)
            else:
                self.signals[key] -= 1
            self.add([call["signal"]])


def _events(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8", errors="replace") as src:
        for line in src:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def compare(events: list[dict[str, Any]], *, observed_at: datetime, own_session: str | None = None,
            copied_uuids: frozenset[str] = frozenset()) -> dict[str, Any]:
    old, new = LegacyTracker(), D.SignalTracker()
    refs: list[dict[str, Any]] = []
    waiting: dict[str, dict[str, Any]] = {}
    skill_calls = set()
    last = None
    excluded = 0
    for line, event in enumerate(events, 1):
        sid = event.get("sessionId")
        if (event.get("uuid") in copied_uuids or (own_session is not None and isinstance(sid, str)
                                                  and sid and sid != own_session)):
            excluded += 1
            continue
        old.feed(event)
        new.feed(event)
        at = T.parse_ts(event.get("timestamp"))
        if at:
            last = at
        msg = event.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                inp = block.get("input")
                name = str(block.get("name") or "")
                if name == "Skill" and isinstance(inp, dict):
                    skill = inp.get("skill")
                    if isinstance(skill, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", skill):
                        skill_calls.add(skill)
                calls = D.operation_calls(D._command_of(name, inp),
                    background=isinstance(inp, dict) and inp.get("run_in_background") is True)
                cid = block.get("id")
                if calls and isinstance(cid, str):
                    waiting[cid] = {"call_id": cid, "call_line": line,
                                    "operations": [c["signal"] for c in calls], "result_line": None}
            elif block.get("type") == "tool_result":
                cid = block.get("tool_use_id")
                if cid in waiting:
                    ref = waiting.pop(cid)
                    refs.append({**ref, "result_line": line, "tool_error": bool(block.get("is_error")),
                                 "denied": bool(event.get("toolDenialKind"))})
    refs.extend(waiting.values())
    old_name = D.outcome(old.signals, old.end, last, observed_at)[0]
    new_name = D.outcome(new.signals, new.end, last, observed_at)[0]
    return {"old_classifier_outcome": old_name, "new_classifier_outcome": new_name,
            "changed": old_name != new_name, "new_operation_signals": dict(new.signals),
            "operation_evidence": refs, "skills_observed": sorted(skill_calls),
            "acceptance": "unknown", "acceptance_reason": "no independent requirement/result binding",
            "copied_records_excluded": excluded,
            "operation_coverage": "complete-results" if refs and all(r["result_line"] for r in refs)
                                   else ("partial-results" if refs else "no-delivery-operations")}


def calibrate(parent_dir: Path, projects: Path, *, limit: int = 30,
              observed_at: datetime | None = None) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be in 1..100")
    now = observed_at or datetime.now(UTC)
    tasks = sorted(T.parse_parent(parent_dir).runs,
                   key=lambda r: (r.finished_at or r.started_at or datetime.min.replace(tzinfo=UTC), r.source_key),
                   reverse=True)[:limit]
    cache: dict[str, dict[str, Any]] = {}
    rows = []
    for task in tasks:
        sid = task.meta.get("session")
        matches = list(projects.glob("*/" + sid + ".jsonl")) if isinstance(sid, str) and UUID.fullmatch(sid) else []
        if len(matches) == 1:
            if sid not in cache:
                from agent_memory import session_resume
                resume = session_resume.resume_of(matches[0])
                cache[sid] = compare(_events(matches[0]), observed_at=now, own_session=sid,
                                     copied_uuids=resume.copied)
            row = {**cache[sid], "transcript_ref": str(matches[0])}
        else:
            row = {"old_classifier_outcome": "unknown", "new_classifier_outcome": "unknown",
                   "changed": False, "acceptance": "unknown", "operation_coverage": "transcript-unavailable",
                   "acceptance_reason": "missing or ambiguous exact transcript identity",
                   "new_operation_signals": {}, "operation_evidence": [], "skills_observed": []}
        rows.append({"task_id": task.meta["task_id"], "attempt_id": task.meta["attempt_id"],
                     "session_id": sid, "reported_task_status": task.meta["final_status"],
                     "integration_first_pass_reported": task.meta.get("integration_first_pass"), **row})
    matrix = Counter((r["old_classifier_outcome"], r["new_classifier_outcome"]) for r in rows)
    from agent_memory.skills import score_of
    by_skill: dict[str, dict[str, dict[str, str]]] = {}
    for row in rows:
        sid = row.get("session_id")
        for skill in row["skills_observed"]:
            pair = by_skill.setdefault(skill, {"old": {}, "new": {}})
            pair["old"][sid] = row["old_classifier_outcome"]
            pair["new"][sid] = row["new_classifier_outcome"]
    sensitivity = []
    for skill, pair in sorted(by_skill.items()):
        n_old, before = score_of(dict(Counter(pair["old"].values())))
        n_new, after = score_of(dict(Counter(pair["new"].values())))
        sensitivity.append({"skill": skill, "matched_sessions": len(pair["new"]),
                            "old_scored": n_old, "new_scored": n_new,
                            "old_cohort_score": before, "new_cohort_score": after})
    return {"format": "outcome-calibration/1", "observed_at": now.isoformat(), "limit": limit,
            "selected_tasks": len(rows), "unique_sessions": len({r["session_id"] for r in rows}),
            "changed_tasks": sum(r["changed"] for r in rows),
            "transition_matrix": [{"old": a, "new": b, "tasks": n} for (a,b),n in sorted(matrix.items())],
            "acceptance_matrix": [{"truth": "unknown", "old": a, "new": b, "tasks": n}
                                  for (a,b),n in sorted(matrix.items())],
            "skill_sensitivity": sensitivity, "rows": rows,
            "limitations": ["Old outcomes are reconstructed v5 classifier outputs, not a fresh database snapshot",
                            "Transition matrix is not a human-ground-truth confusion matrix",
                            "Task DONE and integration_first_pass are reported claims, not independent acceptance",
                            "Skill sensitivity is this exact transcript cohort, not the full production ranking"]}


def render(report: dict[str, Any]) -> str:
    lines = ["# Outcome calibration pilot", "", "Observed: " + report["observed_at"], "",
             f"Selected {report['selected_tasks']} explicit tasks across {report['unique_sessions']} session IDs. "
             f"Classifier outcome changed for {report['changed_tasks']} tasks.", "",
             "Old outcomes are reconstructed from v5 invocation semantics. These are not freshly read stored labels.",
             "The table is a classifier transition matrix. Independent human acceptance remains unknown; "
             "it is not a ground-truth accuracy claim.", "",
             "| Old classifier | New classifier | Tasks | Independent acceptance |",
             "|---|---|---:|---|"]
    for row in report["transition_matrix"]:
        lines.append(f"| {row['old']} | {row['new']} | {row['tasks']} | unknown |")
    lines += ["", "## Per-task evidence", "",
              "Evidence refs use exact transcript paths and parsed record numbers; no transcript text is exported.", "",
              "| Task | Old | New | Reported task result | Operation coverage | Acceptance |",
              "|---|---|---|---|---|---|"]
    for row in report["rows"]:
        lines.append(f"| {row['task_id']} | {row['old_classifier_outcome']} | {row['new_classifier_outcome']} | "
                     f"{row['reported_task_status']} | {row['operation_coverage']} | unknown |")
    lines += ["", "## Machine-readable evidence and cohort sensitivity", "", "```json",
              json.dumps(report, indent=2, sort_keys=True), "```", ""]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--parent-dir", type=Path, required=True)
    p.add_argument("--projects", type=Path, required=True)
    p.add_argument("--limit", type=int, default=30)
    args = p.parse_args()
    print(render(calibrate(args.parent_dir, args.projects, limit=args.limit)))


if __name__ == "__main__":
    main()
