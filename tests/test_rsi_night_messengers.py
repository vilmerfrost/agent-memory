"""The night never turns a messenger routine's failure into a candidate (rsi_night.MESSENGER_ROUTINES).

routine-audit and config-check only report other routines' failures (Night Desk src/plain.ts MESSENGERS, decision
24). Their FAIL lines make no `routine:<messenger>` sighting: the night's routine_failures coverage counts them as
skipped (`messenger`), and an earlier night's open messenger candidate stays in rsi_candidates but is not picked.

Disposable Postgres (conftest pg_url), temp HOME and RSI_SANDBOX_DIR. The runs rows come from real-shaped
routines.log lines through the real parser and ingest (routines_log.parse_lines, store.ingest_runs); the night uses
the REAL delta (rsi_evidence_pack.delta_since) with no providers, and its fixture/stub stages. No network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import night_coverage as NC, rsi_approval as AP, rsi_brief as BR, rsi_evidence_pack as EP
from agent_memory import rsi_night as N, routines_log as RL, store
from rsi_night_support import adapters, candidate, clean_db, make_home, strip_agent_env

NIGHT = "2026-10-26"
ROUTINES = ("amem-daily", "config-check", "routine-audit", "routine-auditor", "rsi-skills")

# Real-shaped lines: routine-audit's and config-check's live FAIL lines, the real 2026-10-17 amem-daily FAIL with its
# detail line, and the -fail detail lines routine-audit writes (one per problem it found in other routines).
REAL_LOG = """\
2026-10-26T00:40:04+0100 amem-daily FAIL scored=16 escalations=14 new=0 push=none tasks=10 deviations=68 deviation_errors=1 707s
2026-10-26T00:40:04+0100 amem-daily-fail deviations: 1 error; first: session b1b2011c/agent-a079: writer output: deviation 0: followed_grade must be a number in 0..1, got None
2026-10-26T06:00:02+0100 config-check FAIL pass=55 fail=2
2026-10-26T06:00:02+0100 config-check-fail late: rsi-skills last ran 27 h ago
2026-10-26T09:10:04+0100 routine-audit FAIL problems=5
2026-10-26T09:10:04+0100 routine-audit-fail amem-daily: FAIL at 00:40
2026-10-26T09:10:04+0100 routine-audit-fail amem-rsi-night: no line for night 2026-10-25
"""


class DeltaEvidence(N.PackEvidence):
    """The real delta (settle off), no provider stand-ins."""

    def __init__(self, db_url: str) -> None:
        super().__init__(db_url=db_url, retrievers={})

    def delta(self, since: str | None, *, conn: Any) -> Any:
        return EP.delta_since(since, conn=conn, limits=EP.DeltaLimits(settle_s=0), retrievers={})


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    monkeypatch.delenv("AMEM_COVERAGE_WRITE", raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    for r in ROUTINES:          # every routine has a prompt file in the allowlist (~/.claude/scheduled-tasks/)
        (home / ".claude" / "scheduled-tasks" / r).mkdir(parents=True)
        (home / ".claude" / "scheduled-tasks" / r / "SKILL.md").write_text(f"# {r}\n\n## Steps\n\n- Run {r}.\n")
    clean_db(pg_url)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("delete from coverage_snapshots")
        conn.execute("delete from routine_consumption")
    return SimpleNamespace(url=pg_url, home=home, tmp=tmp_path)


def ingest(env: SimpleNamespace, text: str) -> int:
    parsed = RL.parse_lines(text.splitlines())
    assert not parsed.unparsed, parsed.unparsed
    new, _ = store.ingest_runs(parsed.runs, db_url=env.url)
    return new


def open_candidate(env: SimpleNamespace, routine: str, status: str = "new") -> None:
    key = f"routine:{routine}"
    with psycopg.connect(env.url, autocommit=True) as conn:
        AP.upsert_candidate(conn, dedupe_key=key, source="routine_failures", kind="fix_routine",
                            target=f"~/.claude/scheduled-tasks/{routine}/SKILL.md", rule_key=key,
                            evidence_refs=["amem:run/00000000-0000-4000-8000-000000000001"],
                            metrics={"failures": 2, "count": 2, "sessions": 2})
        conn.execute("update rsi_candidates set status = %s where dedupe_key = %s", (status, key))


def night(env: SimpleNamespace) -> dict[str, Any]:
    rep = N.run_night(night=NIGHT, db_url=env.url, adapters=adapters(evidence=DeltaEvidence(env.url)))
    assert N.validate_report(rep) == [], N.validate_report(rep)
    assert BR.validate_report(rep) == [], BR.validate_report(rep)
    return rep


def routine_use(env: SimpleNamespace) -> dict[str, Any]:
    with psycopg.connect(env.url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from routine_consumption where job = %s and run_key = %s and dataset = %s",
                    (NC.JOB, NIGHT, NC.ROUTINE_FAILURES))
        row = cur.fetchone()
    assert row is not None
    return row


def routine_keys(env: SimpleNamespace) -> set[str]:
    with psycopg.connect(env.url) as conn:
        return {r[0] for r in conn.execute("select dedupe_key from rsi_candidates where dedupe_key like 'routine:%'")}


def considered(rep: dict[str, Any]) -> set[str]:
    return {c["key"] for c in rep["candidates"]}


def test_the_constant_names_both_messengers() -> None:
    assert N.MESSENGER_ROUTINES == ("routine-audit", "config-check")
    assert N.MESSENGER_KEYS == ("routine:routine-audit", "routine:config-check")


def test_sighting_code_alone_gives_no_messenger_sighting() -> None:
    # Built like tests/test_rsi_night_skill_writer.py's helper (no database). Night.__init__ always sets `notes`
    # before any stage runs, so a real night has it; a bare __new__ Night gets it here the same way.
    night = N.Night.__new__(N.Night)
    night.cfg, night.allowlist, night.notes = N.load_config(), [], []
    fails = [{"routine": r, "run_id": f"r{i}", "ref": f"amem:run/r{i}"}
             for i, r in enumerate(("config-check", "routine-audit", "amem-daily"))]
    (s,) = night._routine_sightings(SimpleNamespace(routine_failures=fails))
    assert s.rule_key == "routine:amem-daily"
    assert night._cov().uses[NC.ROUTINE_FAILURES].skipped == {"messenger": 2}
    assert night.notes == ["candidates: 2 failure(s) of messenger routines not sighted (routine-audit 1, config-check 1)"
                           ": they only report other routines' failures"]


def test_messenger_failures_are_skipped_and_counted_not_sighted(env: SimpleNamespace) -> None:
    assert ingest(env, REAL_LOG) == 3
    rep = night(env)
    assert routine_keys(env) == {"routine:amem-daily"}                        # the only routine sighting
    assert [p["key"] for p in rep["patterns"] if p["key"].startswith("routine:")] == ["routine:amem-daily"]
    assert rep["delta"]["routine_failures"] == 3                              # the delta still reads all three
    use = routine_use(env)
    assert (use["queried"], use["considered"], use["used"], use["findings"]) == (3, 3, 1, 1)
    assert use["skipped"] == {"messenger": 2} and use["status"] == "findings"
    assert any(n.startswith("candidates: 2 failure(s) of messenger routines not sighted (routine-audit 1, "
                            "config-check 1)") for n in rep["notes"]), rep["notes"]
    assert "routine:amem-daily" in considered(rep)


def test_an_earlier_messenger_candidate_is_not_picked_and_the_report_says_why(env: SimpleNamespace) -> None:
    open_candidate(env, "config-check", "new")
    open_candidate(env, "routine-audit", "deferred")
    open_candidate(env, "rsi-skills", "deferred")                            # a normal routine's open candidate
    before = {k: candidate(env.url, k) for k in ("routine:config-check", "routine:routine-audit")}
    rep = night(env)
    keys = considered(rep)
    assert "routine:rsi-skills" in keys
    assert not keys & {"routine:config-check", "routine:routine-audit"}
    assert ("candidates: routine:config-check (new), routine:routine-audit (deferred) not considered: a messenger "
            "routine only reports other routines' failures") in rep["notes"], rep["notes"]
    for k, row in before.items():                                             # open, untouched: not deleted
        after = candidate(env.url, k)
        assert {c: after[c] for c in ("status", "rank", "decided_reason", "last_considered_at", "seen_count")} == \
               {c: row[c] for c in ("status", "rank", "decided_reason", "last_considered_at", "seen_count")}


def test_a_night_with_only_messenger_failures_reads_them_and_sights_nothing(env: SimpleNamespace) -> None:
    # config-check ERROR (a status word routines.log maps to error) and routine-audit FAIL with its detail lines
    ingest(env, "2026-10-26T06:00:02+0100 config-check ERROR rc=1 timeout after 300s\n"
                "2026-10-26T09:10:04+0100 routine-audit FAIL problems=1\n"
                "2026-10-26T09:10:04+0100 routine-audit-fail config-check: ERROR at 06:00\n")
    rep = night(env)
    assert routine_keys(env) == set()
    use = routine_use(env)
    assert (use["queried"], use["considered"], use["used"], use["findings"]) == (2, 2, 0, 0)
    assert use["skipped"] == {"messenger": 2} and use["status"] == "read_excluded"
    assert not [k for k in considered(rep) if k.startswith("routine:")]


def test_a_routine_named_like_a_messenger_is_still_sighted(env: SimpleNamespace) -> None:
    ingest(env, "2026-10-26T09:12:00+0100 routine-auditor FAIL problems=2\n"
                "2026-10-26T09:10:04+0100 routine-audit FAIL problems=1\n")
    rep = night(env)
    assert routine_keys(env) == {"routine:routine-auditor"}
    assert routine_use(env)["skipped"] == {"messenger": 1}
    assert "routine:routine-auditor" in considered(rep)
