"""Delivery signals require matching terminal results; acceptance stays separate."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from agent_memory import amem_digest as D
from test_deviations_support import assistant, result, text, tool


def events(command: str, *, output: str = "ok", error: bool = False, denied: bool = False):
    return [assistant(tool("Bash", "operation-1", command=command)),
            result("operation-1", output, 1, error=error,
                   **({"toolDenialKind": "user"} if denied else {})),
            assistant(text("Status: DONE"), minute=2)]


@pytest.mark.parametrize("command,signal", [
    ("git push origin main", "push_main"),
    ("git push origin feature", "push"),
    ("gh pr create --fill", "pr_create"),
    ("gh pr merge 2 --merge", "pr_merge"),
    ("vercel deploy --prod", "deploy"),
    ("git checkout main && git merge feature", "merge_main"),
])
def test_delivery_waits_for_its_result(command, signal):
    tracker = D.SignalTracker()
    call, response, final = events(command)
    tracker.feed(call)
    assert signal not in tracker.signals
    assert tracker.signals["pending_" + signal] == 1
    tracker = D.SignalTracker(json.loads(json.dumps(tracker.state())))
    tracker.feed(response)
    tracker.feed(final)
    assert tracker.signals[signal] == 1
    assert "pending_" + signal not in tracker.signals
    outcome, evidence = D.outcome(tracker.signals, tracker.end, None)
    assert outcome in ("shipped", "pushed")
    assert evidence["method_version"] == 2 and evidence["acceptance"] == "unverified"


@pytest.mark.parametrize("command", ["git push origin main", "gh pr create --fill", "vercel deploy --prod"])
@pytest.mark.parametrize("error,denied,prefix", [(True, False, "failed_"), (True, True, "denied_")])
def test_failed_or_denied_delivery_never_becomes_success(command, error, denied, prefix):
    signals = D.outcome_signals(events(command, error=error, denied=denied))["signals"]
    signal = D.command_signals(command)[0]
    assert signal not in signals and signals[prefix + signal] == 1
    assert D.outcome(signals, "reply", None)[0] == "failed"


@pytest.mark.parametrize("command", [
    "git push origin main || true", "git push origin main; echo done",
    "git push origin main | tail -1", "true || git push origin main",
    "git push --dry-run origin main", "gh pr merge 2 --auto",
    "vercel deploy --prod --no-wait", "railway up --detach",
])
def test_masked_or_nonterminal_operation_stays_unknown(command):
    got = D.outcome_signals(events(command))["signals"]
    assert not (set(got) & D.SUCCESS_OPERATIONS)
    assert any(key.startswith("unknown_") for key in got)


def test_explicit_nested_nonzero_exit_overrides_tool_success():
    got = D.outcome_signals(events("git push origin main", output='{"exit_code": 1, "output":"rejected"}'))
    assert got["signals"] == {"failed_push_main": 1, "report_done": 1}


def test_live_process_result_is_not_confirmed_delivery():
    got = D.outcome_signals(events("git push origin main", output='{"session_id":42,"output":"running"}'))
    assert got["signals"] == {"unknown_push_main": 1, "report_done": 1}


def test_wrong_result_id_does_not_complete_delivery():
    tracker = D.SignalTracker()
    tracker.feed(assistant(tool("Bash", "push", command="git push origin main")))
    tracker.feed(result("another-call", "ok", 1))
    assert tracker.signals == {"pending_push_main": 1}


def test_background_request_and_interruption_are_not_success():
    tracker = D.SignalTracker()
    tracker.feed(assistant(tool("Bash", "push", command="git push origin main", run_in_background=True)))
    tracker.feed(result("push", "background task created", 1))
    assert tracker.signals == {"unknown_push_main": 1}
    tracker.feed(assistant(tool("Bash", "push2", command="git push origin feature")))
    tracker.feed({"type": "user", "message": {"content": "[Request interrupted by user]"}})
    assert tracker.operation_calls == {} and tracker.signals["unknown_push"] == 1


def test_legacy_invocation_counter_is_explicitly_unverified():
    tracker = D.SignalTracker({"signals": {"push_main": 1, "edit": 1}, "end": "reply"})
    assert tracker.signals == {"unknown_push_main": 1, "edit": 1}
    assert tracker.state()["method_version"] == 2


def test_partial_report_and_proven_commit_remain_compatible():
    ev = events("git push origin main", error=True)
    ev[-1] = assistant(text("Status: PARTIAL"), minute=2)
    got = D.outcome_signals(ev)
    assert D.outcome(got["signals"], got["end"], None)[0] == "partial"
    got = D.outcome_signals(events("git commit -m x && git push origin main", output="[main abc1234] x", error=True))
    assert got["signals"]["commit"] == 1
    assert "push_main" not in got["signals"]
    assert D.outcome(got["signals"], got["end"], None)[0] == "committed"
