"""Guarded T3 sessions (claude_roots.py, docs/sessions-live.md "Transcript roots"): patterns in the
roots file, Codex homes in the same file, and the T3 kind rule. Folder shapes and field names are
the real ones (guarded_support.py); the content is synthetic. Tmp dirs only. The archive side is in
tests/test_guarded_roots_archive.py."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import claude_roots, deviations, session_summary, sessions_ingest, timing_sync
from guarded_support import (
    claude_session,
    codex_session,
    guarded_home,
    plain_sdk_events,
    session_id,
    set_mtime,
    t3_events,
    thread_hash,
    write_jsonl,
    write_roots,
)
from test_deviations_support import (  # noqa: F401
    NOW,
    Home,
    headless_events,
    setup_home,
    tool,
    write_transcript,
)
from test_session_live import db  # noqa: F401

THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
OTHER_THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002"


@pytest.fixture
def control(setup_home: Home, monkeypatch: pytest.MonkeyPatch) -> Path:  # noqa: F811
    """No explicit folder: the roots file decides. `setup_home` sets AMEM_PROJECTS_DIR for other tests."""
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    monkeypatch.delenv("AMEM_CODEX_HOME", raising=False)
    return setup_home.root / "t3-run" / "control"


@pytest.fixture
def listed(control: Path, setup_home: Home) -> Path:  # noqa: F811
    """The roots file a guarded install would use."""
    write_roots(setup_home.claude,
                f"t3\t{control}/native/*/home",
                f"t3\t{control}/native/*/home\tcodex",
                f"t3-admissions\t{control}/owner-admissions/*/home\tcodex",
                f"t3-transitions\t{control}/transitions/*/home\tcodex")
    return control


def collect_all() -> list[sessions_ingest.SessionRow]:
    return sessions_ingest.collect(sessions_ingest.transcript_roots(), days=7, now=NOW)[0]


def ingest_all(db_url: str) -> sessions_ingest.IngestReport:
    return sessions_ingest.ingest(sessions_ingest.transcript_roots(), days=7, now=NOW, db_url=db_url)


def stored(db_url: str, session: str) -> dict[str, Any]:
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        row = conn.execute("select * from sessions where session_id = %s", (session,)).fetchone()
    assert row is not None
    return row


# ---------------------------------------------------------------- patterns


def test_a_pattern_finds_a_new_session_with_no_file_edit(listed: Path) -> None:
    first = claude_session(guarded_home(listed, "native", "thread-a"), session_id(1))
    assert [r.path for r in collect_all()] == [first]
    second = claude_session(guarded_home(listed, "native", "thread-b"), session_id(2))
    assert sorted(r.path for r in collect_all()) == sorted([first, second])


def test_each_match_is_one_root_with_a_stable_short_label(listed: Path) -> None:
    home = guarded_home(listed, "native", "thread-a")
    claude_session(home, session_id(1))
    label = f"t3-{thread_hash('thread-a')[:12]}"
    assert [r.label for r in claude_roots.claude_roots()[1:]] == [label]
    assert claude_roots.LABEL_RE.fullmatch(label)
    assert claude_roots.claude_roots()[1:][0].source == f"claude:{label}"
    [row] = collect_all()
    assert row.meta["claude_root"] == label
    for name in ("thread-0", "thread-z"):   # siblings before and after it in sort order
        claude_session(guarded_home(listed, "native", name), session_id(9))
    assert label in [r.label for r in claude_roots.claude_roots()]
    assert claude_roots.label_of(row.path) == label


@pytest.mark.parametrize("names", [("session-2026-10-01", "session-2026-10-02"),
                                   ("0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001", "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002"),
                                   ("Folder With Spaces", "folder.with.dots")])
def test_a_segment_that_is_no_digest_gets_a_label_from_its_whole_name(setup_home: Home, control: Path,  # noqa: F811
                                                                      names: tuple[str, str]) -> None:
    """Dated names and time-ordered UUIDs (`recovered/<uuid>`) share their first 12 characters."""
    write_roots(setup_home.claude, f"t3\t{control}/*/home")
    for name in names:
        (control / name / "home" / "projects").mkdir(parents=True)
    labels = [r.label for r in claude_roots.claude_roots()[1:]]
    assert len(set(labels)) == 2 and all(x and claude_roots.LABEL_RE.fullmatch(x) for x in labels)
    assert all(x and x.startswith("t3-") for x in labels)


def test_two_stars_match_one_level_each(setup_home: Home, control: Path) -> None:  # noqa: F811
    write_roots(setup_home.claude, f"t3\t{control}/*/*/home")
    (control / "native" / "aa" / "home" / "projects").mkdir(parents=True)
    (control / "native" / "ab" / "home" / "projects").mkdir(parents=True)
    (control / "native" / "ab" / "deeper" / "home" / "projects").mkdir(parents=True)   # three levels: no match
    found = [r.projects for r in claude_roots.claude_roots()[1:]]
    assert found == [control / "native" / "aa" / "home" / "projects", control / "native" / "ab" / "home" / "projects"]


def test_a_hidden_folder_is_no_match(setup_home: Home, control: Path) -> None:  # noqa: F811
    write_roots(setup_home.claude, f"t3\t{control}/native/*/home")
    (control / "native" / ".partial-copy" / "home" / "projects").mkdir(parents=True)
    claude_session(guarded_home(control, "native", "thread-a"), session_id(1))
    assert [r.label for r in claude_roots.claude_roots()[1:]] == [f"t3-{thread_hash('thread-a')[:12]}"]


def test_no_file_is_exact(control: Path, setup_home: Home) -> None:  # noqa: F811
    claude_session(guarded_home(control, "native", "thread-a"), session_id(1))
    codex_session(guarded_home(control, "transitions", "thread-b"), THREAD)
    assert claude_roots.claude_roots() == [claude_roots.ClaudeRoot(None, setup_home.projects)]
    assert claude_roots.codex_roots() == [claude_roots.CodexRoot(None, setup_home.root / ".codex")]
    assert claude_roots.pattern_bases() == []
    assert collect_all() == []


def test_a_pattern_that_matches_nothing_stops_nothing(setup_home: Home, control: Path, capfd: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    claude_roots._warned.clear()
    write_roots(setup_home.claude, f"t3\t{control}/native/*/home", f"t3-x\t{control}/transitions/*/home\tcodex")
    assert [r.label for r in claude_roots.claude_roots()] == [None]
    assert [r.label for r in claude_roots.codex_roots()] == [None]
    assert collect_all() == []
    assert "no folder at" in capfd.readouterr().err
    (control / "native").mkdir(parents=True)     # an empty folder is normal: no warning
    (control / "transitions").mkdir()
    claude_roots._warned.clear()
    assert [r.label for r in claude_roots.claude_roots()] == [None]
    assert capfd.readouterr().err == ""


@pytest.mark.parametrize("bad", ["**", "ho*me", "*.jsonl"])
def test_a_star_inside_a_name_is_a_bad_line(setup_home: Home, control: Path, bad: str, capfd: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    claude_roots._warned.clear()
    (control / "native" / "aa" / "home" / "projects").mkdir(parents=True)
    write_roots(setup_home.claude, f"t3\t{control}/native/{bad}/home")
    assert [r.label for r in claude_roots.claude_roots()] == [None]
    assert "a `*` must be a whole path component" in capfd.readouterr().err


def test_a_third_column_that_is_not_codex_is_a_bad_line(setup_home: Home, control: Path, capfd: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    claude_roots._warned.clear()
    (control / "projects").mkdir(parents=True)
    write_roots(setup_home.claude, f"t3\t{control}\tcursor", f"t3\t{control}\tcodex\textra")
    assert [r.label for r in claude_roots.claude_roots()] == [None]
    assert [r.label for r in claude_roots.codex_roots()] == [None]
    assert "expected <label><TAB><path>" in capfd.readouterr().err


def test_a_match_without_transcripts_of_this_tool_is_no_root(listed: Path) -> None:
    """Claude's own `home/sessions` holds pid files, not rollouts. It is no Codex root by itself."""
    claude_home = guarded_home(listed, "native", "claude-thread")
    claude_session(claude_home, session_id(1))
    assert [r.label for r in claude_roots.claude_roots()][1:] == [f"t3-{thread_hash('claude-thread')[:12]}"]
    assert [r.label for r in claude_roots.codex_roots()] == [None]
    (claude_home / "sessions").mkdir()
    (claude_home / "sessions" / "1234.json").write_text("{}")
    assert sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0] == []


