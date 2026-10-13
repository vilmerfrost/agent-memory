"""Push payload helpers, the undo URL, and the undo poll (pending -> improve revert -> handled)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import edits, edits_push
from conftest import run_amem
from test_daily_support import DailyIO, Push, daily_io  # noqa: F401
from test_edits_support import (  # noqa: F401
    SPAWN_NEW,
    EHome,
    apply_answer,
    edit_rows,
    ehome,
    envelope,
    ewriter,
    home_env,
    proposal,
    seed,
    sha_of,
)
from test_scoring_support import ScoringDb, sdb  # noqa: F401

NOTIFY = {"NTFY_TOPIC": "secret-topic-xyz"}
REF = "abcdefghijklmnop"
HOSTED = "https://" + REF + ".supabase" + ".co"   # built, so no hosted URL sits in the repo's files


# --------------------------------------------------------------------------- URL + payload


def test_undo_url_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    assert edits_push.undo_url() is None
    monkeypatch.setenv("AMEM_DB_URL", f"postgresql://postgres:pw@db.{REF}.supabase.co:5432/postgres")
    assert edits_push.undo_url() == HOSTED + "/functions/v1/amem-undo"
    monkeypatch.setenv("AMEM_DB_URL", f"postgresql://postgres.{REF}:pw@aws-0-eu-north-1.pooler.supabase.com:6543/postgres")
    assert edits_push.undo_url() == HOSTED + "/functions/v1/amem-undo"
    monkeypatch.setenv("AMEM_DB_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
    assert edits_push.undo_url() is None
    monkeypatch.setenv("AMEM_SUPABASE_URL", "https://proj.example.test/")
    assert edits_push.undo_url() == "https://proj.example.test/functions/v1/amem-undo"
    monkeypatch.setenv("AMEM_EDITS_UNDO_URL", "http://127.0.0.1:54321/functions/v1/amem-undo")
    assert edits_push.undo_url() == "http://127.0.0.1:54321/functions/v1/amem-undo"


def _outcome(**kw: Any) -> edits.Outcome:
    base = dict(source="skill:dispatch", rule_id="spawn", status="kept", target="~/.agents/skills/dispatch/SKILL.md",
                edit_id=uuid.UUID(int=7), rule_title="4. Spawn", summary="Spawn names the branch.",
                why="Clearer trigger.")
    return edits.Outcome(**{**base, **kw})


def test_payload_and_message() -> None:
    title, body = edits_push.edit_message(_outcome(), with_cli_undo=False)
    assert title == "Agent edit: 4. Spawn"
    assert body == "Spawn names the branch.\nWhy: Clearer trigger.\nFile: dispatch/SKILL.md"
    p = edits_push.ntfy_payload("t", title, body, url="https://u.example.test/x", edit_id=uuid.UUID(int=7), token="tok")
    assert p["topic"] == "t" and p["actions"][0]["body"] == json.dumps({"edit_id": str(uuid.UUID(int=7)), "token": "tok"})
    assert p["actions"][0]["headers"] == {"Content-Type": "application/json"} and p["actions"][0]["clear"] is True
    assert "actions" not in edits_push.ntfy_payload("t", title, body, url=None, edit_id=1, token="tok")
    _, body = edits_push.edit_message(_outcome(), with_cli_undo=True)
    assert body.endswith(f"Undo: amem edits undo {uuid.UUID(int=7)}")
    assert edits_push.short_name("~/.claude/CLAUDE.md") == "CLAUDE.md"


# --------------------------------------------------------------------------- with a database


def make_kept(url: str, ehome: EHome, ewriter: Any, cid: str = "c-0042", status: str = "kept") -> uuid.UUID:  # noqa: F811
    seed(url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=sha_of(ehome.skill))
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer(status, cid))
    edits.run(db_url=url, push=False)
    return edit_rows(url)[0]["id"]


@pytest.fixture
def live(sdb: ScoringDb, monkeypatch: pytest.MonkeyPatch) -> ScoringDb:  # noqa: F811
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    return sdb


def phone_request(url: str, edit_id: uuid.UUID) -> str:
    """What the edge function calls (rpc amem_request_undo). Returns its state."""
    with psycopg.connect(url) as conn:
        (state,) = conn.execute("select amem_request_undo(%s, 'phone')", (edit_id,)).fetchone()
        return state


def requests(url: str) -> list[dict[str, Any]]:
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from undo_requests order by requested_at")
        return cur.fetchall()


def test_no_topic_falls_back_to_mac_with_cli_undo(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                  daily_io: DailyIO) -> None:  # noqa: F811
    push = daily_io.use_push(Push(status=500))
    seed(live.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=sha_of(ehome.skill))
    ewriter(envelope(proposal(SPAWN_NEW)))
    ehome.improve.queue("apply", apply_answer("kept", "c-1"))
    (o,) = edits.run(db_url=live.url, notify=NOTIFY).outcomes
    assert o.push == "mac" and len(push.posts) == 1 and len(push.macs) == 1
    assert push.macs[0][1].endswith(f"Undo: amem edits undo {o.edit_id}")
    (o2,) = edits.run(db_url=live.url, notify={}).outcomes  # handled: no second push
    assert o2.status == "skip"


def test_undo_poll_reverts_and_is_idempotent(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                             daily_io: DailyIO) -> None:  # noqa: F811
    push = daily_io.use_push(Push(status=200))
    edit_id = make_kept(live.url, ehome, ewriter)
    assert edits_push.undo_poll(notify=NOTIFY, db_url=live.url).outcomes == []   # nothing pending
    assert phone_request(live.url, edit_id) == "queued"
    assert phone_request(live.url, edit_id) == "queued"   # a double tap: still one request
    assert len(requests(live.url)) == 1
    ehome.improve.queue("revert", {"stdout": "improve revert: c-0042 reverted\n", "exit": 0})
    r = edits_push.undo_poll(notify=NOTIFY, db_url=live.url)
    assert [o.result for o in r.outcomes] == ["reverted"] and r.summary().startswith("undo-poll: requests=1 reverted=1")
    assert ehome.improve.calls()[-1] == ["revert", "c-0042", "--reason", "phone undo"]
    (req,) = requests(live.url)
    assert req["handled_at"] is not None and req["result"] == "reverted" and req["attempts"] == 1
    (row,) = edit_rows(live.url)
    assert row["status"] == "reverted" and row["reverted_at"] is not None and row["undo_requested_at"] is not None
    (post,) = push.posts
    note = json.loads(post["body"])
    assert note["title"] == "Agent edit undone: 4. Spawn" and "dispatch/SKILL.md" in note["message"]
    # Again: nothing to do, improve not called again.
    n = len(ehome.improve.calls())
    assert edits_push.undo_poll(notify=NOTIFY, db_url=live.url).outcomes == []
    # A re-tap after the revert: honest "reverted", the row stays closed, no new work.
    assert phone_request(live.url, edit_id) == "reverted"
    (req2,) = requests(live.url)
    assert req2 == req
    assert edits_push.undo_poll(notify=NOTIFY, db_url=live.url).outcomes == [] and len(ehome.improve.calls()) == n
    # The reverted rule is not edited again at the old version.
    assert edits.run(dry_run=True, db_url=live.url).outcomes[0].status == "skip"


def test_revert_failure_is_recorded_retried_and_closed(live: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                       daily_io: DailyIO) -> None:  # noqa: F811
    push = daily_io.use_push(Push(status=200))
    edit_id = make_kept(live.url, ehome, ewriter)
    phone_request(live.url, edit_id)
    fail = {"stdout": "", "stderr": "improve: lock held", "exit": 1}
    ehome.improve.queue("revert", fail, fail, fail)
    for attempt in (1, 2):
        (o,) = edits_push.undo_poll(notify=NOTIFY, db_url=live.url).outcomes
        assert o.result == "retry" and f"attempt {attempt}" in o.detail and "lock held" in o.detail
        (req,) = requests(live.url)
        assert req["handled_at"] is None and req["attempts"] == attempt and "lock held" in req["result"]
    assert push.posts == []
    (o,) = edits_push.undo_poll(notify=NOTIFY, db_url=live.url).outcomes
    assert o.result == "failed"
    (req,) = requests(live.url)
    assert req["handled_at"] is not None and req["attempts"] == 3
    assert edit_rows(live.url)[0]["status"] == "kept"
    assert json.loads(push.posts[0]["body"])["title"] == "Agent edit undo failed: 4. Spawn"
    # Closed: the poll leaves it alone until someone taps again.
    assert edits_push.undo_poll(notify=NOTIFY, db_url=live.url).outcomes == []
    # A phone re-tap reopens it (same as `amem edits undo`) and the next poll reverts.
    assert phone_request(live.url, edit_id) == "queued"
    (req,) = requests(live.url)
    assert req["handled_at"] is None and req["attempts"] == 0 and req["result"] is None and req["source"] == "phone"
    ehome.improve.queue("revert", {"stdout": "ok\n", "exit": 0})
    (o,) = edits_push.undo_poll(notify=NOTIFY, db_url=live.url).outcomes
    assert o.result == "reverted"
    assert edit_rows(live.url)[0]["status"] == "reverted"
    (req,) = requests(live.url)
    assert req["result"] == "reverted" and req["attempts"] == 1


def test_request_undo_states(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    assert phone_request(live.url, uuid.uuid4()) == "missing"
    assert requests(live.url) == []
    edit_id = make_kept(live.url, ehome, ewriter, status="auto-reverted")
    assert phone_request(live.url, edit_id) == "nothing"   # queued, but the poll will find nothing


def test_function_is_not_public_api(live: ScoringDb) -> None:
    with psycopg.connect(live.url) as conn:
        (acl,) = conn.execute("select proacl::text[] from pg_proc where proname = 'amem_request_undo'").fetchone()
    assert acl is not None and not any(a.startswith("=") for a in acl)   # no EXECUTE for PUBLIC


def test_nothing_to_revert_for_an_auto_reverted_edit(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    edit_id = make_kept(live.url, ehome, ewriter, status="auto-reverted")
    phone_request(live.url, edit_id)
    (o,) = edits_push.undo_poll(push=False, db_url=live.url).outcomes
    assert o.result == "nothing" and "status auto-reverted" in o.detail
    assert [c[0] for c in ehome.improve.calls()] == ["apply"]


def test_cli_undo_and_undo_poll(live: ScoringDb, ehome: EHome, ewriter: Any) -> None:  # noqa: F811
    edit_id = make_kept(live.url, ehome, ewriter)
    env = {**live.env, **home_env(ehome)}
    res = run_amem("edits", "undo-poll", "--no-push", env=env)
    assert res.returncode == 0 and "undo-poll: requests=0" in res.stdout
    ehome.improve.queue("revert", {"stdout": "ok\n", "exit": 0})
    res = run_amem("edits", "undo", str(edit_id), "--no-push", env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"reverted edit={edit_id}" in res.stdout
    assert ehome.improve.calls()[-1] == ["revert", "c-0042", "--reason", "manual undo"]
    assert requests(live.url)[0]["source"] == "cli"
    res = run_amem("edits", "undo", str(edit_id), "--no-push", env=env)
    assert res.returncode == 0 and "already reverted" in res.stdout
    res = run_amem("edits", "undo", str(uuid.uuid4()), "--no-push", env=env)
    assert res.returncode == 1 and "no edit" in res.stderr
