"""Several Claude transcript roots (claude_roots.py, docs/sessions-live.md "Transcript roots"):
the roots file, ingest, the resolver and the kind rule. The archive side is in
tests/test_claude_roots_archive.py. Synthetic transcripts in the real shape: `uuid`, `sessionId`,
`entrypoint`, subagent folders with `agent-*.meta.json`, resume copies. Tmp dirs only."""

from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import (
    claude_roots,
    deviations,
    dispatch_share,
    session_find,
    session_live,
    session_resume,
    session_summary,
    sessions_ingest,
    timing_sync,
)
from conftest import run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    Home,
    assistant,
    busy,
    headless_events,
    make_all_kinds,
    parent_events,
    result,
    setup_home,
    text,
    tool,
    user,
    worker_events,
    write_transcript,
)
from test_session_live import _row, db, payload  # noqa: F401
from test_session_resume_chain import TA, TB, copy, segment, tokens_of

PROJECT = "-work-demo-repo"
SID = "11111111-0000-4000-8000-000000000001"
OTHER = "22222222-0000-4000-8000-000000000002"


@pytest.fixture
def roots(setup_home: Home, monkeypatch: pytest.MonkeyPatch) -> Home:  # noqa: F811
    """No explicit folder: the roots file decides. `setup_home` sets AMEM_PROJECTS_DIR for the other tests."""
    monkeypatch.delenv("AMEM_PROJECTS_DIR")
    return setup_home


def add_root(home: Home, label: str = "t3", name: str | None = None) -> Path:
    """A second Claude home, listed in the roots file. Returns its projects folder."""
    other = home.root / "runtime" / (name or label)
    (other / "projects").mkdir(parents=True, exist_ok=True)
    roots_file = home.claude / "parent" / "claude-roots.tsv"
    roots_file.parent.mkdir(parents=True, exist_ok=True)
    with roots_file.open("a", encoding="utf-8") as fh:
        fh.write(f"{label}\t{other}\n")
    return other / "projects"


def set_mtime(path: Path, minutes: int) -> None:
    stamp = (NOW + timedelta(minutes=minutes)).timestamp()
    os.utime(path, (stamp, stamp))


def main_row(events: list[dict[str, Any]], sid: str) -> list[dict[str, Any]]:
    """The real line shape: every line carries a `uuid` and the `sessionId`."""
    return [{**e, "uuid": f"{sid[:8]}-{i}", "sessionId": sid} for i, e in enumerate(events)]


def chat(home: Home, projects: Path, sid: str = SID, mtime: int = -60, tag: str = "x") -> Path:
    cwd = str(home.repo)
    events = [user(f"Please fix {tag}.", 0, cwd=cwd), assistant(text("Fixed."), minute=1, cwd=cwd), *busy(cwd)]
    path = write_transcript(projects / PROJECT / f"{sid}.jsonl", main_row(events, sid))
    set_mtime(path, mtime)
    return path


def collect(home: Home) -> tuple[dict[str, sessions_ingest.SessionRow], int]:
    rows, skipped = sessions_ingest.collect(sessions_ingest.transcript_roots(), days=7, now=NOW, tools=("claude",))
    return {r.session_id: r for r in rows}, skipped


def stored(url: str, sid: str) -> dict[str, Any]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        row = conn.execute("select * from sessions where session_id = %s", (sid,)).fetchone()
    assert row is not None
    return row


# ------------------------------------------------------------------ the roots file


def test_no_file_is_one_default_root(roots: Home) -> None:
    assert claude_roots.claude_roots() == [claude_roots.ClaudeRoot(None, roots.projects)]
    assert claude_roots.listed() == []
    assert claude_roots.label_of(roots.projects / PROJECT / "x.jsonl") is None


