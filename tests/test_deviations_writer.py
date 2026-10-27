"""The deviation writer's reply contract: `--json-schema` on the CLI call, the envelope's
`structured_output`, and the one in-call retry on a malformed reply. No database.

No test runs the real `claude`: the envelopes below follow the shape of the installed CLI
(2.1.308) result message: `structured_output` on a success result (with `result` set to its
JSON text when the model wrote no other text), and an `error_max_structured_output_retries`
result with `is_error: true`, an `errors` list, no `result`, printed on stdout with exit 1.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_memory import deviations, distill, rule_catalog
from test_deviations_support import FakeWriter, envelope, finding, setup_home  # noqa: F401

OFFERED = {"claude-md:global"}


def cli_envelope(structured: Any = None, *, result: str | None = None, subtype: str = "success",
                 **over: Any) -> str:
    """A `claude -p --output-format json --json-schema ...` result envelope."""
    env: dict[str, Any] = {
        "type": "result", "subtype": subtype, "is_error": subtype != "success", "duration_ms": 41210,
        "duration_api_ms": 40877, "num_turns": 2, "stop_reason": "end_turn",
        "session_id": "00000000-0000-4000-8000-000000000000", "total_cost_usd": 0.0412,
        "usage": {"input_tokens": 9100, "output_tokens": 640}, "modelUsage": {}, "permission_denials": [],
        "uuid": "00000000-0000-4000-8000-000000000001",
    }
    if subtype == "success":
        env["result"] = json.dumps(structured) if result is None else result
        if structured is not None:
            env["structured_output"] = structured
    else:
        env["errors"] = ["Failed to provide valid structured output after maximum retries"]
        env["terminal_reason"] = "structured_output_retry_exhausted"
    env.update(over)
    return json.dumps(env)


GOOD = {"deviations": [finding()]}
NULL_GRADE = {"deviations": [finding(followed=None)]}


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Any:
    def use(*replies: Any) -> FakeWriter:
        fake = FakeWriter(list(replies))
        monkeypatch.setattr(deviations, "call_writer", fake)
        return fake
    return use


# ------------------------------------------------------------------- the schema on the CLI call


def test_writer_argv_passes_the_reply_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(deviations.CLAUDE_ENV, raising=False)
    argv = deviations.writer_argv()
    i = argv.index("--json-schema")
    assert json.loads(argv[i + 1]) == deviations.REPLY_SCHEMA
    assert argv[:i] == distill.writer_argv(deviations.CLAUDE_ENV)  # the shared flags, unchanged
    # Only the deviation writer sends a schema; the lessons writer's call is as before.
    assert "--json-schema" not in distill.writer_argv()


def test_schema_requires_both_grades_as_numbers_never_null() -> None:
    item = deviations.REPLY_SCHEMA["properties"]["deviations"]["items"]
    assert {"followed_grade", "result_grade", "instruction_source", "instruction_ref", "turn",
            "what", "why", "why_category", "how"} <= set(item["required"])
    assert item["properties"]["why_category"] == {"type": "string", "enum": list(deviations.WHY_CATEGORIES)}
    assert list(deviations.WHY_CATEGORIES) == ["unclear", "wrong", "conflict", "missing_context",
                                               "tool_blocked", "ignored"]
    for grade in ("followed_grade", "result_grade"):
        assert item["properties"][grade] == {"type": "number", "minimum": 0, "maximum": 1}
    assert item["properties"]["turn"]["type"] == ["integer", "null"]
    assert deviations.REPLY_SCHEMA["properties"]["deviations"]["maxItems"] == deviations.MAX_FINDINGS
    assert deviations.REPLY_SCHEMA["required"] == ["deviations"]


# ------------------------------------------------------------------- the envelope


def test_reply_text_prefers_structured_output() -> None:
    # The CLI sets `result` to the JSON text of structured_output when the model wrote nothing else.
    assert json.loads(deviations.reply_text(cli_envelope(GOOD))) == GOOD
    # Model text next to the tool call: `result` is that text, the data is in structured_output.
    assert json.loads(deviations.reply_text(cli_envelope(GOOD, result="Here are the deviations."))) == GOOD


def test_reply_text_falls_back_to_result_without_structured_output() -> None:
    # Older CLI or a run that ended without the StructuredOutput call: the text is parsed as before.
    assert deviations.reply_text(envelope(GOOD)) == json.dumps(GOOD)


def test_reply_text_sorts_malformed_from_failed() -> None:
    with pytest.raises(deviations.FindingError, match="no reply matched the schema: Failed to provide valid"):
        deviations.reply_text(cli_envelope(subtype="error_max_structured_output_retries"))
    with pytest.raises(deviations.FindingError, match="not a JSON envelope"):
        deviations.reply_text("Sorry, I cannot do that.")
    with pytest.raises(deviations.FindingError, match="neither structured_output nor result"):
        deviations.reply_text(cli_envelope(None, result=""))
    # A failed run (API error, auth): a writer error, not a malformed reply.
    with pytest.raises(distill.WriterError, match="CLI reported an error: API Error: 529 overloaded"):
        deviations.reply_text(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                          "result": "API Error: 529 overloaded"}))


def test_a_null_grade_in_structured_output_is_still_skipped_per_item() -> None:
    # The schema forbids it; if one slips through (no validation), the scan's own check skips that item.
    reasons: list[str] = []
    found, skipped = deviations.parse_reply(deviations.reply_text(cli_envelope(
        {"deviations": [finding(followed=None), finding(turn=2)]})), OFFERED, reasons)
    assert [f.turn for f in found] == [2] and skipped == 1
    assert reasons == ["deviation 0: followed_grade must be a number in 0..1, got None"]


# ------------------------------------------------------------------- one in-call retry


def test_malformed_then_valid_reply_succeeds_via_the_retry(calls: Any) -> None:
    w = calls("model reply that is not JSON at all", cli_envelope(GOOD))
    findings, reasons = deviations.write("PROMPT", OFFERED, timeout=5)
    assert [f.instruction_ref for f in findings] == ["Explaining"] and reasons == []
    assert len(w.prompts) == 2
    assert w.prompts[0] == "PROMPT"
    assert w.prompts[1].startswith("PROMPT\n\n=== Note from the scanner (not part of the session) ===")
    assert "Your previous reply was rejected: CLI output is not a JSON envelope" in w.prompts[1]


def test_null_grade_only_reply_is_retried_with_the_error(calls: Any) -> None:
    # The 2026-10-18 failure: one item, `"followed_grade": null`. Now the call is retried once.
    w = calls(envelope(NULL_GRADE), cli_envelope(GOOD))
    findings, _ = deviations.write("PROMPT", OFFERED, timeout=5)
    assert len(findings) == 1 and len(w.prompts) == 2
    assert ("rejected: deviation 0: followed_grade must be a number in 0..1, got None"
            in w.prompts[1])


def test_schema_exhausted_then_valid_succeeds(calls: Any) -> None:
    w = calls(cli_envelope(subtype="error_max_structured_output_retries"), cli_envelope(GOOD))
    assert len(deviations.write("PROMPT", OFFERED, timeout=5)[0]) == 1
    assert len(w.prompts) == 2


def test_malformed_twice_is_still_an_error_and_costs_one_extra_call(calls: Any) -> None:
    w = calls(envelope(NULL_GRADE), envelope(NULL_GRADE), cli_envelope(GOOD))
    with pytest.raises(deviations.FindingError) as exc:
        deviations.write("PROMPT", OFFERED, timeout=5)
    assert str(exc.value) == "deviation 0: followed_grade must be a number in 0..1, got None (after 1 retry)"
    assert len(w.prompts) == 2  # never a third call


def test_a_failed_run_is_not_retried_in_the_call(calls: Any) -> None:
    w = calls(distill.WriterError("timed out after 5s"), cli_envelope(GOOD))
    with pytest.raises(distill.WriterError):
        deviations.write("PROMPT", OFFERED, timeout=5)
    assert len(w.prompts) == 1


def test_a_reply_with_some_valid_items_is_kept_not_retried(calls: Any) -> None:
    w = calls(cli_envelope({"deviations": [finding(followed=None), finding(turn=2)]}), cli_envelope(GOOD))
    findings, reasons = deviations.write("PROMPT", OFFERED, timeout=5)
    assert [f.turn for f in findings] == [2] and len(reasons) == 1
    assert len(w.prompts) == 1


def test_retry_note_is_redacted_and_one_line() -> None:
    note = deviations.retry_prompt("P", "bad\nvalue sk-ant-api03-" + "a" * 40)
    assert "sk-ant-api03" not in note and "bad value" in note


# ------------------------------------------------------------------- the real call path, fake CLI


def _fake_cli(tmp_path: Path, stdout: str, code: int = 0) -> Path:
    exe = tmp_path / "fake-claude"
    (tmp_path / "reply.json").write_text(stdout, encoding="utf-8")
    exe.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, pathlib\n"
        "d = pathlib.Path(__file__).parent\n"
        "(d / 'argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        "(d / 'stdin.txt').write_text(sys.stdin.read())\n"
        "sys.stdout.write((d / 'reply.json').read_text())\n"
        f"sys.exit({code})\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_call_writer_sends_the_schema_and_reads_structured_output(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(deviations.CLAUDE_ENV, str(_fake_cli(tmp_path, cli_envelope(GOOD))))
    findings, _ = deviations.write("PROMPT", OFFERED, timeout=30)
    assert len(findings) == 1
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert json.loads(argv[argv.index("--json-schema") + 1]) == deviations.REPLY_SCHEMA
    assert (tmp_path / "stdin.txt").read_text() == "PROMPT"


# Two realistic writer items: an agent that skipped a clear rule (ignored) and one that misread a
# vague one (unclear). Also the items of the evidence run in the worker report.
REALISTIC = {"deviations": [
    {"instruction_source": "claude-md:global", "instruction_ref": "verify-before-done", "turn": 14,
     "what": "The agent called the UI fix done after the unit tests passed, without opening the page.",
     "why": "The rule is clear and applied; the agent skipped the browser check to finish faster.",
     "why_category": "ignored",
     "how": "Near the end of the session; the broken layout on the narrow width shipped and needed a second fix.",
     "followed_grade": 0.1, "result_grade": 0.3},
    {"instruction_source": "skill:dispatch", "instruction_ref": "brief", "turn": 3,
     "what": "The parent sent workers briefs without a done-when line.",
     "why": "The skill says a brief needs a DoD but never says what one looks like for a docs-only task.",
     "why_category": "unclear",
     "how": "Early in the session; one worker asked back and lost about ten minutes, the result was fine.",
     "followed_grade": 0.5, "result_grade": 0.8},
]}
REALISTIC_SOURCES = [
    deviations.LoadedSource("claude-md:global", "", [rule_catalog.Rule("verify-before-done", "Verify Before Done")],
                            "a" * 64),
    deviations.LoadedSource("skill:dispatch", "", [rule_catalog.Rule("brief", "Brief")], "b" * 64),
]


def test_realistic_reply_end_to_end_reaches_jev_as_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The real call path with a fake CLI: envelope -> parse -> resolve -> Jev state.
    monkeypatch.setenv(deviations.CLAUDE_ENV, str(_fake_cli(tmp_path, cli_envelope(REALISTIC))))
    findings, reasons = deviations.write("PROMPT", {s.name for s in REALISTIC_SOURCES}, timeout=30)
    assert reasons == []
    ignored, unclear = deviations.resolve(findings, REALISTIC_SOURCES)
    assert (ignored.rule_id, ignored.why_category, unclear.rule_id, unclear.why_category) == \
        ("verify-before-done", "ignored", "brief", "unclear")
    state_i = deviations.jev_state(ignored, "worker", 2, "pushed", "worker")
    state_u = deviations.jev_state(unclear, "parent", 0, "shipped")
    assert "why_category: ignored (the writer's view of why: the agent ignored a clear instruction)" in state_i
    assert "why_category: unclear (the writer's view of why: the instruction is unclear)" in state_u
    for state, item in ((state_i, REALISTIC["deviations"][0]), (state_u, REALISTIC["deviations"][1])):
        for text in (item["what"], item["why"], item["how"]):
            assert text not in state and text[:30] not in state
    # Same labels, different category: a different Jev state (a different fixture key).
    assert state_i != deviations.jev_state(replace(ignored, why_category="unclear"), "worker", 2, "pushed", "worker")


def test_call_writer_keeps_the_error_envelope_of_exit_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI exits 1 on an is_error result and prints the envelope: the scan sees a malformed reply.
    exhausted = cli_envelope(subtype="error_max_structured_output_retries")
    monkeypatch.setenv(deviations.CLAUDE_ENV, str(_fake_cli(tmp_path, exhausted, code=1)))
    out = deviations.call_writer("PROMPT", timeout=30)
    with pytest.raises(deviations.FindingError, match="no reply matched the schema"):
        deviations.reply_text(out)


def test_run_cli_exit_1_without_envelope_still_raises(tmp_path: Path) -> None:
    exe = _fake_cli(tmp_path, "boom", code=1)
    with pytest.raises(distill.WriterError, match="exit 1: boom"):
        distill.run_cli([str(exe)], "P", timeout=30, keep_error_envelope=True)
    exe = _fake_cli(tmp_path, cli_envelope(subtype="error_max_structured_output_retries"), code=1)
    with pytest.raises(distill.WriterError, match="exit 1"):
        distill.run_cli([str(exe)], "P", timeout=30)  # the lessons writer: unchanged
