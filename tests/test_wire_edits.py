"""Cost-aware edit order and the scoreboard's session outcomes (e-wire, items 1 and 3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from agent_memory import edits, scoreboard
from test_edits_support import EHome, ehome, seed, sha_of  # noqa: F401
from test_scoring_support import ScoringDb, sdb  # noqa: F401

# --------------------------------------------------------------------------- 3. order


def cand(name: str, conf: float | None, score: float) -> dict[str, Any]:
    return {"instruction_source": name, "jev": {"confidence_mean": conf},
            "cost": {"sessions": 1, "tokens": int(score * 1_000_000), "active_seconds": 0.0, "score": score}}


def test_order_is_confidence_tier_then_cost() -> None:
    cands = [cand("a", 0.71, 1.0), cand("b", 0.79, 9.0), cand("c", 0.80, 0.5), cand("d", None, 99.0),
             cand("e", 0.70, 9.0)]
    assert [c["instruction_source"] for c in edits.ordered(cands)] == ["c", "b", "e", "a", "d"]
    assert edits.confidence_tier(cand("x", 0.7, 0)) == 0.7 and edits.confidence_tier(cand("x", 0.69999, 0)) == 0.6
    assert edits.cost_text(cand("x", 0.8, 2.5)) == "2.5M tok/0s"
    assert edits.cost_text({"jev": {}}) is None


def _spend(url: str, prefix: str, tokens: int, active: float) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("update sessions set tokens_input = %s, tokens_output = 0, tokens_cache_read = 0, "
                     "tokens_cache_creation = 0, active_seconds = %s where session_id like %s",
                     (tokens, active, f"{prefix}-%"))


def test_run_edits_the_costly_rule_first_and_shows_cost(sdb: ScoringDb, ehome: EHome) -> None:  # noqa: F811
    # spawn recurs more (3 sessions) but shell-habits cost more: same tier, so cost decides.
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=3, sha=sha_of(ehome.skill), prefix="sp")
    seed(sdb.url, "claude-md:global", "shell-habits", "Shell Habits", n=2, sha=sha_of(ehome.global_md), prefix="sh")
    _spend(sdb.url, "sp", 1_000_000, 60.0)
    _spend(sdb.url, "sh", 20_000_000, 600.0)
    r = edits.run(dry_run=True, limit=1, db_url=sdb.url)
    (o,) = r.outcomes
    assert o.status == "plan-edit" and o.source == "claude-md:global" and r.deferred == 1
    assert o.line().startswith("plan edit claude-md:global shell-habits -> ")
    assert " cost=40.0M tok/20m00s: " in o.line()


# --------------------------------------------------------------------------- 1. scoreboard outcomes

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
EDITED = NOW - timedelta(days=10)


def sessions(url: str, prefix: str, started: datetime, outcomes: list[str | None], tokens: int = 1000,
             active: float = 60.0) -> None:
    with psycopg.connect(url) as conn:
        for i, outcome in enumerate(outcomes):
            conn.execute("insert into sessions (session_id, kind, project_dir, started_at, instructions, scanned_at, "
                         "outcome, tokens_input, tokens_output, tokens_cache_read, tokens_cache_creation, "
                         "active_seconds) values (%s, 'chat', 'p', %s, '[\"skill:dispatch\"]', now(), %s, %s, 0, 0, 0,"
                         " %s)", (f"{prefix}-{i}", started + timedelta(hours=i), outcome, tokens, active))


def add_edit(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into edits (status, instruction_source, rule_id, rule_title, target_path, "
                     "instruction_sha_before, improve_id, created_at) values ('kept', 'skill:dispatch', 'spawn', "
                     "'4. Spawn', '~/.agents/skills/dispatch/SKILL.md', %s, 'c-1', %s)", ("a" * 64, EDITED))


def test_outcome_stats_and_verdicts() -> None:
    st = scoreboard.outcome_stats([("shipped", 60.0, 100), ("failed", 120.0, 300), ("active", None, None),
                                   (None, 30.0, 200), ("no-change", 90.0, None)])
    assert (st.sessions, st.known, st.good, st.bad) == (5, 3, 1, 1)
    assert (st.good_rate, st.bad_rate, st.median_tokens, st.median_active_seconds) == (0.333, 0.333, 200, 75.0)
    good = scoreboard.OutcomeStats(known=5, good_rate=0.6, bad_rate=0.2)
    assert scoreboard.outcome_verdict(good, scoreboard.OutcomeStats(known=5, good_rate=0.4, bad_rate=0.2)) == "worse"
    assert scoreboard.outcome_verdict(good, scoreboard.OutcomeStats(known=5, good_rate=0.6, bad_rate=0.4)) == "worse"
    assert scoreboard.outcome_verdict(good, scoreboard.OutcomeStats(known=5, good_rate=0.8, bad_rate=0.2)) == "better"
    assert scoreboard.outcome_verdict(good, scoreboard.OutcomeStats(known=5, good_rate=0.65, bad_rate=0.2)) == "same"
    assert scoreboard.outcome_verdict(good, scoreboard.OutcomeStats(known=4, good_rate=0.0, bad_rate=1.0)) == "too early"
    assert [scoreboard.overall(v, o) for v, o in (
        ("fewer", "same"), ("fewer", "too early"), ("fewer", "worse"), ("same", "worse"), ("same", "better"),
        ("more", "better"), ("too early", "better"))] == ["better", "better", "mixed", "worse", "same", "worse",
                                                          "too early"]


@pytest.mark.parametrize(("after", "want"), [
    (["shipped"] * 4 + ["committed"] * 2 + ["failed"], "better"),       # fewer deviations, outcomes held
    (["abandoned"] * 4 + ["failed"] * 3, "mixed"),                     # fewer deviations, outcomes worse
])
def test_scoreboard_puts_outcomes_next_to_deviations(sdb: ScoringDb, ehome: EHome,  # noqa: F811
                                                     after: list[str], want: str) -> None:
    add_edit(sdb.url)
    before_at = EDITED - timedelta(days=5)
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", n=4, sha="a" * 64, started=before_at, prefix="bd")
    sessions(sdb.url, "bq", before_at, ["shipped", "shipped", "committed", "pushed", "failed", "active"],
             tokens=2_000_000, active=600.0)
    sessions(sdb.url, "aq", EDITED + timedelta(days=1), after, tokens=1_000_000, active=300.0)
    (s,) = scoreboard.scores(days=28, now=NOW, db_url=sdb.url)
    assert s.verdict == "fewer" and s.overall == want
    # before: 4 seeded (no outcome) + 6; known = 5 (active and null left out)
    assert (s.before_outcomes.sessions, s.before_outcomes.known, s.before_outcomes.good_rate) == (10, 5, 0.8)
    assert s.after_outcomes.sessions == 7 and s.after_outcomes.median_tokens == 1_000_000
    lines = scoreboard.text_lines([s], 28)
    assert lines[2].startswith("  outcomes: before good 0.80 bad 0.20 (5 of 10), median 2.0M tok 10m00s -> after ")
    assert lines[2].endswith(f"; overall {want}")
    doc = scoreboard.as_json([s], 28)["edits"][0]
    assert doc["overall"] == want and doc["after_outcomes"]["known"] == 7