def test_explicit_folder_ignores_the_file(roots: Home, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    add_root(roots)
    assert [r.label for r in claude_roots.claude_roots()] == [None, "t3"]
    assert claude_roots.claude_roots(tmp_path) == [claude_roots.ClaudeRoot(None, tmp_path)]
    monkeypatch.setenv("AMEM_PROJECTS_DIR", str(tmp_path))
    assert claude_roots.claude_roots() == [claude_roots.ClaudeRoot(None, tmp_path)]


def test_bad_lines_are_skipped_with_a_warning(roots: Home, capfd: pytest.CaptureFixture[str]) -> None:
    good = roots.root / "good"
    (good / "projects").mkdir(parents=True)
    other = roots.root / "other"
    (other / "projects").mkdir(parents=True)
    f = claude_roots.roots_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes("\n".join([
        "# a comment", "", f"t3\t{good}", f"BadLabel\t{other}", f"default\t{other}", f"claude\t{other}",
        "no-tab-here", f"relative\tsome/where", f"t3\t{other}", f"copy\t{good}", "nul\t/tmp/a\x00b",
        f"-dash\t{other}", f"fine\t{other}\r",
    ]).encode())
    assert [(r.label, r.projects) for r in claude_roots.listed()] == [("t3", good / "projects"),
                                                                      ("fine", other / "projects")]
    err = capfd.readouterr().err
    for word in ("bad label 'BadLabel'", "reserved label 'default'", "reserved label 'claude'", "expected <label>",
                 "not absolute", "duplicate label 't3'", "already listed", "bad label '-dash'"):
        assert word in err
    claude_roots.listed()
    assert capfd.readouterr().err == ""       # one warning per process, not one per row


def test_a_roots_file_that_cannot_be_read_stops_nothing(roots: Home) -> None:
    claude_roots.roots_file().mkdir(parents=True)
    assert claude_roots.claude_roots() == [claude_roots.ClaudeRoot(None, roots.projects)]


def test_a_symlink_to_the_default_root_is_no_second_root(roots: Home) -> None:
    alias = roots.root / "alias"
    alias.mkdir()
    (alias / "projects").symlink_to(roots.projects)
    f = claude_roots.roots_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"alias\t{alias}\n")
    chat(roots, roots.projects)
    assert claude_roots.claude_roots() == [claude_roots.ClaudeRoot(None, roots.projects)]
    rows, skipped = collect(roots)
    assert list(rows) == [SID] and skipped == 0 and "claude_root" not in rows[SID].meta


def test_a_listed_root_that_is_gone_is_skipped(roots: Home) -> None:
    projects = add_root(roots)
    projects.rmdir()
    assert [r.label for r in claude_roots.claude_roots()] == [None]
    assert [r.label for r in claude_roots.listed()] == ["t3"]       # still known: the resolver and archive need it
    assert claude_roots.resolve("t3") == projects


# ------------------------------------------------------------------ no file: as before


def test_no_file_keeps_rows_identical(roots: Home) -> None:
    """The rows of a realistic set of all kinds, through the path every caller now takes."""
    make_all_kinds(roots)
    before, skipped = sessions_ingest.collect(roots.projects, days=7, now=NOW)
    after, skipped_after = sessions_ingest.collect(sessions_ingest.transcript_roots(), days=7, now=NOW)
    assert len(before) >= 6 and skipped == skipped_after
    assert [r.session_id for r in before] == [r.session_id for r in after]
    for a, b in zip(before, after, strict=True):
        assert (a.kind, a.project_dir, a.meta, a.skills, a.started_at) == (b.kind, b.project_dir, b.meta, b.skills,
                                                                           b.started_at)
        assert "claude_root" not in a.meta


# ------------------------------------------------------------------ two roots


