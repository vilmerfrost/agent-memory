"""Canonical source_keys: one cloud run is one row, whichever ingest path sees it first.

A cloud run reaches `runs` twice: `ingest git-ref` reads the published line, and
`ingest routines-log` reads routine-sync's copy in the local log (report= rewritten).
Both orders, on both backends, must end with one row.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import backend_rest, cli, cli_cloud, routines_log
from agent_memory.routines_log import canonical_line, parse_lines, source_key
from test_rest_backend import Stub, _serve_stub
from test_scoring_support import ScoringDb, sdb  # noqa: F401

_SYNC_RE = re.compile(r"report=[^ ]*/([^/ ]+)$")


def routine_sync(line: str) -> str:
    """The rewrite in ~/agent-config/bin/routine-sync:
    sed -E 's#report=[^ ]*/([^/ ]+)$#report=~/.claude/parent/reports/cloud/\\1#'"""
    return _SYNC_RE.sub(r"report=~/.claude/parent/reports/cloud/\1", line)


def _published(tag: str) -> str:
    """A published log in the real shape (fresh-machine, 2026-10-11 and 2026-10-12)."""
    return (
        f"2026-10-11T08:26:30+0000 {tag} PASS pass=4 fail=0 2s "
        f"report=/tmp/tmp.HIatBDBcYC/.claude/parent/reports/{tag}-2026-10-11.md\n"
        f"2026-10-12T04:10:06+0000 {tag} FAIL pass=3 fail=1 2s "
        f"report=/tmp/tmp.JrYYGs0CVa/.claude/parent/reports/{tag}-2026-10-12.md\n"
        f"2026-10-12T04:10:06+0000 {tag}-fail disk check: 91% used\n"
    )


def _local(tag: str) -> str:
    """The local routines.log after routine-sync: one launchd run plus the rewritten cloud lines."""
    synced = "".join(routine_sync(line) + "\n" for line in _published(tag).splitlines())
    return f"2026-10-11T09:00:00+0200 {tag} PASS pass=9 fail=0 1s report=~/.claude/parent/reports/{tag}.md\n" + synced


def _contents(text: str) -> str:
    encoded = base64.encodebytes(text.encode()).decode()
    return json.dumps({"type": "file", "encoding": "base64", "size": len(text), "content": encoded})


def _fake_gh(text: str):  # noqa: ANN202
    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["gh", "api"] and "-X" not in argv
        return subprocess.CompletedProcess(argv, 0, _contents(text), "")
    return run


# ---------------------------------------------------------------- pure parser


def test_routine_sync_rewrite_matches_the_real_lines() -> None:
    published = ("2026-10-11T08:26:30+0000 fresh-machine PASS pass=4 fail=0 2s "
                 "report=/tmp/tmp.HIatBDBcYC/.claude/parent/reports/fresh-machine-2026-10-11.md")
    local = ("2026-10-11T08:26:30+0000 fresh-machine PASS pass=4 fail=0 2s "
             "report=~/.claude/parent/reports/cloud/fresh-machine-2026-10-11.md")
    assert routine_sync(published) == local
    assert canonical_line(published) == canonical_line(local) == (
        "2026-10-11T08:26:30+0000 fresh-machine PASS pass=4 fail=0 2s report=fresh-machine-2026-10-11.md")
    assert source_key("fresh-machine", published) == source_key("fresh-machine", local)


def test_canonical_line_only_touches_report_tokens() -> None:
    assert canonical_line("t r PASS a=1 report=x.md") == "t r PASS a=1 report=x.md"
    assert canonical_line("t r PASS report=/a/b/c.md 3s") == "t r PASS report=c.md 3s"
    assert canonical_line("t r PASS myreport=/a/b.md") == "t r PASS myreport=/a/b.md"
    assert canonical_line("t r PASS report=") == "t r PASS report="


def test_different_fields_stay_different_runs() -> None:
    base = "2026-10-11T08:26:30+0000 r PASS pass=4 fail=0 report=/x/r.md"
    keys = {source_key("r", line) for line in (
        base,
        base.replace("08:26:30", "08:26:31"),  # timestamp
        base.replace("PASS", "FAIL"),  # verdict
        base.replace("pass=4", "pass=5"),  # a field
        base.replace("r.md", "r2.md"),  # another report file
    )}
    assert len(keys) == 5


def test_published_line_and_synced_copy_share_keys() -> None:
    tag = "fresh-machine"
    published = cli_cloud.parse_published(_published(tag))
    local = parse_lines(_local(tag).splitlines())
    assert [r.source_key for r in published.runs] == [r.source_key for r in local.runs[1:]]
    assert local.runs[2].meta["failures"] == published.runs[1].meta["failures"] == ["disk check: 91% used"]


def test_synced_copy_next_to_its_original_is_dropped() -> None:
    # The improve case in the real log: the original line, then routine-sync's copy of it.
    original = "2026-10-11T11:29:19+0200 improve PASS tasks=36 0s report=~/.claude/parent/reports/improve-2026-10-11.md"
    lines = [original, "2026-10-11T11:29:19+0200 improve-fail late detail", routine_sync(original)]
    result = parse_lines(lines)
    assert [r.line_no for r in result.runs] == [1]
    assert result.duplicates == 1
    assert result.runs[0].source == "launchd"
    assert result.runs[0].meta["failures"] == ["late detail"]


def test_details_after_a_dropped_copy_attach_to_the_original() -> None:
    original = "2026-10-11T11:29:19+0200 improve FAIL report=/r/improve.md"
    other = "2026-10-11T11:29:19+0200 improve FAIL another=1"
    lines = [original, other, routine_sync(original), "2026-10-11T11:29:19+0200 improve-fail d1"]
    runs = parse_lines(lines).runs
    assert [r.meta["failures"] for r in runs] == [["d1"], []]


def test_byte_identical_repeats_stay_separate_runs() -> None:
    # Rule (docs/cloud.md): a byte-identical raw line again is a separate event (#n suffix);
    # routine-sync never appends a line that is already in the local log.
    line = "2026-10-12T10:00:00+0200 alpha PASS fail=0 report=/a/alpha.md"
    runs = parse_lines([line, line, routine_sync(line)]).runs
    assert len(runs) == 2
    assert runs[1].source_key == source_key("alpha", line, 2)


# ---------------------------------------------------------------- Postgres, both orders


def _rows(url: str, tag: str) -> list[dict]:
    with psycopg.connect(url) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select * from runs where routine = %s order by started_at", (tag,))
        return cur.fetchall()


@pytest.mark.parametrize("order", ["git-ref first", "routines-log first"])
def test_postgres_one_row_per_run_in_both_orders(sdb: ScoringDb, tmp_path: Path, order: str,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    tag = f"t{uuid.uuid4().hex[:8]}"
    log = tmp_path / "routines.log"
    log.write_text(_local(tag), encoding="utf-8")
    monkeypatch.setattr(cli_cloud, "run_command", _fake_gh(_published(tag)))
    steps = [["ingest", "git-ref"], ["ingest", "routines-log", str(log)]]
    if order == "routines-log first":
        steps.reverse()

    assert cli.main(steps[0]) == 0
    assert cli.main(steps[1]) == 0
    out = capsys.readouterr().out.splitlines()
    if order == "git-ref first":
        assert out == ["ingested 2 new, 0 already present, 0 unparsed",
                       "ingested 1 new, 2 already present, 0 unparsed"]
    else:
        assert out == ["ingested 3 new, 0 already present, 0 unparsed",
                       "ingested 0 new, 2 already present, 0 unparsed"]

    rows = _rows(sdb.url, tag)
    assert len(rows) == 3  # one launchd run + two cloud runs, each once
    first_source = "routine-reports" if order == "git-ref first" else "routines.log"
    assert [r["meta"]["log_source"] for r in rows[1:]] == [first_source, first_source]
    assert rows[2]["meta"]["failures"] == ["disk check: 91% used"]

    # Re-ingesting both again adds nothing.
    for step in steps:
        assert cli.main(step) == 0
    assert all(line.startswith("ingested 0 new") for line in capsys.readouterr().out.splitlines())
    assert len(_rows(sdb.url, tag)) == 3


# ---------------------------------------------------------------- REST, both orders


@pytest.fixture
def stub() -> Iterator[tuple[Stub, str]]:
    with _serve_stub() as served:
        yield served


@pytest.mark.parametrize("order", ["git-ref first", "routines-log first"])
def test_rest_one_row_per_run_in_both_orders(stub: tuple[Stub, str], tmp_path: Path, order: str,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    state, url = stub
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("AMEM_SUPABASE_URL", url)
    tag = "fresh-machine"
    log = tmp_path / "routines.log"
    log.write_text(_local(tag), encoding="utf-8")
    monkeypatch.setattr(cli_cloud, "run_command", _fake_gh(_published(tag)))
    steps = [["ingest", "git-ref"], ["ingest", "routines-log", str(log)]]
    if order == "routines-log first":
        steps.reverse()
    for step in steps + steps:
        assert cli.main(step) == 0
    assert len(state.rows) == 3
    assert len({r["source_key"] for r in state.rows}) == 3
    assert backend_rest.existing_meta([r.source_key for r in cli_cloud.parse_published(_published(tag)).runs])
    assert [r["started_at"] for r in state.rows].count("2026-10-11T08:26:30+00:00") == 1
