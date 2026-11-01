"""A failed or empty night must look failed: the phone title and priority, the brief's reason lines, the routine
line, the suggested hand edits that stay visible, and the auth recovery line. The report shapes are the live ones of
nights 2026-10-30 and 2026-10-31 (`~/sandbox/nights/<night>/report.json`). ntfy is never called: test_rsi_brief's
`sent` fixture replaces every way out."""

from __future__ import annotations

import json
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