def test_two_roots_give_rows_from_both(roots: Home, db: str) -> None:  # noqa: F811
    t3 = add_root(roots)
    chat(roots, roots.projects, SID)
    chat(roots, t3, OTHER, tag="y")
    sub = t3 / PROJECT / OTHER / "subagents"
    write_transcript(sub / "agent-w1.jsonl", main_row(worker_events(str(roots.repo)), OTHER))
    (sub / "agent-w1.meta.json").write_text(json.dumps({"agentType": "worker", "toolUseId": "a1"}))
    rows, skipped = collect(roots)
    assert set(rows) == {SID, OTHER, f"{OTHER}/agent-w1"} and skipped == 0
    assert "claude_root" not in rows[SID].meta
    assert rows[OTHER].meta["claude_root"] == rows[f"{OTHER}/agent-w1"].meta["claude_root"] == "t3"
    assert rows[f"{OTHER}/agent-w1"].kind == "worker"
    assert rows[OTHER].meta["subagent_tokens"]["sessions"] == 1

    report = sessions_ingest.ingest(sessions_ingest.transcript_roots(), days=7, now=NOW, db_url=db, tools=("claude",))
    assert report.new == 3 and report.kinds == {"chat": 2, "worker": 1}
    assert stored(db, OTHER)["meta"]["claude_root"] == "t3"
    assert "claude_root" not in stored(db, SID)["meta"]
    again = sessions_ingest.ingest(sessions_ingest.transcript_roots(), days=7, now=NOW, db_url=db, tools=("claude",))
    assert (again.new, again.updated, again.files) == (0, 0, 3)


def test_cli_ingest_reads_every_root(roots: Home, db: str) -> None:  # noqa: F811
    t3 = add_root(roots)
    chat(roots, roots.projects, SID, mtime=0)
    chat(roots, t3, OTHER, mtime=0)
    now = time.time()
    for f in (roots.projects / PROJECT / f"{SID}.jsonl", t3 / PROJECT / f"{OTHER}.jsonl"):
        os.utime(f, (now, now))
    env = {"AMEM_DB_URL": db, "AMEM_CLAUDE_HOME": str(roots.claude), "AMEM_CODEX_HOME": str(roots.root / "no-codex"),
           "AMEM_AGENT_CONFIG_DIR": str(roots.agent_config)}
    proc = run_amem("sessions", "ingest", "--days", "3650", env=env)
    assert proc.returncode == 0, proc.stderr
    assert "2 new" in proc.stdout
    assert stored(db, OTHER)["meta"]["claude_root"] == "t3"


# ------------------------------------------------------------------ the same session id in two roots


def test_same_id_newest_file_wins_and_stays(roots: Home, db: str) -> None:  # noqa: F811
    t3 = add_root(roots)
    chat(roots, roots.projects, SID, mtime=-120, tag="old")
    newer = t3 / "-elsewhere" / f"{SID}.jsonl"
    cwd = str(roots.repo)
    write_transcript(newer, main_row([user("Newer.", 0, cwd=cwd), *busy(cwd)], SID))
    set_mtime(newer, -60)
    rows, skipped = collect(roots)
    assert skipped == 1
    assert rows[SID].project_dir == "-elsewhere" and rows[SID].meta["claude_root"] == "t3"
    run = sessions_ingest.transcript_roots()
    first = sessions_ingest.ingest(run, days=7, now=NOW, db_url=db, tools=("claude",))
    assert (first.new, first.updated, first.skipped) == (1, 0, 1)
    for _ in range(2):      # no flip of project_dir or meta.claude_root
        again = sessions_ingest.ingest(run, days=7, now=NOW, db_url=db, tools=("claude",))
        assert (again.new, again.updated) == (0, 0)
    row = stored(db, SID)
    assert row["project_dir"] == "-elsewhere" and row["meta"]["claude_root"] == "t3"


def test_same_id_tie_goes_to_the_earlier_root(roots: Home) -> None:
    t3 = add_root(roots)
    chat(roots, roots.projects, SID, mtime=-60)
    chat(roots, t3, SID, mtime=-60)
    rows, skipped = collect(roots)
    assert skipped == 1 and "claude_root" not in rows[SID].meta


def test_a_row_that_moves_back_to_the_default_root_loses_its_label(roots: Home, db: str) -> None:  # noqa: F811
    t3 = add_root(roots)
    default = chat(roots, roots.projects, SID, mtime=-120)
    chat(roots, t3, SID, mtime=-60)
    run = sessions_ingest.transcript_roots()
    sessions_ingest.ingest(run, days=7, now=NOW, db_url=db, tools=("claude",))
    assert stored(db, SID)["meta"]["claude_root"] == "t3"
    set_mtime(default, -30)
    moved = sessions_ingest.ingest(run, days=7, now=NOW, db_url=db, tools=("claude",))
    assert moved.updated == 1 and "claude_root" not in stored(db, SID)["meta"]
    assert sessions_ingest.ingest(run, days=7, now=NOW, db_url=db, tools=("claude",)).updated == 0


