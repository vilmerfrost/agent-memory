"""`archive`: the encrypted transcript archive's library (docs/archive.md). Synthetic data only."""

from __future__ import annotations

import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from agent_memory import archive
from archive_support import (AGENT, CODEX_TID, FILE_TID, MAIN_ID, MARK, NOW, PROJ, Arc, all_bytes, arc,  # noqa: F401
                             by_path, line, make_sources, object_paths, records, set_mtime, snapshot, versions)

DAY = timedelta(days=1)


def _init_and_run(a: Arc) -> dict[str, Path]:
    srcs = make_sources(a)
    archive.init(now=NOW)
    archive.run(now=NOW)
    return srcs


# ---------------------------------------------------------------- 1, 2: first run, then a no-op


def test_first_run_archives_everything(arc: Arc) -> None:  # noqa: F811
    srcs = make_sources(arc)
    archive.init(now=NOW)
    rep = archive.run(now=NOW)
    assert (rep.files, rep.new_versions, rep.unchanged, rep.new_objects, rep.errors, rep.skipped) == (6, 6, 0, 6, 0, 0)
    assert rep.bytes_new == rep.bytes_seen == sum(p.stat().st_size for p in srcs.values())
    assert rep.bytes_stored > 0 and rep.index == "loaded"
    assert len(object_paths(arc)) == 6
    st = archive.status()
    assert (st.initialized, st.key, st.versions, st.paths, st.sessions, st.gone) == (True, True, 6, 6, 5, 0)
    assert st.last_run == "2026-10-16T09:00:00Z"
    assert archive.verify().ok


def test_second_run_is_a_noop(arc: Arc) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    manifest = arc.layout.manifest.read_bytes()
    objects = snapshot(arc.archive / "objects")
    rep = archive.run(now=NOW + DAY)
    assert (rep.files, rep.new_versions, rep.unchanged, rep.new_objects, rep.bytes_new) == (6, 0, 6, 0, 0)
    set_mtime(srcs["main"], NOW + 2 * DAY)  # touched, same bytes: re-hashed, still unchanged
    rep = archive.run(now=NOW + 2 * DAY)
    assert (rep.new_versions, rep.unchanged, rep.new_objects) == (0, 6, 0)
    assert arc.layout.manifest.read_bytes() == manifest
    assert snapshot(arc.archive / "objects") == objects
    assert archive.status().last_run == "2026-10-18T09:00:00Z"
    assert archive.verify().ok


# ---------------------------------------------------------------- 3, 4, 5: versions, resumes, deletions


