"""The outcome check (`amem rsi outcome`, rsi_outcome.py) and the retirement of the morning proposals
(`amem rsi supersede-morning`, rsi_supersede.py). A throwaway Postgres (the rsi_approval fixtures); no model."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import rsi_outcome as O, rsi_supersede as SUP, routines_log as RL
from conftest import base_env, run_amem
from test_rsi_approval import make_artifact, proposal, rdb, sandbox  # noqa: F401  (fixtures)

CEST = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 26, 9, 20, 0, tzinfo=CEST)


# --------------------------------------------------------------------------- the verdict rule


def test_judge_verdicts() -> None:
    assert O.judge(12, 12, 1, 12, 10)[0] == "helped"
    v, p, why, undo = O.judge(1, 10, 8, 10, 8)
    assert (v, undo) == ("worse", True) and p is not None and p <= O.ALPHA and "0.10 -> 0.80" in why
    assert O.judge(0, 10, 4, 10, 8)[0] == "worse"                     # from zero
    assert O.judge(10, 20, 9, 20, 8)[:1] == ("no-change",) and O.judge(10, 20, 9, 20, 8)[3] is False
    assert O.judge(10, 20, 9, 20, 14)[3] is True                       # no effect after the full window: suggest Undo
    assert O.judge(0, 20, 0, 20, 5)[:3] == ("no-change", None, "no deviations of this rule before or after the change")
    assert O.judge(9, 10, 0, 10, 2.9)[0] == "too-early"                # too young
    assert O.judge(9, 10, 0, 4, 9)[0] == "too-early"                   # too few sessions after
    assert O.judge(4, 4, 0, 10, 9)[0] == "too-early"                   # too few before
    # a small drop in a small sample is not "helped"
    assert O.judge(3, 10, 1, 10, 9)[0] == "no-change"


def test_binomial_tail_is_exact() -> None:
    assert O._binom_tail(1, 13, 0.5, upper=False) == pytest.approx(14 / 8192)
    assert O._binom_tail(8, 9, 0.5, upper=True) == pytest.approx(10 / 512)
    assert O._binom_tail(0, 0, 0.5, upper=True) == 1.0


# --------------------------------------------------------------------------- fixtures on the throwaway Postgres


@pytest.fixture
def odb(rdb: str) -> str:  # noqa: F811
    with psycopg.connect(rdb) as conn:
        conn.execute("delete from rsi_outcomes")
    return rdb


def sessions(conn: psycopg.Connection, tag: str, source: str, rule_id: str, *, start: datetime, days: float,
             devs: list[int], env: str | None = None, scanned: bool = True, loads: bool = True) -> None:
    """len(devs) sessions spread over [start, start + days), session i with devs[i] deviations of the rule."""
    n = len(devs)
    for i, k in enumerate(devs):
        at = start + timedelta(days=days * (i + 0.5) / n)
        sid = f"{tag}-{i}"
        conn.execute("insert into sessions (session_id, kind, project_dir, started_at, scanned_at, instructions, env) "
                     "values (%s, 'chat', '/work', %s, %s, %s::jsonb, %s)",
                     (sid, at, at + timedelta(hours=1) if scanned else None,
                      json.dumps([source] if loads else ["agents-md:global"]), env))
        for j in range(k):
            conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, rule_id, "
                         "what, why, how, followed_grade, result_grade) values (%s,%s,%s,%s,%s,'w','y','h',0.2,0.5)",
                         (f"dev:{sid}:{j}", sid, source, rule_id, rule_id))


def edit(conn: psycopg.Connection, source: str, rule_id: str, created: datetime, status: str = "kept",
         **cols: Any) -> str:
    extra = "".join(f", {k}" for k in cols)
    marks = "".join(", %s" for _ in cols)
    return str(conn.execute(f"insert into edits (instruction_source, rule_id, rule_title, status, created_at{extra}) "
                            f"values (%s, %s, %s, %s, %s{marks}) returning id",
                            (source, rule_id, rule_id.title(), status, created, *cols.values())).fetchone()[0])


def promoted(url: str, sandbox: Path, rule_key: str, at: datetime, exp: str, title: str) -> str:  # noqa: F811
    _, d = make_artifact(sandbox, [(f"~/{exp}.md", b"a\n", b"b\n")], exp=exp)
    pid = proposal(url, d, title=title)
    with psycopg.connect(url) as conn:
        cid = conn.execute("insert into rsi_candidates (dedupe_key, source, kind, rule_key) values (%s, 'deviation', "
                           "'edit_instruction', %s) returning id", (f"dev:{rule_key}", rule_key)).fetchone()[0]
        conn.execute("update rsi_proposals set status = 'promoted', promoted_at = %s, candidate_id = %s where id = %s",
                     (at, cid, pid))
    return pid


def seed_fixture(url: str, sandbox: Path) -> dict[str, str]:  # noqa: F811
    """A drop (helped), a rise (worse), too little data, too young; and what must not count."""
    ids: dict[str, str] = {}
    t_a = NOW - timedelta(days=10)
    ids["helped"] = promoted(url, sandbox, "claude-md:global:closing-line", t_a, "exp-20261016-0a0a0a0a",
                             "Closing line: name the skills used")
    with psycopg.connect(url) as conn:
        sessions(conn, "a-before", "claude-md:global", "closing-line", start=t_a - timedelta(days=10), days=10,
                 devs=[1] * 12)
        sessions(conn, "a-after", "claude-md:global", "closing-line", start=t_a, days=10, devs=[1] + [0] * 11)
        # not real sessions, or not scanned yet: their deviations never count
        sessions(conn, "a-sandbox", "claude-md:global", "closing-line", start=t_a, days=10, devs=[5] * 4, env="sandbox")
        sessions(conn, "a-unscanned", "claude-md:global", "closing-line", start=t_a, days=10, devs=[3] * 4,
                 scanned=False)
        t_b = NOW - timedelta(days=8)
        ids["worse"] = edit(conn, "skill:meltdown", "intro", t_b)
        sessions(conn, "b-before", "skill:meltdown", "intro", start=t_b - timedelta(days=8), days=8,
                 devs=[1] + [0] * 9)
        sessions(conn, "b-after", "skill:meltdown", "intro", start=t_b, days=8, devs=[1] * 8 + [0] * 2)
        sessions(conn, "b-noload", "skill:meltdown", "intro", start=t_b, days=8, devs=[0] * 20, loads=False)
        t_c = NOW - timedelta(days=6)
        ids["few"] = edit(conn, "parent-md", "safety-guards", t_c, status="trial")
        sessions(conn, "c-before", "parent-md", "safety-guards", start=t_c - timedelta(days=6), days=6, devs=[1] * 9)
        sessions(conn, "c-after", "parent-md", "safety-guards", start=t_c, days=6, devs=[0, 0])
        ids["young"] = edit(conn, "agent:worker", "rules", NOW - timedelta(days=1))
        # never measured: reverted, an Undo already asked for, refused
        edit(conn, "skill:pepe", "lock", NOW - timedelta(days=9), status="reverted", reverted_at=NOW - timedelta(days=2))
        edit(conn, "skill:pepe", "riff", NOW - timedelta(days=9), undo_requested_at=NOW - timedelta(days=1))
        edit(conn, "skill:pepe", "pitch", NOW - timedelta(days=9), status="refused")
    return ids


def test_outcome_check_finds_the_drop_the_rise_and_too_little_data(odb: str, sandbox: Path,  # noqa: F811
                                                                  tmp_path: Path) -> None:
    ids = seed_fixture(odb, sandbox)
    log = tmp_path / "routines.log"
    rep, lines = O.run_job(db_url=odb, now=NOW, out_dir=tmp_path / "reports", routines_log=log)
    by_id = {o.change.id: o for o in rep.outcomes}
    assert set(by_id) == set(ids.values())
    a, b, c, d = (by_id[ids[k]] for k in ("helped", "worse", "few", "young"))
    assert (a.verdict, a.sessions_before, a.deviations_before, a.sessions_after, a.deviations_after) == \
           ("helped", 12, 12, 12, 1) and not a.suggest_undo and a.change.kind == "proposal"
    assert (b.verdict, b.sessions_before, b.deviations_before, b.sessions_after, b.deviations_after) == \
           ("worse", 10, 1, 10, 8) and b.suggest_undo and b.change.kind == "edit"
    assert (c.verdict, c.sessions_after) == ("too-early", 2) and "needs 5 on each side" in c.reason
    assert d.verdict == "too-early" and "applied 1.0 days ago" in d.reason
    assert rep.counts() == {"helped": 1, "no-change": 0, "worse": 1, "too-early": 2, "mixed": 0} and rep.suggest_undo == 1
    # the database rows Night Desk reads
    with psycopg.connect(odb) as conn:
        got = {r[0]: r[1:] for r in conn.execute(
            "select change_id, change_kind, verdict, suggest_undo, undo_hint, rule_key from rsi_outcomes").fetchall()}
        rls = conn.execute("select relrowsecurity from pg_class where relname = 'rsi_outcomes'").fetchone()[0]
    assert rls is True
    assert got[ids["helped"]] == ("proposal", "helped", False, None, "claude-md:global:closing-line")
    assert got[ids["worse"]][:3] == ("edit", "worse", True)
    assert got[ids["worse"]][3] == f"Night Desk: Undo on this edit (or `amem edits undo {ids['worse']}` in your terminal)"
    # the report and the routines.log line
    md = (tmp_path / "reports" / "rsi-outcome-2026-10-26.md").read_text()
    assert md.index("## Worse: Intro") < md.index("## Helped: Closing line: name the skills used")
    assert "- Suggested: Undo." in md and "Nothing is undone automatically." in md
    data = json.loads((tmp_path / "reports" / "rsi-outcome-2026-10-26.json").read_text())
    assert data["format"] == "rsi-outcome/1" and data["counts"]["worse"] == 1 and len(data["outcomes"]) == 4
    (run,) = RL.parse_file(log).runs
    assert (run.routine, run.status) == ("amem-rsi-outcome", "pass") and lines[0].startswith(
        "2026-10-26T09:20:00+0200 amem-rsi-outcome PASS changes=4 helped=1 no_change=0 worse=1 too_early=2 mixed=0 "
        "suggest_undo=1 ")
    assert run.report_path == str(tmp_path / "reports" / "rsi-outcome-2026-10-26.md")
    # a second run rewrites the same rows (latest verdict), keeping first_checked_at
    O.run_job(db_url=odb, now=NOW + timedelta(days=1), out_dir=tmp_path / "reports", routines_log=log)
    with psycopg.connect(odb) as conn:
        n, first, checked = conn.execute("select count(*), min(first_checked_at), max(checked_at) from rsi_outcomes"
                                         ).fetchone()
    assert n == 4 and first < checked


def test_a_rule_changed_by_hand_inside_the_windows_is_mixed(odb: str, sandbox: Path, tmp_path: Path,  # noqa: F811
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    ids = seed_fixture(odb, sandbox)
    monkeypatch.setattr(O, "source_paths", lambda conn, source: [f"~/{source}.md"])
    seen: list[tuple[str, str]] = []

    def other(paths: list[str], rule_id: str, t: datetime, w: timedelta) -> int:
        seen.append((paths[0], rule_id))
        return 1 if rule_id in ("closing-line", "intro") else 0
    monkeypatch.setattr(O, "other_changes", other)
    rep = O.check(db_url=odb, now=NOW)
    by_id = {o.change.id: o for o in rep.outcomes}
    a, b = by_id[ids["helped"]], by_id[ids["worse"]]
    assert (a.verdict, a.other_changes, a.suggest_undo) == ("mixed", 1, False) and "was helped" in a.reason
    assert (b.verdict, b.suggest_undo) == ("mixed", False) and "was worse" in b.reason   # no Undo advice on mixed
    assert by_id[ids["few"]].verdict == "too-early" and ("~/parent-md.md", "safety-guards") not in seen
    assert rep.counts()["mixed"] == 2 and rep.suggest_undo == 0
    with psycopg.connect(odb) as conn:   # the check constraint accepts the new verdict
        assert conn.execute("select verdict, (meta->>'other_changes')::int from rsi_outcomes where change_id = %s",
                            (ids["helped"],)).fetchone() == ("mixed", 1)
    assert "## Mixed (rule also changed by hand):" in O.render_markdown(rep)


def test_other_changes_skips_the_change_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import rule_versions
    t = NOW - timedelta(days=5)
    monkeypatch.setattr(rule_versions, "changes", lambda path, rid: [t + timedelta(minutes=2), t + timedelta(days=2),
                                                                      t - timedelta(days=9)])
    assert O.other_changes(["~/a.md"], "rules", t, timedelta(days=5)) == 1     # only the edit 2 days after
    assert O.other_changes(["~/a.md"], "rules", t, timedelta(days=10)) == 2


def test_outcome_job_logs_a_fail_line_when_the_database_is_gone(tmp_path: Path) -> None:
    log = tmp_path / "routines.log"
    rep, lines = O.run_job(db_url="postgresql://postgres:postgres@127.0.0.1:1/x", now=NOW,
                           out_dir=tmp_path / "reports", routines_log=log)
    assert rep.status == "FAIL" and lines[0].split()[2] == "FAIL" and lines[1].startswith(
        "2026-10-26T09:20:00+0200 amem-rsi-outcome-fail database: OperationalError")
    assert (tmp_path / "reports" / "rsi-outcome-2026-10-26.md").is_file()


def test_cli_outcome_dry_run_writes_nothing(odb: str, sandbox: Path, tmp_path: Path) -> None:  # noqa: F811
    seed_fixture(odb, sandbox)
    r = run_amem("rsi", "outcome", "--dry-run", "--json", "--now", NOW.isoformat(), env=base_env(AMEM_DB_URL=odb))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["counts"] == {"helped": 1, "no-change": 0, "worse": 1, "too-early": 2, "mixed": 0}
    with psycopg.connect(odb) as conn:
        assert conn.execute("select count(*) from rsi_outcomes").fetchone() == (0,)
    refused = run_amem("rsi", "outcome", env=base_env(AMEM_DB_URL=odb, AMEM_REQUIRE_REMOTE="1",
                                                       AMEM_ROUTINES_LOG=str(tmp_path / "r.log")))
    assert refused.returncode != 0 and not (tmp_path / "r.log").exists()          # a local DB is refused


# --------------------------------------------------------------------------- supersede the morning proposals


def test_supersede_morning_marks_only_the_morning_steps_waiting_proposals(rdb: str, sandbox: Path) -> None:  # noqa: F811
    _, d1 = make_artifact(sandbox, [("~/m1.md", b"a\n", b"b\n")], exp="exp-20261024-edit-0badc0de")
    morning = proposal(rdb, d1, title="Edit 4. Spawn")
    _, d2 = make_artifact(sandbox, [("~/m2.md", b"a\n", b"c\n")], exp="exp-20261025-manual")
    linked = proposal(rdb, d2, title="Edit Shell habits")                            # named by its edits row
    _, d3 = make_artifact(sandbox, [("~/n.md", b"a\n", b"d\n")], exp="exp-20261025-0123abcd")
    night = proposal(rdb, d3, title="Night: closing line")
    _, d4 = make_artifact(sandbox, [("~/r.md", b"a\n", b"e\n")], exp="exp-20261023-edit-feedf00d")
    rejected = proposal(rdb, d4, title="Edit rejected one")
    with psycopg.connect(rdb) as conn:
        conn.execute("insert into edits (instruction_source, rule_id, status, meta) values ('claude-md:global', "
                     "'shell-habits', 'staged', %s::jsonb)", (json.dumps({"mode": "stage", "proposal_id": linked}),))
        conn.execute("update rsi_proposals set status = 'rejected' where id = %s", (rejected,))
    dry = SUP.supersede_morning(db_url=rdb, dry_run=True)
    assert sorted(r["id"] for r in dry) == sorted([morning, linked])
    assert status(rdb) == {morning: "staged", linked: "staged", night: "staged", rejected: "rejected"}
    done = SUP.supersede_morning(db_url=rdb)
    assert sorted(r["id"] for r in done) == sorted([morning, linked])
    assert status(rdb) == {morning: "superseded", linked: "superseded", night: "staged", rejected: "rejected"}
    with psycopg.connect(rdb) as conn:
        result, meta, n = conn.execute("select result, meta, (select count(*) from rsi_proposals) from rsi_proposals "
                                       "where id = %s", (morning,)).fetchone()
        assert result.startswith("superseded: the morning edits step no longer runs") and n == 4   # nothing deleted
        assert meta["superseded"]["by"] == "amem rsi supersede-morning"
        h = conn.execute("select artifact_hash from rsi_proposals where id = %s", (morning,)).fetchone()[0]
        got = conn.execute("select amem_rsi_record_cli_decision(%s, 'apply', %s, null)", (morning, h)).fetchone()[0]
    assert got == "not-staged"                                                       # an old button does nothing
    assert SUP.supersede_morning(db_url=rdb) == []                                   # idempotent


def test_cli_supersede_morning(rdb: str, sandbox: Path) -> None:  # noqa: F811
    _, d1 = make_artifact(sandbox, [("~/m1.md", b"a\n", b"b\n")], exp="exp-20261024-edit-0badc0de")
    morning = proposal(rdb, d1, title="Edit 4. Spawn")
    r = run_amem("rsi", "supersede-morning", "--dry-run", "--json", env=base_env(AMEM_DB_URL=rdb))
    assert r.returncode == 0, r.stderr
    assert [p["id"] for p in json.loads(r.stdout)["proposals"]] == [morning] and status(rdb)[morning] == "staged"
    r = run_amem("rsi", "supersede-morning", env=base_env(AMEM_DB_URL=rdb))
    assert r.returncode == 0 and r.stdout.startswith("rsi supersede-morning: superseded 1 morning proposal(s)")
    assert status(rdb)[morning] == "superseded"


def status(url: str) -> dict[str, str]:
    with psycopg.connect(url) as conn:
        return dict(conn.execute("select id::text, status from rsi_proposals").fetchall())
