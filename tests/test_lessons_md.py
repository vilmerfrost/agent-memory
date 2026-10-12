"""lessons.md parser/renderer, `amem lessons import` of both real copies, and the round trip."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest

from agent_memory import lessons
from conftest import run_amem

FIXTURES = Path(__file__).parent / "fixtures"
PARENT = FIXTURES / "lessons.parent.md"  # copy of ~/.claude/parent/lessons.md
AGENT_CONFIG = FIXTURES / "lessons.agent-config.md"  # copy of ~/agent-config/lessons.md
REAL_SLUGS = ("no-compound-cmds", "prove-guards-live", "ui-mid-width")


# --- parser over the real copies ---------------------------------------------------------


@pytest.mark.parametrize("path", [PARENT, AGENT_CONFIG], ids=["parent", "agent-config"])
def test_parses_real_lessons_file(path: Path) -> None:
    parsed = lessons.parse_markdown(path.read_text(encoding="utf-8"))
    assert [l.slug for l in parsed] == list(REAL_SLUGS)
    first, second, third = parsed
    assert first.title.startswith("Workers: no compound `cd && ...` or heredoc chains")
    assert first.rule == first.title
    assert (first.status, first.since, first.evals) == ("promoted", date(2026, 10, 10), "f-live")
    assert first.promoted_to == "agents/worker.md#Command form"
    # Evidence spans six lines and keeps them.
    assert first.evidence is not None
    assert first.evidence.startswith("junk-orchestra run 2026-10-10.")
    assert first.evidence.count("\n") == 5
    assert "A/B regrade 2026-10-11" in first.evidence.splitlines()[-1]
    assert (second.status, second.promoted_to) == ("kept", None)
    assert (third.status, third.evals, third.since) == ("trial", "none (no UI eval case yet)", date(2026, 10, 11))


def test_parser_edge_cases() -> None:
    text = "\r\n".join([
        "# Lessons",
        "preamble with Id: not-a-lesson",
        "",
        "### No id here: so the title becomes the slug",
        "Free text before any key is the rule.",
        "Evidence: first paragraph.",
        "",
        "Second paragraph, Status words inside text stay text.",
        "Status: reverted",
        "",
        "### Scoped one",
        "Id: scoped-one",
        "Rule: Do the thing.",
        "Repo: agent-memory",
        "Task: migration",
        "Source-runs: 00000000-0000-0000-0000-000000000001, 00000000-0000-0000-0000-000000000002",
        "Since: 2026-10-12",
        "",
        "## Archive",
        "Id: ignored-outside-a-lesson",
    ])
    a, b = lessons.parse_markdown(text)
    assert a.slug == "no-id-here-so-the-title-becomes-the-slug"
    assert a.rule == "Free text before any key is the rule."
    assert a.evidence == "first paragraph.\n\nSecond paragraph, Status words inside text stay text."
    assert a.status == "retired"  # lessons.md `reverted` maps to `retired`
    assert (b.slug, b.rule, b.scope_repo, b.scope_task, b.status) == (
        "scoped-one", "Do the thing.", "agent-memory", "migration", "trial")
    assert [str(r)[-1] for r in b.source_run_ids] == ["1", "2"]
    assert lessons.parse_markdown(lessons.render_markdown([a, b])) == [a, b]


@pytest.mark.parametrize("text, match", [
    ("### t\nStatus: maybe\n", "unknown Status"),
    ("### t\nSince: yesterday\n", "Since"),
    ("### t\nId: x\n### u\nId: x\n", "more than once"),
    ("### t\nId: x\nId: y\n", "appears twice"),
    ("### t\nSource-runs: nope\n", "Source-runs"),
])
def test_parser_rejects_bad_input(text: str, match: str) -> None:
    with pytest.raises(lessons.LessonsError, match=match):
        lessons.parse_markdown(text)


# --- import / export against the throwaway Postgres -----------------------------------


def _forget_real_slugs(pg_url: str) -> None:
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from lessons where slug = any(%s)", (list(REAL_SLUGS),))


def test_import_both_real_copies(db_env: dict[str, str], pg_url: str, tmp_path: Path) -> None:
    _forget_real_slugs(pg_url)
    first = run_amem("lessons", "import", str(PARENT), env=db_env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "imported 3 new, 0 updated"

    second = run_amem("lessons", "import", str(AGENT_CONFIG), env=db_env)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "imported 0 new, 0 updated"  # same content

    changed = tmp_path / "lessons.md"
    changed.write_text(AGENT_CONFIG.read_text(encoding="utf-8").replace(
        "Status: trial\nSince: 2026-10-11", "Status: kept\nSince: 2026-10-11"), encoding="utf-8")
    third = run_amem("lessons", "import", str(changed), env=db_env)
    assert third.stdout.strip() == "imported 0 new, 1 updated"
    lesson = lessons.get_lesson("ui-mid-width")
    assert lesson is not None and lesson.status == "kept"

    run_amem("lessons", "import", str(PARENT), env=db_env)
    stored = lessons.get_lesson("no-compound-cmds")
    parsed = lessons.parse_markdown(PARENT.read_text(encoding="utf-8"))[0]
    assert stored == parsed


def test_round_trip_import_export_import_changes_nothing(db_env: dict[str, str], tmp_path: Path) -> None:
    # Include a proposed-style lesson so Rule/Repo/Task/Source-runs round-trip too.
    run_amem("lessons", "import", str(PARENT), env=db_env)
    extra = lessons.Lesson(
        slug=f"rt-{uuid.uuid4().hex[:8]}", title="Round trip title", rule="A different rule.",
        scope_repo="agent-memory", scope_task="migration", source_run_ids=[uuid.uuid4()],
        evidence="line one\n\nline three", since=date(2026, 10, 12),
    )
    lessons.upsert([extra])
    before = lessons.all_lessons()

    exported = run_amem("lessons", "export", env=db_env)
    assert exported.returncode == 0, exported.stderr
    assert exported.stdout.startswith("# Lessons\n")
    assert "### Prove safety guards live through the real run path before dispatch\nId: prove-guards-live\n" \
        in exported.stdout
    path = tmp_path / "exported.md"
    path.write_text(exported.stdout, encoding="utf-8")

    again = run_amem("lessons", "import", str(path), env=db_env)
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip() == "imported 0 new, 0 updated"
    assert lessons.all_lessons() == before
    assert run_amem("lessons", "export", env=db_env).stdout == exported.stdout


def test_import_errors(db_env: dict[str, str], tmp_path: Path) -> None:
    missing = run_amem("lessons", "import", str(tmp_path / "nope.md"), env=db_env)
    assert missing.returncode == 1 and "no such file" in missing.stderr
    bad = tmp_path / "bad.md"
    bad.write_text("### t\nStatus: maybe\n", encoding="utf-8")
    result = run_amem("lessons", "import", str(bad), env=db_env)
    assert result.returncode == 1 and "unknown Status" in result.stderr


def test_import_respects_require_remote(db_env: dict[str, str]) -> None:
    env = dict(db_env, AMEM_REQUIRE_REMOTE="1")
    result = run_amem("lessons", "import", str(PARENT), env=env)
    assert result.returncode == 3
    assert "AMEM_REQUIRE_REMOTE=1" in result.stderr