def test_appending_a_line_gives_a_new_version(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    old = srcs["main"].read_bytes()
    with srcs["main"].open("ab") as fh:
        fh.write(line("one more turn"))
    rep = archive.run(now=NOW + DAY)
    assert (rep.new_versions, rep.new_objects, rep.unchanged) == (1, 1, 5)
    vs = by_path(arc, srcs["main"], arc.projects)
    assert [v["version"] for v in vs] == [1, 2] and vs[1]["prev_version_last_seen"] == "2026-10-16T09:00:00Z"
    archive.restore(MAIN_ID, tmp_path / "v1", version=1)
    archive.restore(MAIN_ID, tmp_path / "v2")
    archive.restore(MAIN_ID, tmp_path / "asof", as_of=NOW + timedelta(hours=1))
    rel = Path(PROJ) / f"{MAIN_ID}.jsonl"
    assert (tmp_path / "v1" / rel).read_bytes() == old == (tmp_path / "asof" / rel).read_bytes()
    assert (tmp_path / "v2" / rel).read_bytes() == srcs["main"].read_bytes()
    assert archive.verify().ok


def test_resumed_transcript_is_its_own_session(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    resumed = srcs["main"].with_name("sess-resumed.jsonl")
    resumed.write_bytes(srcs["main"].read_bytes() + line("after resume") + line("resumed reply", "assistant"))
    copy = srcs["main"].with_name("sess-pure-copy.jsonl")
    copy.write_bytes(srcs["main"].read_bytes())
    rep = archive.run(now=NOW + DAY)
    assert (rep.new_versions, rep.new_objects) == (2, 1)  # the pure copy dedupes to the stored object
    vs = {r["session_id"]: r for r in versions(arc)}
    assert vs["sess-resumed"]["version"] == 1 and vs["sess-pure-copy"]["sha256"] == vs[MAIN_ID]["sha256"]
    assert len(by_path(arc, srcs["main"], arc.projects)) == 1
    archive.restore("sess-resumed", tmp_path / "out")
    assert (tmp_path / "out" / PROJ / "sess-resumed.jsonl").read_bytes() == resumed.read_bytes()
    assert archive.verify().ok


def test_a_source_deleted_at_the_source_is_kept(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    data = srcs["main"].read_bytes()
    srcs["main"].unlink()
    srcs["codex_file"].unlink()
    rep = archive.run(now=NOW + DAY)
    assert (rep.files, rep.new_versions, rep.unchanged) == (4, 0, 4)
    st = archive.status()
    assert (st.versions, st.gone) == (6, 2)
    archive.restore(MAIN_ID, tmp_path / "out")
    assert (tmp_path / "out" / PROJ / f"{MAIN_ID}.jsonl").read_bytes() == data
    assert archive.verify().ok


# ---------------------------------------------------------------- 6: prune


def test_prune_with_a_fake_clock_removes_only_expired_data(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = make_sources(arc)
    for path in srcs.values():
        set_mtime(path, NOW - 20 * DAY)
    set_mtime(srcs["main"], NOW - 200 * DAY)
    archive.init(now=NOW - 300 * DAY)
    archive.run(now=NOW - 250 * DAY)        # an old run record: expires too
    with srcs["sub"].open("ab") as fh:      # a second version of a live path: kept
        fh.write(line("later"))
    set_mtime(srcs["sub"], NOW - 10 * DAY)
    archive.run(now=NOW - 10 * DAY)
    main_sha = by_path(arc, srcs["main"], arc.projects)[0]["sha256"]
    before = snapshot(arc.archive)

    dry = archive.prune(days=180, dry_run=True, now=NOW)
    assert snapshot(arc.archive) == before
    rep = archive.prune(days=180, now=NOW)
    assert (dry.paths_removed, dry.versions_removed, dry.objects_removed) == (1, 1, 1)
    assert (rep.paths_removed, rep.versions_removed, rep.versions_kept, rep.objects_removed, rep.events_removed) \
        == (1, 1, 6, 1, 1)
    assert rep.compacted and rep.bytes_freed > 0
    assert not arc.layout.object_path(main_sha).exists()
    assert len(object_paths(arc)) == 6
    kinds = [r["type"] for r in records(arc)]
    assert kinds[0] == "init" and kinds[-1] == "prune" and kinds.count("run") == 1
    assert records(arc)[-1]["compacted_from"]["lines"] == 1 + 6 + 1 + 1 + 1
    v = archive.verify()
    assert v.ok and v.versions == 6 and v.orphans == 0
    with pytest.raises(archive.ArchiveError, match="no archived versions"):
        archive.restore(MAIN_ID, tmp_path / "gone")
    archive.restore(f"{MAIN_ID}/{AGENT}", tmp_path / "kept", version=1)

    # The expired file is still at the source, unchanged: the next run does not archive it again.
    rep2 = archive.run(now=NOW)
    assert (rep2.new_versions, rep2.unchanged) == (0, 6)
    assert archive.verify().ok
    # Nothing left to remove: one prune record is appended, nothing is rewritten.
    lines = len(archive._manifest_lines(arc.layout))
    rep3 = archive.prune(days=180, now=NOW + DAY)
    assert not rep3.compacted and rep3.versions_removed == 0
    assert len(archive._manifest_lines(arc.layout)) == lines + 1
    # Once it is gone at the source too, its index entry goes; far in the future everything expires.
    srcs["main"].unlink()
    archive.run(now=NOW + 2 * DAY)
    rep4 = archive.prune(days=180, now=NOW + 400 * DAY)
    assert rep4.versions_kept == 0 and object_paths(arc) == []
    v = archive.verify()
    assert v.ok and v.versions == 0


# ---------------------------------------------------------------- 7, 8, 9: tamper and wrong key


def test_a_tampered_object_fails_verify(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    sha = by_path(arc, srcs["main"], arc.projects)[0]["sha256"]
    path = arc.layout.object_path(sha)
    blob = bytearray(path.read_bytes())
    blob[len(blob) // 2] ^= 0x01
    path.write_bytes(bytes(blob))
    rep = archive.verify()
    assert not rep.ok and rep.bad == 1 and rep.problem_ids == [sha]
    assert archive.verify(sample=6).bad == 1
    with pytest.raises(archive.ArchiveError):
        archive.restore(MAIN_ID, tmp_path / "out")


def test_swapped_or_missing_objects_fail_verify(arc: Arc) -> None:  # noqa: F811
    _init_and_run(arc)
    a, b, c = object_paths(arc)[:3]
    data_a, data_b = a.read_bytes(), b.read_bytes()
    a.write_bytes(data_b)          # a valid object under another id: the AAD binds the id
    b.write_bytes(data_a)
    c.unlink()
    rep = archive.verify()
    assert not rep.ok and (rep.bad, rep.missing) == (2, 1)


def test_the_aad_binds_kind_key_id_and_object_id() -> None:
    kr = archive.Keyring(archive.MemoryKeyStore({"k1": os.urandom(32), "k2": os.urandom(32)}), "k1")
    blob = archive._seal("object", kr, b"payload", "a" * 64)
    assert archive._unseal("object", kr, blob, "a" * 64) == b"payload"
    assert blob[:4] == b"AMOB" and blob[4] == archive.FORMAT and blob[6:8] == b"k1"
    with pytest.raises(archive.Undecryptable, match="authentication"):
        archive._unseal("object", kr, blob, "b" * 64)               # another object id
    with pytest.raises(archive.Undecryptable, match="authentication"):
        archive._unseal("object", kr, blob[:6] + b"k2" + blob[8:], "a" * 64)  # header names another key
    with pytest.raises(archive.Undecryptable):
        archive._unseal("index", kr, blob, "a" * 64)                # another kind (magic and AAD)
    nonces = {archive._seal("object", kr, b"payload", "a" * 64)[8:20] for _ in range(50)}
    assert len(nonces) == 50                                         # a fresh nonce per seal


@pytest.mark.parametrize("damage", ["flip", "remove_middle", "remove_last", "reorder"])
def test_a_damaged_manifest_fails_verify(arc: Arc, damage: str) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    with srcs["main"].open("ab") as fh:
        fh.write(line("more"))
    archive.run(now=NOW + DAY)           # 1 init + 6 versions + 1 run + 1 version + 1 run = 10 lines
    lines = arc.layout.manifest.read_bytes().split(b"\n")[:-1]
    assert len(lines) == 10
    if damage == "flip":
        b = bytearray(lines[3])
        b[40] = ord("A") if b[40] != ord("A") else ord("B")
        lines[3] = bytes(b)
    elif damage == "remove_middle":
        del lines[3]
    elif damage == "remove_last":
        del lines[-1]
    else:
        lines[2], lines[3] = lines[3], lines[2]
    arc.layout.manifest.write_bytes(b"\n".join(lines) + b"\n")
    rep = archive.verify()
    assert not rep.ok
    if damage == "flip":
        assert rep.manifest_bad == 1 and rep.problem_lines[0] == 4
    elif damage == "remove_last":
        assert rep.chain_breaks == 0 and rep.index_problems >= 1  # only the index sees a cut tail
    else:
        assert rep.chain_breaks >= 1
    if damage.startswith("remove"):
        with pytest.raises(archive.ArchiveError, match="verify"):
            archive.run(now=NOW + 2 * DAY)


def test_the_wrong_key_fails(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    _init_and_run(arc)
    manifest = arc.layout.manifest.read_bytes()
    arc.key_file.unlink()
    archive.FileKeyStore(arc.key_file).add("k1", os.urandom(32))   # same id, other key
    rep = archive.verify()
    assert not rep.ok and rep.manifest_bad == rep.manifest_lines == 8 and rep.index_problems == 1
    with pytest.raises(archive.ArchiveError, match="does not open"):
        archive.run(now=NOW + DAY)
    with pytest.raises(archive.ArchiveError):
        archive.restore(MAIN_ID, tmp_path / "out")
    assert arc.layout.manifest.read_bytes() == manifest
    arc.key_file.unlink()
    with pytest.raises(archive.KeyMissing):
        archive.run(now=NOW + DAY)
    assert archive.status().key is False


def test_a_key_file_readable_by_others_is_refused(arc: Arc) -> None:  # noqa: F811
    _init_and_run(arc)
    arc.key_file.chmod(0o644)
    with pytest.raises(archive.ArchiveError, match="mode 600"):
        archive.verify()


# ---------------------------------------------------------------- 10: restore


def test_restore_round_trips_bytes_and_mtime(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = make_sources(arc)
    ns = set_mtime(srcs["main"], 1_760_000_000_123_456_789)
    archive.init(now=NOW)
    archive.run(now=NOW)
    out = tmp_path / "out"
    rep = archive.restore(MAIN_ID, out)
    target = out / PROJ / f"{MAIN_ID}.jsonl"
    assert rep.files == [f"{PROJ}/{MAIN_ID}.jsonl"]
    assert target.read_bytes() == srcs["main"].read_bytes()
    assert target.stat().st_mtime_ns == ns
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(archive.ArchiveError, match="overwrite"):
        archive.restore(MAIN_ID, out)
    rep = archive.restore(f"{MAIN_ID}/{AGENT}", tmp_path / "sub")
    assert sorted(rep.files) == [f"{PROJ}/{MAIN_ID}/subagents/{AGENT}.jsonl",
                                 f"{PROJ}/{MAIN_ID}/subagents/{AGENT}.meta.json"]
    rep = archive.restore(f"codex:{CODEX_TID}", tmp_path / "codex-out")
    restored = tmp_path / "codex-out" / rep.files[0]
    assert restored.read_bytes() == srcs["codex_meta"].read_bytes()
    assert restored.stat().st_mtime_ns == srcs["codex_meta"].stat().st_mtime_ns
    with pytest.raises(archive.ArchiveError, match="matches"):
        archive.restore(MAIN_ID, tmp_path / "v9", version=9)


def test_restore_refuses_source_roots_and_the_archive_dir(arc: Arc) -> None:  # noqa: F811
    _init_and_run(arc)
    for bad in (arc.projects / "restored", arc.codex / "x", arc.archive / "x", arc.projects):
        with pytest.raises(archive.ArchiveError, match="refusing to restore"):
            archive.restore(MAIN_ID, bad)
    assert not (arc.projects / "restored").exists() and not (arc.archive / "x").exists()


# ---------------------------------------------------------------- 11, 12: encryption at rest, modes


def test_no_plaintext_marker_anywhere_in_the_archive(arc: Arc, tmp_path: Path) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    with srcs["main"].open("ab") as fh:
        fh.write(line("more"))
    archive.run(now=NOW + DAY)
    archive.prune(days=180, now=NOW + 2 * DAY)
    archive.restore(MAIN_ID, tmp_path / "out")
    assert MARK.encode() in all_bytes(arc.projects)  # the scan does find it where it is plaintext
    blob = all_bytes(arc.archive)
    for secret in (MARK, MAIN_ID, PROJ, AGENT, CODEX_TID, FILE_TID, "hello main", "session_meta",
                   archive._b64url(archive.key_store().get("k1"))):
        assert secret.encode() not in blob, secret
    names = " ".join(p.relative_to(arc.archive).as_posix() for p in arc.archive.rglob("*"))
    assert MARK not in names and "jsonl" in names  # only manifest.jsonl, never a source name


def test_file_modes_are_600_and_700(arc: Arc) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    with srcs["main"].open("ab") as fh:
        fh.write(line("more"))
    archive.run(now=NOW + DAY)
    archive.prune(days=180, now=NOW + 2 * DAY)
    entries = [arc.archive, *arc.archive.rglob("*")]
    assert len(entries) > 10
    for p in entries:
        mode = stat.S_IMODE(p.lstat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), (p.relative_to(arc.tmp), oct(mode))
    assert stat.S_IMODE(arc.key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(arc.key_file.parent.stat().st_mode) == 0o700


# ---------------------------------------------------------------- 13: refused locations


@pytest.mark.parametrize("where", ["rsi_sandbox", "home_sandbox", "projects", "codex", "contains_source",
                                   "git_repo", "key_inside"])
def test_a_bad_archive_dir_is_refused(arc: Arc, monkeypatch: pytest.MonkeyPatch, where: str) -> None:  # noqa: F811
    make_sources(arc)
    if where == "rsi_sandbox":
        target = arc.sandbox / "exp-1" / "archive"
    elif where == "home_sandbox":
        home = arc.tmp / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("RSI_SANDBOX_DIR")
        target = home / "sandbox" / "archive"
    elif where == "projects":
        target = arc.projects / PROJ / "archive"
    elif where == "codex":
        target = arc.codex / "archive"
    elif where == "contains_source":
        target = arc.tmp / "claude"
    elif where == "git_repo":
        (arc.tmp / "repo" / ".git").mkdir(parents=True)
        target = arc.tmp / "repo" / "data" / "archive"
    else:
        target = arc.tmp / "archive2"
        monkeypatch.setenv("AMEM_ARCHIVE_KEY_FILE", str(target / "key.json"))
    monkeypatch.setenv("AMEM_ARCHIVE_DIR", str(target))
    before = snapshot(arc.tmp)
    for call in (lambda: archive.init(), lambda: archive.run(dry_run=True), lambda: archive.run(),
                 lambda: archive.status(), lambda: archive.prune()):
        with pytest.raises(archive.ArchiveError, match="must not"):
            call()
    assert snapshot(arc.tmp) == before


def test_forbidden_mount_roots_name_the_archive_dir_and_the_key_file(arc: Arc) -> None:  # noqa: F811
    roots = archive.forbidden_mount_roots()
    assert arc.archive.resolve() in roots and arc.key_file.resolve() in roots
    assert archive.default_archive_dir().resolve() in roots


# ---------------------------------------------------------------- 15: dry run


def test_dry_run_writes_nothing_and_needs_no_key(arc: Arc) -> None:  # noqa: F811
    srcs = make_sources(arc)
    sources_before = snapshot(arc.tmp / "claude"), snapshot(arc.codex)
    rep = archive.run(dry_run=True, now=NOW)
    assert (rep.files, rep.new_versions, rep.new_objects, rep.index, rep.dry_run) == (6, 6, 6, "none", True)
    assert not arc.archive.exists() and not arc.key_file.exists()

    archive.init(now=NOW)
    archive.run(now=NOW)
    with srcs["main"].open("ab") as fh:
        fh.write(line("more"))
    before = snapshot(arc.archive)
    rep = archive.run(dry_run=True, now=NOW + DAY)
    assert (rep.new_versions, rep.unchanged, rep.new_objects, rep.index) == (1, 5, 1, "loaded")
    assert snapshot(arc.archive) == before

    shutil.move(arc.key_file, arc.tmp / "key-away.json")
    rep = archive.run(dry_run=True, now=NOW + DAY)  # no key: every file counts, stored objects still dedupe
    assert (rep.new_versions, rep.new_objects, rep.index) == (6, 1, "none")
    assert snapshot(arc.archive) == before and not arc.key_file.exists()
    shutil.move(arc.tmp / "key-away.json", arc.key_file)
    real = archive.run(now=NOW + DAY)
    assert (real.new_versions, real.new_objects) == (1, 1)
    assert snapshot(arc.tmp / "claude")[f"projects/{PROJ}/notes.txt"] == sources_before[0][f"projects/{PROJ}/notes.txt"]
    assert snapshot(arc.codex) == sources_before[1]


# ---------------------------------------------------------------- 16, 17: identity


def test_codex_session_id_is_the_thread_id(arc: Arc) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    ids = {r["path"]: (r["session_id"], r["role"], r["source"]) for r in versions(arc)}
    rel = {k: srcs[k].relative_to(arc.codex).as_posix() for k in ("codex_meta", "codex_file", "codex_bare")}
    assert ids[rel["codex_meta"]] == (f"codex:{CODEX_TID}", "codex", "codex")      # session_meta wins
    assert ids[rel["codex_file"]] == (f"codex:{FILE_TID}", "codex", "codex")      # the id in the name
    assert ids[rel["codex_bare"]] == ("codex:rollout-nothread", "codex", "codex")  # the file name
    assert rel["codex_file"].startswith("archived_sessions/")


def test_claude_main_and_subagent_identity(arc: Arc) -> None:  # noqa: F811
    srcs = _init_and_run(arc)
    ids = {r["path"]: r for r in versions(arc)}
    main = ids[srcs["main"].relative_to(arc.projects).as_posix()]
    sub = ids[srcs["sub"].relative_to(arc.projects).as_posix()]
    meta = ids[srcs["meta"].relative_to(arc.projects).as_posix()]
    assert (main["session_id"], main["role"], main["source"]) == (MAIN_ID, "main", "claude")
    assert (sub["session_id"], sub["role"]) == (f"{MAIN_ID}/{AGENT}", "subagent")
    assert (meta["session_id"], meta["role"]) == (f"{MAIN_ID}/{AGENT}", "subagent-meta")
    for r in (main, sub, meta):
        assert r["size"] > 0 and r["mtime_ns"] > 0 and r["first_seen"] == "2026-10-16T09:00:00Z"
        assert r["birthtime"] is None or r["birthtime"] > 0


def test_symlinks_are_not_followed(arc: Arc) -> None:  # noqa: F811
    make_sources(arc)
    outside = arc.tmp / "outside.jsonl"
    outside.write_bytes(line("outside the root"))
    (arc.projects / PROJ / "linked.jsonl").symlink_to(outside)
    (arc.projects / "linked-project").symlink_to(arc.tmp)
    rep = archive.run(dry_run=True, now=NOW)
    assert rep.files == 6


# ---------------------------------------------------------------- keys


def test_init_refuses_an_existing_archive_or_key(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    archive.init(now=NOW)
    key = archive.key_store().get("k1")
    with pytest.raises(archive.ArchiveError, match="already initialized"):
        archive.init()
    monkeypatch.setenv("AMEM_ARCHIVE_DIR", str(arc.tmp / "archive2"))
    with pytest.raises(archive.ArchiveError, match="refusing to overwrite"):
        archive.init()
    assert archive.key_store().get("k1") == key and not (arc.tmp / "archive2").exists()
    archive.init(reuse_key=True)
    assert archive.status().initialized
    (arc.tmp / "archive3").mkdir()
    (arc.tmp / "archive3" / "stray").write_text("x")
    monkeypatch.setenv("AMEM_ARCHIVE_DIR", str(arc.tmp / "archive3"))
    with pytest.raises(archive.ArchiveError, match="not empty"):
        archive.init(reuse_key=True)


def test_key_export_and_import_round_trip(arc: Arc, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    _init_and_run(arc)
    text = archive.export_key()
    kid, key = archive.parse_recovery_string(text)
    assert kid == "k1" and key == archive.key_store().get("k1") and text.startswith("amem-archive-key:1:k1:")
    with pytest.raises(archive.ArchiveError, match="checksum"):
        archive.parse_recovery_string(text[:-1] + ("0" if text[-1] != "0" else "1"))
    # A new machine: the archive dir came back from a backup, the key did not.
    monkeypatch.setenv("AMEM_ARCHIVE_KEY_FILE", str(arc.tmp / "keys2" / "key.json"))
    with pytest.raises(archive.KeyMissing):
        archive.verify()
    other = archive.recovery_string("k1", os.urandom(32))
    with pytest.raises(archive.ArchiveError, match="does not open"):
        archive.import_key(other)
    assert not (arc.tmp / "keys2" / "key.json").exists()
    assert archive.import_key(text) == ("k1", "stored")
    assert archive.verify().ok
    assert archive.import_key(f"  {text}\n") == ("k1", "already-present")
    # No archive yet: import creates one that uses the key.
    monkeypatch.setenv("AMEM_ARCHIVE_DIR", str(arc.tmp / "fresh"))
    assert archive.import_key(text) == ("k1", "already-present+archive-created")
    assert archive.status().initialized and archive.verify().ok
