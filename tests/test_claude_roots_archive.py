"""The archive with several Claude roots (claude_roots.py, docs/archive.md): old keys stay
byte-identical, new roots use `claude:<label>`, a root that is not there is never `deleted`, and
restore keeps two roots apart. Synthetic files in tmp dirs (archive_support.py)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_memory import archive, claude_roots
from archive_support import (  # noqa: F401
    AGENT,
    MAIN_ID,
    NOW,
    PROJ,
    Arc,
    arc,
    line,
    make_sources,
    set_mtime,
    versions,
)


@pytest.fixture
def two(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Arc, Path]]:  # noqa: F811
    """An initialized archive with the default sources and one added root `t3`."""
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    make_sources(arc)
    archive.init()
    home = arc.tmp / "runtime" / "t3"
    (home / "projects").mkdir(parents=True)
    write_roots(arc, f"t3\t{home}\n")
    yield arc, home / "projects"


def write_roots(a: Arc, text: str) -> None:
    f = a.tmp / "claude" / "parent" / "claude-roots.tsv"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def transcript(projects: Path, name: str = "sess-t3.jsonl", body: str = "t3 hello") -> Path:
    path = projects / "-work-t3" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(line(body) + line("reply", "assistant"))
    set_mtime(path, NOW - timedelta(days=1))
    return path


def keys(a: Arc) -> set[str]:
    return {archive._key_of(r) for r in versions(a)}


def test_no_file_keeps_every_key(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    make_sources(arc)
    archive.init()
    assert archive.run(now=NOW).new_versions == 6
    assert keys(arc) == {
        f"claude/{PROJ}/{MAIN_ID}.jsonl", f"claude/{PROJ}/{MAIN_ID}/subagents/{AGENT}.jsonl",
        f"claude/{PROJ}/{MAIN_ID}/subagents/{AGENT}.meta.json",
        *{f"codex/{r['path']}" for r in versions(arc) if r["source"] == "codex"}}
    assert {r["source"] for r in versions(arc)} == {"claude", "codex"}


def test_explicit_folder_ignores_the_added_root(two: tuple[Arc, Path]) -> None:
    a, added = two
    transcript(added)
    assert archive.run(projects=a.projects, now=NOW).new_versions == 6
    assert {r["source"] for r in versions(a)} == {"claude", "codex"}


def test_an_added_root_uses_its_own_source(two: tuple[Arc, Path]) -> None:
    a, added = two
    path = transcript(added)
    sub = added / "-work-t3" / "sess-t3" / "subagents" / "agent-w1.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_bytes(line("worker"))
    (sub.parent / "agent-w1.meta.json").write_text('{"agentType": "worker"}')
    rep = archive.run(now=NOW)
    assert rep.new_versions == 9 and rep.errors == 0
    got = {archive._key_of(r): r for r in versions(a) if r["source"] == "claude:t3"}
    assert set(got) == {"claude:t3/-work-t3/sess-t3.jsonl", "claude:t3/-work-t3/sess-t3/subagents/agent-w1.jsonl",
                        "claude:t3/-work-t3/sess-t3/subagents/agent-w1.meta.json"}
    assert got["claude:t3/-work-t3/sess-t3.jsonl"]["session_id"] == "sess-t3"
    assert got["claude:t3/-work-t3/sess-t3.jsonl"]["path"] == path.relative_to(added).as_posix()
    assert archive.run(now=NOW + timedelta(hours=1)).new_versions == 0
    report = archive.verify()
    assert report.ok and report.versions == 9


def test_same_session_in_two_roots_keeps_two_paths_and_no_alternating(two: tuple[Arc, Path]) -> None:
    a, added = two
    first = a.projects / PROJ / f"{MAIN_ID}.jsonl"
    twin = added / PROJ / f"{MAIN_ID}.jsonl"
    twin.parent.mkdir(parents=True)
    twin.write_bytes(line("the same id, other bytes"))
    set_mtime(twin, NOW)
    archive.run(now=NOW)
    for hours in (1, 2):
        assert archive.run(now=NOW + timedelta(hours=hours)).new_versions == 0
    mine = sorted((r["source"], r["version"]) for r in versions(a) if r["session_id"] == MAIN_ID)
    assert mine == [("claude", 1), ("claude:t3", 1)]
    assert first.exists()


def test_restore_keeps_two_roots_apart_and_old_paths_unchanged(two: tuple[Arc, Path]) -> None:
    a, added = two
    twin = added / PROJ / f"{MAIN_ID}.jsonl"
    twin.parent.mkdir(parents=True)
    twin.write_bytes(line("the same id, other bytes"))
    archive.run(now=NOW)
    to = a.tmp / "restored"
    got = archive.restore(MAIN_ID, to)
    rel = f"{PROJ}/{MAIN_ID}.jsonl"
    assert got.files == [rel, f"claude-t3/{rel}"]
    assert (to / rel).read_bytes() == (a.projects / rel).read_bytes()
    assert (to / "claude-t3" / rel).read_bytes() == twin.read_bytes()


def test_old_key_restore_after_roots_arrive(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Records written before the roots file: same keys, same restore path, no new version."""
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    make_sources(arc)
    archive.init()
    archive.run(now=NOW)
    before = keys(arc)
    home = arc.tmp / "runtime" / "t3"
    (home / "projects").mkdir(parents=True)
    write_roots(arc, f"t3\t{home}\n")
    transcript(home / "projects")
    assert archive.run(now=NOW + timedelta(hours=1)).new_versions == 1
    assert before < keys(arc)
    to = arc.tmp / "restored"
    assert f"{PROJ}/{MAIN_ID}.jsonl" in archive.restore(MAIN_ID, to).files
    assert (to / PROJ / f"{MAIN_ID}.jsonl").read_bytes() == (arc.projects / PROJ / f"{MAIN_ID}.jsonl").read_bytes()