def test_a_guarded_sessions_folder_is_no_codex_root_unless_listed(setup_home: Home, control: Path) -> None:  # noqa: F811
    home = guarded_home(control, "native", "codex-thread")
    codex_session(home, THREAD)
    claude_session(home, session_id(1))
    write_roots(setup_home.claude, f"t3\t{control}/native/*/home")
    assert sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0] == []
    write_roots(setup_home.claude, f"t3\t{control}/native/*/home", f"t3\t{control}/native/*/home\tcodex")
    assert [r.session_id for r in sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]] == [f"codex:{THREAD}"]


def test_an_explicit_home_ignores_the_codex_lines(listed: Path, setup_home: Home, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    codex_session(guarded_home(listed, "transitions", "thread-a"), THREAD)
    assert len(claude_roots.codex_roots()) == 2
    other = setup_home.root / "other-codex"
    assert claude_roots.codex_roots(other) == [claude_roots.CodexRoot(None, other)]
    monkeypatch.setenv("AMEM_CODEX_HOME", str(other))
    assert claude_roots.codex_roots() == [claude_roots.CodexRoot(None, other)]


def test_claude_row_resolves_after_new_siblings_appear_and_the_hash_gone_is_missing(listed: Path) -> None:
    home = guarded_home(listed, "native", "thread-a")
    path = claude_session(home, session_id(1))
    [row] = collect_all()
    assert deviations.transcript_path(row) == path
    for n, name in enumerate(("thread-0", "thread-b", "thread-z")):
        claude_session(guarded_home(listed, "native", name), session_id(n + 5))
    assert deviations.transcript_path(row) == path
    assert sessions_ingest.projects_for(row.meta) == home / "projects"
    shutil.rmtree(home.parent)
    assert sessions_ingest.projects_for(row.meta) == claude_roots.MISSING
    assert not deviations.transcript_path(row).exists()


def test_a_session_summary_finds_a_guarded_transcript_of_either_tool(listed: Path, db: str) -> None:  # noqa: F811
    claude = claude_session(guarded_home(listed, "native", "thread-a"), session_id(1))
    codex = codex_session(guarded_home(listed, "transitions", "thread-b"), THREAD)
    ingest_all(db)
    assert session_summary.transcript_of(stored(db, session_id(1))) == claude
    assert session_summary.transcript_of(stored(db, f"codex:{THREAD}")) == codex


# ---------------------------------------------------------------- Codex homes


def test_codex_two_roots_give_rows_from_both(listed: Path, setup_home: Home) -> None:  # noqa: F811
    default = setup_home.root / ".codex"
    plain = codex_session(default, THREAD, originator="codex_exec", source="exec")
    guarded = codex_session(guarded_home(listed, "transitions", "thread-a"), OTHER_THREAD)
    rows = {r.session_id: r for r in sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]}
    assert set(rows) == {f"codex:{THREAD}", f"codex:{OTHER_THREAD}"}
    assert "codex_root" not in rows[f"codex:{THREAD}"].meta
    label = f"t3-transitions-{thread_hash('thread-a')[:12]}"
    row = rows[f"codex:{OTHER_THREAD}"]
    assert row.meta["codex_root"] == label and row.project_dir == "sessions/2026/10/14"
    assert rows[f"codex:{THREAD}"].kind == "headless" and row.kind == "chat"
    assert deviations.transcript_path(row) == guarded
    assert deviations.transcript_path(rows[f"codex:{THREAD}"]) == plain


