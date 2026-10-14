"""Skill usage: log parser, `amem ingest skills`, session matching, scores, cache, suggest."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_memory import skills
from conftest import run_amem

FIXTURE = Path(__file__).parent / "fixtures" / "skill-usage.sample.jsonl"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


# --- parser -------------------------------------------------------------------------


def test_fixture_every_line_accounted_for() -> None:
    result = skills.parse_file(FIXTURE)
    assert result.unparsed == [24, 25, 26, 27]  # not JSON, no skill, by=robot, ts without offset
    assert result.duplicates == 1  # line 23 repeats line 22
    assert len(result.calls) == 21
    assert result.incomplete_tail is False
    assert len({c.source_key for c in result.calls}) == 21


def test_both_timestamp_shapes_and_fields() -> None:
    calls = {c.line_no: c for c in skills.parse_file(FIXTURE).calls}
    old = calls[1]
    assert old.ts == datetime(2026, 4, 6, 19, 34, 40, 533000, tzinfo=timezone.utc)
    assert (old.skill, old.invoked_by, old.agent, old.backfill) == ("release-auditor", "codex", "codex", True)
    new = calls[10]
    assert new.ts == datetime(2026, 10, 7, 15, 36, 30, tzinfo=timezone.utc)
    assert (new.skill, new.invoked_by, new.agent, new.backfill) == ("spark", "me", "claude", False)
    assert new.project == "alpha"  # ~/worktrees/<repo>/<name> -> repo
    assert calls[3].project == "beta"


def test_source_key_is_sha256_of_canonical_json() -> None:
    a = skills.parse_line('{"skill": "x", "t": "2026-10-01T00:00:00Z", "by": "me", "agent": "claude"}', 1)
    b = skills.parse_line('{"agent":"claude","by":"me","skill":"x","t":"2026-10-01T00:00:00Z"}', 2)
    assert a is not None and b is not None and a.source_key == b.source_key
    assert a.cwd is None and a.project == "unknown"


def test_incomplete_tail_skipped_but_whole_json_tail_kept(tmp_path: Path) -> None:
    line = '{"t": "2026-10-01T00:00:00Z", "skill": "x", "by": "me", "agent": "claude"}'
    path = tmp_path / "log.jsonl"
    path.write_text(line + "\n" + line[:-10], encoding="utf-8")
    result = skills.parse_file(path)
    assert (len(result.calls), result.incomplete_tail, result.unparsed) == (1, True, [])
    path.write_text(line + "\n" + line.replace('"x"', '"y"'), encoding="utf-8")
    result = skills.parse_file(path)
    assert [c.skill for c in result.calls] == ["x", "y"] and result.incomplete_tail is False


def test_compute_scores_rule() -> None:
    rows = [
        {"skill": "s", "project": "p", "session_id": "a", "session_project": "q", "category": "worker",
         "outcome": "shipped"},
        {"skill": "s", "project": "p", "session_id": "a", "session_project": "q", "category": "worker",
         "outcome": "shipped"},  # second call in the same session: one session
        {"skill": "s", "project": "p", "session_id": "b", "session_project": "q", "category": "chat",
         "outcome": "failed"},
        {"skill": "s", "project": "p", "session_id": "c", "session_project": "q", "category": "chat",
         "outcome": "active"},  # not final: counted, not scored
        {"skill": "s", "project": "p", "session_id": None, "session_project": None, "category": None,
         "outcome": None},
    ]
    got = {r["bucket"]: r for r in skills.compute_scores(rows)}
    assert set(got) == {"*", "project:q", "project:p", "category:worker", "category:chat"}
    star = got["*"]
    assert (star["n_calls"], star["n_sessions"], star["n_scored"]) == (5, 3, 2)
    assert star["outcomes"] == {"shipped": 1, "failed": 1, "active": 1}
    assert star["score"] == round((1.0 + 0.0 + 0.5 * 2) / (2 + 2), 4)
    assert got["project:p"]["n_sessions"] == 0 and got["project:p"]["score"] == 0.5  # prior only
    assert got["category:worker"]["score"] == round((1.0 + 1.0) / 3, 4)


# --- database -----------------------------------------------------------------------


def _tagged_fixture(tmp_path: Path, tag: str) -> Path:
    """Fixture with every skill prefixed by `tag`, so this test owns its rows."""
    out = []
    for line in FIXTURE.read_text(encoding="utf-8").split("\n"):
        out.append(line.replace('"skill": "', f'"skill": "{tag}-'))
    path = tmp_path / "log.jsonl"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def _calls(pg_url: str, tag: str) -> list[dict]:
    with psycopg.connect(pg_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from skill_calls where skill like %s order by ts, skill", (f"{tag}-%",))
        return cur.fetchall()


def test_ingest_is_idempotent(db_env: dict[str, str], pg_url: str, tmp_path: Path) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"
    path = _tagged_fixture(tmp_path, tag)
    first = run_amem("ingest", "skills", str(path), env=db_env)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "ingested 21 new, 0 already present, 4 unparsed"
    assert "unparsed lines: 24, 25, 26, 27" in first.stderr
    assert "1 line(s) repeat an earlier line" in first.stderr
    second = run_amem("ingest", "skills", str(path), env=db_env)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "ingested 0 new, 21 already present, 4 unparsed"
    rows = _calls(pg_url, tag)
    assert len(rows) == 21
    assert sum(r["invoked_by"] == "me" for r in rows) == 9
    assert sum(r["backfill"] for r in rows) == 9
    assert all(r["session_id"] is None for r in rows)


def test_ingest_missing_file(db_env: dict[str, str], tmp_path: Path) -> None:
    result = run_amem("ingest", "skills", str(tmp_path / "nope.jsonl"), env=db_env)
    assert result.returncode == 1
    assert "no such file" in result.stderr


def _session(conn: psycopg.Connection, sid: str, *, tool: str, cwd: str, start: datetime, end: datetime | None,
             last: datetime | None, outcome: str | None, category: str = "worker", project: str = "proj") -> None:
    conn.execute(
        "insert into sessions (session_id, tool, kind, project_dir, started_at, ended_at, last_activity_at, meta,"
        " category, project, outcome) values (%s, %s, 'chat', 'p', %s, %s, %s, %s, %s, %s, %s)",
        (sid, tool, start, end, last, Jsonb({"cwd": cwd}), category, project, outcome),
    )


def _log(path: Path, lines: list[dict]) -> Path:
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    return path


def test_session_matching(db_env: dict[str, str], pg_url: str, tmp_path: Path) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"
    cwd = f"/Users/demo/worktrees/{tag}/w1"
    t0 = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    with psycopg.connect(pg_url) as conn:
        _session(conn, f"{tag}-a", tool="claude", cwd=cwd, start=t0, end=None,
                 last=t0 + timedelta(hours=1), outcome="shipped")
        _session(conn, f"{tag}-b", tool="claude", cwd=cwd, start=t0 + timedelta(minutes=30),
                 end=t0 + timedelta(minutes=40), last=None, outcome="partial")

    def call(skill: str, at: datetime, agent: str = "claude", where: str = cwd) -> dict:
        return {"t": at.isoformat(), "skill": f"{tag}-{skill}", "by": "me", "agent": agent, "cwd": where}

    path = _log(tmp_path / "log.jsonl", [
        call("inside-a", t0 + timedelta(minutes=10)),
        call("overlap", t0 + timedelta(minutes=35)),  # inside both: the later start (b) wins
        call("slack", t0 + timedelta(hours=1, seconds=30)),  # 30 s after last activity: still a
        call("after", t0 + timedelta(hours=2)),
        call("other-tool", t0 + timedelta(minutes=10), agent="codex"),
        call("other-cwd", t0 + timedelta(minutes=10), where=cwd + "x"),
        call("late-session", t0 + timedelta(days=1, minutes=5)),
    ])
    result = run_amem("ingest", "skills", str(path), env=db_env)
    assert result.returncode == 0, result.stderr
    assert "matched 3 call(s) to a session" in result.stderr
    got = {r["skill"].removeprefix(f"{tag}-"): r["session_id"] for r in _calls(pg_url, tag)}
    assert got == {"inside-a": f"{tag}-a", "overlap": f"{tag}-b", "slack": f"{tag}-a", "after": None,
                   "other-tool": None, "other-cwd": None, "late-session": None}

    # The session is ingested after the skill call: the next refresh links it.
    with psycopg.connect(pg_url) as conn:
        _session(conn, f"{tag}-c", tool="claude", cwd=cwd, start=t0 + timedelta(days=1),
                 end=t0 + timedelta(days=1, minutes=10), last=None, outcome="failed")
    refresh = run_amem("skills", "scores", "--refresh", env=db_env)
    assert refresh.returncode == 0, refresh.stderr
    got = {r["skill"].removeprefix(f"{tag}-"): r["session_id"] for r in _calls(pg_url, tag)}
    assert got["late-session"] == f"{tag}-c"


def test_scores_refresh_and_cache(db_env: dict[str, str], pg_url: str, tmp_path: Path) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"
    cwd = f"/Users/demo/Projects/{tag}"
    t0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    skill = f"{tag}-review"
    with psycopg.connect(pg_url) as conn:
        for i, outcome in enumerate(["shipped", "pushed", "no-change", None]):
            start = t0 + timedelta(hours=i)
            _session(conn, f"{tag}-{i}", tool="codex", cwd=cwd, start=start, end=start + timedelta(minutes=20),
                     last=None, outcome=outcome, project=tag, category="worker" if i < 2 else "chat")
    calls = [{"t": (t0 + timedelta(hours=i, minutes=5)).isoformat(), "skill": skill, "by": "codex",
              "agent": "codex", "cwd": cwd} for i in range(4)]
    calls.append({"t": (t0 + timedelta(days=3)).isoformat(), "skill": skill, "by": "me", "agent": "codex",
                  "cwd": cwd})  # no session
    path = _log(tmp_path / "log.jsonl", calls)
    assert run_amem("ingest", "skills", str(path), env=db_env).returncode == 0

    result = run_amem("skills", "scores", "--refresh", "--skill", skill, env=db_env)
    assert result.returncode == 0, result.stderr
    with psycopg.connect(pg_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from skill_scores where skill = %s", (skill,))
        rows = {r["bucket"]: r for r in cur.fetchall()}
    assert set(rows) == {"*", f"project:{tag}", "category:worker", "category:chat"}
    star = rows["*"]
    assert (star["n_calls"], star["n_sessions"], star["n_scored"]) == (5, 4, 3)
    assert star["outcomes"] == {"shipped": 1, "pushed": 1, "no-change": 1}
    assert star["score"] == round((1.0 + 0.9 + 0.25 + 1.0) / 5, 4)
    assert rows["category:worker"]["score"] == round((1.0 + 0.9 + 1.0) / 4, 4)
    assert f"{skill}\t*\tscore=0.63\tcalls=5\tsessions=4\tscored=3" in result.stdout

    out = tmp_path / "cache" / "skills.json"
    cache = run_amem("skills", "cache", str(out), env=db_env)
    assert cache.returncode == 0, cache.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "generated_at", "refreshed_at", "score_rule", "skills"}
    assert payload["version"] == 1
    assert datetime.fromisoformat(payload["generated_at"]).tzinfo is not None
    assert payload["score_rule"] == {"weights": skills.WEIGHTS, "prior": 0.5, "prior_weight": 2}
    entry = payload["skills"][skill]
    assert set(entry) == {"*", f"project:{tag}", "category:worker", "category:chat"}
    assert entry["*"] == {"score": star["score"], "n_calls": 5, "n_sessions": 4, "n_scored": 3,
                          "outcomes": {"shipped": 1, "pushed": 1, "no-change": 1}}


def test_suggest_threshold(db_env: dict[str, str], tmp_path: Path) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"

    def call(skill: str, days_ago: float, by: str = "me") -> dict:
        at = NOW - timedelta(days=days_ago)
        return {"t": at.isoformat(), "skill": f"{tag}-{skill}", "by": by, "agent": "claude", "cwd": "/x"}

    lines = [call("handoff", d) for d in (0.1, 1, 2, 3)]
    lines += [call("spark", d) for d in (0.2, 1.5)]
    lines += [call("spark", 40), call("spark", 41)]  # outside the 30-day window
    lines += [call("auto", d, by="claude") for d in (0.3, 1, 2, 3, 4)]  # not manual
    assert run_amem("ingest", "skills", str(_log(tmp_path / "log.jsonl", lines)), env=db_env).returncode == 0

    result = run_amem("skills", "suggest", "--days", "30", "--min", "3", env=db_env)
    assert result.returncode == 0, result.stderr
    mine = [l for l in result.stdout.splitlines() if l.startswith(tag)]
    assert mine == [f"{tag}-handoff: 4 manual runs in 30 days"]

    result = run_amem("skills", "suggest", "--days", "60", "--min", "2", env=db_env)
    mine = [l for l in result.stdout.splitlines() if l.startswith(tag)]
    assert mine == [f"{tag}-handoff: 4 manual runs in 60 days", f"{tag}-spark: 4 manual runs in 60 days"]


def test_rls_on_skill_tables(pg_url: str) -> None:
    with psycopg.connect(pg_url) as conn:
        rows = dict(conn.execute(
            "select c.relname, c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'public' and c.relname in ('skill_calls', 'skill_scores')"
        ).fetchall())
    assert rows == {"skill_calls": True, "skill_scores": True}