def test_a_pure_copy_never_hides_the_real_transcript(roots: Home) -> None:
    """Newest file, but a pure copy of an earlier transcript in its root: no row, so no win."""
    t3 = add_root(roots)
    cwd = str(roots.repo)
    real = segment("a", SID, 0, TA, "spark", cwd)
    write_transcript(roots.projects / PROJECT / f"{SID}.jsonl", real, pad=False)
    set_mtime(roots.projects / PROJECT / f"{SID}.jsonl", -120)
    write_transcript(t3 / PROJECT / f"{OTHER}.jsonl", real, pad=False)
    time.sleep(0.01)
    write_transcript(t3 / PROJECT / f"{SID}.jsonl", copy(real, OTHER)[:1], pad=False)
    rows, _skipped = collect(roots)
    assert "claude_root" not in rows[SID].meta


def test_subagents_of_a_session_in_two_roots_are_summed_once(roots: Home) -> None:
    t3 = add_root(roots)
    chat(roots, roots.projects, SID, mtime=-120)
    chat(roots, t3, SID, mtime=-60)
    cwd = str(roots.repo)
    for projects, stem in ((roots.projects, "agent-a"), (t3, "agent-b"), (roots.projects, "agent-both"),
                           (t3, "agent-both")):
        events = [{**reply_event(cwd), "uuid": f"{stem}-1", "sessionId": SID}]
        write_transcript(projects / PROJECT / SID / "subagents" / f"{stem}.jsonl", events, pad=False)
    rows, _skipped = collect(roots)
    assert rows[SID].meta["claude_root"] == "t3"
    assert rows[SID].meta["subagent_tokens"]["sessions"] == 3


def reply_event(cwd: str) -> dict[str, Any]:
    from test_session_tokens import reply, usage

    return reply("msg_sub", usage(1, 2, 3, 4), minute=1, cwd=cwd)


# ------------------------------------------------------------------ resume


def resume_files(home: Home, first: Path, second: Path) -> None:
    cwd = str(home.repo)
    a = segment("a", SID, 0, TA, "spark", cwd)
    b = copy(a, OTHER) + segment("b", OTHER, 30, TB, "handoff", cwd)
    write_transcript(first / PROJECT / f"{SID}.jsonl", a, pad=False)
    time.sleep(0.01)
    write_transcript(second / PROJECT / f"{OTHER}.jsonl", b, pad=False)


def test_resume_inside_an_added_root(roots: Home) -> None:
    t3 = add_root(roots)
    resume_files(roots, t3, t3)
    rows, _skipped = collect(roots)
    assert tokens_of(rows[OTHER]) == TB and rows[OTHER].meta["resumed_from"] == SID
    assert rows[OTHER].meta["claude_root"] == "t3"


def test_resume_chain_across_roots_counts_each_line_once(roots: Home) -> None:
    """A guarded chat resumes a native one: the copy is in the added root, the original in the default."""
    t3 = add_root(roots)
    resume_files(roots, roots.projects, t3)
    rows, _skipped = collect(roots)
    assert (tokens_of(rows[SID]), tokens_of(rows[OTHER])) == (TA, TB)
    assert rows[OTHER].meta["resumed_from"] == SID and rows[OTHER].meta["claude_root"] == "t3"
    assert rows[OTHER].meta["copied_lines"] == 3
    assert "resumed_from" not in rows[SID].meta


def test_same_session_in_another_root_is_no_earlier_transcript(roots: Home) -> None:
    """The same id continued in a second root keeps all its lines: it did not resume itself."""
    t3 = add_root(roots)
    cwd = str(roots.repo)
    a = segment("a", SID, 0, TA, "spark", cwd)
    write_transcript(roots.projects / PROJECT / f"{SID}.jsonl", a, pad=False)
    time.sleep(0.01)
    path = write_transcript(t3 / PROJECT / f"{SID}.jsonl", a + segment("b", SID, 30, TB, "handoff", cwd), pad=False)
    assert session_resume.resume_of(path) == session_resume.NONE
    rows, _skipped = collect(roots)
    assert rows[SID].meta["claude_root"] == "t3"
    assert tokens_of(rows[SID]).output == TA.output + TB.output


