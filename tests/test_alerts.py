"""alerts.py: the night-result push, the last-day proposal reminder and the safety-slip alert. ntfy is never
called: daily.http_post is replaced. A throwaway Postgres (the rsi_approval fixtures)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from agent_memory import alerts, daily
from test_rsi_approval import make_artifact, proposal, rdb, sandbox  # noqa: F401  (fixtures)

TOPIC = {"NTFY_TOPIC": "test-topic"}


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def fake(url: str, body: bytes, headers: dict[str, str]) -> int:
        out.append(json.loads(body))
        return 200

    monkeypatch.setattr(daily, "http_post", fake)
    monkeypatch.setattr(daily, "mac_notify", lambda title, body: pytest.fail("fell back to the Mac"))
    return out


def brief(**kw: Any) -> dict[str, Any]:
    b = {"night": "2026-10-24", "status": "ok", "notes": [], "skipped": None,
         "experiments": {"items": [], "total": 0}, "pending_decisions": {"total": 0},
         "costs": {"total_usd": 0.165085}, "health": {"degraded": []}}
    b.update(kw)
    return b


def test_rule_names_match_night_desk() -> None:
    assert alerts.rule_name("dev:skill:meltdown:intro") == "Meltdown skill › Intro"
    assert alerts.rule_name("dev:agent:worker:rules") == "worker.md › Rules"
    assert alerts.rule_name("dev:claude-md:global:global-claude-code-context/shell-habits") == "CLAUDE.md › Shell habits"
    assert alerts.rule_name("dev:routine:deviations-daily:whole") == "deviations-daily routine › whole file"


def test_night_message_says_what_happened_and_stays_quiet_otherwise() -> None:
    assert alerts.night_message(brief()) is None
    tested = alerts.night_message(brief(experiments={"items": [{"candidate_key": "dev:skill:meltdown:intro",
                                                                "verdict": "WORSE"}], "total": 1}))
    assert tested is not None
    title, body = tested
    assert title == "Night 2026-10-24: Tested Meltdown skill › Intro: made things worse, nothing changed"
    assert "Nothing waits for your OK." in body and "Cost $0.17." in body
    skipped = alerts.night_message(brief(status="preflight_failed", notes=[
        "skipped by amem-rsi-night: prerequisite amem-daily: last run FAIL at 2026-10-21T09:20:05+0200"]))
    assert skipped is not None
    assert skipped[0] == "Night 2026-10-24: skipped"
    assert "Skipped: the deviation scan (amem-daily) failed." in skipped[1]
    waiting = alerts.night_message(brief(pending_decisions={"total": 2}))
    assert waiting is not None and "Waiting for your OK: 2." in waiting[1]


def test_push_night_sends_one_message_and_nothing_for_a_quiet_night(sent: list[dict[str, Any]]) -> None:
    assert alerts.push_night(brief(), notify=TOPIC) == "quiet"
    assert sent == []
    b = brief(status="partial")
    assert alerts.push_night(b, notify=TOPIC) == "phone"
    assert len(sent) == 1 and sent[0]["topic"] == "test-topic" and "partly ran" in sent[0]["message"]


def deviation(conn: psycopg.Connection, key: str, rule_id: str, source: str = "claude-md:global") -> None:
    conn.execute("insert into sessions(session_id, kind, project_dir) values (%s, 'chat', '/test') on conflict do nothing", (key,))
    conn.execute("insert into deviations(source_key, session_id, instruction_source, instruction_ref, rule_id, "
                 "rule_title, what, why, how, followed_grade, result_grade) values (%s,%s,%s,%s,%s,%s,%s,'w','h',0.2,0.5)",
                 (key, key, source, rule_id, rule_id, rule_id.split("/")[-1],
                  f"The agent renamed a folder so the guard no longer matched ({key})."))


def test_safety_slips_are_pushed_once(rdb: str, sent: list[dict[str, Any]]) -> None:  # noqa: F811
    with psycopg.connect(rdb) as conn:
        deviation(conn, "s1", "desktop-delegation-policy-v1-1/approval-without-handing-work-back")
        deviation(conn, "s2", "safety-guards", source="parent-md")
        deviation(conn, "s3", "shell-habits")                    # not a safety rule
    assert alerts.push_safety_slips(db_url=rdb, push=False, notify=TOPIC) == "off"
    assert sent == []
    assert alerts.push_safety_slips(db_url=rdb, notify=TOPIC) == "phone"
    assert len(sent) == 1
    assert sent[0]["title"] == "Safety rule broken: 2 slips"
    assert "CLAUDE.md › Approval without handing work back" in sent[0]["message"]
    assert "PARENT.md › Safety guards" in sent[0]["message"] and "shell" not in sent[0]["message"].lower()
    assert alerts.push_safety_slips(db_url=rdb, notify=TOPIC) is None   # already alerted
    with psycopg.connect(rdb) as conn:
        marked = conn.execute("select count(*) from deviations where meta->>'alert' = 'phone'").fetchone()[0]
    assert marked == 2


def test_last_day_proposals_get_one_reminder(rdb: str, sandbox, sent: list[dict[str, Any]]) -> None:  # noqa: F811
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    soon = proposal(rdb, d, title="Edit closing line", ttl=timedelta(hours=10))
    h2, d2 = make_artifact(sandbox, [("~/y.md", b"a\n", b"c\n")], exp="exp-20261016-02")
    later = proposal(rdb, d2, title="Edit opening line", ttl=timedelta(hours=60))
    assert alerts.remind_proposals(db_url=rdb, notify=TOPIC) == ["phone"]
    assert len(sent) == 1 and sent[0]["title"].startswith("Expires in 10 h · Proposal: Edit closing line")
    assert alerts.remind_proposals(db_url=rdb, notify=TOPIC) == []          # once
    with psycopg.connect(rdb) as conn:
        meta = dict(conn.execute("select id::text, meta->>'reminded' from rsi_proposals").fetchall())
    assert meta[soon] == "phone" and meta[later] is None


# --------------------------------------------------------------------------- the one morning push (2026-10-26)

ESC_IDS = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")


@pytest.fixture
def escalations(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What scoring.escalations returns for two open runs. Their summaries and ids must never reach a message."""
    from agent_memory import scoring
    rows = [{"run_id": ESC_IDS[0], "routine": "config-check", "summary": "SECRET-SUMMARY", "failures": ["SECRET-F"]},
            {"run_id": ESC_IDS[1], "routine": "routine-audit", "summary": "SECRET-TWO", "failures": []}]
    monkeypatch.setattr(scoring, "escalations", lambda **kw: list(rows))
    return rows


