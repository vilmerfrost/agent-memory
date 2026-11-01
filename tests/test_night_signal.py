"""A failed or empty night must look failed: the phone title and priority, the brief's reason lines, the routine
line, the suggested hand edits that stay visible, and the auth recovery line. The report shapes are the live ones of
nights 2026-10-30 and 2026-10-31 (`~/sandbox/nights/<night>/report.json`). ntfy is never called: test_rsi_brief's
`sent` fixture replaces every way out."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import (alerts, claude_auth, cli, daily, daily_recovery as recovery, routines_log, rsi_approval as AP,
                          rsi_brief as BR, rsi_schedule as S)
from test_rsi_brief import (experiment, night_report, rdb, sandbox, sent, write_report,  # noqa: F401  (fixtures)
                            make_artifact, NIGHT)

TOPIC = {"NTFY_TOPIC": "test-topic"}
HYP = "a candidate produced no valid hypothesis"
TOO_LONG = {"index": 0, "code": "too_long", "field": "what", "reason": "a field was longer than the rules allow"}


def goal(*reasons: str, replayed: int = 0, attempted: int = 0, minimum: int = 1, target: int = 3) -> dict[str, Any]:
    return {"minimum": minimum, "target": target, "attempted": attempted, "replayed": replayed,
            "minimum_met": replayed >= minimum, "target_met": replayed >= target,
            "shortfall_reasons": list(reasons or (HYP,))}


def cand(key: str, *rejected: dict[str, Any], accepted: int = 0, selected: bool = True) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "key": key, "source": "deviations", "rank": 0.4, "selected": selected,
            "reason": "selected: rank 0.4", "hypotheses_accepted": accepted, "hypotheses_rejected": list(rejected)}


def night_oct30(**over: Any) -> dict[str, Any]:
    """Night 2026-10-30: four selected candidates, every reply an empty list, all services ok."""
    return night_report(status="partial", experiment_goal=goal(),
                        candidates=[cand(f"dev:claude-md:global:rule-{i}") for i in range(4)],
                        notes=[f"Daily experiments: 0 with replay runs; minimum 1, target 3. {HYP}"], **over)


def night_oct31(**over: Any) -> dict[str, Any]:
    """Night 2026-10-31: three selected candidates, two empty replies, one idea thrown out as too long."""
    return night_report(status="partial", experiment_goal=goal(),
                        candidates=[cand("dev:claude-md:global:a"), cand("dev:claude-md:global:b", TOO_LONG),
                                    cand("dev:claude-md:global:c")], **over)


def brief_of(rep: dict[str, Any], root: Path, db: BR.Db | None = None, **kw: Any) -> dict[str, Any]:
    return BR.build_brief(rep, db or BR.Db("ok"), artifacts_root=root / "artifacts", **kw)


# --------------------------------------------------------------------------- the phone


def test_failed_night_says_so_in_the_title_at_a_higher_priority(sandbox: Path, sent: list[Any],  # noqa: F811
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BR.PUSH_ENV, "1")
    b = brief_of(night_oct30(), sandbox)
    title, _ = BR.notification(b)
    assert title.startswith(f"RSI night {NIGHT} FAILED: ") and "no experiments" in title and "no usable idea" in title
    assert "to decide" not in title
    assert BR.push_brief(b, notify=TOPIC) == "phone"
    assert len(sent) == 1
    msg = json.loads(sent[0][2])
    assert msg["priority"] == 4 and msg["title"] == title


def test_normal_night_keeps_its_title_and_priority(sandbox: Path, sent: list[Any],  # noqa: F811
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BR.PUSH_ENV, "1")
    b = brief_of(night_report(), sandbox)
    assert "night_signal" not in b
    assert BR.notification(b)[0] == f"RSI brief {NIGHT}: 0 to decide"
    assert BR.push_brief(b, notify=TOPIC) == "phone"
    msg = json.loads(sent[0][2])
    assert msg["priority"] == 3 and msg["tags"] == ["sunrise"]


def test_morning_push_title_and_priority_for_a_failed_night(sandbox: Path, sent: list[Any]) -> None:  # noqa: F811
    b = brief_of(night_oct31(), sandbox)
    assert alerts.push_night(b, notify=TOPIC) == "phone"
    assert len(sent) == 1
    msg = json.loads(sent[0][2])
    assert msg["title"].startswith(f"Night {NIGHT} FAILED: ") and "no usable idea" in msg["title"]
    assert msg["priority"] == 4 and msg["tags"] == ["warning"]


def test_an_ok_status_that_missed_the_minimum_is_not_a_quiet_night(sandbox: Path) -> None:
    b = brief_of(night_report(experiment_goal=goal(), candidates=[cand("dev:skill:x:y")]), sandbox)
    msg = alerts.night_message(b)
    assert msg is not None and "FAILED" in msg[0]


def test_quiet_ok_night_stays_quiet(sandbox: Path, sent: list[Any]) -> None:  # noqa: F811
    assert alerts.push_night(brief_of(night_report(), sandbox), notify=TOPIC) == "quiet"
    assert sent == []


@pytest.mark.parametrize(("status", "word", "note"), [
    ("aborted", "ABORTED", "interrupted by SIGTERM"),
    ("budget_exhausted", "STOPPED EARLY", "budget exhausted: usd"),
])
def test_each_bad_status_names_itself(sandbox: Path, status: str, word: str, note: str) -> None:
    b = brief_of(night_report(status=status, notes=[note]), sandbox)
    assert b["night_signal"]["state"] == word
    assert BR.notification(b)[0].startswith(f"RSI night {NIGHT} {word}: ")


def test_skipped_night_title_carries_the_reason(sandbox: Path) -> None:
    rep = night_report(status="preflight_failed", notes=[
        "skipped by amem-rsi-night: prerequisite amem-daily: last run FAIL at 2026-10-21T09:20:05+0200"],
        skipped={"reason": "prerequisites", "summary": "amem-daily did not pass", "items": []})
    b = brief_of(rep, sandbox)
    assert b["night_signal"]["state"] == "DID NOT RUN"
    assert "amem-daily did not pass" in BR.notification(b)[0]
    assert alerts.night_message(b)[0] == f"Night {NIGHT} DID NOT RUN: the deviation scan (amem-daily) failed"


def test_phone_title_is_scrubbed_and_capped(sandbox: Path) -> None:
    rep = night_report(status="partial", experiment_goal=goal("x /Users/someone/secret.md " + "y" * 300))
    title, _ = BR.notification(brief_of(rep, sandbox))
    assert len(title) <= 120 and "/Users/someone" not in title


# --------------------------------------------------------------------------- the brief's reason lines


def test_partial_headline_points_at_the_real_reason(sandbox: Path) -> None:
    b = brief_of(night_oct30(), sandbox)
    assert b["headline"].startswith("The night finished in part: ")
    assert "see Service health" not in b["headline"] and "no usable idea" in b["headline"]
    md = BR.render_markdown(b)
    assert md.splitlines()[2].startswith("**Night FAILED.**")


def test_partial_headline_keeps_service_health_when_a_service_is_the_cause(sandbox: Path) -> None:
    rep = night_report(status="partial", health={"supabase": "ok", "langfuse": "error"},
                       health_reasons={"langfuse": ["lf.scores: timeout after 20s"]})
    b = brief_of(rep, sandbox)
    assert "langfuse error" in b["headline"] and "see Service health" in b["headline"]
    assert b["night_signal"]["state"] == "PARTLY FAILED"


def test_failed_experiment_is_named_as_the_cause(sandbox: Path) -> None:
    rep = night_report(status="partial", experiment_goal=goal("an experiment did not produce any replay runs",
                                                             replayed=0, attempted=1),
                       experiments=[experiment("exp-20261029-412890c3", "failed", None,
                                               verdict_reasons=["ExperimentFailed: writer intervention deferred"])])
    b = brief_of(rep, sandbox)
    assert b["night_signal"]["reasons"][0].startswith("no experiments ran: an experiment stopped before its replay ran")
    assert "1 experiment failed: writer intervention deferred" in b["headline"]
    assert "see Service health" not in b["headline"]


@pytest.mark.parametrize(("raw", "words"), [
    ("KeyError: SECRET-MODEL-WORDS", "1 experiment failed: an internal error"),
    ("SubscriptionError: Failed to authenticate: OAuth session expired", "1 experiment failed: authentication failed"),
])
def test_a_raw_exception_on_an_experiment_stays_out_of_the_push(sandbox: Path, raw: str, words: str) -> None:
    rep = night_report(status="partial", experiment_goal=goal("an experiment did not produce any replay runs",
                                                             replayed=0, attempted=1),
                       experiments=[experiment("exp-20261029-412890c3", "failed", None, verdict_reasons=[raw])])
    b = brief_of(rep, sandbox)
    assert words in b["night_signal"]["reasons"]
    assert_plain(*phone(b), "SECRET-MODEL-WORDS", "OAuth session", "KeyError")
    assert_plain(*BR.notification(b), "SECRET-MODEL-WORDS", "OAuth session", "KeyError")


def test_a_skip_from_before_the_skipped_field_keeps_its_reason(sandbox: Path) -> None:
    # Night 2026-10-19: the note is the only record of why it did not run.
    rep = night_report(status="preflight_failed", notes=[
        "skipped by amem-rsi-night: prerequisite amem-daily: last run FAIL at 2026-10-19T09:20:04+0200"])
    b = brief_of(rep, sandbox)
    assert b["night_signal"]["reason"].startswith("prerequisite amem-daily: last run FAIL")
    assert "see Service health" not in b["headline"] and "prerequisite amem-daily" in BR.notification(b)[0]


def test_findings_with_no_experiment_do_not_read_like_a_good_night(sandbox: Path) -> None:
    v = {**BR._verdict("findings"), "findings": 35}
    b = brief_of(night_oct31(), sandbox, verdict=v)
    md = BR.render_markdown(b)
    assert "**Verdict: Findings (35).**" not in md
    assert "**Verdict: Findings (35), but nothing was tested.**" in md
    assert "Verdict: Findings (35), nothing tested." in BR.notification(b)[1]


def test_findings_on_a_night_that_tested_keep_their_line(sandbox: Path) -> None:
    v = {**BR._verdict("findings"), "findings": 3}
    rep = night_report(experiments=[experiment("exp-20261018-aaaa", "not_promotable", "NO_EFFECT")])
    md = BR.render_markdown(brief_of(rep, sandbox, verdict=v))
    assert "**Verdict: Findings (3).** See the patterns and experiments below." in md


def test_all_clear_on_a_failed_night_says_the_night_failed(sandbox: Path) -> None:
    v = BR._verdict("all_clear")
    md = BR.render_markdown(brief_of(night_oct30(), sandbox, verdict=v))
    assert "**Verdict: No problem found.**" in md and "The data was fine, but the night FAILED." in md


def test_zero_experiment_reason_oct30(sandbox: Path) -> None:
    md = BR.render_markdown(brief_of(night_oct30(), sandbox))
    assert ("- None ran. Why: 4 of 4 selected candidates gave no usable idea (4 returned no ideas)." in md)


def test_zero_experiment_reason_oct31_counts_rejected_ideas(sandbox: Path) -> None:
    md = BR.render_markdown(brief_of(night_oct31(), sandbox))
    assert ("- None ran. Why: 3 of 3 selected candidates gave no usable idea (2 returned no ideas; 1 had every idea "
            "thrown out: 1 x a field was longer than the rules allow)." in md)


def test_zero_experiment_reason_with_no_selected_candidate(sandbox: Path) -> None:
    rep = night_report(status="partial", candidates=[cand("dev:skill:x:y", selected=False)],
                       experiment_goal=goal("no permitted candidate with applicable replay cases"))
    md = BR.render_markdown(brief_of(rep, sandbox))
    assert "- None ran. Why: no candidate had a replay test it could use." in md


def test_target_missed_with_some_replays_says_why_fewer(sandbox: Path) -> None:
    rep = night_report(status="partial", experiment_goal=goal("an experiment did not produce any replay runs", HYP,
                                                             replayed=1, attempted=2),
                       experiments=[experiment("exp-20261029-0146c33f", "not_promotable", "NO_EFFECT", replay_runs=3),
                                    experiment("exp-20261029-412890c3", "failed", None)],
                       candidates=[cand("dev:a:b:c", TOO_LONG, TOO_LONG), cand("dev:d:e:f", accepted=2)])
    md = BR.render_markdown(brief_of(rep, sandbox))
    assert ("- 1 of 3 daily experiments produced replay runs. Why fewer: 1 of 2 selected candidates gave no usable "
            "idea (1 had every idea thrown out: 2 x a field was longer than the rules allow); an experiment stopped "
            "before its replay ran." in md)


def test_new_night_report_codes_are_read_and_validated(sandbox: Path) -> None:
    # worker-night-binding's optional fields and fixed codes: a code the brief does not know is still shown.
    reply = {"chars": 18, "sha256": "a" * 64, "tokens_in": 900, "tokens_out": 12}
    rep = night_report(status="partial", experiment_goal=goal(), candidates=[
        {**cand("dev:a:b:c", {"index": None, "code": "no_ideas", "field": None,
                              "reason": "the idea writer read the rule's section and proposed no change"}),
         "hypothesizer_reply": reply},
        cand("dev:d:e:f", {"index": None, "code": "stale_pending", "field": "edit",
                           "reason": "an idea stored on an earlier night no longer matches the file, so it was dropped"}),
        cand("dev:g:h:i", {"code": "brand_new"}), {**cand("dev:j:k:l"), "hypothesizer_reply": reply}])
    assert BR.validate_report(rep) == []
    md = BR.render_markdown(brief_of(rep, sandbox))
    assert ("4 of 4 selected candidates gave no usable idea (2 returned no ideas; 2 had every idea thrown out: "
            "1 x an idea stored on an earlier night no longer matches the file, so it was dropped; 1 x brand new)" in md)


def test_loop_faults_are_named_though_those_candidates_are_not_selected(sandbox: Path) -> None:
    # rsi_night._defer_loop_fault: selected False, no hypotheses_accepted, a fixed code and its words.
    missing = {"index": None, "code": "rule_section_missing", "field": None,
               "reason": "the rule this candidate names matches no section of the file, so no model call ran"}
    faulted = [{k: v for k, v in cand(f"dev:claude-md:global:{n}", missing, selected=False).items()
                if k != "hypotheses_accepted"} for n in ("a", "b")]
    rep = night_report(status="partial", candidates=faulted, experiment_goal=goal(
        "fewer permitted candidates than the daily target", "a candidate's rule section could not be shown to the model"))
    assert BR.validate_report(rep) == []
    b = brief_of(rep, sandbox)
    assert b["night_signal"]["reason"] == "no experiments ran: 2 candidates never reached the model"
    assert ("- None ran. Why: 2 candidates never reached the model (2 x the rule this candidate names matches no section "
            "of the file, so no model call ran); fewer candidates could be tested than the daily target." in
            BR.render_markdown(b))


def test_a_report_without_the_goal_keeps_the_old_line(sandbox: Path) -> None:
    md = BR.render_markdown(brief_of(night_report(), sandbox))
    assert "- None ran.\n" in md


def test_eval_line_says_what_it_counted(sandbox: Path) -> None:
    md = BR.render_markdown(brief_of(night_report(), sandbox))
    assert ("- 7 deviations, 2 failed sandbox evals (rsi_eval_results only; a red evals-parent run counts as a routine "
            "failure), 1 routine failure." in md)


# --------------------------------------------------------------------------- the routine line


def test_brief_job_stays_pass_and_shows_the_bad_night(sandbox: Path, rdb: str, tmp_path: Path,  # noqa: F811
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_memory.dashboard.upsert_night", lambda *a, **k: None)
    monkeypatch.setattr("agent_memory.telemetry_analysis.daily_review", lambda *a, **k: None)
    write_report(sandbox, night_oct31())
    log = tmp_path / "routines.log"
    res = S.run_brief_job(night=NIGHT, routines_log=log, out_dir=tmp_path / "reports", db_url=rdb, push=False,
                          root=sandbox)
    assert res.status == "PASS"
    assert res.fields["night_status"] == "partial" and res.fields["night_state"] == "FAILED"
    assert res.fields["experiments"] == 0 and res.fields["night_reason"] == "no_experiments"
    (run,) = routines_log.parse_file(log).runs
    assert run.status == "pass" and run.meta["night"] == NIGHT and run.meta["night_state"] == "FAILED"
    assert run.meta["night_status"] == "partial" and "no usable idea" in run.meta["warnings"][0]


def test_brief_job_line_for_a_good_night_has_no_night_state(sandbox: Path, rdb: str, tmp_path: Path,  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_memory.dashboard.upsert_night", lambda *a, **k: None)
    monkeypatch.setattr("agent_memory.telemetry_analysis.daily_review", lambda *a, **k: None)
    write_report(sandbox, night_report())
    res = S.run_brief_job(night=NIGHT, routines_log=tmp_path / "routines.log", out_dir=tmp_path / "reports", db_url=rdb,
                          push=False, root=sandbox)
    assert res.status == "PASS" and "night_state" not in res.fields and res.warnings == []


# --------------------------------------------------------------------------- suggested hand edits


def suggested_entry(root: Path, exp: str) -> dict[str, Any]:
    h, _ = make_artifact(root, [("~/.claude/CLAUDE.md", b"# G\n\nold\n", b"# G\n\nnew\n")], exp=exp)
    rec = {"format": "rsi-suggestion/1", "suggest_only": True, "applied_by": "hand", "target": "~/.claude/CLAUDE.md",
           "lock": "hand_edited", "artifact_hash": h, "baseline_id": "bl-000000000001", "section_id": "shell-habits",
           "summary": "Say what the command does", "why": "12 deviations", "diff": "-old\n+new",
           "diff_summary": {"added": 1, "removed": 1, "truncated": False}, "verdict": "IMPROVES",
           "verdict_reasons": ["score 0.5 -> 0.9"], "arms": {}, "evidence_refs": ["amem:deviation/9"]}
    return experiment(exp, "suggested", "IMPROVES", h=h, suggestion=rec, target=rec["target"],
                      candidate_key="dev:claude-md:global:shell-habits")


def row(url: str, e: dict[str, Any], *, night: str = "2026-10-15", age: timedelta = timedelta(hours=1)) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into rsi_experiments (id, night, kind, status, verdict, meta, created_at, finished_at) "
                     "values (%s, %s, 'edit_instruction', 'evaluated', 'IMPROVES', %s, now() - %s, now() - %s)",
                     (e["experiment_id"], night, json.dumps({"report_status": "suggested", "suggestion": e["suggestion"],
                                                             "candidate_key": e["candidate_key"]}), age, age))


def test_a_won_suggestion_does_not_say_it_waits_for_an_ok() -> None:
    b = {"night": "2026-10-24", "status": "ok", "notes": [], "skipped": None,
         "experiments": {"total": 1, "items": [{"candidate_key": "dev:claude-md:global:shell-habits",
                                                "verdict": "IMPROVES", "status": "suggested"}]},
         "pending_decisions": {"total": 0}, "costs": {"total_usd": 0.1}, "health": {"degraded": []},
         "suggested": [{"experiment_id": "exp-20261024-aaaa"}]}
    title, body = alerts.night_message(b)
    assert "waits for your OK" not in title and "waits for your OK" not in body.replace("No proposal waits for your OK", "")
    assert "apply it by hand" in title
    assert "No proposal waits for your OK. Suggested hand edits open: 1." in body


def test_a_staged_win_still_waits_for_an_ok() -> None:
    b = {"night": "2026-10-24", "status": "ok", "notes": [], "skipped": None,
         "experiments": {"total": 1, "items": [{"candidate_key": "dev:skill:meltdown:intro", "verdict": "IMPROVES",
                                                "status": "staged"}]},
         "pending_decisions": {"total": 1}, "costs": {"total_usd": 0.1}, "health": {"degraded": []}}
    title, body = alerts.night_message(b)
    assert "it won and waits for your OK" in title and "Waiting for your OK: 1." in body


def test_a_suggestion_stays_in_later_briefs_until_it_expires(sandbox: Path, rdb: str) -> None:  # noqa: F811
    e = suggested_entry(sandbox, "exp-20261015-aaaa")
    row(rdb, e)
    write_report(sandbox, night_report())
    b = BR.make_brief(night=NIGHT, root=sandbox, db_url=rdb)
    assert [x["experiment_id"] for x in b["suggested"]] == [e["experiment_id"]]
    assert b["suggested"][0]["night"] == "2026-10-15" and b["suggested"][0]["expires_at"]
    md = BR.render_markdown(b)
    assert f"## {BR.SUGGESTED_HEADING}" in md and "Say what the command does" in md
    assert f"amem rsi suggestion {e['experiment_id']} applied" in md
    assert f"amem rsi suggestion {e['experiment_id']} dismissed" in md
    assert "amem rsi decide" not in md and "- Nothing waits for you." not in md
    assert "- No proposal waits for you. 1 suggested hand edit is still open." in md


def test_a_suggestion_of_this_night_is_listed_once(sandbox: Path, rdb: str) -> None:  # noqa: F811
    e = suggested_entry(sandbox, "exp-20261018-bbbb")
    row(rdb, e, night=NIGHT)
    write_report(sandbox, night_report(experiments=[e]))
    b = BR.make_brief(night=NIGHT, root=sandbox, db_url=rdb)
    assert [x["experiment_id"] for x in b["suggested"]] == [e["experiment_id"]]
    assert b["suggested"][0]["expires_at"]


@pytest.mark.parametrize("state", ["applied", "dismissed"])
def test_applied_or_dismissed_suggestions_leave_the_brief(sandbox: Path, rdb: str, state: str) -> None:  # noqa: F811
    e = suggested_entry(sandbox, "exp-20261015-cccc")
    row(rdb, e)
    write_report(sandbox, night_report(experiments=[]))
    with psycopg.connect(rdb) as conn:
        assert AP.set_suggestion_state(conn, e["experiment_id"], state, reason="done by hand") == "recorded"
        assert AP.set_suggestion_state(conn, e["experiment_id"], state) == "already"
        meta = conn.execute("select meta from rsi_experiments where id = %s", (e["experiment_id"],)).fetchone()[0]
    assert meta["suggestion_state"]["state"] == state and meta["suggestion_state"]["reason"] == "done by hand"
    assert "suggested" not in BR.make_brief(night=NIGHT, root=sandbox, db_url=rdb)


def test_a_closed_suggestion_of_this_night_leaves_its_own_brief(sandbox: Path, rdb: str) -> None:  # noqa: F811
    e = suggested_entry(sandbox, "exp-20261018-dddd")
    row(rdb, e, night=NIGHT)
    write_report(sandbox, night_report(experiments=[e]))
    with psycopg.connect(rdb) as conn:
        AP.set_suggestion_state(conn, e["experiment_id"], "dismissed")
    assert "suggested" not in BR.make_brief(night=NIGHT, root=sandbox, db_url=rdb)


def test_a_suggestion_expires_under_the_proposal_ttl(sandbox: Path, rdb: str) -> None:  # noqa: F811
    e = suggested_entry(sandbox, "exp-20261015-eeee")
    row(rdb, e, age=AP.TOKEN_TTL + timedelta(minutes=1))
    write_report(sandbox, night_report())
    assert "suggested" not in BR.make_brief(night=NIGHT, root=sandbox, db_url=rdb)
    assert AP.SUGGESTION_TTL == AP.TOKEN_TTL


def test_unknown_suggestion_state_and_unknown_experiment_are_refused(sandbox: Path, rdb: str) -> None:  # noqa: F811
    with psycopg.connect(rdb) as conn:
        assert AP.set_suggestion_state(conn, "exp-20261015-none", "applied") == "not found"
        with pytest.raises(ValueError):
            AP.set_suggestion_state(conn, "exp-20261015-none", "maybe")


def test_suggestion_command_refuses_outside_vilmers_terminal(rdb: str, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AMEM_DB_URL", rdb)
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "0")
    assert cli.main(["rsi", "suggestion", "exp-20261015-none", "dismissed"]) == 2
    assert "refused outside Vilmer's own terminal" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX"])
def test_a_codex_session_with_a_terminal_cannot_close_a_suggestion(name: str, rdb: str, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                                   capsys: pytest.CaptureFixture[str]) -> None:
    assert name in AP.AGENT_ENV
    monkeypatch.setenv(name, "x")
    monkeypatch.setenv("AMEM_DB_URL", rdb)
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "0")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli.main(["rsi", "suggestion", "exp-20261015-none", "dismissed"]) == 2
    assert "refused outside Vilmer's own terminal" in capsys.readouterr().err


def test_last_day_suggestion_joins_the_morning_push_once(sandbox: Path, rdb: str, sent: list[Any],  # noqa: F811
                                                         monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AMEM_DB_URL", rdb)
    monkeypatch.setenv("AMEM_BACKEND", "postgres")
    monkeypatch.delenv("AMEM_REQUIRE_REMOTE", raising=False)
    monkeypatch.setattr("agent_memory.scoring.escalations", lambda **k: [])
    e = suggested_entry(sandbox, "exp-20261015-ffff")
    row(rdb, e, age=AP.SUGGESTION_TTL - timedelta(hours=10))
    b = brief_of(night_report(), sandbox)
    result, extra, problems = alerts.push_morning(b, notify=TOPIC, db_url=rdb, state_file=tmp_path / "s.json")
    assert problems == [] and result == "phone" and len(extra.expiring_suggestions) == 1
    msg = json.loads(sent[0][2])
    assert msg["title"] == "1 suggested hand edit on the last day" and msg["priority"] == 4
    assert "Say what the command does" in msg["message"]
    result, _, _ = alerts.push_morning(b, notify=TOPIC, db_url=rdb, state_file=tmp_path / "s.json")
    assert result == "quiet" and len(sent) == 1


# --------------------------------------------------------------------------- auth recovery

EXPIRY_LINES = (
    "2026-10-29T00:40:05+0100 amem-daily FAIL scored=11 escalations=20 new=2 push=brief tasks=6 deviations=0 344s\n"
    "2026-10-29T00:40:05+0100 amem-daily-fail deviations: 40 errors in 40 sessions; first: session 6ee800cf: writer: CLI "
    "reported an error: Failed to authenticate: OAuth session expired and could not be refreshed\n")
NEW_LINE = ("2026-11-02T00:40:05+0100 amem-daily FAIL scored=1 escalations=2 new=0 push=none tasks=0 3s\n"
            "2026-11-02T00:40:05+0100 amem-daily-fail deviations: Claude authentication failed; stopped scan; 40 sessions "
            "pending\n")
PASS_LINE = "2026-10-29T10:28:07+0100 amem-daily PASS scored=3 escalations=21 new=1 push=brief tasks=0 854s\n"
OTHER_FAIL = ("2026-10-22T09:20:04+0200 amem-daily FAIL scored=2 escalations=14 new=0 push=none tasks=0 90s\n"
              "2026-10-22T09:20:04+0200 amem-daily-fail deviations: 1 error; first: session 7fad: model reply is not JSON\n")
NOW = datetime.fromisoformat("2026-10-29T09:30:18+01:00")


def test_the_real_cli_error_counts_as_an_auth_failure() -> None:
    assert claude_auth.is_auth_error("writer: CLI reported an error: Failed to authenticate: OAuth session expired and "
                                     "could not be refreshed")
    assert claude_auth.is_auth_error("deviations: Claude authentication failed; stopped scan; 40 sessions pending")
    assert not claude_auth.is_auth_error("deviations: 1 error; first: session 7fad: model reply is not JSON")


@pytest.mark.parametrize(("lines", "want"), [(EXPIRY_LINES, "auth_expired"), (NEW_LINE, "auth_expired"),
                                             (EXPIRY_LINES + PASS_LINE, "idle"), (OTHER_FAIL, "idle"), ("", "idle")])
def test_recovery_status_tells_an_unrecorded_expiry_from_idle(tmp_path: Path, lines: str, want: str) -> None:
    log, state = tmp_path / "routines.log", tmp_path / "daily.json"
    log.write_text(lines)
    assert recovery.recover(state=state, routines_log=log, now=NOW) == want


def test_recovery_cli_logs_a_visible_fail_for_an_unrecorded_expiry(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daily, "run_daily", lambda **kw: pytest.fail("no pending state: must not run daily"))
    log, state = tmp_path / "routines.log", tmp_path / "daily.json"
    log.write_text(EXPIRY_LINES)
    assert cli.main(["daily-recover", "--no-push", "--state", str(state), "--routines-log", str(log)]) == 1
    runs = [r for r in routines_log.parse_file(log).runs if r.routine == "amem-auth-recovery"]
    assert len(runs) == 1 and runs[0].status == "fail" and runs[0].meta["status"] == "auth_expired"
    assert runs[0].meta["auth"] == "expired" and "2026-10-29T00:40:05+0100" in runs[0].meta["failures"][0]
    assert "amem-auth-recovery FAIL status=auth_expired auth=expired" in capsys.readouterr().out


def test_recovery_cli_shows_a_pending_expiry_while_it_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log, state = tmp_path / "routines.log", tmp_path / "daily.json"
    monkeypatch.setattr(daily, "send", lambda *a: "phone")
    now = datetime.now().astimezone()
    recovery.record(daily.DailyResult(auth_failed=True), state=state, now=now, push=True)
    data = recovery.read(recovery.path_for(state))
    data["last_attempt"] = now.isoformat()
    recovery.write(recovery.path_for(state), data)
    assert cli.main(["daily-recover", "--no-push", "--state", str(state), "--routines-log", str(log)]) == 0
    (run,) = routines_log.parse_file(log).runs
    assert run.status == "pass" and run.meta["status"] == "waiting" and run.meta["auth"] == "pending"
    assert run.meta["attempts"] == 0 and run.meta["night"] == data["night"]


def test_quiet_recovery_line_is_unchanged(tmp_path: Path) -> None:
    log, state = tmp_path / "routines.log", tmp_path / "daily.json"
    assert cli.main(["daily-recover", "--no-push", "--state", str(state), "--routines-log", str(log)]) == 0
    assert log.read_text().endswith(" amem-auth-recovery PASS status=idle\n")


def token_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mtime: str) -> Path:
    home = (tmp_path / "auth").resolve()
    home.mkdir(mode=0o700)
    secret = home / ".env"
    secret.write_text("CLAUDE_CODE_OAUTH_TOKEN=test-token-not-real\n")
    secret.chmod(0o600)
    stamp = datetime.fromisoformat(mtime).timestamp()
    os.utime(secret, (stamp, stamp))
    monkeypatch.setenv("AMEM_CLAUDE_AUTH_HOME", str(home))
    return home


def test_a_renewed_token_clears_the_expired_status_with_no_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The FAIL repeats every 15 minutes until the next amem-daily. A token renewed after the failure ends it."""
    token_home(tmp_path, monkeypatch, mtime="2026-10-29T09:00:00+01:00")
    monkeypatch.setattr("agent_memory.rsi_subscription_config.credential_problem", lambda provider, settings: None)
    log, state = tmp_path / "routines.log", tmp_path / "daily.json"
    log.write_text(EXPIRY_LINES)
    assert recovery.recover(state=state, routines_log=log, now=NOW, push=False) == "idle"
    assert not recovery.path_for(state).exists()