def test_a_codex_row_of_a_gone_hash_finds_no_same_named_default_file(listed: Path, setup_home: Home) -> None:  # noqa: F811
    home = guarded_home(listed, "transitions", "thread-a")
    codex_session(home, THREAD)
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    codex_session(setup_home.root / ".codex", THREAD)
    shutil.rmtree(home.parent)
    assert sessions_ingest.codex_home_for(row.meta) == claude_roots.MISSING
    assert not deviations.transcript_path(row).exists()


def test_a_bad_rollout_name_in_an_added_home_stays_missing(listed: Path) -> None:
    home = guarded_home(listed, "transitions", "thread-a")
    codex_session(home, THREAD)
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    row.meta["file"] = "../../secret.jsonl"
    assert deviations.transcript_path(row).name == "missing-transcript"


def test_same_rollout_in_two_transition_folders_gives_one_row_and_stays(listed: Path, db: str) -> None:  # noqa: F811
    """The same thread twice in one transition: one file newer than the other. Newest wins."""
    older = codex_session(guarded_home(listed, "transitions", "aa-source"), THREAD)
    newer = codex_session(guarded_home(listed, "transitions", "zz-target"), THREAD, text="say hello again")
    set_mtime(older, -30)
    set_mtime(newer, -10)
    rows, skipped = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))
    assert [r.path for r in rows] == [newer] and skipped == 1
    set_mtime(older, -5)
    set_mtime(newer, -10)
    assert [r.path for r in sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]] == [older]
    set_mtime(older, -30)
    set_mtime(newer, -10)
    first = ingest_all(db)
    assert first.new == 1
    label = f"t3-transitions-{thread_hash('zz-target')[:12]}"
    assert stored(db, f"codex:{THREAD}")["meta"]["codex_root"] == label
    for _ in range(2):
        again = ingest_all(db)
        assert (again.new, again.updated) == (0, 0)


