"""`amem deviations scan|report` against the throwaway Postgres: storage, errors, re-scans,
Jev state, dry runs, the CLI gate and the candidates JSON."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from agent_memory import deviations, distill, jev, sessions_ingest
from conftest import base_env, run_amem
from test_deviations_support import (  # noqa: F401
    NOW,
    FakeJev,
    FakeWriter,
    Home,
    envelope,
    fake_jev,
    finding,
    make_all_kinds,
    setup_home,
    writer,
    write_transcript,
    chat_events,
)

Writer = Callable[..., FakeWriter]


@pytest.fixture
def db(setup_home: Home, pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:  # noqa: F811
    with psycopg.connect(pg_url) as conn:
        conn.execute("delete from deviations")
        conn.execute("delete from sessions")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    yield pg_url


def _ingest(home: Home, url: str) -> dict[str, str]:
    ids = make_all_kinds(home)
    sessions_ingest.ingest(home.projects, days=7, now=NOW, db_url=url)
    return ids


def _q(url: str, sql: str, *args: object) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


def _sources_in(prompt: str) -> set[str]:
    line = next(ln for ln in prompt.splitlines() if ln.startswith("- instruction_source: exactly one of: "))
    return set(line.split(": ", 2)[2].split(", "))


def _reply_for(prompt: str) -> str:
    """A deviation on the global CLAUDE.md for every session, plus one on dispatch when offered."""
    items = [finding()]
    if "skill:dispatch" in _sources_in(prompt):
        items.append(finding("skill:dispatch", "Brief", 2, 0.1, 0.4))
    return envelope({"deviations": items})


def test_scan_stores_deviations_and_marks_sessions(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                   fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    w = writer(default=_reply_for)
    report = deviations.scan(now=NOW)
    # eval is never picked; skills first, then routine, parent, worker, chat, headless.
    assert [o.session_id for o in report.outcomes] == [ids["chat"], ids["parent"], ids["routine"],
                                                        ids["worker"], ids["headless"]]
    assert report.summary() == "deviations: sessions=5 found=6 edit=6 enforce=0 leave=0 errors=0"
    assert len(w.prompts) == 5
    rows = _q(db, "select session_id, instruction_source, instruction_ref, turn, followed_grade, result_grade, "
                  "jev_verdict, jev_confidence, jev_mode, writer_model from deviations "
                  "where session_id = %s order by instruction_source", ids["parent"])
    assert rows == [
        (ids["parent"], "claude-md:global", "Explaining", 1, pytest.approx(0.3), pytest.approx(0.8),
         "edit_instruction", pytest.approx(0.8), "fixture", "claude-cli:sonnet"),
        (ids["parent"], "skill:dispatch", "Brief", 2, pytest.approx(0.1), pytest.approx(0.4),
         "edit_instruction", pytest.approx(0.8), "fixture", "claude-cli:sonnet"),
    ]
    scanned = _q(db, "select session_id from sessions where scanned_at is not null order by 1")
    assert {r[0] for r in scanned} == {ids[k] for k in ("chat", "parent", "routine", "worker", "headless")}


def test_writer_prompt_holds_instructions_and_digest_but_no_env(setup_home: Home, db: str,  # noqa: F811
                                                                writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    w = writer(default=envelope({"deviations": []}))
    deviations.scan(session_id=ids["parent"], now=NOW)
    (prompt,) = w.prompts
    assert _sources_in(prompt) == {"claude-md:global", "parent-md", "skill:dispatch", "claude-md:demo-repo"}
    assert "Merge to integration only." in prompt and "Every brief has a DoD." in prompt
    assert "TOOL Agent: worker worker-api" in prompt
    assert "do-not-read-me" not in prompt


def test_jev_state_never_holds_transcript_or_writer_text(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                         fake_jev: FakeJev) -> None:  # noqa: F811
    _ingest(setup_home, db)
    writer(default=_reply_for)
    deviations.scan(now=NOW)
    assert fake_jev.states
    for state in fake_jev.states:
        for text in ("recap", "habit", "harder to read", "flaky test", "parent-lock", "Add the endpoint",
                     "pytest"):
            assert text not in state, (text, state)
        assert set(ln.split(":", 1)[0] for ln in state.splitlines()[1:]) == {
            "instruction_source", "instruction_rule", "rule_title", "session_kind", "followed_grade",
            "result_grade", "objective_result", "recurrence"}


def test_recurrence_counts_other_sessions(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                          fake_jev: FakeJev) -> None:  # noqa: F811
    _ingest(setup_home, db)
    writer(default=_reply_for)
    deviations.scan(now=NOW)
    global_states = [s for s in fake_jev.states if "instruction_source: claude-md:global" in s]
    counts = [int(s.split("recurrence: ", 1)[1].split()[0]) for s in global_states]
    assert counts == [0, 1, 2, 3, 4]


def test_one_session_error_does_not_stop_the_scan(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                  fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    # A failed run is not retried in the call; a malformed reply is retried once (twice here).
    nope = envelope({"deviations": [finding(source="skill:nope")]})
    w = writer(distill.WriterError("timed out after 300s"), "not an envelope", "not an envelope", nope, nope,
               default=_reply_for)
    report = deviations.scan(now=NOW)
    assert [o.status for o in report.outcomes] == ["error", "error", "error", "ok", "ok"]
    assert report.errors == 3 and report.found == 2 and len(w.prompts) == 7
    assert report.outcomes[0].reason == "writer: timed out after 300s"
    assert report.outcomes[1].reason.startswith("writer output: CLI output is not a JSON envelope")
    assert report.outcomes[1].reason.endswith(" (after 1 retry)")
    assert "instruction_source 'skill:nope' was not offered" in report.outcomes[2].reason
    chat = _q(db, "select scanned_at, meta ->> 'scan_error', meta ->> 'scan_errors' from sessions "
                  "where session_id = %s", ids["chat"])
    assert chat == [(None, "writer: timed out after 300s", "1")]
    # The failed sessions are retried next time; the scanned ones are not picked again.
    w = writer(default=envelope({"deviations": []}))
    again = deviations.scan(now=NOW)
    assert {o.session_id for o in again.outcomes} == {ids["chat"], ids["parent"], ids["routine"]}
    assert again.errors == 0 and len(w.prompts) == 3
    assert _q(db, "select count(*) from sessions where meta ? 'scan_error'") == [(0,)]


def test_a_null_grade_is_skipped_not_a_session_error(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                     fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    w = writer(envelope({"deviations": [finding(followed_grade=None), finding(turn=2)]}))
    report = deviations.scan(session_id=ids["chat"], now=NOW)
    (o,) = report.outcomes
    assert o.status == "ok" and o.skipped == 1 and o.line().endswith(" skipped=1")
    assert report.summary() == "deviations: sessions=1 found=1 edit=1 enforce=0 leave=0 errors=0 skipped=1"
    assert "never null" in w.prompts[0]
    assert _q(db, "select turn from deviations") == [(2,)]
    assert _q(db, "select scanned_at is not null, meta ->> 'scan_skipped', meta ? 'scan_error' from sessions "
                  "where session_id = %s", ids["chat"]) == [(True, "1", False)]


def test_any_malformed_item_skips_only_that_item(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                 fake_jev: FakeJev) -> None:  # noqa: F811
    # 2026-10-17 (session b1b2011c.../agent-a079...): item 0 had `"followed_grade": null` and
    # the session's valid items were dropped with it. Now only the bad items are skipped:
    # a null, a missing or an out-of-range grade alike, each named.
    ids = _ingest(setup_home, db)
    missing = finding(turn=4)
    del missing["result_grade"]
    w = writer(default=envelope({"deviations": [finding(followed=None), finding(turn=2),
                                                 finding(turn=3, result_="high"), missing,
                                                 finding(turn=5, followed=1.5)]}))
    report = deviations.scan(session_id=ids["chat"], now=NOW)
    (o,) = report.outcomes
    assert (o.status, report.errors, report.found, report.skipped) == ("ok", 0, 1, 4)
    assert o.skipped_reasons == ["deviation 0: followed_grade must be a number in 0..1, got None",
                                 "deviation 2: result_grade must be a number in 0..1, got 'high'",
                                 "deviation 3: result_grade must be a number in 0..1, got None",
                                 "deviation 4: followed_grade must be a number in 0..1, got 1.5"]
    assert o.line().endswith("found=1 edit=1 enforce=0 leave=0 unjudged=0 skipped=4")
    assert report.summary() == "deviations: sessions=1 found=1 edit=1 enforce=0 leave=0 errors=0 skipped=4"
    # The valid item is stored with its own grades; nothing is invented for the bad ones.
    assert _q(db, "select turn, followed_grade, result_grade from deviations") == \
        [(2, pytest.approx(0.3), pytest.approx(0.8))]
    (row,) = _q(db, "select scanned_at is not null, meta -> 'scan_skipped', meta -> 'scan_skipped_reasons', "
                    "meta ? 'scan_error', meta ? 'scan_errors' from sessions where session_id = %s", ids["chat"])
    assert row == (True, 4, o.skipped_reasons, False, False)
    # Scanned: not picked again (no second writer call, no duplicate rows under new refs).
    assert ids["chat"] not in {r.session_id for r in deviations.pick_sessions(since=NOW.replace(day=1), limit=50)}
    assert len(w.prompts) == 1


def test_reply_with_only_malformed_items_is_a_session_error(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                            fake_jev: FakeJev) -> None:  # noqa: F811
    # Nothing usable, also after the one in-call retry: a writer failure, retried on the next scan
    # (never stored as "found nothing").
    ids = _ingest(setup_home, db)
    one_bad = envelope({"deviations": [finding(followed=None)]})
    two_bad = envelope({"deviations": [finding(followed=None), finding(turn=2, result_=2)]})
    w = writer(one_bad, one_bad, two_bad, two_bad, default=envelope({"deviations": [finding()]}))
    one = deviations.scan(session_id=ids["chat"], now=NOW)
    two = deviations.scan(session_id=ids["parent"], now=NOW)
    assert len(w.prompts) == 4
    assert one.outcomes[0].reason == ("writer output: deviation 0: followed_grade must be a number in 0..1, "
                                      "got None (after 1 retry)")
    assert two.outcomes[0].reason == ("writer output: deviation 0: followed_grade must be a number in 0..1, "
                                      "got None (+1 more skipped) (after 1 retry)")
    assert (one.errors, two.errors, one.skipped, two.skipped) == (1, 1, 0, 0)
    assert _q(db, "select count(*) from deviations") == [(0,)]
    assert _q(db, "select scanned_at, meta ->> 'scan_errors' from sessions where session_id = %s", ids["chat"]) == \
        [(None, "1")]
    again = deviations.scan(session_id=ids["chat"], now=NOW)  # retried: the next reply is good
    assert (again.errors, again.found) == (0, 1)


def test_jev_error_fails_only_that_session(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                           fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    writer(default=_reply_for)
    fake_jev.error = jev.JevError("Jev API error: 500")
    report = deviations.scan(session_id=ids["chat"], now=NOW)
    assert report.errors == 1 and report.outcomes[0].reason == "jev: Jev API error: 500"
    assert _q(db, "select count(*) from deviations") == [(0,)]


def test_sessions_failing_too_often_are_not_picked(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                   fake_jev: FakeJev) -> None:  # noqa: F811
    _ingest(setup_home, db)
    writer(default=distill.WriterError("exit 1: boom"))
    for _ in range(deviations.MAX_SCAN_ERRORS):
        deviations.scan(now=NOW)
    assert deviations.scan(now=NOW).outcomes == []


RELEASED = {"v": 1, "releasedAt": "2026-10-15T14:14:15.516Z", "reason": "delete"}


def _release(home: Home, session: str) -> None:
    """What the Desktop app leaves when it deletes a session: the marker, no transcript, no folder."""
    proj = home.projects / "-work-demo-repo"
    (proj / f"{session}.jsonl").unlink()
    shutil.rmtree(proj / session, ignore_errors=True)
    (proj / f"{session}{deviations.RELEASED_SUFFIX}").write_text(json.dumps(RELEASED, indent=2))


def test_released_transcript_is_skipped_not_an_error(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                     fake_jev: FakeJev) -> None:  # noqa: F811
    # 2026-10-15: session 57434622... was deleted in the Desktop app; only its
    # .desktop-released.json was left, and every scan counted "transcript file not found".
    ids = _ingest(setup_home, db)
    _release(setup_home, ids["chat"])
    w = writer(default=_reply_for)
    report = deviations.scan(now=NOW)
    by = {o.session_id: o for o in report.outcomes}
    chat = by[ids["chat"]]
    assert chat.status == "skipped"
    assert chat.reason == "transcript released by Claude Desktop (reason delete at 2026-10-15T14:14:15.516Z)"
    assert chat.line() == f"skipped {ids['chat']} kind=chat: {chat.reason}"
    assert report.errors == 0 and report.skipped_sessions == 1
    assert report.summary() == ("deviations: sessions=5 found=5 edit=5 enforce=0 leave=0 errors=0 "
                                "skipped_sessions=1")
    assert len(w.prompts) == 4  # no writer call for it
    assert _q(db, "select scanned_at, meta ->> 'scan_status', meta ->> 'scan_skip_reason', meta ? 'scan_error', "
                  "meta ? 'scan_errors' from sessions where session_id = %s", ids["chat"]) == \
        [(None, "skipped", chat.reason, False, False)]
    # Never picked again (not retried like an error), and it stays out of the scanned count.
    assert deviations.scan(now=NOW).outcomes == []


def test_released_after_errors_clears_the_error(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    proj = setup_home.projects / "-work-demo-repo"
    (proj / f"{ids['chat']}.jsonl").rename(proj / "elsewhere.bak")
    writer(default=_reply_for)
    deviations.scan(session_id=ids["chat"], now=NOW)
    assert _q(db, "select meta ->> 'scan_error' from sessions where session_id = %s", ids["chat"]) == \
        [("transcript file not found",)]
    (proj / f"{ids['chat']}{deviations.RELEASED_SUFFIX}").write_text(json.dumps(RELEASED))
    again = deviations.scan(session_id=ids["chat"], now=NOW)
    assert again.outcomes[0].status == "skipped" and again.errors == 0
    assert _q(db, "select meta ? 'scan_error', meta ->> 'scan_status' from sessions where session_id = %s",
              ids["chat"]) == [(False, "skipped")]


def test_missing_transcript_without_marker_stays_an_error(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                          fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    proj = setup_home.projects / "-work-demo-repo"
    (proj / f"{ids['chat']}.jsonl").unlink()
    (proj / f"{ids['parent']}{deviations.RELEASED_SUFFIX}").write_text(json.dumps(RELEASED))  # another id's marker
    writer(default=_reply_for)
    report = deviations.scan(session_id=ids["chat"], now=NOW)
    (o,) = report.outcomes
    assert (o.status, o.reason, report.errors, report.skipped_sessions) == \
        ("error", "transcript file not found", 1, 0)
    assert _q(db, "select meta ->> 'scan_errors', meta ? 'scan_status' from sessions where session_id = %s",
              ids["chat"]) == [("1", False)]
    assert ids["chat"] in {r.session_id for r in deviations.pick_sessions(since=NOW.replace(day=1), limit=50)}


def test_subagent_of_a_released_session_is_skipped(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                   fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    _release(setup_home, ids["parent"])  # its subagents/ folder goes with it
    writer(default=_reply_for)
    report = deviations.scan(now=NOW)
    by = {o.session_id: o.status for o in report.outcomes}
    assert by[ids["parent"]] == "skipped" and by[ids["worker"]] == "skipped"
    assert report.errors == 0 and report.skipped_sessions == 2


def test_released_marker_lookup(setup_home: Home, db: str) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    rows = {r.session_id: r for r in deviations.pick_sessions(since=NOW.replace(day=1), limit=50)}
    assert deviations.released_marker(rows[ids["chat"]]) is None  # transcript there, no marker
    _release(setup_home, ids["parent"])
    marker = setup_home.projects / "-work-demo-repo" / f"{ids['parent']}{deviations.RELEASED_SUFFIX}"
    assert deviations.released_marker(rows[ids["parent"]]) == marker
    assert deviations.released_marker(rows[ids["worker"]]) == marker  # the parent's marker
    assert deviations.released_marker(replace(rows[ids["parent"]], tool="codex")) is None
    assert deviations.released_marker(replace(rows[ids["parent"]], project_dir="../x")) is None
    marker.write_text("not json")
    assert deviations.released_reason(marker) == "transcript released by Claude Desktop"


def test_dry_run_reports_a_released_transcript_without_writing(setup_home: Home, db: str) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    _release(setup_home, ids["chat"])
    report = deviations.scan(session_id=ids["chat"], now=NOW, dry_run=True)
    assert report.outcomes[0].status == "skipped" and report.errors == 0
    assert _q(db, "select meta ? 'scan_status' from sessions where session_id = %s", ids["chat"]) == [(False,)]


def test_rescan_is_idempotent(setup_home: Home, db: str, writer: Writer, fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    writer(default=_reply_for)
    deviations.scan(now=NOW)
    before = _q(db, "select source_key from deviations order by 1")
    assert deviations.scan(now=NOW).outcomes == []  # nothing unscanned left
    fake_jev.verdict = "enforce"
    again = deviations.scan(session_id=ids["parent"], now=NOW)  # explicit re-scan
    assert again.summary() == "deviations: sessions=1 found=2 edit=0 enforce=2 leave=0 errors=0"
    assert _q(db, "select source_key from deviations order by 1") == before
    assert _q(db, "select distinct jev_verdict from deviations where session_id = %s", ids["parent"]) == \
        [("enforce",)]
    # Re-ingest keeps the scan state.
    sessions_ingest.ingest(setup_home.projects, days=7, now=NOW, db_url=db)
    assert _q(db, "select count(*) from sessions where scanned_at is null and kind <> 'eval'") == [(0,)]


def test_missing_jev_fixture_stores_the_deviation_unjudged(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                                            tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _ingest(setup_home, db)
    monkeypatch.setenv("AMEM_JEV_FIXTURES_DIR", str(tmp_path / "no-fixtures"))
    writer(default=envelope({"deviations": [finding()]}))
    report = deviations.scan(session_id=ids["chat"], now=NOW)
    assert report.errors == 0 and report.found == 1
    assert report.outcomes[0].line().endswith("found=1 edit=0 enforce=0 leave=0 unjudged=1")
    assert _q(db, "select jev_verdict, jev_mode from deviations") == [(None, "fixture-missing")]


def test_jev_fixture_mode_end_to_end(setup_home: Home, db: str, writer: Writer, tmp_path: Path,  # noqa: F811
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    ids = _ingest(setup_home, db)
    fixtures = tmp_path / "jev"
    fixtures.mkdir()
    monkeypatch.setenv("AMEM_JEV_FIXTURES_DIR", str(fixtures))
    f = deviations.parse_findings(json.dumps({"deviations": [finding()]}), {"claude-md:global"})[0]
    f = replace(f, rule_id="explaining", rule_title="Explaining")  # what the scan resolves it to
    state = deviations.jev_state(f, "chat", 0, "no-change")  # the chat session answered, changed nothing
    key = jev.fixture_key(state, deviations.question_set().jev_questions())
    (fixtures / f"{key}.json").write_text(json.dumps({"model": "jev-latest", "usage": {}, "answers": {
        "verdict": {"type": "choice", "choice": "enforce", "confidence": 0.91,
                    "probabilities": {"edit_instruction": 0.05, "enforce": 0.91, "leave": 0.04}},
        "urgency": {"type": "score", "score": 1.4, "confidence": 0.6, "legend": {"0": "a", "1": "b", "2": "c"},
                    "probabilities": {"0": 0.1, "1": 0.4, "2": 0.5}},
        "systemic": {"type": "noul", "noul": 0.3},
    }}))
    writer(default=envelope({"deviations": [finding()]}))
    report = deviations.scan(session_id=ids["chat"], now=NOW)
    assert report.summary() == "deviations: sessions=1 found=1 edit=0 enforce=1 leave=0 errors=0"
    (row,) = _q(db, "select jev_verdict, jev_confidence, jev_mode, jev_answers -> 'systemic' ->> 'value' "
                    "from deviations")
    assert row == ("enforce", pytest.approx(0.91), "fixture", "0.3")


def test_dry_run_calls_and_writes_nothing(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                          fake_jev: FakeJev) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    w = writer()
    report = deviations.scan(dry_run=True, now=NOW, limit=2)
    assert w.prompts == [] and fake_jev.states == []
    assert [o.session_id for o in report.outcomes] == [ids["chat"], ids["parent"]]
    line = report.outcomes[1].line()
    assert line.startswith(f"dry-run {ids['parent']} kind=parent digest_chars=")
    assert line.endswith("instructions=claude-md:global,parent-md,skill:dispatch,claude-md:demo-repo")
    assert report.summary().endswith("errors=0 (dry run, nothing called or written)")
    assert _q(db, "select count(*) from sessions where scanned_at is not null") == [(0,)]


def test_tiny_and_eval_sessions_are_skipped(setup_home: Home, db: str) -> None:  # noqa: F811
    proj = setup_home.project("-work-demo-repo")
    write_transcript(proj / "tiny.jsonl", chat_events(str(setup_home.repo)), pad=False)
    write_transcript(proj / "idle.jsonl", chat_events(str(setup_home.repo)))  # big file, 3 actions
    _ingest(setup_home, db)
    picked = {r.session_id for r in deviations.pick_sessions(since=NOW.replace(day=1), limit=50)}
    assert "tiny" not in picked and "idle" not in picked and "eval-1" not in picked and "chat-1" in picked


def test_non_worker_subagents_are_picked_last(setup_home: Home, db: str) -> None:  # noqa: F811
    # Their parent's scan usually covers them (then they are skipped), so they must not use up
    # the daily limit before the parents and chats: 181 of 339 pickable sessions on 2026-10-14.
    ids = _ingest(setup_home, db)
    with psycopg.connect(db) as conn:
        conn.execute("""update sessions set skills = '["dispatch"]'::jsonb,
                        meta = meta || '{"agent_type": "Explore", "parent_session": "gone"}'::jsonb
                        where session_id = %s""", (ids["worker"],))
    picked = [r.session_id for r in deviations.pick_sessions(since=NOW.replace(day=1), limit=50)]
    assert picked[-1] == ids["worker"] and ids["chat"] in picked and ids["headless"] in picked
    assert [r.session_id for r in deviations.pick_sessions(since=NOW.replace(day=1), limit=1)] != [ids["worker"]]


def test_cli_scan_needs_live_flag_unless_dry_run(setup_home: Home, db: str) -> None:  # noqa: F811
    _ingest(setup_home, db)
    env = base_env(AMEM_DB_URL=db, AMEM_PROJECTS_DIR=str(setup_home.projects),
                   AMEM_CLAUDE_HOME=str(setup_home.claude), AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config))
    res = run_amem("deviations", "scan", env=env)
    assert res.returncode == 2 and "AMEM_DEVIATIONS_LIVE=1" in res.stderr
    dry = run_amem("deviations", "scan", "--dry-run", "--days", "3650", "--limit", "3", env=env)
    assert dry.returncode == 0, dry.stderr
    assert dry.stdout.splitlines()[-1] == ("deviations: sessions=3 found=0 edit=0 enforce=0 leave=0 errors=0 "
                                           "(dry run, nothing called or written)")


def test_cli_scan_live_with_a_missing_writer_records_errors(setup_home: Home, db: str) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    env = base_env(AMEM_DB_URL=db, AMEM_PROJECTS_DIR=str(setup_home.projects),
                   AMEM_CLAUDE_HOME=str(setup_home.claude), AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config),
                   AMEM_DEVIATIONS_LIVE="1", AMEM_DEVIATIONS_CLAUDE=str(setup_home.root / "missing-claude"))
    res = run_amem("deviations", "scan", "--session", ids["chat"], "--days", "3650", env=env)
    assert res.returncode == 1
    assert "error chat-1 kind=chat: writer: cannot run" in res.stdout
    assert res.stdout.splitlines()[-1].endswith("errors=1")


def test_cli_scan_names_skipped_items_and_exits_0(setup_home: Home, db: str, tmp_path: Path) -> None:  # noqa: F811
    ids = _ingest(setup_home, db)
    reply = tmp_path / "reply.json"
    reply.write_text(envelope({"deviations": [finding(followed=None), finding(turn=2)]}))
    exe = tmp_path / "fake-claude"
    exe.write_text(f'#!/bin/sh\ncat >/dev/null\ncat "{reply}"\n')
    exe.chmod(0o755)
    env = base_env(AMEM_DB_URL=db, AMEM_PROJECTS_DIR=str(setup_home.projects),
                   AMEM_CLAUDE_HOME=str(setup_home.claude), AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config),
                   AMEM_DEVIATIONS_LIVE="1", AMEM_DEVIATIONS_CLAUDE=str(exe),
                   AMEM_JEV_FIXTURES_DIR=str(tmp_path / "no-fixtures"))
    res = run_amem("deviations", "scan", "--session", ids["chat"], "--days", "3650", env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    lines = res.stdout.splitlines()
    assert "  invalid item skipped: deviation 0: followed_grade must be a number in 0..1, got None" in lines
    assert lines[-1] == "deviations: sessions=1 found=1 edit=0 enforce=0 leave=0 errors=0 skipped=1"


def test_report_groups_and_json_candidates(setup_home: Home, db: str, writer: Writer,  # noqa: F811
                                           fake_jev: FakeJev) -> None:  # noqa: F811
    _ingest(setup_home, db)
    writer(default=_reply_for)
    deviations.scan(now=NOW)
    cands = deviations.candidates(now=NOW)
    assert [(c["instruction_source"], c["sessions"], c["count"]) for c in cands] == [
        ("claude-md:global", 5, 5), ("skill:dispatch", 1, 1)]
    top = cands[0]
    assert top["followed_grade_mean"] == pytest.approx(0.3) and top["result_grade_mean"] == pytest.approx(0.8)
    assert top["jev"] == {"edit_instruction": 5, "enforce": 0, "leave": 0, "unjudged": 0,
                          "confidence_mean": pytest.approx(0.8)}
    assert top["verdict"] == "edit_instruction"
    assert top["target_paths"] == [str(setup_home.claude / "CLAUDE.md")]
    assert len(top["examples"]) == deviations.EXAMPLES_MAX and len(top["deviation_ids"]) == 5
    assert (top["rule_id"], top["rule_title"], top["refs"]) == ("explaining", "Explaining", {"Explaining": 5})
    assert len(top["instruction_shas"]) == 1 and len(top["instruction_shas"][0]) == 64
    lines = deviations.report_lines(cands, 7)
    assert lines[0] == "deviations (last 7 days): 2 rules, 6 deviations"
    assert lines[1].startswith("claude-md:global / Explaining [explaining]: count=5 sessions=5 followed=0.30 "
                               "result=0.80 objective=0.70 | ")  # chat, routine, headless: no-change on Q&A

    env = base_env(AMEM_DB_URL=db, AMEM_CLAUDE_HOME=str(setup_home.claude),
                   AMEM_AGENT_CONFIG_DIR=str(setup_home.agent_config))
    res = run_amem("deviations", "report", "--json", "--days", "3650", env=env)
    assert res.returncode == 0, res.stderr
    doc = json.loads(res.stdout)
    assert doc["version"] == 2 and doc["window_days"] == 3650
    assert set(doc["candidates"][0]) == {
        "instruction_source", "rule_id", "rule_title", "instruction_ref", "refs", "instruction_shas",
        "target_paths", "count", "sessions", "kinds", "tools", "team", "team_sessions",
        "followed_grade_mean", "result_grade_mean", "objective_result_mean", "outcomes", "cost", "jev", "verdict", "session_ids", "deviation_ids", "examples",
        "session_titles"}  # session_titles: {session id: title} for sessions with a known title


def test_rls_on_new_tables_and_api_roles_revoked(db: str) -> None:
    rows = dict(_q(db, "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n "
                       "on n.oid = c.relnamespace where n.nspname = 'public' and c.relname in "
                       "('sessions', 'deviations')"))
    assert rows == {"sessions": True, "deviations": True}
    migration = (deviations.config.MIGRATIONS_DIR / "20261016010000_sessions_deviations.sql").read_text()
    with psycopg.connect(db) as conn:
        # Lock first: the grants update the tables' pg_class rows, then the migration's ALTER TABLE
        # waits for a running autovacuum ANALYZE, which waits on those rows: a deadlock.
        conn.execute("lock table public.sessions, public.deviations in access exclusive mode")
        for role in ("anon", "authenticated"):
            if not conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone():
                conn.execute(f"create role {role}")
            conn.execute(f"grant select on public.sessions, public.deviations to {role}")
        conn.execute(migration)  # re-runnable, and revokes the grants
        granted = conn.execute("select count(*) from information_schema.role_table_grants where table_name in "
                               "('sessions', 'deviations') and grantee in ('anon', 'authenticated')").fetchone()
        assert granted == (0,)
        conn.rollback()


def test_check_constraints(db: str) -> None:
    with psycopg.connect(db) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into sessions (session_id, kind, project_dir) values ('x', 'other', 'p')")
    with psycopg.connect(db) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir) values ('x', 'chat', 'p')")
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, "
                         "what, why, how, followed_grade, result_grade) "
                         "values ('k', 'x', 's', 'r', 'w', 'y', 'h', 1.5, 0.5)")