# ------------------------------------------------------------------ the resolver


def row_of(meta: dict[str, Any], *, sid: str = SID, project_dir: str = PROJECT) -> sessions_ingest.SessionRow:
    return sessions_ingest.SessionRow(sid, "chat", None, project_dir, None, None, 0, [], [], meta)


def test_missing_label_means_the_default_root(roots: Home) -> None:
    add_root(roots)
    path = chat(roots, roots.projects)
    assert deviations.transcript_path(row_of({})) == path
    assert sessions_ingest.projects_for({}) == sessions_ingest.projects_for(None) == roots.projects


def test_a_label_finds_its_own_root(roots: Home) -> None:
    t3 = add_root(roots)
    chat(roots, roots.projects)            # a same-ID file in the default root is not this row's
    path = chat(roots, t3, tag="own")
    sub = t3 / PROJECT / SID / "subagents" / "agent-w1.jsonl"
    assert deviations.transcript_path(row_of({"claude_root": "t3"})) == path
    worker = row_of({"claude_root": "t3", "parent_session": SID}, sid=f"{SID}/agent-w1")
    assert deviations.transcript_path(worker) == sub
    marker = t3 / PROJECT / f"{OTHER}{deviations.RELEASED_SUFFIX}"
    marker.write_text("{}")
    assert deviations.released_marker(row_of({"claude_root": "t3"}, sid=OTHER)) == marker
    assert deviations.released_marker(row_of({}, sid=OTHER)) is None


@pytest.mark.parametrize("label", ["removed", "default", "claude", 7, ""])
def test_an_unknown_label_never_finds_a_same_id_file(roots: Home, label: Any) -> None:
    add_root(roots)
    chat(roots, roots.projects)
    path = deviations.transcript_path(row_of({"claude_root": label}))
    assert not path.is_file() and sessions_ingest.projects_for({"claude_root": label}) == claude_roots.MISSING
    rec = {"session_id": SID, "kind": "chat", "project_dir": "-gone", "meta": {"claude_root": label}}
    assert not session_summary.transcript_of(rec).is_file()   # the moved-session glob finds nothing


def test_a_removed_entry_leaves_a_stored_row_without_a_transcript(roots: Home) -> None:
    t3 = add_root(roots)
    chat(roots, t3)
    chat(roots, roots.projects)
    claude_roots.roots_file().unlink()
    assert not deviations.transcript_path(row_of({"claude_root": "t3"})).is_file()


def test_moved_session_lookup_stays_inside_the_rows_root(roots: Home) -> None:
    t3 = add_root(roots)
    in_t3 = chat(roots, t3)
    moved_t3 = in_t3.parent.parent / "-moved" / in_t3.name
    moved_t3.parent.mkdir()
    in_t3.rename(moved_t3)
    decoy = chat(roots, roots.projects)
    moved_default = decoy.parent.parent / "-moved-too" / decoy.name
    moved_default.parent.mkdir()
    decoy.rename(moved_default)
    rec = {"session_id": SID, "kind": "chat", "project_dir": PROJECT}
    assert session_summary.transcript_of({**rec, "meta": {"claude_root": "t3"}}) == moved_t3
    assert session_summary.transcript_of({**rec, "meta": {}}) == moved_default


def test_timing_sync_accepts_a_listed_root_and_labels_the_row(roots: Home, db: str) -> None:  # noqa: F811
    t3 = add_root(roots)
    path = chat(roots, t3)
    got = timing_sync.sync_transcript(path, db_url=db)
    assert got["new"] == 1
    assert stored(db, SID)["meta"]["claude_root"] == "t3"
    outside = chat(roots, roots.root / "not-a-root", OTHER)
    with pytest.raises(ValueError, match="agent homes"):
        timing_sync.sync_transcript(outside, db_url=db)