def test_same_rollout_tie_goes_to_the_earlier_root_in_file_order(listed: Path, setup_home: Home) -> None:  # noqa: F811
    transit = codex_session(guarded_home(listed, "transitions", "aa"), THREAD)
    admit = codex_session(guarded_home(listed, "owner-admissions", "zz"), THREAD)
    for f in (transit, admit):
        set_mtime(f, -10)
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    assert row.path == admit   # owner-admissions is listed first
    default = codex_session(setup_home.root / ".codex", THREAD)
    set_mtime(default, -10)
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    assert row.path == default and "codex_root" not in row.meta


def test_same_thread_in_two_folders_of_one_home_gives_one_row(listed: Path, db: str) -> None:  # noqa: F811
    """`sessions` and `archived_sessions` of one home: two statements would flip the row each run."""
    home = guarded_home(listed, "transitions", "thread-a")
    live = codex_session(home, THREAD)
    archived = codex_session(home, THREAD, folder="archived_sessions")
    set_mtime(live, -10)
    set_mtime(archived, -20)
    assert ingest_all(db).new == 1
    assert stored(db, f"codex:{THREAD}")["project_dir"] == "sessions/2026/10/14"
    again = ingest_all(db)
    assert (again.new, again.updated) == (0, 0)


def test_a_shadow_symlink_across_roots_stays_one_row(listed: Path, setup_home: Home, db: str) -> None:  # noqa: F811
    """Tie on mtime: the plain file wins, in either root order."""
    real = codex_session(guarded_home(listed, "transitions", "thread-a"), THREAD)
    link = setup_home.root / ".codex" / "sessions/2026/10/14" / real.name
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    assert row.path == real
    assert ingest_all(db).new == 1
    again = ingest_all(db)
    assert (again.new, again.updated) == (0, 0)


def test_a_row_that_moves_back_to_the_default_home_loses_its_label(listed: Path, setup_home: Home, db: str) -> None:  # noqa: F811
    home = guarded_home(listed, "transitions", "thread-a")
    codex_session(home, THREAD)
    ingest_all(db)
    assert stored(db, f"codex:{THREAD}")["meta"]["codex_root"].startswith("t3-transitions-")
    codex_session(setup_home.root / ".codex", THREAD)
    shutil.rmtree(home.parent)
    report = ingest_all(db)
    assert report.updated == 1
    assert "codex_root" not in stored(db, f"codex:{THREAD}")["meta"]


def test_a_stored_guarded_codex_row_finds_its_transcript_again(listed: Path, db: str) -> None:  # noqa: F811
    path = codex_session(guarded_home(listed, "owner-admissions", "thread-a"), THREAD)
    ingest_all(db)
    row = deviations._row(stored(db, f"codex:{THREAD}"))
    assert deviations.transcript_path(row) == path


def test_a_home_title_stays_with_its_own_home(listed: Path) -> None:
    home = guarded_home(listed, "transitions", "thread-a")
    codex_session(home, THREAD)
    (home / "session_index.jsonl").write_text(f'{{"id": "{THREAD}", "thread_name": "Guarded title"}}\n')
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    assert row.title == "Guarded title"


def test_timing_sync_accepts_a_listed_codex_home_and_labels_the_row(listed: Path, db: str) -> None:  # noqa: F811
    path = codex_session(guarded_home(listed, "transitions", "thread-a"), THREAD)
    assert timing_sync.sync_transcript(path, db_url=db)["new"] == 1
    assert stored(db, f"codex:{THREAD}")["meta"]["codex_root"].startswith("t3-transitions-")


def test_timing_sync_refuses_an_unlisted_home(setup_home: Home, control: Path, db: str) -> None:  # noqa: F811
    path = codex_session(guarded_home(control, "transitions", "thread-a"), THREAD)
    with pytest.raises(ValueError, match="inside configured agent homes"):
        timing_sync.sync_transcript(path, db_url=db)


