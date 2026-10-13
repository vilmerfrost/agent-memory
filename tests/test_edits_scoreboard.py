"""`amem edits scoreboard`: deviations per scanned session before vs after each edit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agent_memory import scoreboard
from conftest import run_amem
from test_edits_support import EHome, ehome, home_env, seed  # noqa: F401
from test_scoring_support import ScoringDb, sdb  # noqa: F401

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
EDITED = NOW - timedelta(days=10)
OLD, NEW = "a" * 64, "b" * 64


@pytest.mark.parametrize("args,want", [
    ((4, 8, 1, 10), "fewer"),       # 0.50 -> 0.10
    ((4, 8, 4, 8), "same"),
    ((4, 8, 5, 10), "same"),        # 0.50 -> 0.50
    ((1, 10, 4, 10), "more"),       # 0.10 -> 0.40
    ((0, 10, 0, 10), "same"),
    ((0, 10, 1, 10), "more"),       # 0 -> 0.10
    ((4, 8, 0, 4), "too early"),    # 4 sessions after < 5
    ((0, 0, 0, 9), "too early"),
])
def test_verdict(args: tuple[int, int, int, int], want: str) -> None:
    assert scoreboard.verdict(*args) == want


def add_edit(url: str, status: str = "kept", created: datetime = EDITED, reverted: datetime | None = None,
             rule: str = "spawn") -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into edits (status, instruction_source, rule_id, rule_title, target_path, "
                     "instruction_sha_before, instruction_sha_after, improve_id, created_at, reverted_at) "
                     "values (%s, 'skill:dispatch', %s, '4. Spawn', '~/.agents/skills/dispatch/SKILL.md', %s, %s, "
                     "'c-1', %s, %s)", (status, rule, OLD, NEW, created, reverted))


def quiet_sessions(url: str, n: int, started: datetime, prefix: str) -> None:
    """Scanned sessions with the source in force and no deviation."""
    with psycopg.connect(url) as conn:
        for i in range(n):
            conn.execute("insert into sessions (session_id, kind, project_dir, started_at, instructions, scanned_at) "
                         "values (%s, 'chat', 'p', %s, '[\"skill:dispatch\"]', now())",
                         (f"{prefix}-{i}", started + timedelta(hours=i)))


def test_scores_before_and_after(sdb: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    add_edit(sdb.url)
    before = EDITED - timedelta(days=5)
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=4, sha=OLD, started=before, prefix="b")
    quiet_sessions(sdb.url, 4, before, "bq")
    after = EDITED + timedelta(days=2)
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=1, sha=NEW, started=after, prefix="a")
    quiet_sessions(sdb.url, 9, after, "aq")
    # A late-scanned session on the old text after the edit does not count as "after".
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=1, sha=OLD, started=after, prefix="late")
    # Other rules, eval sessions and sessions without the source do not count.
    seed(sdb.url, "skill:dispatch", "track", "5. Track", n=2, sha=NEW, started=after, prefix="other")
    with psycopg.connect(sdb.url) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, started_at, instructions, scanned_at) "
                     "values ('ev', 'eval', 'p', %s, '[\"skill:dispatch\"]', now())", (after,))
    (s,) = scoreboard.scores(days=28, now=NOW, db_url=sdb.url)
    assert (s.before_deviations, s.before_sessions) == (4, 8)
    # after sessions: 1 + 9 + 1 late + 2 other-rule sessions (the source was in force) = 13
    assert (s.after_deviations, s.after_sessions) == (1, 13)
    assert s.before_rate == 0.5 and s.after_rate == round(1 / 13, 3) and s.verdict == "fewer"
    lines = scoreboard.text_lines([s], 28)
    assert lines[0] == "edits scoreboard (last 28 days): 1 edit, 1 fewer, 0 same, 0 more, 0 too early"
    assert lines[1].startswith("- skill:dispatch / 4. Spawn [spawn] (kept, ")
    assert "before 0.50 per session (4 in 8) -> after 0.08 per session (1 in 13): fewer" in lines[1]


def test_reverted_edit_stops_at_the_revert_and_unapplied_edits_are_left_out(sdb: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    add_edit(sdb.url, status="reverted", reverted=EDITED + timedelta(days=1))
    add_edit(sdb.url, status="refused", rule="track")
    add_edit(sdb.url, status="kept", created=NOW - timedelta(days=40), rule="old")
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=NEW, started=EDITED + timedelta(days=3))
    (s,) = scoreboard.scores(days=28, now=NOW, db_url=sdb.url)
    assert s.status == "reverted" and s.after_deviations == 0 and s.after_sessions == 0 and s.verdict == "too early"


def test_cooldown_is_shown(sdb: ScoringDb, ehome: EHome, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    add_edit(sdb.url, status="kept", created=NOW - timedelta(days=2))
    add_edit(sdb.url, status="reverted", created=NOW - timedelta(days=2), reverted=NOW - timedelta(days=1),
             rule="track")
    by = {s.rule_id: s for s in scoreboard.scores(days=28, now=NOW, db_url=sdb.url)}
    assert by["spawn"].cooldown_until == "2026-11-04T12:00:00+00:00" and not by["spawn"].cooldown_lifted
    assert by["spawn"].line().endswith(": too early; cooldown until 2026-11-04")
    assert by["track"].cooldown_until is None and "cooldown" not in by["track"].line()   # reverted: none
    head = scoreboard.text_lines(list(by.values()), 28)[0]
    assert head == "edits scoreboard (last 28 days): 2 edits, 0 fewer, 0 same, 0 more, 2 too early, 1 in cooldown"
    (doc,) = scoreboard.as_json([by["spawn"]], 28)["edits"]
    assert doc["cooldown_until"] == "2026-11-04T12:00:00+00:00" and doc["cooldown_lifted"] is False
    monkeypatch.setenv(scoreboard.COOLDOWN_ENV, "1")
    (s, _) = sorted(scoreboard.scores(days=28, now=NOW, db_url=sdb.url), key=lambda s: s.rule_id)
    assert s.rule_id == "spawn" and s.cooldown_until is None


def test_empty_and_cli(sdb: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    assert scoreboard.text_lines([], 28) == ["edits scoreboard (last 28 days): 0 edits, 0 fewer, 0 same, 0 more, "
                                             "0 too early", "No applied edits in this window yet."]
    add_edit(sdb.url, created=datetime.now(UTC) - timedelta(days=1))
    env = {**sdb.env, **home_env(ehome)}
    res = run_amem("edits", "scoreboard", env=env)
    assert res.returncode == 0 and res.stdout.startswith("edits scoreboard (last 28 days): 1 edit")
    res = run_amem("edits", "scoreboard", "--json", "--days", "7", env=env)
    doc = json.loads(res.stdout)
    assert doc["window_days"] == 7 and doc["edits"][0]["verdict"] == "too early"