@pytest.mark.parametrize("how", ["folder-gone", "left-the-file"])
def test_a_root_that_is_gone_does_not_count_as_deleted(two: tuple[Arc, Path], how: str) -> None:
    a, added = two
    transcript(added)
    archive.run(now=NOW)
    seen = archive.status()
    assert seen.gone == 0
    if how == "left-the-file":
        write_roots(a, "")
    else:
        (added / "-work-t3" / "sess-t3.jsonl").unlink()
        (added / "-work-t3").rmdir()
        added.rmdir()
    rep = archive.run(now=NOW + timedelta(hours=1))
    assert rep.new_versions == 0 and rep.errors == 0
    state = archive.status()
    assert state.gone == 0 and state.versions == seen.versions
    assert archive.verify().ok


def test_a_file_deleted_in_a_root_that_was_read_is_gone(two: tuple[Arc, Path]) -> None:
    a, added = two
    keep = transcript(added)
    gone = transcript(added, "sess-gone.jsonl", "gone")
    archive.run(now=NOW)
    gone.unlink()
    archive.run(now=NOW + timedelta(hours=1))
    assert archive.status().gone == 1 and keep.exists()


def test_prune_of_a_gone_root_keeps_the_marker_and_nothing_comes_back_as_new(two: tuple[Arc, Path]) -> None:
    """Retention by the source's mtime, as for any path. The index still says `at the source`, so
    a root that returns with the same old file is not archived again."""
    a, added = two
    home = added.parent
    path = transcript(added)
    old = NOW - timedelta(days=200)
    set_mtime(path, old)
    archive.run(now=NOW)
    home.rename(home.with_name("t3-away"))
    archive.run(now=NOW + timedelta(hours=1))
    rep = archive.prune(now=NOW + timedelta(hours=2))
    assert rep.paths_removed >= 1
    home.with_name("t3-away").rename(home)
    again = archive.run(now=NOW + timedelta(hours=3))
    assert again.new_versions == 0
    assert archive.verify().ok


def test_dry_run_changes_nothing_for_a_gone_root(two: tuple[Arc, Path]) -> None:
    a, added = two
    transcript(added)
    archive.run(now=NOW)
    write_roots(a, "")
    index = a.layout.index.read_bytes()
    assert archive.run(now=NOW + timedelta(hours=1), dry_run=True).dry_run
    assert a.layout.index.read_bytes() == index


def test_a_symlink_to_the_default_root_adds_no_records(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    make_sources(arc)
    archive.init()
    home = arc.tmp / "alias"
    home.mkdir()
    (home / "projects").symlink_to(arc.projects)
    write_roots(arc, f"alias\t{home}\n")
    assert archive.run(now=NOW).new_versions == 6
    assert {r["source"] for r in versions(arc)} == {"claude", "codex"}


def test_shadow_codex_home_adds_no_records(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Shadow `sessions` symlink, and a symlinked rollout inside the shared home."""
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    files = make_sources(arc)
    archive.init()
    assert archive.run(now=NOW).new_versions == 6
    before = len(versions(arc))
    link = arc.codex / "sessions/2026/10/15" / files["codex_meta"].name
    link.parent.mkdir(parents=True)
    link.symlink_to(files["codex_meta"])
    assert archive.run(now=NOW + timedelta(hours=1)).new_versions == 0
    shadow = arc.tmp / "shadow"
    shadow.mkdir()
    (shadow / "sessions").symlink_to(arc.codex / "sessions")
    assert archive.run(codex=shadow, now=NOW + timedelta(hours=2)).new_versions == 0
    assert len(versions(arc)) == before


def test_added_roots_are_source_roots_even_when_gone(two: tuple[Arc, Path]) -> None:
    a, added = two
    added.rmdir()
    assert archive._real(added) in archive.source_roots()
    with pytest.raises(archive.ArchiveError, match="source root"):
        archive.restore(MAIN_ID, added / "inside")


def test_archive_dir_inside_an_added_root_is_refused(two: tuple[Arc, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    a, added = two
    monkeypatch.setenv("AMEM_ARCHIVE_DIR", str(added / "archive"))
    with pytest.raises(archive.ArchiveError, match="source root"):
        archive.run(now=NOW)


def test_the_source_is_safe_in_every_key_use(two: tuple[Arc, Path]) -> None:
    """`claude:<label>` through the key helpers, a rebuilt index and status."""
    a, added = two
    transcript(added)
    archive.run(now=NOW)
    a.layout.index.unlink()
    assert archive.run(now=NOW + timedelta(hours=1)).index == "rebuilt"
    assert archive.status().paths == 7
    rec: dict[str, Any] = next(r for r in versions(a) if r["source"] == "claude:t3")
    assert archive._key_of(rec).split("/", 1)[0] == "claude:t3"
    assert archive._restore_rel(rec) == Path("claude-t3") / rec["path"]
    assert archive._restore_rel({**rec, "source": "claude"}) == Path(rec["path"])
    assert archive._restore_rel({**rec, "source": "claude:../x"}) == Path(rec["path"])
    with pytest.raises(archive.ArchiveError, match="unsafe"):
        archive._restore_rel({**rec, "path": "../escape"})
    assert claude_roots.ClaudeRoot("t3", added).source == "claude:t3"
