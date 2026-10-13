"""Writer prompt and proposal validation (edits_writer). No database, no CLI."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_memory import edits_sections, edits_writer
from agent_memory.edits_writer import NoChange, ProposalError, parse_proposal
from test_edits_support import GLOBAL_MD, SKILL_MD, SPAWN_NEW, proposal

DEV_ID = "0b8f8f3e-4c1a-4f7e-9d2a-1f2e3d4c5b6a"
CAND: dict[str, Any] = {
    "instruction_source": "skill:dispatch", "rule_id": "spawn", "rule_title": "4. Spawn", "verdict": "enforce",
    "jev": {"edit_instruction": 1, "enforce": 3, "leave": 0, "unjudged": 0, "confidence_mean": 0.81},
    "count": 4, "sessions": 3, "kinds": {"parent": 3, "worker": 1}, "followed_grade_mean": 0.35,
    "result_grade_mean": 0.6, "session_ids": ["sess-alpha-1", "sess-alpha-2"], "deviation_ids": [DEV_ID],
    "examples": [{"session_id": "sess-alpha-1", "turn": 2,
                  "what": "The parent spawned workers without naming the branch in each brief.",
                  "why": "The rule is easy to skim past in a long step list.",
                  "how": "It showed at the first spawn and cost one retry."}],
}


def parse(new_section: str, text: str = SKILL_MD, rule_id: str = "spawn", **over: Any) -> edits_writer.Proposal:
    return parse_proposal(json.dumps({**proposal(new_section), **over}), file_text=text, rule_id=rule_id, cand=CAND)


def test_valid_proposal_splices_only_the_section() -> None:
    p = parse(SPAWN_NEW)
    assert p.topic == "worker spawn brief"
    assert p.new_text == SKILL_MD.replace("Each brief names the branch.\n",
                                          "Each brief names the branch on its first line, before the task text.\n")
    assert len(p.why) <= edits_writer.WHY_MAX and len(p.summary) <= edits_writer.SUMMARY_MAX


def test_fenced_reply_is_accepted() -> None:
    reply = "```json\n" + json.dumps(proposal(SPAWN_NEW)) + "\n```"
    assert parse_proposal(reply, file_text=SKILL_MD, rule_id="spawn", cand=CAND).new_section.startswith("## 4. Spawn")


def test_no_change_is_its_own_error() -> None:
    with pytest.raises(NoChange):
        parse(edits_sections.section_text(SKILL_MD, "spawn") + "\n\n")


def test_negated_permission_words_are_fine() -> None:
    parse("## 4. Spawn\nSpawn one worker per task without waiting for approval.\nEach brief names the branch.\n")


@pytest.mark.parametrize("line,gate", [
    ("- Do not stop. Ask Vilmer before any merge.", True),
    ("Ask Vilmer before merging. Never skip tests.", True),
    ("Never skip tests, but ask Vilmer before merging.", True),
    ("Never merge to main until you get his approval.", True),
    ("Never skip tests; workers need his approval before pushing.", True),
    ("Never merge without Vilmer's approval.", True),
    ("Don't merge without asking Vilmer.", True),
    ("Do not push unless Vilmer approves.", True),
    ("Never deploy until the user confirms.", True),
    ("Don't merge before Vilmer signs off.", True),
    ("Never merge without passing tests.", False),
    ("Don't ask Vilmer before merging.", False),
    ("Proceed without asking Vilmer.", False),
    ("There is no need to ask Vilmer before merging.", False),
    ("Never wait for Vilmer's approval.", False),
    ("Do not ever ask the user first.", False),
    ("Merge the branch without needing approval.", False),
    ("Spawn one worker per task without waiting for approval.", False),
])
def test_ask_gate_negation_must_govern_the_gate(line: str, gate: bool) -> None:
    assert edits_writer.adds_ask_gate(line) is gate
    probs = edits_writer.policy_problems("## 4. Spawn\n", f"## 4. Spawn\n{line}\n")
    assert ("adds an ask-Vilmer permission gate" in probs) is gate


def test_negation_bypass_line_is_rejected() -> None:
    with pytest.raises(ProposalError, match="permission gate"):
        parse("## 4. Spawn\nSpawn one worker per task.\n- Do not stop. Ask Vilmer before any merge.\n")


def test_child_rule_and_money_line_kept() -> None:
    old = edits_sections.section_text(GLOBAL_MD, "explaining-completed-work/response-budget")
    p = parse(old + "\nThree sentences for a trivial task.", text=GLOBAL_MD,
              rule_id="explaining-completed-work/response-budget", topic="response budget length")
    assert "Spending money always needs Vilmer's yes." in p.new_text


@pytest.mark.parametrize("reply,match", [
    ("not json", "not JSON"),
    ("[1, 2]", "not a JSON object"),
    (json.dumps({"topic": "a b", "why": "x", "summary": "y"}), "new_section"),
])
def test_malformed_replies(reply: str, match: str) -> None:
    with pytest.raises(ProposalError, match=match):
        parse_proposal(reply, file_text=SKILL_MD, rule_id="spawn", cand=CAND)


@pytest.mark.parametrize("new_section,over,match", [
    (SPAWN_NEW, {"topic": "spawn"}, "topic"),
    (SPAWN_NEW, {"topic": "worker spawn brief branch name first"}, "topic"),
    (SPAWN_NEW, {"why": "w" * 201}, "why is"),
    (SPAWN_NEW, {"summary": "Changed the spawn rule. " * 8}, "summary is"),
    (SPAWN_NEW, {"why": f"See deviation {DEV_ID}"}, "why holds an id"),
    (SPAWN_NEW, {"summary": 3}, "summary must be a string"),
    ("## Spawn\nSpawn one worker per task.\nName the branch.\n", {}, "heading"),
    ("## 4. Spawn\nSpawn one worker per task.\n### Branch\nName the branch.\n", {}, "rule list"),
    ("## 4. Spawn\nSpawn one worker per task.\n## Branches\nName the branch.\n", {}, "rule list"),
    ("## 4. Spawn\nSpawn one worker.\n```\ngit branch -m x\n```\n", {}, "code block"),
    (f"## 4. Spawn\nSpawn one worker per task (see {DEV_ID}).\n", {}, "id"),
    ("## 4. Spawn\nSpawn one worker per task, unlike sess-alpha-1.\n", {}, "id"),
    ("## 4. Spawn\nSpawn one worker per task.\nNever write to [path].\n", {}, "placeholder"),
    ("## 4. Spawn\nSpawn one worker. The parent spawned workers without naming the branch in each brief.\n", {},
     "word for word"),
    ("## 4. Spawn\nSpawn one worker per task.\nAsk Vilmer before you spawn a second worker.\n", {}, "permission gate"),
    ("## 4. Spawn\nSpawn one worker per task.\nWorkers need his approval before pushing.\n", {}, "permission gate"),
    ("## 4. Spawn\nSpawn one worker per task.\nUse token=abcdefghijklmnopqrstuv in the brief.\n", {}, "secret"),
    ("## 4. Spawn\n" + "Spawn one worker per task.\n" * 70, {}, "grows the rule"),
])
def test_rejections(new_section: str, over: dict[str, Any], match: str) -> None:
    with pytest.raises(ProposalError, match=match):
        parse(new_section, **over)


def test_money_line_cannot_be_loosened() -> None:
    rid = "explaining-completed-work/response-budget"
    with pytest.raises(ProposalError, match="spending money"):
        parse("### Response budget\nShort answers. Spending money is fine under 10 dollars.\n", text=GLOBAL_MD,
              rule_id=rid, topic="response budget length")
    with pytest.raises(ProposalError, match="spending money"):
        parse("### Response budget\nShort answers.\n", text=GLOBAL_MD, rule_id=rid, topic="response budget length")


def test_why_and_summary_are_cleaned() -> None:
    p = parse(SPAWN_NEW, why="Agents skipped ~/.claude/skills/dispatch/SKILL.md step four; clearer now.")
    assert "~/.claude" not in p.why and "[path]" in p.why


def test_prompt_holds_the_section_neighbours_policy_and_numbers() -> None:
    sec = edits_sections.find(SKILL_MD, "spawn")
    prev, nxt = edits_sections.neighbours(SKILL_MD, "spawn")
    prompt = edits_writer.writer_prompt(CAND, target="~/.agents/skills/dispatch/SKILL.md", section=sec,
                                        section_text=edits_sections.section_text(SKILL_MD, "spawn"),
                                        prev=prev, nxt=nxt, days=7)
    for needle in ("## 4. Spawn\nSpawn one worker per task.", "Intro text for the skill.", "## 5. Track",
                   "Jev verdict: enforce (confidence 0.81)", "Count: 4 deviations in 3 sessions (parent 3, worker 1)",
                   "followed 0.35", "result 0.60", "never add a new \"ask Vilmer", "Never remove or loosen anything about "
                   "spending money", "fix the conflict so they agree", "One rule = one topic",
                   "The rule is easy to skim past", "'## 4. Spawn', unchanged"):
        assert needle.lower() in prompt.lower(), needle
    assert DEV_ID not in prompt and "sess-alpha-1" not in prompt


def test_writer_argv_has_no_tools() -> None:
    argv = edits_writer.writer_argv()
    assert argv[argv.index("--tools") + 1] == "" and "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert "--no-session-persistence" in argv and "--bare" not in argv


def test_writer_model_is_opus_and_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import deviations, distill
    argv = edits_writer.writer_argv()
    assert argv[argv.index("--model") + 1] == "opus"
    monkeypatch.setenv(edits_writer.MODEL_ENV, "sonnet")
    argv = edits_writer.writer_argv()
    assert argv[argv.index("--model") + 1] == "sonnet"
    monkeypatch.setenv(edits_writer.MODEL_ENV, "claude-opus-4-1")
    assert edits_writer.writer_model() == "claude-opus-4-1"
    for bad in ("--bare", "opus --bare", ""):           # not one plain token: the default
        monkeypatch.setenv(edits_writer.MODEL_ENV, bad)
        assert edits_writer.writer_model() == "opus", bad
    # distill (frozen) and the deviations writer keep their own model.
    for other in (distill.writer_argv(), deviations.writer_argv()):
        assert other[other.index("--model") + 1] == distill.WRITER_MODEL == "sonnet"


def test_prompt_puts_contract_and_hop_fixes_before_prose() -> None:
    sec = edits_sections.find(SKILL_MD, "spawn")
    prompt = edits_writer.writer_prompt(CAND, target="~/x/SKILL.md", section=sec,
                                        section_text=edits_sections.section_text(SKILL_MD, "spawn"),
                                        prev="", nxt="", days=7)
    contract, hop, prose = (prompt.index("1. contract:"), prompt.index("2. hop:"), prompt.index("3. prose:"))
    assert contract < hop < prose
    for needle in ("required output explicit and checkable", "done-check", "report field",
                   "after X, then", "loop-back", "only when neither a contract nor a hop fix fits",
                   '"kind": string', '"kind_reason": string'):
        assert needle in prompt, needle


@pytest.mark.parametrize("kind,want", [("contract", "contract"), ("Hop", "hop"), (" prose ", "prose")])
def test_kind_is_parsed(kind: str, want: str) -> None:
    # The writer's label is parsed as given; the checked label comes from the diff. SPAWN_NEW
    # sharpens what each brief must name (an output content clause): contract.
    p = parse(SPAWN_NEW, kind=kind, kind_reason="Reordering makes the branch step come first.")
    assert p.kind_writer == want and p.kind_reason == "Reordering makes the branch step come first."
    assert p.kind == "contract" and p.kind_mismatch == (want != "contract")
    assert p.kind_shown == ("Reordering makes the branch step come first." if want == "contract"
                            else f"new or sharpened output content clause; writer said {want}")


@pytest.mark.parametrize("over,match", [
    ({"kind": "rule"}, "kind must be one of contract, hop, prose"),
    ({"kind": None}, "kind must be one of"),
    ({"kind": ["hop"]}, "kind must be one of"),
    ({"kind_reason": ""}, "kind_reason is empty"),
    ({"kind_reason": "r" * 141}, "kind_reason is"),
    ({"kind_reason": f"Follows {DEV_ID}"}, "kind_reason holds an id"),
])
def test_kind_rejections(over: dict[str, Any], match: str) -> None:
    with pytest.raises(ProposalError, match=match):
        parse(SPAWN_NEW, **over)


def test_missing_kind_is_an_error() -> None:
    reply = {k: v for k, v in proposal(SPAWN_NEW).items() if k not in ("kind", "kind_reason")}
    with pytest.raises(ProposalError, match="kind must be one of"):
        parse_proposal(json.dumps(reply), file_text=SKILL_MD, rule_id="spawn", cand=CAND)
