"""`amem db dedupe-runs`: merge rows stored twice under old (raw-line) source_keys.

Each test gets a private database (`sdb`), seeded the way the old code stored
data: every line its own row, keyed by sha256 of the raw line.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from agent_memory import cli_cloud, routines_log, store
from agent_memory.routines_log import legacy_source_key, parse_lines, source_key
from conftest import base_env, run_amem
from test_dedupe_keys import routine_sync
from test_scoring_support import ScoringDb, sdb  # noqa: F401

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _seed(url: str, lines: list[str], *, published: bool = False, occurrence: int = 1) -> uuid.UUID:
    """Store one run the old way: parsed from `lines` (summary + its details), old raw-line key."""
    text = "\n".join(lines) + "\n"
    parsed = cli_cloud.parse_published(text) if published else parse_lines(text.splitlines())
    (run,) = parsed.runs
    run.source_key = legacy_source_key(run.routine, lines[0], occurrence)
    store.ingest_runs([run], db_url=url)  # own transaction: created_at grows per call
    with psycopg.connect(url) as conn:
        return conn.execute("select id from runs where source_key = %s", (run.source_key,)).fetchone()[0]


def _score(url: str, run_id: uuid.UUID, question: str, value: str, age_days: int) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            """insert into scores (run_id, question, kind, value, jev_mode, created_at)
               values (%s, %s, 'choice', %s, 'fixture', %s)""",
            (run_id, question, value, T0 - timedelta(days=age_days)))


def _label(url: str, run_id: uuid.UUID, verdict: str, age_days: int) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("insert into labels (run_id, verdict, labeled_at) values (%s, %s, %s)",
                     (run_id, verdict, T0 - timedelta(days=age_days)))


def _published(tag: str) -> list[str]:
    return [
        f"2026-10-11T08:26:30+0000 {tag} PASS pass=4 fail=0 2s "
        f"report=/tmp/tmp.HIatBDBcYC/.claude/parent/reports/{tag}-2026-10-11.md",
        f"2026-10-12T04:10:06+0000 {tag} FAIL pass=3 fail=1 2s "
        f"report=/tmp/tmp.JrYYGs0CVa/.claude/parent/reports/{tag}-2026-10-12.md",
        f"2026-10-12T04:10:06+0000 {tag}-fail disk check: 91% used",
    ]


class Seeded:
    """Old-style state: every cloud run twice, the improve run twice, plus rows to leave alone."""

    def __init__(self, url: str, tag: str) -> None:
        pub = _published(tag)
        self.p1 = _seed(url, [pub[0]], published=True)                      # git-ref, first
        self.l1 = _seed(url, [routine_sync(pub[0])])                        # routine-sync copy
        self.l2 = _seed(url, [routine_sync(pub[1]), pub[2]])                # local copy first here
        self.p2 = _seed(url, [pub[1], pub[2]], published=True)
        improve = f"2026-10-11T11:29:19+0200 {tag}-improve PASS tasks=36 0s report=~/.claude/parent/reports/i.md"
        self.a = _seed(url, [improve])
        self.b = _seed(url, [routine_sync(improve)])
        solo = f"2026-10-10T23:22:52+0200 {tag}-solo FAIL pass=1 report=~/.claude/parent/reports/s.md"
        self.solo = _seed(url, [solo])
        twin = f"2026-10-12T10:00:00+0200 {tag}-twin PASS fail=0 report=/a/t.md"
        self.twin1 = _seed(url, [twin], occurrence=1)                       # a true repeat: 2 events
        self.twin2 = _seed(url, [twin], occurrence=2)
        self.orphan = _seed(url, [f"2026-10-12T10:00:05+0200 {tag}-orphan-fail lost detail"])
        self.manual = store.insert_run(f"{tag}-manual", "pass", db_url=url)  # no source_key
        with psycopg.connect(url) as conn:
            self.unmatched = conn.execute(
                "insert into runs (routine, status, started_at, source_key) values (%s, 'pass', %s, %s) returning id",
                (f"{tag}-odd", T0, "not-a-known-key")).fetchone()[0]
        self.local =[improve, routine_sync(improve), solo, twin, twin,
                      routine_sync(pub[0]), routine_sync(pub[1]), pub[2],
                      f"2026-10-12T10:00:05+0200 {tag}-orphan-fail lost detail"]
        self.published = pub


def _count(url: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute("select count(*) from runs").fetchone()[0]


def _ids(url: str) -> set[uuid.UUID]:
    with psycopg.connect(url) as conn:
        return {r[0] for r in conn.execute("select id from runs").fetchall()}


def test_dry_run_lists_duplicates_and_changes_nothing(sdb: ScoringDb) -> None:  # noqa: F811
    tag = f"t{uuid.uuid4().hex[:8]}"
    s = Seeded(sdb.url, tag)
    _score(sdb.url, s.l1, "outcome", "pass", age_days=1)
    _label(sdb.url, s.l1, "down", age_days=1)
    before = _ids(sdb.url)

    result = run_amem("db", "dedupe-runs", env=sdb.env)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "duplicate runs: 3 groups, 3 rows to delete" in out
    assert f"keep {s.p1} (routine-reports); delete {s.l1} (routines.log) [labels 1, scores 1]" in out
    assert f"keep {s.l2} (routines.log); delete {s.p2} (routine-reports)" in out
    assert f"keep {s.a} (routines.log); delete {s.b} (routines.log)" in out
    assert "unmatched rows (key not recognised, left alone): 1" in out
    assert str(s.unmatched) in out
    assert "dry run: nothing changed" in out
    assert _ids(sdb.url) == before


def test_apply_merges_references_deletes_duplicates_and_rekeys(sdb: ScoringDb, tmp_path: Path) -> None:  # noqa: F811
    url = sdb.url
    tag = f"t{uuid.uuid4().hex[:8]}"
    s = Seeded(url, tag)
    # Duplicate l1: a newer score on a question the keeper also has, one more question,
    # a newer label than the keeper's, a lesson use and a lesson source.
    _score(url, s.p1, "outcome", "old-keeper", age_days=5)
    _score(url, s.l1, "outcome", "new-dup", age_days=1)
    _score(url, s.l1, "quality", "good", age_days=1)
    _label(url, s.p1, "up", age_days=5)
    _label(url, s.l1, "down", age_days=1)
    # Duplicate p2 (keeper l2): the keeper's score is the newer one; the dup's label moves.
    _score(url, s.l2, "outcome", "keeper-newer", age_days=1)
    _score(url, s.p2, "outcome", "dup-older", age_days=5)
    _label(url, s.p2, "up", age_days=2)
    other = uuid.uuid4()
    with psycopg.connect(url) as conn:
        lesson = conn.execute(
            "insert into lessons (slug, title, rule, source_run_ids) values (%s, 't', 'r', %s) returning id",
            (f"{tag}-lesson", [other, s.l1, s.p1, s.b])).fetchone()[0]
        conn.execute("insert into lesson_uses (lesson_id, run_id) values (%s, %s)", (lesson, s.l1))
        conn.execute("insert into lesson_uses (lesson_id, run_id) values (%s, %s)", (lesson, s.b))

    result = run_amem("db", "dedupe-runs", "--apply", env=sdb.env)
    assert result.returncode == 0, result.stderr
    # Rekeyed: keepers p1, l2, a plus solo, twin1, twin2 (each has a report= path).
    # The orphan row has no report=, so its old key already is canonical.
    assert result.stdout.strip() == (
        "deleted 3 duplicate runs in 3 groups; moved references: labels 2, lesson_uses 2, "
        "lessons.source_run_ids 2, scores 2; rewrote 6 source_keys; 1 unmatched rows left alone")

    ids = _ids(url)
    assert {s.l1, s.p2, s.b}.isdisjoint(ids)
    assert {s.p1, s.l2, s.a, s.solo, s.twin1, s.twin2, s.orphan, s.manual, s.unmatched} <= ids
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select run_id, question, value from scores order by run_id, question")
        scores = {(r["run_id"], r["question"]): r["value"] for r in cur.fetchall()}
        cur.execute("select run_id, verdict from labels")
        labels = {r["run_id"]: r["verdict"] for r in cur.fetchall()}
        cur.execute("select run_id from lesson_uses order by run_id")
        uses = [r["run_id"] for r in cur.fetchall()]
        cur.execute("select source_run_ids from lessons where id = %s", (lesson,))
        sources = cur.fetchone()["source_run_ids"]
        cur.execute("select id, source_key from runs where routine = %s and source_key is not null", (tag,))
        keys = {r["id"]: r["source_key"] for r in cur.fetchall()}
        cur.execute("select source_key from runs where id = %s", (s.unmatched,))
        unmatched_key = cur.fetchone()["source_key"]
    assert scores == {(s.p1, "outcome"): "new-dup", (s.p1, "quality"): "good",
                      (s.l2, "outcome"): "keeper-newer"}
    assert labels == {s.p1: "down", s.l2: "up"}
    assert sorted(uses) == sorted([s.p1, s.a])
    assert sources == [other, s.p1, s.a]
    pub = _published(tag)
    assert keys == {s.p1: source_key(tag, pub[0]), s.l2: source_key(tag, pub[1])}
    assert unmatched_key == "not-a-known-key"

    # Re-ingesting both sources adds nothing: stored keys are canonical now.
    log = tmp_path / "routines.log"
    log.write_text("\n".join(s.local) + "\n", encoding="utf-8")
    again = run_amem("ingest", "routines-log", str(log), env=sdb.env)
    assert again.returncode == 0, again.stderr
    assert again.stdout.strip() == "ingested 0 new, 7 already present, 0 unparsed"
    clone = tmp_path / "clone"
    clone.mkdir()
    _git_publish(clone, "\n".join(s.published) + "\n")
    git_ref = run_amem("ingest", "git-ref", "--git-dir", str(clone), "--ref", "main", env=sdb.env)
    assert git_ref.returncode == 0, git_ref.stderr
    assert git_ref.stdout.strip() == "ingested 0 new, 2 already present, 0 unparsed"

    # A second run finds nothing to do.
    second = run_amem("db", "dedupe-runs", env=sdb.env)
    assert "duplicate runs: 0 groups, 0 rows to delete" in second.stdout
    assert "source_keys to rewrite to the canonical form: 0" in second.stdout
    again_apply = run_amem("db", "dedupe-runs", "--apply", env=sdb.env)
    assert again_apply.stdout.startswith("deleted 0 duplicate runs in 0 groups; moved references: none; "
                                          "rewrote 0 source_keys")


def _git_publish(repo: Path, text: str) -> None:
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    (repo / "reports").mkdir()
    (repo / "reports" / "routines.log").write_text(text, encoding="utf-8")
    git("add", "reports/routines.log")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "report")


def test_true_repeats_and_offsets_are_recognised(sdb: ScoringDb) -> None:  # noqa: F811
    # The ts offset is not stored: +0200, +02:00, Z and -0530 lines are all recovered.
    url = sdb.url
    tag = f"t{uuid.uuid4().hex[:8]}"
    lines = [f"2026-10-12T10:00:00+0200 {tag} PASS a=1 report=/x/a.md",
             f"2026-10-12T10:00:01+02:00 {tag} ERROR crashed",
             f"2026-10-12T10:00:02Z {tag} WIN with=1",
             f"2026-10-12T10:00:03-0530 {tag} SKIP"]
    ids = [_seed(url, [line]) for line in lines]
    twin = f"2026-10-12T11:00:00+0200 {tag} PASS fail=0"
    ids += [_seed(url, [twin], occurrence=n) for n in (1, 2, 3)]
    result = run_amem("db", "dedupe-runs", "--apply", env=sdb.env)
    assert result.returncode == 0, result.stderr
    assert "deleted 0 duplicate runs" in result.stdout and "0 unmatched" in result.stdout
    with psycopg.connect(url) as conn:
        keys = dict(conn.execute("select id, source_key from runs").fetchall())
    expected = [source_key(tag, line) for line in lines] + [source_key(tag, twin, n) for n in (1, 2, 3)]
    assert [keys[i] for i in ids] == expected


def test_refuses_rest_backend_and_local_db_under_require_remote(sdb: ScoringDb) -> None:  # noqa: F811
    rest = run_amem("db", "dedupe-runs", env=base_env(AMEM_SUPABASE_URL="https://project.example.com"))
    assert rest.returncode == 1
    assert "needs the Postgres backend" in rest.stderr
    remote = run_amem("db", "dedupe-runs", "--apply", env={**sdb.env, "AMEM_REQUIRE_REMOTE": "1"})
    assert remote.returncode == 3
    assert "AMEM_REQUIRE_REMOTE=1" in remote.stderr and "refusing to write" in remote.stderr


def test_unknown_key_shapes_never_crash() -> None:
    row = {"routine": "r", "status": "pass", "summary": None, "meta": {}, "source_key": "x",
           "started_at": T0}
    from agent_memory import dedupe

    assert dedupe.canonical_key(row, 1) is None
    row["source_key"] = routines_log.legacy_source_key("r", "2026-10-01T00:00:00+0000 r PASS")
    assert dedupe.canonical_key(row, 1) == source_key("r", "2026-10-01T00:00:00+0000 r PASS")
    row["started_at"] = None
    assert dedupe.canonical_key(row, 1) is None
