"""routines.log parser (every line shape) and `amem ingest routines-log` round trip."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from agent_memory import routines_log
from agent_memory.routines_log import parse_lines
from conftest import run_amem

FIXTURE = Path(__file__).parent / "fixtures" / "routines.sample.log"
CEST = timezone(timedelta(hours=2))


def _fixture_lines() -> list[str]:
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def _by_line(result: routines_log.ParseResult) -> dict[int, routines_log.ParsedRun]:
    return {r.line_no: r for r in result.runs}


# --- parser over the real fixture -------------------------------------------------


def test_fixture_every_line_accounted_for() -> None:
    result = routines_log.parse_file(FIXTURE)
    lines = [l for l in _fixture_lines() if l.strip()]
    assert len(lines) == 358
    assert result.unparsed == []
    assert result.orphan_details == 0
    assert result.incomplete_tail is False
    # Lines 172/173 are routine-sync copies of 155/156 (report= rewritten): same runs, dropped.
    assert len(result.runs) + result.details + result.duplicates == len(lines)
    assert (len(result.runs), result.details, result.duplicates) == (221, 135, 2)
    assert len({r.source_key for r in result.runs}) == len(result.runs)
    # Every detail line landed in some row's failures.
    assert sum(len(r.meta["failures"]) for r in result.runs) == result.details


def test_fixture_status_mapping() -> None:
    runs = routines_log.parse_file(FIXTURE).runs
    by_status: dict[str, int] = {}
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    assert by_status == {"pass": 143, "fail": 72, "error": 4, "skip": 2}  # 172/173 = copies of 155/156
    raw = sorted(r.meta["raw_status"] for r in runs if "raw_status" in r.meta)
    assert raw == ["INCONCLUSIVE", "INCONCLUSIVE", "TIE", "WIN"]
    assert all(r.meta["log_source"] == "routines.log" for r in runs)


def test_summary_with_details_config_check() -> None:
    run = _by_line(routines_log.parse_file(FIXTURE))[343]
    assert run.routine == "config-check"
    assert run.status == "fail"
    assert run.source == "launchd"
    assert run.started_at == datetime(2026, 10, 12, 9, 0, 14, tzinfo=CEST)
    assert run.report_path == "~/.claude/parent/reports/config-check-2026-10-12.md"
    assert run.summary == "pass=36 fail=5 info=0 10s report=~/.claude/parent/reports/config-check-2026-10-12.md"
    assert run.meta["pass"] == 36 and run.meta["fail"] == 5 and run.meta["info"] == 0
    assert run.meta["duration_s"] == 10
    assert "report" not in run.meta
    assert run.meta["failures"] == [
        "Orca LAN reach: answers on 192.168.50.152:6768",
        "repos in iCloud: 1: ~/Desktop/Projects/Classifed",
        "settings.json drift: drift outside Evlat's known patterns in skillOverrides: /skillOverrides/anatomy",
        "parent footprint junk-orchestra: tracked: .claude/worktree-setup.sh; tracked: .worktreeinclude",
        "parent footprint simplitics drift check: tracked: .claude/worktree-setup.sh; tracked: .worktreeinclude",
    ]


def test_detail_starting_with_uppercase_word_is_a_detail() -> None:
    # "canary-simplitics-selftest-fail QPI disabled: ..." : QPI looks like a status word.
    runs = _by_line(routines_log.parse_file(FIXTURE))
    assert 31 not in runs
    run = runs[30]
    assert run.routine == "canary-simplitics-selftest"
    assert run.meta["failures"][0] == "QPI disabled: QPI_ENABLED=true (docker compose config)"
    assert len(run.meta["failures"]) == 3


def test_detail_text_keeps_inner_spacing_and_odd_chars() -> None:
    runs = _by_line(routines_log.parse_file(FIXTURE))
    assert runs[41].meta["failures"][0] == "git status clean: 1 change(s):  M README.md"
    assert runs[111].routine == "skill-evals"
    assert runs[111].meta["failures"][0].startswith('handoff: chat:{"skill_invoked": true};')


def test_details_attach_to_latest_summary_with_same_ts_and_routine() -> None:
    # Lines 141/142 share a ts but are different routines; 155 and 172 share ts + routine.
    # 172 is routine-sync's copy of 155 (only the report dir differs): one run, keyed by 155.
    runs = _by_line(routines_log.parse_file(FIXTURE))
    assert runs[141].routine == "improve-e2e" and runs[142].routine == "improve-selftest"
    assert 172 not in runs and 173 not in runs
    assert runs[155].started_at != runs[156].started_at  # different ts: still two runs
    assert runs[155].source_key != runs[156].source_key


def test_cloud_source_detection() -> None:
    runs = _by_line(routines_log.parse_file(FIXTURE))
    fresh = runs[106]
    assert fresh.routine == "fresh-machine"
    assert fresh.source == "cloud"  # /root/... report path
    assert fresh.started_at == datetime(2026, 10, 10, 23, 28, 30, tzinfo=timezone.utc)
    assert runs[132].source == "cloud"  # .../reports/cloud/...
    assert runs[155].source == "launchd"  # first copy wins; its cloud copy (172) is the same run
    assert runs[128].source == "launchd"  # +0000 ts, but a local report path


def test_unknown_status_words_and_odd_free_text() -> None:
    runs = _by_line(routines_log.parse_file(FIXTURE))
    win = runs[119]
    assert win.status == "error" and win.meta["raw_status"] == "WIN"
    assert win.meta["with"] == 0.0 and win.meta["without"] == 1.0
    assert win.meta["regrade-of"] == "2026-10-11"
    assert win.meta["status"] == "promoted"  # a k=v named status stays in meta, not the column
    assert runs[120].meta["raw_status"] == "INCONCLUSIVE"
    misses = runs[272]
    assert misses.status == "fail"
    assert misses.summary == "misses=3 ['p good run', 'q good setup', 'q restored']"
    assert misses.meta["misses"] == 3
    skip = runs[199]
    assert (skip.routine, skip.status, skip.meta["changed"]) == ("improve-fence", "skip", 0)


def test_summary_without_report_or_duration() -> None:
    run = _by_line(routines_log.parse_file(FIXTURE))[98]
    assert (run.routine, run.status, run.report_path) == ("scan-selftest", "pass", None)
    assert "duration_s" not in run.meta
    assert run.meta["failures"] == []


def test_source_key_is_sha256_of_routine_and_raw_line() -> None:
    # Canonical line: report=<path> is cut to its basename (docs/cloud.md, "Dedupe keys").
    line = _fixture_lines()[342].replace("report=~/.claude/parent/reports/", "report=")
    run = _by_line(routines_log.parse_file(FIXTURE))[343]
    assert run.source_key == hashlib.sha256(f"config-check\n{line}".encode()).hexdigest()
    again = _by_line(routines_log.parse_file(FIXTURE))[343]
    assert again.source_key == run.source_key


# --- synthetic shapes the real file does not (yet) contain ---------------------


def test_orphan_details_are_kept_as_one_row_per_group() -> None:
    lines = [
        "2026-10-12T10:00:00+0200 alpha PASS pass=1 fail=0",
        "2026-10-12T10:00:05+0200 alpha-fail lost detail one",
        "2026-10-12T10:00:05+0200 alpha-fail lost detail two",
        "2026-10-12T10:00:00+0200 beta-fail other routine, same ts as alpha",
    ]
    result = parse_lines(lines)
    assert result.orphan_details == 3 and result.details == 0 and result.unparsed == []
    alpha, orphan_a, orphan_b = result.runs
    assert alpha.meta["failures"] == []
    assert (orphan_a.routine, orphan_a.status, orphan_a.summary) == ("alpha", "fail", None)
    assert orphan_a.meta == {"log_source": "routines.log", "orphan_detail": True,
                             "failures": ["lost detail one", "lost detail two"]}
    assert orphan_b.routine == "beta" and orphan_b.meta["failures"] == ["other routine, same ts as alpha"]


def test_unparsed_and_blank_lines() -> None:
    lines = [
        "",
        "   ",
        "not-a-timestamp alpha PASS x=1",
        "2026-10-12T10:00:00 alpha PASS no offset",
        "2026-10-12T10:00:00+0200",
        "2026-10-12T10:00:00+0200 alpha",
        "2026-10-12T10:00:00+0200 alpha passed lowercase status",
        "2026-10-12T10:00:00+0200 alpha ERROR crashed exit=2",
        "2026-10-12T10:00:00+0200 alpha SKIP disabled",
    ]
    result = parse_lines(lines)
    assert result.unparsed == [3, 4, 5, 6, 7]
    assert [(r.status, r.summary) for r in result.runs] == [("error", "crashed exit=2"), ("skip", "disabled")]
    assert "raw_status" not in result.runs[0].meta


def test_routine_ending_in_fail_with_status_is_a_summary() -> None:
    result = parse_lines(["2026-10-12T10:00:00+0200 fast-fail PASS ok=1"])
    assert [(r.routine, r.status) for r in result.runs] == [("fast-fail", "pass")]


def test_identical_lines_get_distinct_stable_keys() -> None:
    line = "2026-10-12T10:00:00+0200 alpha PASS fail=0"
    keys = [r.source_key for r in parse_lines([line, line, line]).runs]
    assert len(set(keys)) == 3
    assert keys[0] == hashlib.sha256(f"alpha\n{line}".encode()).hexdigest()
    assert keys == [r.source_key for r in parse_lines([line, line, line]).runs]


def test_incomplete_last_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "routines.log"
    path.write_text("2026-10-12T10:00:00+0200 alpha PASS a=1\n2026-10-12T10:00:01+0200 beta PA", encoding="utf-8")
    result = routines_log.parse_file(path)
    assert [r.routine for r in result.runs] == ["alpha"]
    assert result.incomplete_tail is True
    assert result.unparsed == []


def test_reserved_meta_keys_cannot_be_overwritten_by_kv() -> None:
    run = parse_lines(["2026-10-12T10:00:00+0200 alpha PASS failures=x log_source=y 5s duration_s=9"]).runs[0]
    assert run.meta == {"log_source": "routines.log", "failures": [], "duration_s": 5}


# --- ingest round trip against the throwaway Postgres ---------------------------


def _tagged_copy(tmp_path: Path, tag: str) -> Path:
    """Fixture with every routine prefixed by `tag`, so this test owns its rows."""
    out = []
    for line in _fixture_lines():
        ts, token, rest = line.split(" ", 2)
        out.append(f"{ts} {tag}-{token} {rest}")
    path = tmp_path / "routines.log"
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _rows(pg_url: str, tag: str) -> list[dict]:
    with psycopg.connect(pg_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "select * from runs where routine = %s or routine like %s order by started_at, source_key",
            (tag, f"{tag}-%"),
        )
        return cur.fetchall()


def test_ingest_round_trip_and_reingest_inserts_nothing(db_env: dict[str, str], pg_url: str, tmp_path: Path) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"
    path = _tagged_copy(tmp_path, tag)

    first = run_amem("ingest", "routines-log", str(path), env=db_env)
    assert first.returncode == 0, first.stderr
    # 221 = 223 summary lines minus 2 routine-sync copies (172/173 of 155/156).
    assert first.stdout.strip() == "ingested 221 new, 0 already present, 0 unparsed"

    second = run_amem("ingest", "routines-log", str(path), env=db_env)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "ingested 0 new, 221 already present, 0 unparsed"

    rows = _rows(pg_url, tag)
    assert len(rows) == 221
    assert all(r["source_key"] for r in rows)
    row = next(r for r in rows if r["routine"] == f"{tag}-config-check"
               and r["started_at"] == datetime(2026, 10, 12, 7, 0, 14, tzinfo=timezone.utc))
    assert row["status"] == "fail"
    assert row["source"] == "launchd"
    assert row["report_path"] == "~/.claude/parent/reports/config-check-2026-10-12.md"
    assert row["meta"]["pass"] == 36 and row["meta"]["duration_s"] == 10
    assert len(row["meta"]["failures"]) == 5
    assert row["meta"]["log_source"] == "routines.log"
    win = next(r for r in rows if r["meta"].get("raw_status") == "WIN")
    assert win["status"] == "error"
    assert sum(r["source"] == "cloud" for r in rows) == 3  # 172/173 (cloud copies of 155/156) merged


def test_ingest_picks_up_new_lines_and_late_details(db_env: dict[str, str], pg_url: str, tmp_path: Path) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"
    path = tmp_path / "routines.log"
    path.write_text(f"2026-10-12T10:00:00+0200 {tag} FAIL pass=1 fail=1\n", encoding="utf-8")
    first = run_amem("ingest", "routines-log", str(path), env=db_env)
    assert first.stdout.strip() == "ingested 1 new, 0 already present, 0 unparsed"

    # The routine finishes writing its detail, a new run lands, plus a junk line and a partial tail.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"2026-10-12T10:00:00+0200 {tag}-fail disk full\n")
        fh.write(f"2026-10-12T11:00:00+0200 {tag} PASS pass=2 fail=0\n")
        fh.write("garbage line\n")
        fh.write(f"2026-10-12T12:00:00+0200 {tag} PA")
    second = run_amem("ingest", "routines-log", str(path), env=db_env)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "ingested 1 new, 1 already present, 1 unparsed"
    assert "unparsed lines: 4" in second.stderr
    assert "skipped the last line" in second.stderr

    rows = _rows(pg_url, tag)
    assert [(r["status"], r["meta"]["failures"]) for r in rows] == [("fail", ["disk full"]), ("pass", [])]


def test_ingest_keeps_other_meta_keys_on_refresh(db_env: dict[str, str], pg_url: str, tmp_path: Path) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"
    path = tmp_path / "routines.log"
    path.write_text(f"2026-10-12T10:00:00+0200 {tag} FAIL fail=1\n", encoding="utf-8")
    run_amem("ingest", "routines-log", str(path), env=db_env)
    with psycopg.connect(pg_url) as conn:
        conn.execute("update runs set meta = meta || '{\"jev\": \"scored\"}' where routine = %s", (tag,))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"2026-10-12T10:00:00+0200 {tag}-fail late detail\n")
    run_amem("ingest", "routines-log", str(path), env=db_env)
    (row,) = _rows(pg_url, tag)
    assert row["meta"]["jev"] == "scored"
    assert row["meta"]["failures"] == ["late detail"]


def test_ingest_missing_file(db_env: dict[str, str], tmp_path: Path) -> None:
    result = run_amem("ingest", "routines-log", str(tmp_path / "nope.log"), env=db_env)
    assert result.returncode == 1
    assert "no such file" in result.stderr