# ---------------------------------------------------------------- kinds


def test_a_t3_chat_in_a_guarded_root_is_a_chat(listed: Path) -> None:
    claude_session(guarded_home(listed, "native", "thread-a"), session_id(1))
    [row] = collect_all()
    assert (row.kind, row.meta["client"]) == ("chat", "t3")
    assert row.meta["entrypoints"] == ["sdk-ts"]


def test_a_t3_chat_with_no_guarded_root_is_a_chat_by_what_the_transcript_carries(setup_home: Home, control: Path) -> None:  # noqa: F811
    claude_session(setup_home.claude, session_id(1), flat="-work-demo-repo")
    [row] = collect_all()
    assert (row.kind, row.meta["client"]) == ("chat", "t3")
    assert "claude_root" not in row.meta


@pytest.mark.parametrize("field", ["system", "tools"])
def test_either_line_of_t3_evidence_is_enough(setup_home: Home, control: Path, field: str) -> None:  # noqa: F811
    events = t3_events(session_id(1))
    shape = next(e for e in events if e["type"] == "api-request-shape")["shape"]
    if field == "system":
        shape["tools"] = [{"name": "Bash"}]
    else:
        shape["system"] = [{"type": "text", "text": "You are Claude Code."}]
    events = [e for e in events if e["type"] != "attachment"]
    claude_session(setup_home.claude, session_id(1), events, flat="-work-demo-repo")
    assert collect_all()[0].kind == "chat"


def test_a_system_prompt_snapshot_alone_is_enough(setup_home: Home, control: Path) -> None:  # noqa: F811
    events = [e for e in t3_events(session_id(1)) if e["type"] != "api-request-shape"]
    claude_session(setup_home.claude, session_id(1), events, flat="-work-demo-repo")
    assert collect_all()[0].kind == "chat"


def test_a_t3_session_with_parent_evidence_is_a_parent(listed: Path) -> None:
    sid = session_id(1)
    events = t3_events(sid, blocks=(tool("Bash", "t1", command="parent-lock acquire"),))
    claude_session(guarded_home(listed, "native", "thread-a"), sid, events)
    [row] = collect_all()
    assert (row.kind, row.meta["client"]) == ("parent", "t3")


def test_a_t3_subagent_stays_a_worker(listed: Path) -> None:
    sid = session_id(1)
    home = guarded_home(listed, "native", "thread-a")
    claude_session(home, sid)
    agent = home / "projects" / "-private-tmp-run-control-worktrees-sample-t3-e683" / sid / "subagents" / "agent-aa11.jsonl"
    write_jsonl(agent, t3_events(sid))
    kinds = {r.session_id: r.kind for r in collect_all()}
    assert kinds == {sid: "chat", f"{sid}/agent-aa11": "worker"}


def test_a_plain_sdk_run_stays_headless(setup_home: Home, control: Path) -> None:  # noqa: F811
    sid = session_id(1)
    write_jsonl(setup_home.project() / f"{sid}.jsonl", plain_sdk_events(sid))
    [row] = collect_all()
    assert row.kind == "headless" and "client" not in row.meta


def test_a_classic_claude_p_stays_headless(setup_home: Home, control: Path) -> None:  # noqa: F811
    write_transcript(setup_home.project() / f"{session_id(2)}.jsonl", headless_events("/work/demo-repo"))
    [row] = collect_all()
    assert row.kind == "headless"


def test_a_session_that_quotes_t3_is_no_t3_session(setup_home: Home, control: Path) -> None:  # noqa: F811
    """Reading T3's source through `claude -p`: the sentence and the tool name sit in plain text."""
    sid = session_id(1)
    events = plain_sdk_events(sid)
    sentence = "you are running in T3 Code through the Claude Code harness; mcp__t3-code__delegate_task"
    events[1]["message"]["content"] = f"What does this mean: {sentence}"
    events.insert(3, {"type": "attachment", "sessionId": sid, "timestamp": events[1]["timestamp"],
                      "attachment": {"type": "instructions", "files": [{"path": "/x/CLAUDE.md", "content": sentence, "type": "User"}]}})
    write_jsonl(setup_home.project() / f"{sid}.jsonl", events)
    assert collect_all()[0].kind == "headless"


