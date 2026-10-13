"""Target mapping (instruction source -> file, edit / suggest / skip) and improve rule ids. No database."""

from __future__ import annotations

import re

import pytest

from agent_memory import edits, edits_targets
from agent_memory.edits_targets import EDIT, SKIP, SUGGEST
from test_edits_support import ALLOWED, EHome, ehome  # noqa: F401


def test_skill_symlink_resolves_to_the_real_file(ehome: EHome) -> None:  # noqa: F811
    assert (ehome.claude / "skills" / "dispatch").is_symlink()
    t = edits_targets.resolve("skill:dispatch")
    assert t.action == EDIT and t.path == ehome.skill.resolve()
    assert edits_targets.short(t.path).endswith(".agents/skills/dispatch/SKILL.md") or ".agents" in str(t.path)


def test_skill_found_only_under_agents(ehome: EHome) -> None:  # noqa: F811
    (ehome.agents / "skills" / "spark").mkdir()
    (ehome.agents / "skills" / "spark" / "SKILL.md").write_text("# Spark\n## Ideas\nMany.\n")
    t = edits_targets.resolve("skill:spark")
    assert t.action == EDIT and t.path == (ehome.agents / "skills" / "spark" / "SKILL.md").resolve()


def test_skill_outside_agents_is_suggested(ehome: EHome) -> None:  # noqa: F811
    d = ehome.claude / "skills" / "local-only"
    d.mkdir()
    (d / "SKILL.md").write_text("# Local\n## Rule\nText.\n")
    t = edits_targets.resolve("skill:local-only")
    assert t.action == SUGGEST and "not under ~/.agents/skills" in t.reason


def test_missing_skill_and_unknown_source_skip(ehome: EHome) -> None:  # noqa: F811
    assert edits_targets.resolve("skill:nope").action == SKIP
    assert edits_targets.resolve("skill:ns:plugin").action == SKIP
    assert edits_targets.resolve("weird-source").action == SKIP
    assert edits_targets.resolve("agent:../etc").action == SKIP


@pytest.mark.parametrize("source,rel,action", [
    ("claude-md:global", ".claude/CLAUDE.md", EDIT),
    ("parent-md", "agent-config/parent/PARENT.md", EDIT),
    ("agent:worker", "agent-config/agents/worker.md", EDIT),
    ("routine:nightly-check", ".claude/scheduled-tasks/nightly-check/SKILL.md", SUGGEST),  # not an allowed target
    ("agents-md:global", ".codex/AGENTS.md", SUGGEST),                                      # not an allowed target
])
def test_fixed_sources(ehome: EHome, source: str, rel: str, action: str) -> None:  # noqa: F811
    t = edits_targets.resolve(source)
    assert t.action == action and t.path == (ehome.root / rel).resolve()
    if action == SUGGEST:
        assert t.reason == "outside improve's allowed targets"


def test_allowed_targets_come_from_criteria(ehome: EHome) -> None:  # noqa: F811
    ehome.allow([*ALLOWED, "~/.codex/AGENTS.md", "~/.claude/scheduled-tasks/", "@/agents/"])
    (ehome.agent_config / "agents" / "reviewer.md").write_text("# Reviewer\n## Scope\nRead only.\n")
    assert edits_targets.resolve("agents-md:global").action == EDIT
    assert edits_targets.resolve("routine:nightly-check").action == EDIT
    assert edits_targets.resolve("agent:reviewer").action == EDIT


def test_unreadable_criteria_falls_back_to_the_default(ehome: EHome) -> None:  # noqa: F811
    (ehome.agent_config / "improve" / "criteria.json").write_text("not json")
    assert edits_targets.resolve("claude-md:global").action == EDIT
    assert edits_targets.resolve("agents-md:global").action == SUGGEST


def test_other_agent_is_suggested(ehome: EHome) -> None:  # noqa: F811
    (ehome.agent_config / "agents" / "reviewer.md").write_text("# Reviewer\n## Scope\nRead only.\n")
    assert edits_targets.resolve("agent:reviewer").action == SUGGEST


@pytest.mark.parametrize("source", ["claude-md:demo-repo", "agents-md:demo-repo"])
def test_repo_files_are_never_edited(ehome: EHome, source: str) -> None:  # noqa: F811
    t = edits_targets.resolve(source, [str(ehome.repo / "CLAUDE.md")])
    assert t.action == SUGGEST and t.path == ehome.repo / "CLAUDE.md"
    assert "repo file" in t.reason
    # Even when the repo sits inside an allowed folder, it is still a suggestion.
    ehome.allow(["~/"])
    assert edits_targets.resolve(source).action == SUGGEST


def test_improve_rule_id_is_kebab_and_namespaced() -> None:
    rx = re.compile(r"[a-z0-9][a-z0-9-]{2,60}")
    cases = {
        ("skill:dispatch", "spawn"): "skill-dispatch--spawn",
        ("claude-md:global", "explaining-completed-work/core-rule"): "claude-md-global--explaining-completed-work--core-rule",
        ("parent-md", "intro"): "parent-md--intro",
    }
    for (src, rid), want in cases.items():
        got = edits.improve_rule_id(src, rid)
        assert got == want and rx.fullmatch(got)
    long = edits.improve_rule_id("skill:" + "x" * 50, "a-very-long-rule-id/with-a-child-" + "y" * 30)
    assert rx.fullmatch(long) and len(long) <= 61
    assert long != edits.improve_rule_id("skill:" + "x" * 50, "a-very-long-rule-id/with-a-child-" + "z" * 30)