def test_session_find_ingests_from_an_added_root(roots: Home, db: str) -> None:  # noqa: F811
    t3 = add_root(roots)
    cwd = str(roots.repo)
    write_transcript(t3 / PROJECT / SID / "subagents" / "agent-w1.jsonl", main_row(worker_events(cwd), SID))
    assert session_find.ingest("w1", [r.projects for r in claude_roots.claude_roots()], db_url=db) == (1, 1, 0)
    assert stored(db, f"{SID}/agent-w1")["meta"]["claude_root"] == "t3"


def test_token_share_counts_every_root(roots: Home) -> None:
    t3 = add_root(roots)
    start, end = NOW - timedelta(days=2), NOW + timedelta(days=1)

    def line(mid: str, tokens: int) -> str:
        return json.dumps({"timestamp": NOW.isoformat(), "sessionId": SID,
                           "message": {"id": mid, "usage": {"input_tokens": tokens, "output_tokens": 0}}}) + "\n"

    for projects, sid, tokens in ((roots.projects, SID, 100), (t3, OTHER, 300)):
        (projects / PROJECT).mkdir(parents=True)
        (projects / PROJECT / f"{sid}.jsonl").write_text(line(sid, tokens))
    many = dispatch_share.claude_window([r.projects for r in claude_roots.claude_roots()], start, end, SID)
    assert (many["own"], many["total"], many["sessions"]) == (100, 400, 2)
    assert dispatch_share.claude_window(roots.projects, start, end, SID)["total"] == 100


# ------------------------------------------------------------------ kind


def sdk(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**e, "entrypoint": "sdk-ts"} for e in events]


def test_sdk_with_parent_evidence_is_a_parent(roots: Home) -> None:
    cwd = str(roots.repo)
    proj = roots.projects / PROJECT
    write_transcript(proj / "all.jsonl", sdk(parent_events(cwd) + busy(cwd)))
    write_transcript(proj / "lock.jsonl", sdk([user("Go.", 0, cwd=cwd), assistant(
        tool("Bash", "b1", command="parent-lock acquire demo"), minute=1, cwd=cwd), result("b1", "ok", 1, cwd=cwd)]))
    write_transcript(proj / "spawn.jsonl", sdk([user("Go.", 0, cwd=cwd), assistant(
        tool("Agent", "a1", subagent_type="worker", description="w", prompt="x"), minute=1, cwd=cwd)]))
    write_transcript(proj / "skill.jsonl", sdk([user("Go.", 0, cwd=cwd), assistant(
        tool("Skill", "s1", skill="dispatch"), minute=1, cwd=cwd)]))
    rows, _skipped = collect(roots)
    assert {sid: r.kind for sid, r in rows.items()} == dict.fromkeys(("all", "lock", "spawn", "skill"), "parent")
    assert rows["all"].meta["entrypoints"] == ["sdk-ts"]


def test_sdk_without_evidence_stays_headless(roots: Home) -> None:
    cwd = str(roots.repo)
    write_transcript(roots.projects / PROJECT / "h.jsonl", headless_events(cwd))
    write_transcript(roots.projects / PROJECT / "e.jsonl", sdk([
        user("Go.", 0, cwd=cwd), assistant(tool("Agent", "a1", subagent_type="Explore", description="x", prompt="y"),
                                           minute=1, cwd=cwd)]))
    rows, _skipped = collect(roots)
    assert rows["h"].kind == rows["e"].kind == "headless" and rows["h"].meta["entrypoints"] == ["sdk-cli"]


def test_evidence_does_not_beat_eval_worker_or_routine(roots: Home) -> None:
    cwd = str(roots.repo)
    evidence = sdk(parent_events(cwd))
    routine = [user('<scheduled-task name="nightly-check" file="/x/SKILL.md">\nRun.\n</scheduled-task>', 0, cwd=cwd,
                    entrypoint="sdk-cli"), *evidence]
    write_transcript(roots.projects / PROJECT / "r.jsonl", routine)
    write_transcript(roots.projects / PROJECT / "p" / "subagents" / "agent-w.jsonl", evidence)
    write_transcript(roots.projects / "-home--cache-agent-evals-eval-jo-a" / "e.jsonl", evidence)
    rows, _skipped = collect(roots)
    assert (rows["r"].kind, rows["p/agent-w"].kind, rows["e"].kind) == ("routine", "worker", "eval")


