"""The archive with guarded T3 homes (claude_roots.py, docs/archive.md): patterns, Codex homes, old keys
that stay byte-identical, `codex:<label>` keys that never collide, and hash folders that go away
without counting as deleted. Real folder shapes, synthetic content, tmp dirs (guarded_support.py)."""

from __future__ import annotations

import dataclasses
import shutil
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from agent_memory import archive
from archive_support import (  # noqa: F401
    NOW,
    Arc,
    arc,
    make_sources,
    versions,
)
from guarded_support import (
    claude_session,
    codex_session,
    guarded_home,
    session_id,
    set_mtime,
    thread_hash,
    write_jsonl,
    rollout_events,
    write_roots,
)

THREAD = "0199cccc-bbbb-7ccc-8ddd-eeeeffff0a01"   # not an id of archive_support.make_sources


@pytest.fixture
def setup(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Arc, Path]]:  # noqa: F811
    """An initialized archive, the default sources in the default homes, and a guarded roots file."""
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    monkeypatch.delenv("AMEM_CODEX_HOME")
    a = dataclasses.replace(arc, codex=arc.tmp / ".codex")
    make_sources(a)
    archive.init()
    control = a.tmp / "t3-run" / "control"
    write_roots(a.tmp / "claude",
                f"t3\t{control}/native/*/home",
                f"t3\t{control}/native/*/home\tcodex",
                f"t3-admissions\t{control}/owner-admissions/*/home\tcodex",
                f"t3-transitions\t{control}/transitions/*/home\tcodex")
    yield a, control


def keys(a: Arc) -> set[str]:
    return {archive._key_of(r) for r in versions(a)}


def label(family: str, thread: str) -> str:
    return f"{family}-{thread_hash(thread)[:12]}"


