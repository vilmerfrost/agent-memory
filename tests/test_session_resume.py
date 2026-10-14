"""A resumed Claude Code session (docs/sessions-live.md, "Resumed sessions"): its new transcript
starts with a copy of the earlier session's lines, which keep the earlier `sessionId`, and its
subagent files are copied whole. Only the session's own lines count for tokens and turns, in
`sessions ingest` and in the live Stop ping. Synthetic transcripts only."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent_memory import session_live, session_tokens, sessions_ingest
from test_deviations_support import NOW, T0, Home, busy, setup_home, user, write_transcript  # noqa: F401
from test_session_live import _row, db, payload  # noqa: F401
from test_session_tokens import EXPECTED, reply, session_events, usage

OLD = "0bd0a1d0-0000-4000-8000-0000000000aa"
NEW = "d3ad0e11-0000-4000-8000-0000000000bb"
OWN = session_tokens.Tokens(input=7 + 1, output=70 + 9, cache_read=700 + 0, cache_creation=7 + 2)


def tagged(events: list[dict[str, Any]], sid: str) -> list[dict[str, Any]]:
    return [{**e, "sessionId": sid} for e in events]


def own_events(cwd: str) -> list[dict[str, Any]]:
    """The resumed session's own lines: two typed prompts, two API responses (one written as two lines)."""
    return tagged([
        user("Carry on with the tests.", 20, cwd=cwd),
        reply("own_1", usage(7, 70, 700, 7), minute=21, cwd=cwd),
        reply("own_1", usage(7, 70, 700, 7), minute=21, cwd=cwd),
        user("And the docs.", 22, cwd=cwd),
        reply("own_2", usage(1, 9, 0, 2), minute=23, cwd=cwd),
    ], NEW)


def resumed_events(cwd: str) -> list[dict[str, Any]]:
    """The copied prefix (the earlier session's lines, same message ids, the OLD sessionId) + own lines."""
    return tagged(session_events(cwd), OLD) + own_events(cwd)


def write_subagents(proj: Any, cwd: str) -> None:
    # The earlier session's subagent, and its whole copy under the resumed session.
    old_sub = tagged([user("## Task\nOld.", 0, cwd=cwd), reply("w_old", usage(100, 200, 300, 400), cwd=cwd)], OLD)
    write_transcript(proj / OLD / "subagents" / "agent-a1.jsonl", old_sub, pad=False)
    write_transcript(proj / NEW / "subagents" / "agent-a1.jsonl", old_sub, pad=False)
    # The resumed session's own subagent: its lines carry the parent's (NEW) id.
    own_sub = tagged([user("## Task\nNew.", 21, cwd=cwd), reply("w_new", usage(1, 2, 3, 4), minute=21, cwd=cwd)], NEW)
    write_transcript(proj / NEW / "subagents" / "agent-a2.jsonl", own_sub, pad=False)


def test_scanner_counts_only_own_lines() -> None:
    events = resumed_events("/work/demo-repo")
    s = session_tokens.claude_summary(events, own_session=NEW)
    assert s.tokens == OWN and s.turns == 2 and s.calls == 2
    assert s.first_at == T0 + timedelta(minutes=20)
    # Without an own session id every line counts (the old behaviour; the cloud digest parity).
    assert session_tokens.claude_summary(events).tokens.output == EXPECTED.output + OWN.output
    # Lines without a sessionId always count.
    assert session_tokens.claude_summary(own_events("/w") + session_events("/w"), own_session=NEW).tokens.output \
        == OWN.output + EXPECTED.output


def test_copied_title_is_a_fallback_only() -> None:
    def title(t: str, sid: str) -> dict[str, Any]:
        return {"type": "custom-title", "customTitle": t, "sessionId": sid}

    assert session_tokens.claude_summary([title("Earlier", OLD)], own_session=NEW).title == "Earlier"
    assert session_tokens.claude_summary([title("Earlier", OLD), title("Mine", NEW)], own_session=NEW).title == "Mine"


def test_ingest_counts_only_own_lines(setup_home: Home) -> None:  # noqa: F811
    proj = setup_home.project("-work-demo-repo")
    cwd = str(setup_home.repo)
    write_transcript(proj / f"{OLD}.jsonl", tagged(session_events(cwd) + busy(cwd), OLD))
    write_transcript(proj / f"{NEW}.jsonl", resumed_events(cwd) + tagged(busy(cwd), NEW))
    write_subagents(proj, cwd)
    rows = {r.session_id: r for r in sessions_ingest.collect(setup_home.projects, days=7, now=NOW)[0]}
    old, new = rows[OLD], rows[NEW]
    assert old.tokens is not None and old.tokens.tokens == EXPECTED and old.tokens.turns == 2
    assert new.tokens is not None and new.tokens.tokens == OWN and new.tokens.turns == 2
    # Subagents: the copied file is a pure copy (no row, adds nothing); the original keeps its tokens.
    assert f"{NEW}/agent-a1" not in rows
    assert rows[f"{NEW}/agent-a2"].tokens.tokens.as_list() == [1, 2, 3, 4]  # type: ignore[union-attr]
    assert new.meta["subagent_tokens"] == {"input": 1, "output": 2, "cache_read": 3, "cache_creation": 4,
                                           "sessions": 1}
    assert old.meta["subagent_tokens"] == {"input": 100, "output": 200, "cache_read": 300, "cache_creation": 400,
                                           "sessions": 1}


def test_stop_ping_counts_only_own_lines(setup_home: Home, db: str) -> None:  # noqa: F811
    proj = setup_home.project("-work-demo-repo")
    cwd = str(setup_home.repo)
    write_transcript(proj / f"{NEW}.jsonl", resumed_events(cwd), pad=False)
    write_subagents(proj, cwd)
    assert session_live.ping(payload("Stop", setup_home, sid=NEW), now=NOW + timedelta(minutes=30),
                             db_url=db) == "written"
    row = _row(db, NEW)
    assert row is not None
    assert [row["tokens_input"], row["tokens_output"], row["tokens_cache_read"],
            row["tokens_cache_creation"]] == OWN.as_list()
    assert row["turns"] == 2 and set(row["models"]) == {"claude-test-1"}
    assert row["started_at"] == T0 + timedelta(minutes=20)  # the session's own first line
    # The copied file is a pure copy: not one of this session's subagents (as in `sessions ingest`).
    assert row["meta"]["subagent_tokens"] == {"input": 1, "output": 2, "cache_read": 3, "cache_creation": 4,
                                              "sessions": 1}
