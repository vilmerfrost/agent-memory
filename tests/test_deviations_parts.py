"""Deviation finder parts without a database: digest, redaction, cap, instruction collection,
writer output validation and the Jev state."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_memory import deviations, distill, questions, rule_catalog
from agent_memory.deviations import FindingError, parse_findings
from agent_memory.sessions_ingest import SessionRow
from test_deviations_support import (  # noqa: F401
    Home,
    assistant,
    finding,
    result,
    setup_home,
    text,
    tool,
    user,
)

OFFERED = {"claude-md:global", "skill:dispatch"}


# --------------------------------------------------------------------------- digest


def test_digest_shapes_turns_tools_errors_and_denials() -> None:
    events = [
        user("## Task\nDo the thing.", 0),
        assistant(text("On it."), tool("Bash", "b1", command="pytest -q"), minute=1),
        result("b1", "3 failed", 2, error=True),
        assistant(tool("Bash", "b2", command="cd x && make"), minute=3),
        result("b2", "nope", 3, toolDenialKind="permission-rule"),
        user("<command-name>/handoff</command-name>", 4),
        user("[Request interrupted by user]", 5),
        user("<system-reminder>ignore me</system-reminder>", 6),
        {"type": "attachment", "attachment": {"type": "hook_blocking_error", "hookEvent": "Stop",
                                              "blockingError": "no Next: line"}},
        user("Thanks, now ship it.", 7),
        assistant({"type": "thinking", "thinking": "private"}, minute=8),
    ]
    lines = deviations.digest_lines(events)
    assert lines == [
        "[turn 1] USER: ## Task\nDo the thing.",
        "ASSISTANT: On it.",
        "TOOL Bash: pytest -q",
        "ERROR Bash: 3 failed",
        "TOOL Bash: cd x && make",
        "DENIED Bash (permission-rule)",
        "[turn 2] COMMAND /handoff",
        "INTERRUPTED by the user",
        "HOOK BLOCKED Stop: no Next: line",
        "[turn 3] USER: Thanks, now ship it.",
    ]


def test_digest_scheduled_task_start_is_one_line() -> None:
    lines = deviations.digest_lines([user('<scheduled-task name="nightly-check" file="/x">\nlong prompt', 0)])
    assert lines == ["[turn 1] SCHEDULED TASK nightly-check started (its prompt is in the instructions)"]


def test_digest_activity_before_the_first_prompt_is_turn_1() -> None:
    # A resumed session: tool calls and a reply before any typed prompt (the writer used to cite turn 0).
    events = [
        assistant(tool("Skill", "s1", skill="handoff", args="resume"), minute=0),
        assistant(text("I resumed the handoff."), minute=1),
        user("Fix the bug.", 2),
        assistant(text("Fixed."), minute=3),
        user("Now ship it.", 4),
    ]
    lines = deviations.digest_lines(events)
    assert lines[0] == "[turn 1] START: activity before the first typed prompt"
    assert "[turn 2] USER: Fix the bug." in lines and "[turn 3] USER: Now ship it." in lines
    assert [line for line in lines if line.startswith("[turn")][1:] == ["[turn 2] USER: Fix the bug.",
                                                                        "[turn 3] USER: Now ship it."]


def test_digest_redacts_credentials() -> None:
    secrets = ["sk-ant-api03-abcdefghijklmnop", "ghp_abcdefghijklmnopqrstuvwx", "AKIAABCDEFGHIJKLMNOP",
               "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.c2lnbmF0dXJl"]
    events = [user("keys: " + " ".join(secrets), 0),
              assistant(tool("Bash", "b1", command="export API_KEY=abcdef1234567890abcd"), minute=1),
              result("b1", "postgresql://user:hunter2pass@db.example.com/x", 1, error=True)]
    digest = deviations.build_digest(events)
    for s in [*secrets, "abcdef1234567890abcd", "hunter2pass"]:
        assert s not in digest, s
    assert digest.count("[redacted]") >= 5


def test_digest_cap_keeps_head_and_tail() -> None:
    events = [user(f"prompt number {i} " + "y" * 900, i) for i in range(200)]
    digest = deviations.build_digest(events, limit=20_000)
    assert len(digest) <= 20_000
    assert digest.startswith("[turn 1] USER: prompt number 0 ")
    assert "prompt number 199 " in digest  # the tail (the outcome) is kept
    assert "chars cut from the middle" in digest
    assert "prompt number 100 " not in digest


def test_first_user_message_keeps_a_long_brief_later_ones_are_short() -> None:
    brief = "B" * 5_000
    lines = deviations.digest_lines([user(brief, 0), user("C" * 5_000, 1)])
    assert len(lines[0]) > 5_000 and lines[1].endswith(" [...]") and len(lines[1]) < 1_600


# --------------------------------------------------------------------------- instructions


def _row(home: Home, kind: str, skills: list[str], **meta: object) -> SessionRow:
    return SessionRow("s1", kind, "demo-repo", "-work-demo-repo", None, None, 0, skills, [],
                      {"cwd": str(home.repo), **meta})


def test_instructions_for_a_parent_with_skills(setup_home: Home) -> None:  # noqa: F811
    row = _row(setup_home, "parent", ["dispatch", "anthropic-skills:docs", "missing-skill"])
    loaded = deviations.load_instructions(deviations.instruction_sources(row))
    names = [n for n, _ in loaded]
    assert names == ["claude-md:global", "parent-md", "skill:dispatch", "claude-md:demo-repo"]
    repo_text = dict(loaded)["claude-md:demo-repo"]
    assert "Run tests with pytest." in repo_text and "Never touch the hosted DB." in repo_text
    assert all("do-not-read-me" not in t for _, t in loaded)  # .env is never read


def test_instructions_for_worker_and_routine(setup_home: Home) -> None:  # noqa: F811
    w = deviations.instruction_sources(_row(setup_home, "worker", [], agent_type="worker"))
    r = deviations.instruction_sources(_row(setup_home, "routine", [], routine="nightly-check"))
    assert [s.name for s in w] == ["claude-md:global", "agent:worker", "claude-md:demo-repo"]
    assert [s.name for s in r] == ["claude-md:global", "routine:nightly-check", "claude-md:demo-repo"]


def test_routine_falls_back_to_the_tag_file_only_inside_the_claude_home(setup_home: Home,  # noqa: F811
                                                                         tmp_path: Path) -> None:
    alt = setup_home.claude / "old-tasks" / "gone-task" / "SKILL.md"
    alt.parent.mkdir(parents=True)
    alt.write_text("# Gone task\n")
    outside = tmp_path / "elsewhere" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("# Outside\n")
    inside = deviations.instruction_sources(_row(setup_home, "routine", [], routine="gone-task", routine_file=str(alt)))
    out = deviations.instruction_sources(_row(setup_home, "routine", [], routine="gone-task",
                                              routine_file=str(outside)))
    assert [s.paths for s in inside if s.name == "routine:gone-task"] == [[alt]]
    assert "routine:gone-task" not in [s.name for s in out]


def test_instructions_follow_a_worktree_to_the_main_checkout(setup_home: Home, tmp_path: Path) -> None:  # noqa: F811
    wt = tmp_path / "worktrees" / "demo-repo" / "wt-1"
    wt.mkdir(parents=True)
    (wt / "CLAUDE.md").write_text("# tracked copy\n")
    (wt / ".git").write_text(f"gitdir: {setup_home.repo}/.git/worktrees/wt-1\n")
    (setup_home.repo / ".git" / "worktrees" / "wt-1").mkdir(parents=True)
    row = SessionRow("s1", "worker", "demo-repo", "p", None, None, 0, [], [], {"cwd": str(wt)})
    (src,) = [s for s in deviations.instruction_sources(row) if s.name == "claude-md:demo-repo"]
    assert src.paths == [wt / "CLAUDE.md", setup_home.repo / "CLAUDE.md", setup_home.repo / "CLAUDE.local.md"]


def test_env_files_and_path_tricks_are_refused(setup_home: Home) -> None:  # noqa: F811
    assert not deviations._safe(setup_home.repo / ".env")
    row = _row(setup_home, "chat", ["../../../work/demo-repo/.env", ".env"])
    assert [s.name for s in deviations.instruction_sources(row)] == ["claude-md:global", "claude-md:demo-repo"]


def test_instructions_are_capped(setup_home: Home) -> None:  # noqa: F811
    (setup_home.claude / "CLAUDE.md").write_text("z" * (deviations.INSTRUCTION_MAX + 5_000))
    loaded = deviations.load_instructions(deviations.instruction_sources(_row(setup_home, "chat", [])))
    assert len(loaded[0][1]) < deviations.INSTRUCTION_MAX + 100 and loaded[0][1].endswith("[... cut ...]")


# --------------------------------------------------------------------------- writer output


def _reply(*items: dict[str, object]) -> str:
    return json.dumps({"deviations": list(items)})


def test_parse_good_reply() -> None:
    (f,) = parse_findings(_reply(finding()), OFFERED)
    assert (f.instruction_source, f.instruction_ref, f.turn, f.followed_grade, f.result_grade) == \
        ("claude-md:global", "Explaining", 1, 0.3, 0.8)
    assert parse_findings('{"deviations": []}', OFFERED) == []
    assert len(parse_findings("```json\n" + _reply(finding()) + "\n```", OFFERED)) == 1


@pytest.mark.parametrize(("reply", "message"), [
    ("not json", "not JSON"),
    ('{"other": []}', "no 'deviations' list"),
    (_reply(finding(source="skill:unknown")), "was not offered"),
    (_reply(finding(followed=1.5)), "followed_grade must be a number in 0..1"),
    (_reply(finding(result_=True)), "result_grade must be a number"),
    (_reply(finding(turn=True)), "turn must be a positive integer"),
    (_reply(finding(turn=-1)), "turn must be a positive integer"),
    (_reply(finding(what=7)), "what must be a string"),
    (_reply(finding(why="```\nrm -rf x\n```")), "code block"),
    (_reply(finding(how='"the exact words the user typed in that turn"')), "empty after removing"),
    (_reply(*[finding(ref=f"r{i}") for i in range(9)]), "max 8"),
])
def test_parse_rejects_malformed(reply: str, message: str) -> None:
    with pytest.raises(FindingError, match=message):
        parse_findings(reply, OFFERED)


def test_parse_skips_a_null_grade_and_keeps_the_rest() -> None:
    reply = _reply(finding(followed_grade=None), finding(ref="Brief", turn=2),
                   finding(ref="Dod", result_grade=None))
    findings, skipped = deviations.parse_reply(reply, OFFERED)
    assert [f.instruction_ref for f in findings] == ["Brief"] and skipped == 2
    # A missing grade is skipped by the scan's parse_reply too; strict parse_findings rejects it.
    item = finding()
    del item["followed_grade"]
    assert deviations.parse_reply(_reply(item, finding(ref="Brief", turn=2)), OFFERED)[1] == 1
    with pytest.raises(FindingError, match="followed_grade must be a number in 0..1, got None"):
        parse_findings(_reply(item), OFFERED)


# 2026-10-15 and 2026-10-17: the writer returned `"followed_grade": null` on one item and the
# whole session (all its valid items) was dropped. parse_reply skips any malformed item and
# names it; its grade is never filled in.
_MISSING = object()
BAD_GRADES = [None, _MISSING, "0.5", "high", -0.1, 1.5, True, float("nan")]
BAD_IDS = ["null", "missing", "string-number", "string-word", "below-0", "above-1", "bool", "nan"]


def _bad_item(name: str, value: object) -> dict[str, object]:
    item = finding()
    if value is _MISSING:
        del item[name]
    else:
        item[name] = value
    return item


@pytest.mark.parametrize("name", ["followed_grade", "result_grade"])
@pytest.mark.parametrize("value", BAD_GRADES, ids=BAD_IDS)
def test_strict_parse_still_rejects_a_bad_grade(name: str, value: object) -> None:
    with pytest.raises(FindingError, match=f"deviation 0: {name} must be a number in 0..1"):
        parse_findings(_reply(_bad_item(name, value)), OFFERED)


@pytest.mark.parametrize("name", ["followed_grade", "result_grade"])
@pytest.mark.parametrize("value", BAD_GRADES, ids=BAD_IDS)
def test_parse_reply_skips_a_bad_grade_and_keeps_the_rest(name: str, value: object) -> None:
    reasons: list[str] = []
    found, skipped = deviations.parse_reply(
        _reply(_bad_item(name, value), finding("skill:dispatch", "Brief", 2, 0.1, 0.4)), OFFERED, reasons)
    assert [(f.instruction_source, f.instruction_ref, f.followed_grade, f.result_grade) for f in found] == \
        [("skill:dispatch", "Brief", 0.1, 0.4)]  # the valid item, its own grades
    got = None if value is _MISSING else value
    assert skipped == 1 and reasons == [f"deviation 0: {name} must be a number in 0..1, got {got!r}"]


def test_parse_reply_skips_any_bad_item_but_not_a_bad_reply() -> None:
    good = finding(turn=9)
    items = ["not an object", finding(source="skill:nope"), finding(source=["claude-md:global"]),
             finding(turn=-1), finding(what=7), finding(why="```\nrm -rf x\n```"), good]
    reasons: list[str] = []
    (f,), skipped = deviations.parse_reply(_reply(*items), OFFERED, reasons)
    assert f.turn == 9 and skipped == 6
    assert [r.split(":")[0].split(" is ")[0] for r in reasons] == [f"deviation {i}" for i in range(6)]
    assert "instruction_source 'skill:nope' was not offered" in reasons[1]
    assert "was not offered" in reasons[2]  # an unhashable source is a skipped item, not a crash
    for reply in ("not json", '{"other": []}', _reply(*[finding(ref=f"r{i}") for i in range(9)])):
        with pytest.raises(FindingError):
            deviations.parse_reply(reply, OFFERED, [])


BAD_WHY_CATEGORIES = [_MISSING, None, "", "lazy", "Ignored", 3, ["ignored"]]
BAD_WHY_IDS = ["missing", "null", "empty", "unknown", "wrong-case", "number", "list"]


@pytest.mark.parametrize("value", BAD_WHY_CATEGORIES, ids=BAD_WHY_IDS)
def test_strict_parse_rejects_a_bad_why_category(value: object) -> None:
    with pytest.raises(FindingError, match="deviation 0: why_category must be one of unclear, wrong, conflict, "
                                           "missing_context, tool_blocked, ignored"):
        parse_findings(_reply(_bad_item("why_category", value)), OFFERED)


@pytest.mark.parametrize("value", BAD_WHY_CATEGORIES, ids=BAD_WHY_IDS)
def test_parse_reply_skips_a_bad_why_category_and_keeps_the_rest(value: object) -> None:
    reasons: list[str] = []
    found, skipped = deviations.parse_reply(
        _reply(_bad_item("why_category", value), finding("skill:dispatch", "Brief", 2, why_category="ignored")),
        OFFERED, reasons)
    assert [(f.instruction_ref, f.why_category) for f in found] == [("Brief", "ignored")]
    got = None if value is _MISSING else value
    assert skipped == 1 and reasons[0].startswith("deviation 0: why_category must be one of")
    assert reasons[0].endswith(f"got {got!r}")


@pytest.mark.parametrize("value", list(deviations.WHY_CATEGORIES))
def test_every_why_category_parses(value: str) -> None:
    (f,) = parse_findings(_reply(finding(why_category=value)), OFFERED)
    assert f.why_category == value


def test_skipped_reasons_are_redacted_and_one_line() -> None:
    reasons: list[str] = []
    deviations.parse_reply(_reply(_bad_item("followed_grade", "ghp_abcdefghijklmnopqrstuvwx\n" + "z" * 500)),
                           OFFERED, reasons)
    (reason,) = reasons
    assert "ghp_" not in reason and "\n" not in reason and len(reason) <= deviations.SKIPPED_REASON_MAX


def test_parse_turn_0_means_the_first_turn() -> None:
    (f,) = parse_findings(_reply(finding(turn=0)), OFFERED)
    assert f.turn == 1


def test_parse_strips_quotes_paths_code_and_secrets() -> None:
    (f,) = parse_findings(_reply(finding(
        what='It ran `git push --force` on /Users/x/Projects/app/src/main.py and said "I will skip the tests this time".',
        why="It read .env.local and config.toml for ghp_abcdefghijklmnopqrstuvwx before asking.",
        how="See ~/agent-config/parent/PARENT.md for the rule it broke.",
    )), OFFERED)
    assert "git push" not in f.what and "/Users/" not in f.what and "skip the tests" not in f.what
    assert "[code]" in f.what and "[path]" in f.what and "[quote]" in f.what
    assert ".env" not in f.why and "config.toml" not in f.why and "ghp_" not in f.why
    assert "agent-config" not in f.how and "PARENT.md" not in f.how


def test_parse_truncates_long_fields_and_dedupes() -> None:
    long = "word " * 200
    items = [finding(what=long), finding(what="A second copy of the same deviation, same turn.")]
    (f,) = parse_findings(_reply(*items), OFFERED)
    assert len(f.what) <= deviations.FIELD_MAX


def test_envelope_errors_are_draft_errors() -> None:
    with pytest.raises(distill.DraftError):
        distill.parse_envelope(json.dumps({"type": "result", "is_error": True, "result": "x"}))


def test_source_key_is_stable_and_case_insensitive_on_ref() -> None:
    a = parse_findings(_reply(finding(ref="Explaining")), OFFERED)[0]
    b = parse_findings(_reply(finding(ref="explaining")), OFFERED)[0]
    assert deviations.source_key("s1", a) == deviations.source_key("s1", b)
    assert deviations.source_key("s1", a).startswith("dev:s1:")
    assert deviations.source_key("s2", a) != deviations.source_key("s1", a)


# --------------------------------------------------------------------------- Jev state


def test_jev_state_has_no_writer_text() -> None:
    (raw,) = parse_findings(_reply(finding()), OFFERED)
    (f,) = deviations.resolve([raw], [deviations.LoadedSource(
        "claude-md:global", "", [rule_catalog.Rule("intro", "Global (intro)"),
                                 rule_catalog.Rule("explaining", "Explaining")], "0" * 64)])
    state = deviations.jev_state(f, "worker", 2)
    assert "instruction_source: claude-md:global" in state and "instruction_rule: explaining" in state
    assert "rule_title: Explaining" in state and "instruction_ref" not in state
    assert "rule_title: Explaining (not in the rule list)" in deviations.jev_state(raw, "worker", 2)
    assert "followed_grade: 0.30" in state and "result_grade: 0.80" in state
    assert "session_kind: worker" in state and "recurrence: 2 other sessions" in state
    assert "why_category: conflict (the writer's view of why: the instruction conflicts with another " \
           "instruction)" in state.splitlines()
    for word in ("recap", "habit", "harder to read", "competes", "summarising"):
        assert word not in state


@pytest.mark.parametrize(("value", "gloss"), list(deviations.WHY_CATEGORIES.items()))
def test_jev_state_names_each_why_category_with_its_fixed_gloss(value: str, gloss: str) -> None:
    (f,) = parse_findings(_reply(finding(why_category=value)), OFFERED)
    lines = deviations.jev_state(f, "chat", 0).splitlines()
    assert lines[4] == f"why_category: {value} (the writer's view of why: {gloss})"  # right after rule_title
    assert lines[3].startswith("rule_title: ")
    # A label outside the list (never from the writer: _item rejects it) is not passed through.
    assert "why_category: unknown" in deviations.jev_state(replace(f, why_category="free text"), "chat", 0)


def test_question_file_loads_and_is_not_a_routine() -> None:
    qset = deviations.question_set()
    assert list(qset.spec("verdict").options) == list(deviations.VERDICTS)
    assert "deviation" not in questions.QuestionCache(questions.QUESTIONS_DIR).routines()


def test_writer_argv_reuses_distill_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(deviations.CLAUDE_ENV, raising=False)
    argv = deviations.writer_argv()
    assert argv[:11] == ["claude", "-p", "--model", "sonnet", "--output-format", "json", "--tools", "",
                         "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence"]
    assert argv[11] == "--json-schema" and len(argv) == 13  # the reply schema (test_deviations_writer.py)
    assert "--bare" not in argv
    monkeypatch.setenv(deviations.CLAUDE_ENV, "/opt/fake-claude")
    assert deviations.writer_argv()[0] == "/opt/fake-claude"