def state_ids(path: Any) -> list[str]:
    return json.loads(path.read_text())["pushed_run_ids"]


def test_quiet_night_with_new_escalations_still_pushes_once(rdb: str, sent: list[dict[str, Any]],  # noqa: F811
                                                            escalations: list[dict[str, Any]], tmp_path: Any) -> None:
    state = tmp_path / "state.json"
    assert alerts.night_message(brief()) is None                                  # the night alone is quiet
    result, extra, problems = alerts.push_morning(brief(), notify=TOPIC, db_url=rdb, state_file=state)
    assert (result, problems, len(extra.new_rows), extra.open_escalations) == ("phone", [], 2, 2)
    (msg,) = sent
    assert msg["title"] == "amem: 2 new runs to label"
    assert msg["message"].startswith("2 new escalations (2 open): config-check 1, routine-audit 1.")
    for secret in ("SECRET", *ESC_IDS):                                           # counts and routines only
        assert secret not in msg["message"] and secret not in msg["title"]
    assert sorted(state_ids(state)) == sorted(ESC_IDS)                            # marked after ntfy said 200
    again = alerts.push_morning(brief(), notify=TOPIC, db_url=rdb, state_file=state)
    assert again[0] == "quiet" and len(sent) == 1                                 # pushed once


def test_morning_push_joins_the_night_escalations_and_safety_slips(rdb: str, sent: list[dict[str, Any]],  # noqa: F811
                                                                    escalations: list[dict[str, Any]],
                                                                    tmp_path: Any) -> None:
    with psycopg.connect(rdb) as conn:
        deviation(conn, "s1", "safety-guards", source="parent-md")
    b = brief(experiments={"items": [{"candidate_key": "dev:skill:meltdown:intro", "verdict": "IMPROVES"}],
                           "total": 1}, pending_decisions={"total": 1})
    result, extra, _ = alerts.push_morning(b, notify=TOPIC, db_url=rdb, state_file=tmp_path / "s.json")
    assert result == "phone" and len(sent) == 1 and len(extra.safety) == 1
    msg = sent[0]
    assert msg["title"] == "Night 2026-10-24: Tested Meltdown skill › Intro: it won and waits for your OK"
    body = msg["message"]
    assert "Waiting for your OK: 1." in body and "Safety rule broken: 1 slip:" in body
    assert "PARENT.md › Safety guards" in body and "2 new escalations (2 open)" in body
    assert body.rstrip().endswith("Details: Night Desk, Last night.") and msg["priority"] == 4
    with psycopg.connect(rdb) as conn:                                           # each part goes out once
        assert conn.execute("select meta->>'alert' from deviations").fetchall() == [("phone",)]
    assert alerts.push_morning(brief(), notify=TOPIC, db_url=rdb, state_file=tmp_path / "s.json")[0] == "quiet"


def test_morning_push_fallback_leaves_escalations_unmarked_and_quiet_sends_nothing(
        rdb: str, monkeypatch: pytest.MonkeyPatch, escalations: list[dict[str, Any]], tmp_path: Any) -> None:  # noqa: F811
    macs: list[tuple[str, str]] = []
    monkeypatch.setattr(daily, "http_post", lambda url, body, headers: 500)
    monkeypatch.setattr(daily, "mac_notify", lambda title, body: macs.append((title, body)))
    state = tmp_path / "state.json"
    result, _, _ = alerts.push_morning(brief(), notify=TOPIC, db_url=rdb, state_file=state)
    assert result == "mac" and len(macs) == 1 and state_ids(state) == []          # retried tomorrow, as daily did
    from agent_memory import scoring
    monkeypatch.setattr(scoring, "escalations", lambda **kw: [])
    quiet = alerts.push_morning(brief(), notify=TOPIC, db_url=rdb, state_file=tmp_path / "other.json")
    assert quiet[0] == "quiet" and len(macs) == 1 and not (tmp_path / "other.json").exists()


def test_morning_parts_need_a_remote_postgres(rdb: str, sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                             escalations: list[dict[str, Any]], tmp_path: Any) -> None:
    monkeypatch.setenv("AMEM_REQUIRE_REMOTE", "1")                                # the test DB is local: refused
    result, extra, problems = alerts.push_morning(brief(), notify=TOPIC, db_url=rdb, state_file=tmp_path / "s.json")
    assert result == "quiet" and extra.empty() and sent == []
    assert problems == ["morning parts: needs the Postgres backend (remote required)"]