def test_no_file_keeps_every_codex_key(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    monkeypatch.delenv("AMEM_CODEX_HOME")
    a = dataclasses.replace(arc, codex=arc.tmp / ".codex")
    make_sources(a)
    archive.init()
    archive.run(now=NOW)
    assert {r["source"] for r in versions(a)} == {"claude", "codex"}
    assert sum(k.startswith("codex/sessions/") or k.startswith("codex/archived_sessions/") for k in keys(a)) == 3


def test_a_new_hash_folder_is_archived_with_no_file_edit(setup: tuple[Arc, Path]) -> None:
    a, control = setup
    archive.run(now=NOW)
    before = keys(a)
    claude_session(guarded_home(control, "native", "thread-a"), session_id(1))
    assert archive.run(now=NOW + timedelta(hours=1)).new_versions == 1
    codex_session(guarded_home(control, "transitions", "thread-b"), THREAD)
    assert archive.run(now=NOW + timedelta(hours=2)).new_versions == 1
    added = keys(a) - before
    assert before < keys(a)
    source = "claude:" + label("t3", "thread-a")
    assert {k.split("/", 1)[0] for k in added} == {source, "codex:" + label("t3-transitions", "thread-b")}
    assert all("/projects/" not in k for k in added)


def test_a_removed_hash_folder_does_not_count_as_deleted(setup: tuple[Arc, Path]) -> None:
    a, control = setup
    native = guarded_home(control, "native", "thread-a")
    transit = guarded_home(control, "transitions", "thread-b")
    claude_session(native, session_id(1))
    codex_session(transit, THREAD)
    archive.run(now=NOW)
    seen = archive.status()
    assert seen.gone == 0 and seen.versions
    shutil.rmtree(native.parent)
    shutil.rmtree(transit.parent)
    rep = archive.run(now=NOW + timedelta(hours=1))
    assert rep.new_versions == 0 and rep.errors == 0
    state = archive.status()
    assert state.gone == 0 and state.versions == seen.versions
    assert archive.verify().ok


def test_a_rollout_deleted_in_a_home_that_was_read_is_gone(setup: tuple[Arc, Path]) -> None:
    _a, control = setup
    home = guarded_home(control, "transitions", "thread-a")
    keep = codex_session(home, THREAD)
    other = "0199cccc-bbbb-7ccc-8ddd-eeeeffff0a09"
    gone = codex_session(home, other)
    archive.run(now=NOW)
    gone.unlink()
    archive.run(now=NOW + timedelta(hours=1))
    assert archive.status().gone == 1 and keep.exists()


def test_same_rollout_in_two_folders_keeps_two_keys_and_versions_do_not_alternate(setup: tuple[Arc, Path]) -> None:
    a, control = setup
    archive.run(now=NOW - timedelta(hours=1))
    one = codex_session(guarded_home(control, "transitions", "aa"), THREAD)
    two = codex_session(guarded_home(control, "transitions", "zz"), THREAD, text="said again")
    three = codex_session(guarded_home(control, "transitions", "aa"), THREAD, folder="archived_sessions", text="said again")
    assert archive.run(now=NOW).new_versions == 3
    for hours in (1, 2, 3):
        rep = archive.run(now=NOW + timedelta(hours=hours))
        assert (rep.new_versions, rep.unchanged >= 3) == (0, True)
    mine = sorted((r["source"], r["path"].split("/")[0], r["version"])
                  for r in versions(a) if r["session_id"] == f"codex:{THREAD}")
    aa, zz = "codex:" + label("t3-transitions", "aa"), "codex:" + label("t3-transitions", "zz")
    assert mine == sorted([(aa, "sessions", 1), (aa, "archived_sessions", 1), (zz, "sessions", 1)])
    assert one.exists() and two.exists() and three.exists()


def test_old_codex_key_restores_at_its_old_path(setup: tuple[Arc, Path]) -> None:
    """Records from before the roots file: same key, same restore path, no new version."""
    a, control = setup
    tid = "0199cccc-bbbb-7ccc-8ddd-eeeeffff0a02"
    plain = codex_session(a.codex, tid, originator="codex_exec")
    archive.run(now=NOW)
    old = f"codex/sessions/2026/10/14/{plain.name}"
    assert old in keys(a)
    codex_session(guarded_home(control, "transitions", "thread-a"), THREAD)
    assert archive.run(now=NOW + timedelta(hours=1)).new_versions == 1
    assert sum(k == old for k in keys(a)) == 1
    to = a.tmp / "restored"
    assert archive.restore(f"codex:{tid}", to).files == [f"sessions/2026/10/14/{plain.name}"]
    assert (to / "sessions/2026/10/14" / plain.name).read_bytes() == plain.read_bytes()


def test_labelled_codex_restore_never_collides_with_the_default_home(setup: tuple[Arc, Path]) -> None:
    a, control = setup
    default = codex_session(a.codex, THREAD, text="default bytes")
    guarded = codex_session(guarded_home(control, "transitions", "thread-a"), THREAD, text="guarded bytes")
    assert archive.run(now=NOW).new_versions >= 2
    to = a.tmp / "restored"
    rel = f"sessions/2026/10/14/{default.name}"
    got = archive.restore(f"codex:{THREAD}", to)
    prefix = "codex-" + label("t3-transitions", "thread-a")
    assert sorted(got.files) == sorted([rel, f"{prefix}/{rel}"])
    assert (to / rel).read_bytes() == default.read_bytes()
    assert (to / prefix / rel).read_bytes() == guarded.read_bytes()


def test_an_older_version_of_a_labelled_rollout_can_be_restored(setup: tuple[Arc, Path]) -> None:
    a, control = setup
    home = guarded_home(control, "owner-admissions", "thread-a")
    path = codex_session(home, THREAD, text="first")
    archive.run(now=NOW)
    write_jsonl(path, rollout_events(THREAD, text="second, longer"))
    set_mtime(path, 60)
    assert archive.run(now=NOW + timedelta(hours=1)).new_versions == 1
    to = a.tmp / "restored"
    rel = archive.restore(f"codex:{THREAD}", to, version=1).files[0]
    assert b"first" in (to / rel).read_bytes()


def test_an_archive_dir_below_a_pattern_is_refused(setup: tuple[Arc, Path]) -> None:
    _a, control = setup
    (control / "native").mkdir(parents=True)
    with pytest.raises(archive.ArchiveError, match="transcript source root"):
        archive.check_archive_dir(control / "native" / "new-hash" / "archive", archive.source_roots())


def test_restore_refuses_a_listed_codex_home_and_a_pattern_folder(setup: tuple[Arc, Path]) -> None:
    a, control = setup
    home = guarded_home(control, "transitions", "thread-a")
    codex_session(home, THREAD)
    archive.run(now=NOW)
    for target in (home / "restored", control / "transitions" / "later" / "restored"):
        with pytest.raises(archive.ArchiveError, match="refusing to restore"):
            archive.restore(f"codex:{THREAD}", target)
    assert a.tmp.is_dir()


def test_a_plain_listed_codex_home_is_a_source_root(setup: tuple[Arc, Path]) -> None:
    a, _control = setup
    home = a.tmp / "runtime" / "codex-plain"
    write_roots(a.tmp / "claude", f"plain\t{home}\tcodex")
    with pytest.raises(archive.ArchiveError, match="transcript source root"):
        archive.check_archive_dir(home / "archive", archive.source_roots())


def test_dry_run_counts_guarded_files_and_writes_nothing(setup: tuple[Arc, Path]) -> None:
    a, control = setup
    claude_session(guarded_home(control, "native", "thread-a"), session_id(1))
    before = a.layout.manifest.read_bytes()
    rep = archive.run(dry_run=True, now=NOW)
    assert rep.new_versions >= 1 and a.layout.manifest.read_bytes() == before
