"""`amem ingest git-ref`: the routines.log that cloud routines publish to a git branch.

`gh` is never run: `cli_cloud.run_command` is replaced by a fake that returns a
fixture. The `--git-dir` tests use a throwaway local repo (no network).
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from agent_memory import cli, cli_cloud, routines_log, store

CLOUD_LOG = (
    "2026-10-12T05:00:03+0000 morning-briefing PASS sections=3 42s report=/root/reports/briefing.md\n"
    "2026-10-12T05:10:00+0000 cloud-audit FAIL pass=9 fail=1 3s\n"
    "2026-10-12T05:10:00+0000 cloud-audit-fail token scope too wide\n"
    "not a log line\n"
)
ENDPOINT = "repos/vilmerfrost/agent-config/contents/reports/routines.log?ref=claude/routine-reports"


def _done(argv: list[str], code: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, code, out, err)


class FakeGh:
    """Replays `gh api` answers; records every argv."""

    def __init__(self, *answers: tuple[int, str, str]) -> None:
        self.answers = list(answers)
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv[:2] == ["gh", "api"], argv
        assert "-X" not in argv and "--method" not in argv  # GET only, never a write
        self.calls.append(argv)
        code, out, err = self.answers.pop(0)
        return _done(argv, code, out, err)


def _contents(text: str) -> str:
    encoded = base64.encodebytes(text.encode()).decode()  # GitHub wraps base64 in lines too
    return json.dumps({"type": "file", "encoding": "base64", "size": len(text), "content": encoded})


@pytest.fixture(autouse=True)
def _no_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AMEM_SUPABASE_URL", "AMEM_SUPABASE_KEY", "AMEM_BACKEND"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    calls: list[list[Any]] = []
    monkeypatch.setattr(store, "ingest_runs", lambda rows, **k: calls.append(list(rows)) or (len(calls[-1]), 0))
    return calls


def test_parse_published_tags_cloud_and_keeps_keys() -> None:
    parsed = cli_cloud.parse_published(CLOUD_LOG)
    local = routines_log.parse_lines(CLOUD_LOG.splitlines())
    assert [r.source_key for r in parsed.runs] == [r.source_key for r in local.runs]  # same dedupe keys
    assert {r.source for r in parsed.runs} == {"cloud"}
    assert {r.meta["log_source"] for r in parsed.runs} == {"routine-reports"}
    assert parsed.runs[1].meta["failures"] == ["token scope too wide"]
    assert parsed.unparsed == [4]
    assert parsed.incomplete_tail is False
    assert cli_cloud.parse_published(CLOUD_LOG + "2026-10-12T06:00:00+0000 x PA").incomplete_tail


def test_gh_fixture_ingests_into_postgres(db_env: dict[str, str], monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeGh((0, _contents(CLOUD_LOG), ""), (0, _contents(CLOUD_LOG), ""))
    monkeypatch.setattr(cli_cloud, "run_command", fake)

    assert cli.main(["ingest", "git-ref"]) == 0
    captured_io = capsys.readouterr()
    assert "ingested 2 new, 0 already present, 1 unparsed" in captured_io.out
    assert "unparsed lines: 4" in captured_io.err
    assert fake.calls[0] == ["gh", "api", "-H", "Accept: application/vnd.github+json", ENDPOINT]

    with psycopg.connect(db_env["AMEM_DB_URL"]) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("select routine, source, status, meta from runs where routine in "
                    "('morning-briefing', 'cloud-audit') order by routine")
        rows = cur.fetchall()
    assert [(r["routine"], r["source"], r["status"]) for r in rows] == [
        ("cloud-audit", "cloud", "fail"), ("morning-briefing", "cloud", "pass")]
    assert {r["meta"]["log_source"] for r in rows} == {"routine-reports"}

    assert cli.main(["ingest", "git-ref"]) == 0
    assert "ingested 0 new, 2 already present, 1 unparsed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "err",
    ["gh: Not Found (HTTP 404)", "gh: No commit found for the ref claude/routine-reports (HTTP 404)"],
)
def test_missing_branch_or_file_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                           captured: list[list[Any]], err: str) -> None:
    monkeypatch.setattr(cli_cloud, "run_command", FakeGh((1, '{"message":"Not Found"}', err)))
    assert cli.main(["ingest", "git-ref"]) == 0
    out = capsys.readouterr().out
    assert "nothing to ingest" in out and "claude/routine-reports" in out
    assert captured == []


def test_other_gh_failure_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                    captured: list[list[Any]]) -> None:
    monkeypatch.setattr(cli_cloud, "run_command", FakeGh((1, "", "gh: Bad credentials (HTTP 401)")))
    assert cli.main(["ingest", "git-ref"]) == 1
    assert "HTTP 401" in capsys.readouterr().err
    assert captured == []


def test_large_file_falls_back_to_raw(monkeypatch: pytest.MonkeyPatch, captured: list[list[Any]]) -> None:
    big = json.dumps({"type": "file", "encoding": "none", "size": 2_000_000, "content": ""})
    fake = FakeGh((0, big, ""), (0, CLOUD_LOG, ""))
    monkeypatch.setattr(cli_cloud, "run_command", fake)
    assert cli.main(["ingest", "git-ref", "--repo", "o/r", "--ref", "main", "--path", "a/b.log"]) == 0
    assert fake.calls[1][3] == "Accept: application/vnd.github.raw+json"
    assert fake.calls[1][4] == "repos/o/r/contents/a/b.log?ref=main"
    assert len(captured[0]) == 2


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_git_dir_reads_a_given_ref(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                   captured: list[list[Any]]) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")

    assert cli.main(["ingest", "git-ref", "--git-dir", str(repo)]) == 0  # branch missing
    assert "nothing to ingest" in capsys.readouterr().out

    _git(repo, "checkout", "-q", "-b", "claude/routine-reports")
    assert cli.main(["ingest", "git-ref", "--git-dir", str(repo)]) == 0  # file missing
    assert "nothing to ingest" in capsys.readouterr().out
    assert captured == []

    (repo / "reports").mkdir()
    (repo / "reports" / "routines.log").write_text(CLOUD_LOG)
    _git(repo, "add", "reports/routines.log")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "report")
    _git(repo, "checkout", "-q", "main")  # the working tree no longer has the file: git show reads the ref
    assert cli.main(["ingest", "git-ref", "--git-dir", str(repo)]) == 0
    assert "ingested 2 new" in capsys.readouterr().out
    assert [r.routine for r in captured[0]] == ["morning-briefing", "cloud-audit"]
    assert {r.source for r in captured[0]} == {"cloud"}
