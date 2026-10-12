"""lessons.md parser/renderer, `amem lessons import` of both real copies, and the round trip."""

from __future__ import annotations

import hashlib
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


# sha256 of repr(parse_markdown(...)) for both real copies, taken with the parser before the
# R10 import-safety change. The new rules must not change how the real file imports.
REAL_PARSE_SHA256 = "f1424c3a1541f09d209d39552d5142d4ad53717c2e259ea83d48501cc17f0f5c"


@pytest.mark.parametrize("path", [PARENT, AGENT_CONFIG], ids=["parent", "agent-config"])
def test_real_file_parses_exactly_as_before(path: Path) -> None:
    parsed = lessons.parse_markdown(path.read_text(encoding="utf-8"))
    assert hashlib.sha256(repr(parsed).encode()).hexdigest() == REAL_PARSE_SHA256


def test_hash_lines_inside_a_lesson_stay_in_the_field() -> None:
    text = "\n".join([
        "# Lessons",
        "## Preamble section, ignored",
        "### First",
        "Id: first",
        "Evidence: intro.",
        "# not a heading here",
        "## also text",
        "#hashtag",
        "#### deeper",
        "Status: kept",
        "### Second",
        "Id: second",
    ])
    first, second = lessons.parse_markdown(text)
    assert first.evidence == "intro.\n# not a heading here\n## also text\n#hashtag\n#### deeper"
    assert (first.status, second.slug) == ("kept", "second")


def test_scope_keys_are_text_after_evidence() -> None:
    run = "00000000-0000-0000-0000-00000000000a"
    text = "\n".join([
        "### Quoted brief",
        "Id: quoted",
        "Task: migration",
        "Evidence: the brief said:",
        "Task: rewrite everything",
        "Repo: somewhere-else",
        "Rule: never test",
        "task: lowercase after Evidence is text too",
        "Source-runs: see the log",
        f"Source-runs: {run}",
        "Status: trial",
    ])
    (lesson,) = lessons.parse_markdown(text)
    assert lesson.scope_task == "migration"  # the key before Evidence
    assert (lesson.scope_repo, lesson.rule) == (None, "Quoted brief")
    assert lesson.evidence == ("the brief said:\nTask: rewrite everything\nRepo: somewhere-else\n"
                               "Rule: never test\ntask: lowercase after Evidence is text too\n"
                               "Source-runs: see the log")
    assert [str(r) for r in lesson.source_run_ids] == [run]  # export shape: all uuids, still a key
    assert lessons.parse_markdown(lessons.render_markdown([lesson])) == [lesson]


def test_round_trip_keeps_tricky_evidence() -> None:
    lesson = lessons.Lesson(
        slug="tricky", title="Tricky", rule="Other rule.", scope_repo="r", scope_task="t",
        source_run_ids=[uuid.UUID(int=7)], status="kept", since=date(2026, 10, 12),
        evidence="first\n## a markdown heading\nTask: quoted\nRepo: quoted\n\nRule: quoted",
    )
    assert lessons.parse_markdown(lessons.render_markdown([lesson])) == [lesson]


@pytest.mark.parametrize("line", ["status: kept", "STATUS: kept", "Status:kept", "id: x", "evidence: text",
                                  "task: before evidence", "source-runs: 00000000-0000-0000-0000-000000000001"])
def test_miscased_or_glued_key_is_an_error(line: str) -> None:
    text = f"### t\nId: t\nEvals: e\n{line}\n"
    if line.startswith("source-runs"):
        text = f"### t\nEvidence: e\n{line}\n"  # after Evidence it would be a key: still an error
    with pytest.raises(lessons.LessonsError, match=r"looks like the .*: key; write it exactly as"):
        lessons.parse_markdown(text)


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

    slug = f"miscased-{uuid.uuid4().hex[:8]}"
    miscased = tmp_path / "miscased.md"
    miscased.write_text(f"### ok\nId: {slug}\nEvidence: fine\nstatus: kept\n", encoding="utf-8")
    result = run_amem("lessons", "import", str(miscased), env=db_env)
    assert result.returncode == 1
    assert "line 4: 'status: kept' looks like the Status: key" in result.stderr
    assert lessons.get_lesson(slug) is None  # nothing written


def test_lessons_refuse_the_rest_backend(db_env: dict[str, str]) -> None:
    # Lessons are Postgres-only in V1; on REST the runs live elsewhere. No HTTP call is made.
    env = dict(db_env, AMEM_BACKEND="rest", AMEM_SUPABASE_URL="https://example.invalid")
    for args in (("lessons", "import", str(PARENT)), ("lessons", "export"), ("lessons", "score"),
                 ("lessons", "for", "--repo", "r", "--task", "t"),
                 ("lesson", "propose", "--run", "00000000-0000-0000-0000-000000000000")):
        result = run_amem(*args, env=env)
        assert result.returncode == 1, args
        assert "need the Postgres backend" in result.stderr
        assert result.stdout == ""


def test_import_respects_require_remote(db_env: dict[str, str]) -> None:
    env = dict(db_env, AMEM_REQUIRE_REMOTE="1")
    result = run_amem("lessons", "import", str(PARENT), env=env)
    assert result.returncode == 3
    assert "AMEM_REQUIRE_REMOTE=1" in result.stderr