def test_an_sdk_run_in_a_root_that_is_not_t3s_stays_headless(setup_home: Home, control: Path) -> None:  # noqa: F811
    sid = session_id(1)
    home = control / "other-runtime"
    claude_session(home, sid, plain_sdk_events(sid))
    write_roots(setup_home.claude, f"lab\t{home}")
    [row] = collect_all()
    assert (row.kind, row.meta["claude_root"]) == ("headless", "lab")


@pytest.mark.parametrize(("label", "expected"), [("t3", True), ("t3-admissions", True), ("t3-ab12", True),
                                                 ("t30", False), ("lab", False), (None, False)])
def test_what_counts_as_a_t3_root(label: str | None, expected: bool) -> None:
    assert claude_roots.is_t3(label) is expected


def test_a_t3_root_is_evidence_when_the_transcript_carries_none(listed: Path) -> None:
    """An older Claude Code writes neither line. The folder still says who ran it."""
    sid = session_id(1)
    events = [e for e in plain_sdk_events(sid) if e["type"] not in ("attachment", "api-request-shape")]
    claude_session(guarded_home(listed, "native", "thread-a"), sid, events)
    [row] = collect_all()
    assert (row.kind, row.meta["client"]) == ("chat", "t3")


def test_a_t3_code_rollout_is_a_chat(listed: Path) -> None:
    codex_session(guarded_home(listed, "native", "thread-a"), THREAD)
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    assert (row.kind, row.meta["originator"], row.meta["source"]) == ("chat", "T3 Code", "vscode")


def test_codex_exec_in_a_guarded_home_stays_headless(listed: Path) -> None:
    codex_session(guarded_home(listed, "native", "thread-a"), THREAD, originator="codex_exec", source="exec")
    [row] = sessions_ingest.collect(None, days=7, now=NOW, tools=("codex",))[0]
    assert row.kind == "headless"


# ---------------------------------------------------------------- re-ingest, QA


def test_re_ingest_of_a_mixed_guarded_install_gives_zero_updates(listed: Path, setup_home: Home, db: str) -> None:  # noqa: F811
    claude_session(guarded_home(listed, "native", "thread-a"), session_id(1))
    codex_session(guarded_home(listed, "native", "thread-b"), OTHER_THREAD)
    codex_session(guarded_home(listed, "transitions", "thread-c"), THREAD)
    codex_session(guarded_home(listed, "owner-admissions", "thread-d"), THREAD)
    sid = session_id(2)
    write_jsonl(setup_home.project() / f"{sid}.jsonl", plain_sdk_events(sid))
    first = ingest_all(db)
    assert first.new == 4 and first.kinds == {"chat": 3, "headless": 1}
    for _ in range(2):
        again = ingest_all(db)
        assert (again.new, again.updated) == (0, 0)


def test_a_stored_headless_t3_row_turns_into_a_chat(setup_home: Home, control: Path, db: str) -> None:  # noqa: F811
    sid = session_id(1)
    plain = write_jsonl(setup_home.project() / f"{sid}.jsonl", plain_sdk_events(sid))
    ingest_all(db)
    assert stored(db, sid)["kind"] == "headless"
    write_jsonl(plain, t3_events(sid))
    assert ingest_all(db).updated == 1
    row = stored(db, sid)
    assert (row["kind"], row["category"], row["meta"]["client"]) == ("chat", "chat", "t3")


def test_a_t3_chat_grades_and_scans_like_any_chat(listed: Path, setup_home: Home, db: str) -> None:  # noqa: F811
    """Chat and headless grade alike. The scan order puts a chat one place ahead of headless."""
    assert deviations.objective_result("no-change", "chat") == deviations.objective_result("no-change", "headless") == 0.7
    assert deviations.qa_session("chat") and deviations.qa_session("headless")
    busy = tuple(tool("Read", f"t{n}", file_path="/work/demo-repo/README.md") for n in range(4))
    claude_session(guarded_home(listed, "native", "thread-a"), session_id(1), [*t3_events(session_id(1), busy), *_pad()])
    sid = session_id(2)
    write_jsonl(setup_home.project() / f"{sid}.jsonl", [*plain_sdk_events(sid, busy), *_pad()])
    ingest_all(db)
    picked = deviations.pick_sessions(since=NOW.replace(year=2025), limit=5, db_url=db)
    assert [(r.session_id, r.kind) for r in picked] == [(session_id(1), "chat"), (sid, "headless")]


def _pad() -> list[dict[str, Any]]:
    return [{"type": "progress", "data": "x" * 400} for _ in range(deviations.MIN_BYTES // 400 + 1)]