def test_a_later_ping_does_not_downgrade_an_ingested_parent(roots: Home, db: str, monkeypatch: pytest.MonkeyPatch  # noqa: F811
                                                            ) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-ts")
    monkeypatch.setenv("AMEM_PING_ALLOW_LOCAL", "1")
    cwd = str(roots.repo)
    path = write_transcript(roots.projects / PROJECT / f"{SID}.jsonl",
                            main_row(sdk(parent_events(cwd) + busy(cwd)), SID))
    sessions_ingest.ingest(sessions_ingest.transcript_roots(), days=7, now=NOW, db_url=db, tools=("claude",))
    assert (stored(db, SID)["kind"], stored(db, SID)["category"]) == ("parent", "parent")
    ping = session_live.Ping("Stop", SID, cwd, path)
    entry: dict[str, Any] = {}
    rec = session_live.build_record(ping, entry, NOW + timedelta(hours=1))
    assert rec["kind"] == "headless"                # a hook sees no evidence
    with psycopg.connect(db) as conn:
        conn.execute(session_live.PING_SQL, session_live.record_params(rec))
    assert (stored(db, SID)["kind"], stored(db, SID)["category"]) == ("parent", "parent")


def test_a_ping_labels_a_row_of_an_added_root(roots: Home, db: str) -> None:  # noqa: F811
    t3 = add_root(roots)
    path = chat(roots, t3)
    entry: dict[str, Any] = {}
    rec = session_live.build_record(session_live.Ping("SessionStart", SID, str(roots.repo), path), entry, NOW)
    assert rec["meta"]["claude_root"] == "t3"
    default = chat(roots, roots.projects, OTHER)
    rec = session_live.build_record(session_live.Ping("SessionStart", OTHER, str(roots.repo), default), entry, NOW)
    assert "claude_root" not in rec["meta"]


def test_objective_result_of_a_moved_session() -> None:
    """Report item: what moving headless -> parent does to the grade."""
    assert deviations.objective_result("no-change", "headless") == 0.7
    assert deviations.objective_result("no-change", "parent") is None
    assert deviations.objective_result("committed", "headless") == deviations.objective_result("committed", "parent")


def test_scan_priority_of_a_moved_session(roots: Home, db: str) -> None:  # noqa: F811
    """Report item: `_PICK_SQL` scans an SDK parent before a chat. It stays eligible either way, and
    a session with skills goes first whatever its kind."""
    cwd = str(roots.repo)
    proj = roots.projects / PROJECT
    spawn = [user("Go.", 0, cwd=cwd), assistant(tool("Agent", "a1", subagent_type="worker", description="w",
                                                     prompt="x"), minute=1, cwd=cwd), *busy(cwd)]
    write_transcript(proj / "sdk-spawn.jsonl", sdk(spawn))
    write_transcript(proj / "sdk-plain.jsonl", sdk([user("Go.", 0, cwd=cwd), *busy(cwd)]))
    write_transcript(proj / "plain-chat.jsonl", [user("Go.", 0, cwd=cwd), *busy(cwd)])
    write_transcript(proj / "sdk-dispatch.jsonl", sdk(parent_events(cwd) + busy(cwd)))
    sessions_ingest.ingest(sessions_ingest.transcript_roots(), days=7, now=NOW, db_url=db, tools=("claude",))
    picked = deviations.pick_sessions(since=NOW - timedelta(days=7), limit=10, db_url=db)
    assert [(r.session_id, r.kind) for r in picked] == [("sdk-dispatch", "parent"), ("sdk-spawn", "parent"),
                                                        ("plain-chat", "chat"), ("sdk-plain", "headless")]