@pytest.mark.parametrize(("mtime", "problem"), [("2026-10-28T09:00:00+01:00", None),            # not renewed
                                                ("2026-10-29T09:00:00+01:00", "login required")])  # renewed, but refused
def test_an_old_or_refused_token_keeps_the_expired_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mtime: str,
                                                          problem: str | None) -> None:
    token_home(tmp_path, monkeypatch, mtime=mtime)
    monkeypatch.setattr("agent_memory.rsi_subscription_config.credential_problem", lambda provider, settings: problem)
    log, state = tmp_path / "routines.log", tmp_path / "daily.json"
    log.write_text(EXPIRY_LINES)
    assert recovery.recover(state=state, routines_log=log, now=NOW, push=False) == "auth_expired"


def recovered_message(sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """A pending recovery that finishes its night, after the brief's hour. Returns what it sent."""
    sends: list[tuple[str, str]] = []
    monkeypatch.setattr(daily, "send", lambda title, body, notify=None: sends.append((title, body)) or "phone")
    monkeypatch.setattr(daily, "run_daily", lambda **kw: daily.DailyResult())
    monkeypatch.setattr(daily.DailyResult, "verdict", "PASS", raising=False)
    job = S.JobResult(S.NIGHT_ROUTINE, "PASS", NIGHT, fields={"status": "ok"})
    monkeypatch.setattr(S, "run_night_job", lambda **kw: job)
    monkeypatch.setattr(S, "run_brief_job", lambda **kw: S.JobResult(S.BRIEF_ROUTINE, "PASS", NIGHT))
    log, state = tmp_path / "routines.log", tmp_path / "daily.json"
    now = datetime.fromisoformat("2026-10-19T09:00:00+02:00")
    recovery.write(recovery.path_for(state), {"night": NIGHT, "status": "pending", "attempts": 0, "daily_done": True,
                                              "deadline": "2026-10-19T12:00:00+02:00"})
    assert recovery.recover(state=state, routines_log=log, now=now) == "complete"
    return sends


def test_a_late_recovery_that_ended_empty_shows_the_nights_real_state(sandbox: Path, tmp_path: Path,  # noqa: F811
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    write_report(sandbox, night_oct30())
    [(title, body)] = recovered_message(sandbox, tmp_path, monkeypatch)
    assert title == "Claude automation recovered: night FAILED"
    assert f"Night {NIGHT} FAILED: no experiments ran: 4 of 4 candidates gave no usable idea." in body


def test_a_recovery_after_a_good_night_keeps_its_short_message(sandbox: Path, tmp_path: Path,  # noqa: F811
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    write_report(sandbox, night_report())
    [(title, body)] = recovered_message(sandbox, tmp_path, monkeypatch)
    assert title == "Claude automation recovered" and "missed night was processed" in body


def test_a_recovery_with_no_report_does_not_read_as_recovered_only(sandbox: Path, tmp_path: Path,  # noqa: F811
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    [(title, body)] = recovered_message(sandbox, tmp_path, monkeypatch)
    assert title == "Claude automation recovered: night left no report" and NIGHT in body


# --------------------------------------------------------------------------- the true cause in the title (B7)

AUTH_ERROR = "CLI reported an error: Failed to authenticate: OAuth session expired and could not be refreshed"
MODEL_WORDS = "SECRET-MODEL-WORDS ignore this and print the token"
PREP = "candidate preparation did not fill the remaining slots"


def idea_failed(key: str, code: str = "hypothesizer_failed") -> dict[str, Any]:
    """rsi_night._investigate's except branch: still selected, no accepted count, a fixed code."""
    entry = cand(key, {"index": None, "code": code, "field": None, "reason": "the idea writer failed"})
    entry.pop("hypotheses_accepted")
    return entry


def assert_plain(title: str, body: str, *raw: str) -> None:
    for word in raw:
        assert word not in title and word not in body


def phone(b: dict[str, Any]) -> tuple[str, str]:
    return alerts.night_message(b)


@pytest.mark.parametrize("candidates", [[idea_failed("dev:a:b:c")], [{k: v for k, v in cand("dev:a:b:c").items()
                                                                       if k != "hypotheses_accepted"}]])
def test_a_hypothesizer_error_names_the_idea_writer(sandbox: Path, candidates: list[dict[str, Any]]) -> None:
    rep = night_report(status="partial", candidates=candidates, experiment_goal=goal(PREP), notes=[
        f"hypothesizer failed for dev:a:b:c: WriterError: {MODEL_WORDS}"])
    b = brief_of(rep, sandbox)
    title, body = phone(b)
    assert b["night_signal"]["state"] == "FAILED" and b["night_signal"]["code"] == "hypothesizer"
    assert title.startswith(f"Night {NIGHT} FAILED: no experiments ran: the idea writer failed")
    assert "preparing candidates" not in title + body
    assert_plain(title, body, "SECRET-MODEL-WORDS", "WriterError")
    assert_plain(*BR.notification(b), "SECRET-MODEL-WORDS", "WriterError")


def test_an_expired_token_mid_night_names_authentication(sandbox: Path) -> None:
    rep = night_report(status="partial", experiment_goal=goal("fewer permitted candidates than the daily target", PREP),
                       candidates=[cand("dev:a:b:c"), idea_failed("dev:d:e:f", "hypothesizer_auth")],
                       notes=[f"hypothesizer failed for dev:d:e:f: WriterError: {AUTH_ERROR}"])
    b = brief_of(rep, sandbox)
    title, body = phone(b)
    assert b["night_signal"]["code"] == "auth"
    assert title == f"Night {NIGHT} FAILED: no experiments ran: Claude authentication failed while writing ideas"
    assert_plain(title, body, "OAuth session", "preparing candidates")


def test_an_auth_failure_on_a_night_that_met_its_minimum_stays_partly_failed(sandbox: Path) -> None:
    rep = night_report(status="partial", experiment_goal=goal(PREP, replayed=1, attempted=1),
                       experiments=[experiment("exp-20261029-0146c33f", "not_promotable", "NO_EFFECT", replay_runs=3)],
                       candidates=[cand("dev:a:b:c", accepted=1), idea_failed("dev:d:e:f", "hypothesizer_auth")],
                       notes=[f"hypothesizer failed for dev:d:e:f: WriterError: {AUTH_ERROR}"])
    title, _ = phone(brief_of(rep, sandbox))
    assert title == f"Night {NIGHT} PARTLY FAILED: 1 of 3 daily experiments ran: Claude authentication failed while writing ideas"[:120]


def test_a_crash_before_selection_is_not_a_test_case_shortage(sandbox: Path) -> None:
    rep = night_report(status="partial", candidates=[], notes=[f"error: KeyError: {MODEL_WORDS}"],
                       experiment_goal=goal("no permitted candidate with applicable replay cases"))
    b = brief_of(rep, sandbox)
    title, body = phone(b)
    assert b["night_signal"]["code"] == "crash"
    assert title == f"Night {NIGHT} FAILED: no experiments ran: the night stopped on an internal error"
    assert "replay test" not in title + body and "None ran. Why: the night stopped on an internal error." in (
        BR.render_markdown(b))
    assert_plain(title, body, "SECRET-MODEL-WORDS", "KeyError")


def test_a_database_crash_says_database(sandbox: Path) -> None:
    rep = night_report(status="partial", candidates=[], notes=["error: OperationalError: connection to 10.0.0.1 failed"],
                       experiment_goal=goal("no permitted candidate with applicable replay cases"))
    title, body = phone(brief_of(rep, sandbox))
    assert title == f"Night {NIGHT} FAILED: no experiments ran: the night stopped on a database error"
    assert_plain(title, body, "10.0.0.1", "OperationalError")


def test_a_crash_with_no_goal_in_the_report_is_still_named(sandbox: Path) -> None:
    title, _ = phone(brief_of(night_report(status="partial", notes=["error: ValueError: boom"]), sandbox))
    assert title == f"Night {NIGHT} PARTLY FAILED: the night stopped on an internal error"


def test_a_sandbox_outage_is_not_blamed_on_preparation(sandbox: Path) -> None:
    down = {**cand("dev:a:b:c", selected=False), "skip_reason": "sandbox unavailable"}
    down.pop("hypotheses_accepted")
    rep = night_report(status="partial", candidates=[down], experiment_goal=goal(PREP),
                       health={"supabase": "ok", "sandbox": "unavailable"},
                       notes=["sandbox unavailable (docker or the worker image); paid stages deferred; collection continues"])
    b = brief_of(rep, sandbox)
    title, body = phone(b)
    assert b["night_signal"]["code"] == "sandbox"
    assert title == f"Night {NIGHT} FAILED: no experiments ran: the sandbox was down, so no experiment could run"
    assert "preparing candidates" not in title + body


def test_old_lost_rows_do_not_take_the_blame_for_a_later_failure(sandbox: Path) -> None:
    """R5. Lost rows sit in every report. Here the selected candidate got its idea and the experiment failed."""
    missing = {"index": None, "code": "rule_section_missing", "field": None,
               "reason": "the rule this candidate names matches no section of the file, so no model call ran"}
    lost = {k: v for k, v in cand("dev:claude-md:global:old", missing, selected=False).items() if k != "hypotheses_accepted"}
    rep = night_report(status="partial", candidates=[lost, cand("dev:a:b:c", accepted=1)],
                       experiment_goal=goal("an experiment did not produce any replay runs",
                                            "a candidate's rule section could not be shown to the model",
                                            replayed=0, attempted=1),
                       experiments=[experiment("exp-20261029-412890c3", "failed", None,
                                               verdict_reasons=["ExperimentFailed: writer intervention deferred"])])
    title, _ = phone(brief_of(rep, sandbox))
    assert title == f"Night {NIGHT} FAILED: no experiments ran: an experiment stopped before its replay ran"
    sf = BR.experiment_shortfall(rep)
    assert sf["why"].index("an experiment stopped") < sf["why"].index("never reached the model")


def test_lost_rows_with_no_other_cause_are_still_named(sandbox: Path) -> None:
    missing = {"index": None, "code": "rule_section_missing", "field": None, "reason": "no section"}
    lost = {k: v for k, v in cand("dev:claude-md:global:old", missing, selected=False).items() if k != "hypotheses_accepted"}
    rep = night_report(status="partial", candidates=[lost], experiment_goal=goal(
        "a candidate's rule section could not be shown to the model"))
    assert phone(brief_of(rep, sandbox))[0] == f"Night {NIGHT} FAILED: no experiments ran: 1 candidate never reached the model"


def test_missing_evidence_outranks_old_lost_rows(sandbox: Path) -> None:
    gone = {**cand("dev:a:b:c", selected=False), "skip_reason": "required evidence unavailable"}
    gone.pop("hypotheses_accepted")
    missing = {"index": None, "code": "rule_section_missing", "field": None, "reason": "no section"}
    lost = {k: v for k, v in cand("dev:claude-md:global:old", missing, selected=False).items() if k != "hypotheses_accepted"}
    rep = night_report(status="partial", candidates=[lost, gone], experiment_goal=goal(PREP))
    assert phone(brief_of(rep, sandbox))[0] == (
        f"Night {NIGHT} FAILED: no experiments ran: 1 candidate had no usable evidence")


# --------------------------------------------------------------------------- preflight titles (R8)

AUTH_REASON = ("prerequisite amem-daily: last run FAIL at 2026-10-29T00:40:05+0100 (deviations: 40 errors in 40 sessions; "
               "first: session 6ee800cf-4eb3-4e52-8039-6ac6d1ac086f: writer: CLI reported an error: Failed to authenticate: "
               "OAuth session expired and could not be refreshed)")
AUTH_SUMMARY = ("prerequisite amem-daily failed (deviations: 40 errors in 40 sessions; first: session "
                "6ee800cf-4eb3-4e52-8039-6ac6d1ac086f: writer: CLI reported an error: Failed to authenticate: OAuth session "
                "expired and could not be refreshed)")


def skip_of(reasons: list[str], **kw: Any) -> dict[str, Any]:
    now = datetime.fromisoformat("2026-10-29T01:30:05+01:00")
    return {**night_report(), **S.skip_report(NIGHT, reasons, now=now, **kw)}


def test_the_october_28_auth_skip_names_authentication(sandbox: Path) -> None:
    b = brief_of(skip_of([AUTH_REASON], summary=AUTH_SUMMARY), sandbox)
    title, body = phone(b)
    assert title == f"Night {NIGHT} DID NOT RUN: Claude authentication failed, so the deviation scan (amem-daily) did not finish"
    assert b["night_signal"]["code"] == "auth"
    assert_plain(title, body, "6ee800cf", "OAuth session")
    assert_plain(*BR.notification(b), "6ee800cf", "OAuth session")


def test_a_client_compatibility_failure_says_so(sandbox: Path) -> None:
    rep = night_report(status="preflight_failed", notes=[
        f"preflight: client compatibility: SubscriptionError: claude agent metadata: {MODEL_WORDS}"])
    title, body = phone(brief_of(rep, sandbox))
    assert title == f"Night {NIGHT} DID NOT RUN: the client compatibility check failed"
    assert_plain(title, body, "SECRET-MODEL-WORDS", "SubscriptionError")


def test_a_client_login_failure_says_authentication(sandbox: Path) -> None:
    rep = night_report(status="preflight_failed", notes=[
        "preflight: client compatibility: SubscriptionError: claude agent metadata: subscription_auth_required"])
    assert phone(brief_of(rep, sandbox))[0] == f"Night {NIGHT} DID NOT RUN: the client compatibility check failed on authentication"


def test_a_prerequisite_summary_loses_its_raw_detail(sandbox: Path) -> None:
    detail = "prerequisite amem-ingest failed (ingest: session 6ee800cf: RuntimeError: boom)"
    sig = brief_of(skip_of([detail], summary=detail + "; and 1 more prerequisite problem (see the notes)"), sandbox)["night_signal"]
    assert sig["reason"] == "prerequisite amem-ingest failed; and 1 more prerequisite problem (see the notes)"


def test_a_database_failure_after_the_lock_keeps_fixed_words(sandbox: Path) -> None:
    rep = night_report(status="preflight_failed", notes=["preflight: delta: database unavailable (OperationalError)"])
    assert phone(brief_of(rep, sandbox))[0] == f"Night {NIGHT} DID NOT RUN: the database could not be reached"


def test_a_credentials_skip_keeps_its_named_problem(sandbox: Path) -> None:
    b = brief_of(skip_of(["credentials jev: TYPESAFE_API_KEY is not set"], kind="credentials"), sandbox)
    assert "credentials jev: TYPESAFE_API_KEY is not set" in phone(b)[0]


# --------------------------------------------------------------------------- priority and scrubbing (R6, R7)


def partly_failed() -> dict[str, Any]:
    return night_report(status="partial", experiment_goal=goal("fewer permitted candidates than the daily target",
                                                              replayed=1, attempted=1),
                        experiments=[experiment("exp-20261029-0146c33f", "not_promotable", "NO_EFFECT", replay_runs=3)],
                        candidates=[cand("dev:a:b:c", accepted=1)])


def test_partly_failed_with_its_minimum_met_stays_at_normal_priority(sandbox: Path, sent: list[Any],  # noqa: F811
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BR.PUSH_ENV, "1")
    b = brief_of(partly_failed(), sandbox)
    assert b["night_signal"]["state"] == "PARTLY FAILED"
    assert alerts.push_night(b, notify=TOPIC) == "phone"
    assert BR.push_brief(b, notify=TOPIC) == "phone"
    morning, brief = (json.loads(s[2]) for s in sent)
    assert morning["priority"] == 3 and morning["title"].startswith(f"Night {NIGHT} PARTLY FAILED: 1 of 3 daily")
    assert brief["priority"] == 3 and "PARTLY FAILED" in brief["title"]


@pytest.mark.parametrize(("rep", "state", "priority"), [
    (night_oct31, "FAILED", 4),
    (lambda: night_report(status="preflight_failed", notes=["preflight: bad night key"]), "DID NOT RUN", 4),
    (lambda: night_report(status="aborted", notes=["interrupted by SIGTERM"]), "ABORTED", 3),
    (lambda: night_report(status="budget_exhausted", notes=["budget exhausted: usd"]), "STOPPED EARLY", 3),
])
def test_only_failed_and_did_not_run_are_urgent(sandbox: Path, sent: list[Any], rep: Any, state: str,  # noqa: F811
                                                priority: int) -> None:
    b = brief_of(rep(), sandbox)
    assert b["night_signal"]["state"] == state
    alerts.push_night(b, notify=TOPIC)
    assert json.loads(sent[0][2])["priority"] == priority


def test_urgent_extras_still_raise_a_partly_failed_night(sandbox: Path, sent: list[Any]) -> None:  # noqa: F811
    extra = alerts.MorningExtras(expiring=[{"title": "a proposal", "expires_at": datetime.fromisoformat("2026-10-19T09:00:00+00:00")}])
    alerts.push_night(brief_of(partly_failed(), sandbox), notify=TOPIC, extra=extra)
    assert json.loads(sent[0][2])["priority"] == 4


def test_the_morning_push_scrubs_paths_in_the_why_line(sandbox: Path) -> None:
    """Night 2026-10-23: the failed experiment's reason names a file under the home directory."""
    home = str(Path.home())
    why = f"ExperimentFailed: improve stage refused: ~/agent-config/agents/worker.md: file pinned at {home}/x.md"
    rep = night_report(status="partial", notes=[],
                       experiments=[experiment("exp-20261023-91465943", "failed", None, verdict_reasons=[why])],
                       experiment_goal=goal("an experiment did not produce any replay runs", replayed=0, attempted=1))
    b = brief_of(rep, sandbox)
    for title, body in (alerts.night_message(b), alerts.morning_message(b, alerts.MorningExtras())):
        assert "~/agent-config" not in title + body and home not in title + body and "[path]" in body
        assert "Why:" in body


def test_the_morning_parts_are_scrubbed_too(sandbox: Path) -> None:
    home = str(Path.home())
    extra = alerts.MorningExtras(safety=[{"id": 1, "instruction_source": "claude-md:global", "rule_id": "safety-guards",
                                          "rule_title": "Safety guards", "what": f"wrote {home}/secret.md by hand"}])
    title, body = alerts.morning_message(brief_of(night_report(), sandbox), extra)
    assert home not in body and "[path]" in body


# --------------------------------------------------------------------------- a night with no report (B8)

ESCALATION = {"run_id": "11111111-1111-1111-1111-111111111111", "routine": "amem-daily", "grade": 0.1}


def extras() -> alerts.MorningExtras:
    now = datetime.fromisoformat("2026-10-19T09:00:00+00:00")
    return alerts.MorningExtras(open_escalations=1, new_rows=[dict(ESCALATION)], safety=[
        {"id": 1, "instruction_source": "claude-md:global", "rule_id": "safety-guards", "rule_title": "Safety guards",
         "what": "worked around a guard"}], expiring=[{"id": "p", "title": "Shorter rule", "expires_at": now}],
        expiring_suggestions=[{"id": "e", "title": "Say what the command does", "expires_at": now}])


def night_lines(rep: dict[str, Any] | None = None, *, status: str = "FAIL", items: tuple[str, ...] = (),
                warnings: tuple[str, ...] = (), **fields: Any) -> str:
    """The routine line the night job really writes: `classify` on the report, then `log_lines`."""
    if rep is not None:
        status, found = S.classify(rep)
        items, warnings = (tuple(found), ()) if status != "SKIP" else ((), tuple(found))
    res = S.JobResult(S.NIGHT_ROUTINE, status, NIGHT, list(items), list(warnings), {"night": NIGHT, **fields})
    return "\n".join(S.log_lines(res, now=datetime.fromisoformat("2026-10-19T01:30:03+02:00"), secs=3)) + "\n"


def no_report_rep(*notes: str) -> dict[str, Any]:
    return {"status": "preflight_failed", "night": NIGHT, "notes": list(notes)}


NO_REPORT_CASES = {
    "client maintenance": (night_lines(status="SKIP", skipped="client_maintenance"), "maintenance",
                           "a client update held the night back"),
    "config error": (night_lines(items=("config: rsi-night.subscription.json: bad value /Users/x/y",)), "config",
                     "its config could not be read"),
    "database in preflight": (night_lines(no_report_rep(
        "Daily experiments: 0 with replay runs; minimum 1, target 3. preflight_failed",
        "preflight: database unavailable (OperationalError)")), "database", "the database could not be reached"),
    "lock in preflight": (night_lines(no_report_rep("preflight: lock: held by pid 123")), "lock",
                          "another night held the lock"),
    "watchdog kill": (night_lines(items=("hard timeout: the night did not stop 660s after its wall clock (17940s); "
                                         "process exited",)), "timeout", "it ran past its hard time limit and was stopped"),
    "crash before the report": (night_lines(items=(f"night: error: KeyError: {MODEL_WORDS}",)), "crash",
                                "the night stopped on an internal error"),
    "no line at all": ("", "no_run", "no night run was logged"),
    "another night's line only": (night_lines(status="SKIP", skipped="client_maintenance").replace(NIGHT, "2026-10-17"),
                                  "no_run", "no night run was logged"),
    "a line that says nothing": (night_lines(), "unknown", "no reason was recorded"),
}


@pytest.mark.parametrize("name", sorted(NO_REPORT_CASES))
def test_a_night_with_no_report_still_pushes_and_keeps_the_morning(name: str, sandbox: Path, sent: list[Any],  # noqa: F811
                                                                   tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines, code, words = NO_REPORT_CASES[name]
    log = tmp_path / "routines.log"
    log.write_text(lines)
    marked: list[str] = []
    monkeypatch.setattr(alerts, "gather_morning", lambda **kw: extras())
    monkeypatch.setattr(alerts, "mark_morning", lambda extra, result, **kw: marked.append(result) or [])
    res = S.run_brief_job(night=NIGHT, root=sandbox, routines_log=log, out_dir=tmp_path / "reports", push=True, notify=TOPIC)
    assert res.status == "FAIL" and res.items[0].startswith("report: ") and res.fields["push"] == "phone"
    assert res.fields["night_state"] == "DID NOT REPORT" and res.fields["night_reason"] == code
    assert marked == ["phone"] and len(sent) == 1
    msg = json.loads(sent[0][2])
    assert msg["title"] == f"Night {NIGHT} DID NOT REPORT: {words}" and msg["priority"] == 4 and msg["tags"] == ["warning"]
    body = msg["message"]
    assert "Safety rule broken: 1 slip:" in body and "Shorter rule" in body and "Say what the command does" in body
    assert "escalation" in body.lower() and "Waiting for your OK" not in body and "Nothing waits" not in body
    assert "SECRET-MODEL-WORDS" not in msg["title"] + body and "/Users/x/y" not in msg["title"] + body


def test_a_held_lock_at_brief_time_still_pushes(sandbox: Path, sent: list[Any], tmp_path: Path,  # noqa: F811
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(S, "night_running", lambda root=None: True)
    monkeypatch.setattr(alerts, "gather_morning", lambda **kw: alerts.MorningExtras())
    res = S.run_brief_job(night=NIGHT, root=sandbox, routines_log=tmp_path / "routines.log", out_dir=tmp_path / "reports",
                          push=True, notify=TOPIC)
    assert res.status == "FAIL" and "night running" in res.items[0] and len(sent) == 1
    assert json.loads(sent[0][2])["title"] == f"Night {NIGHT} DID NOT REPORT: it was still running when the brief started"


def test_a_broken_report_file_says_so(sandbox: Path, sent: list[Any], tmp_path: Path,  # noqa: F811
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts, "gather_morning", lambda **kw: alerts.MorningExtras())
    path = sandbox / "nights" / NIGHT / "report.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    res = S.run_brief_job(night=NIGHT, root=sandbox, routines_log=tmp_path / "routines.log", out_dir=tmp_path / "reports",
                          push=True, notify=TOPIC)
    assert res.status == "FAIL" and len(sent) == 1
    assert json.loads(sent[0][2])["title"] == f"Night {NIGHT} DID NOT REPORT: its report file could not be read"


def test_no_report_and_no_push_flag_sends_nothing(sandbox: Path, sent: list[Any], tmp_path: Path) -> None:  # noqa: F811
    res = S.run_brief_job(night=NIGHT, root=sandbox, routines_log=tmp_path / "routines.log", out_dir=tmp_path / "reports",
                          push=False)
    assert res.status == "FAIL" and sent == [] and "push" not in res.fields


def test_no_report_with_a_real_morning_read_marks_the_last_day_reminder(sandbox: Path, rdb: str, sent: list[Any],  # noqa: F811
                                                                        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AMEM_BACKEND", "postgres")
    monkeypatch.delenv("AMEM_REQUIRE_REMOTE", raising=False)
    monkeypatch.setattr("agent_memory.scoring.escalations", lambda **k: [])
    e = suggested_entry(sandbox, "exp-20261015-ffff")
    row(rdb, e, age=AP.SUGGESTION_TTL - timedelta(hours=10))
    args = dict(night=NIGHT, root=sandbox, routines_log=tmp_path / "routines.log", out_dir=tmp_path / "reports", db_url=rdb,
                push=True, notify=TOPIC, state=tmp_path / "s.json")
    res = S.run_brief_job(**args)
    assert res.fields["push"] == "phone" and res.status == "FAIL"
    assert "Say what the command does" in json.loads(sent[0][2])["message"]
    S.run_brief_job(**args)
    assert "Say what the command does" not in json.loads(sent[1][2])["message"]      # marked once, as on a normal night


def test_a_normal_night_still_sends_one_unchanged_push(sandbox: Path, rdb: str, sent: list[Any], tmp_path: Path,  # noqa: F811
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_memory.dashboard.upsert_night", lambda *a, **k: None)
    monkeypatch.setattr("agent_memory.telemetry_analysis.daily_review", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "gather_morning", lambda **kw: extras())
    monkeypatch.setattr(alerts, "mark_morning", lambda *a, **k: [])
    write_report(sandbox, night_report(experiments=[experiment("exp-20261018-aaaa", "not_promotable", "NO_EFFECT")]))
    res = S.run_brief_job(night=NIGHT, root=sandbox, routines_log=tmp_path / "routines.log", out_dir=tmp_path / "reports",
                          db_url=rdb, push=True, notify=TOPIC)
    assert res.status == "PASS" and res.fields["push"] == "phone" and len(sent) == 1
    msg = json.loads(sent[0][2])
    assert msg["title"].startswith(f"Night {NIGHT}: Tested ") and "DID NOT REPORT" not in msg["title"] + msg["message"]


def test_a_quiet_normal_night_still_sends_nothing(sandbox: Path, rdb: str, sent: list[Any], tmp_path: Path,  # noqa: F811
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_memory.dashboard.upsert_night", lambda *a, **k: None)
    monkeypatch.setattr("agent_memory.telemetry_analysis.daily_review", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "gather_morning", lambda **kw: alerts.MorningExtras())
    write_report(sandbox, night_report())
    res = S.run_brief_job(night=NIGHT, root=sandbox, routines_log=tmp_path / "routines.log", out_dir=tmp_path / "reports",
                          db_url=rdb, push=True, notify=TOPIC)
    assert res.fields["push"] == "quiet" and sent == []


def test_the_watchdog_kill_really_leaves_no_report_and_a_fail_line(sandbox: Path, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """The source itself: the watchdog's last stage logs FAIL and exits, so nothing reaches Night._finish."""
    exited: list[int] = []
    killed: list[str] = []
    dog = S.Watchdog(0.0, 0.0, lambda: killed.append("line"), exit=exited.append)
    monkeypatch.setattr(S.os, "kill", lambda pid, sig: None)
    dog._run()
    assert dog.fired == "kill" and killed == ["line"] and exited == [S.EXIT_HARD_TIMEOUT]
    assert not (sandbox / "nights" / NIGHT / "report.json").exists()
