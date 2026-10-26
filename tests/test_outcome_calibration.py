from __future__ import annotations

import json
from datetime import UTC, datetime

from agent_memory.outcome_calibration import calibrate, compare
from test_deviations_support import assistant, result, text, tool


def test_comparison_separates_classifier_transition_from_acceptance():
    ev = [assistant(tool("Bash", "push", command="git push origin main")),
          result("push", "rejected", 1, error=True), assistant(text("Status: DONE"), minute=2)]
    r = compare(ev, observed_at=datetime(2026, 11, 1, tzinfo=UTC))
    assert r["old_classifier_outcome"] == "shipped" and r["new_classifier_outcome"] == "failed"
    assert r["changed"] is True and r["acceptance"] == "unknown"
    assert r["operation_evidence"] == [{"call_id": "push", "call_line": 1, "operations": ["push_main"],
                                         "result_line": 2, "tool_error": True, "denied": False}]
    assert "git push" not in json.dumps(r)


def test_missing_transcript_is_unknown_not_a_negative_label(tmp_path):
    parent = tmp_path / "parent"
    (parent / "repo").mkdir(parents=True)
    (parent / "repo" / "tasks.jsonl").write_text(json.dumps({"id": "t", "session": "missing", "final_status": "DONE"}) + "\n")
    projects = tmp_path / "projects"
    projects.mkdir()
    r = calibrate(parent, projects, limit=30)
    assert r["selected_tasks"] == 1 and r["changed_tasks"] == 0
    assert r["transition_matrix"] == [{"old": "unknown", "new": "unknown", "tasks": 1}]
    assert r["rows"][0]["acceptance"] == "unknown"