def test_a_stored_parent_row_is_read_with_parent_instructions(roots: Home) -> None:
    """Report item: kind `parent` adds PARENT.md to the instructions a session is graded against."""
    cwd = str(roots.repo)
    write_transcript(roots.projects / PROJECT / "p.jsonl", sdk(parent_events(cwd) + busy(cwd)))
    write_transcript(roots.projects / PROJECT / "h.jsonl", sdk([user("Go.", 0, cwd=cwd), *busy(cwd)]))
    rows, _skipped = collect(roots)
    assert "parent-md" in rows["p"].instructions and "parent-md" not in rows["h"].instructions


# ------------------------------------------------------------------ Codex: the shared home only


def rollout(path: Path, thread: str, cwd: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"timestamp": "2026-10-14T09:00:00Z", "type": "session_meta",
         "payload": {"id": thread, "cwd": cwd, "originator": "Codex Desktop", "source": "vscode"}},
        {"timestamp": "2026-10-14T09:01:00Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "Please fix it."}},
        {"timestamp": "2026-10-14T09:02:00Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Done."}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


THREAD = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
ROLLOUT = f"rollout-2026-10-14T09-00-00-{THREAD}.jsonl"


def codex_rows(home: Path) -> list[sessions_ingest.SessionRow]:
    return sessions_ingest.collect(None, days=7, now=NOW, codex=home, tools=("codex",))[0]


def test_codex_never_follows_an_added_claude_root(roots: Home) -> None:
    """An added root's home has a `.codex` next to it. The shared home stays the only one read."""
    t3 = add_root(roots)
    shadow = t3.parent.parent / ".codex"
    rollout(shadow / "sessions/2026/10/14" / ROLLOUT, THREAD, str(roots.repo))
    shared = roots.claude.parent / ".codex"
    assert sessions_ingest.codex_home() == shared
    rows, _skipped = sessions_ingest.collect(sessions_ingest.transcript_roots(), days=7, now=NOW, tools=("codex",))
    assert rows == []
    rollout(shared / "sessions/2026/10/14" / ROLLOUT, THREAD, str(roots.repo))
    rows, _skipped = sessions_ingest.collect(sessions_ingest.transcript_roots(), days=7, now=NOW, tools=("codex",))
    assert [r.path for r in rows] == [shared / "sessions/2026/10/14" / ROLLOUT]


def test_a_shadow_sessions_symlink_gives_no_duplicate(setup_home: Home, db: str) -> None:  # noqa: F811
    shared = setup_home.root / "shared"
    rollout(shared / "sessions/2026/10/14" / ROLLOUT, THREAD, str(setup_home.repo))
    shadow = setup_home.root / "shadow"
    shadow.mkdir()
    (shadow / "sessions").symlink_to(shared / "sessions")
    (shadow / "archived_sessions").symlink_to(shared / "sessions")   # the same folder twice
    rows = codex_rows(shadow)
    assert [r.session_id for r in rows] == [f"codex:{THREAD}"]
    first = sessions_ingest.ingest(None, days=7, now=NOW, db_url=db, codex=shadow, tools=("codex",))
    assert first.new == 1
    assert sessions_ingest.ingest(None, days=7, now=NOW, db_url=db, codex=shared, tools=("codex",)).new == 0


def test_a_symlinked_rollout_gives_one_row_and_no_flip(setup_home: Home, db: str) -> None:  # noqa: F811
    home = setup_home.root / "shared"
    plain = rollout(home / "sessions/2026/10/14" / ROLLOUT, THREAD, str(setup_home.repo))
    for folder in ("sessions/2026/10/13", "archived_sessions"):
        link = home / folder / ROLLOUT
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(plain)
    rows = codex_rows(home)
    assert [(r.session_id, r.project_dir) for r in rows] == [(f"codex:{THREAD}", "sessions/2026/10/14")]
    sessions_ingest.ingest(None, days=7, now=NOW, db_url=db, codex=home, tools=("codex",))
    again = sessions_ingest.ingest(None, days=7, now=NOW, db_url=db, codex=home, tools=("codex",))
    assert (again.new, again.updated) == (0, 0)
