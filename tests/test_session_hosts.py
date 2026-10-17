"""Mission 004C (backfill prep): rows stored before ingestion kept `meta.remote_host` are private (egress.py), and
`amem sessions hosts` re-derives the host from each repo's local .git/config (session_hosts.py; no network)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import cli, session_hosts
from agent_memory.integrations import egress
from codex_support import write_git_config
from conftest import base_env, run_amem

OWNER = {"team": False, "remote_owner": "VilmerFrost"}           # as stored before the host was kept


@pytest.fixture
def rows(pg_url: str, tmp_path: Path) -> Iterator[dict[str, Any]]:
    gh, gl, moved, later = (tmp_path / n for n in ("gh-repo", "gl-repo", "moved-repo", "later-repo"))
    write_git_config(gh, "git@github.com:vilmerfrost/gh-repo.git")
    write_git_config(gl, "https://gitlab.example.com/vilmerfrost/gl-repo.git")
    write_git_config(moved, "https://github.com/someone-else/moved-repo")
    write_git_config(later, "https://github.com/vilmerfrost/later-repo")
    (gh / "src").mkdir()
    gone = str(tmp_path / "removed-worktree" / "w1")
    metas = {
        "sh-gh": {**OWNER, "cwd": str(gh / "src")},                            # a subfolder finds the repo
        "sh-gl": {**OWNER, "cwd": str(gl)},                                    # the owner's name on another host
        "sh-moved": {**OWNER, "cwd": str(moved)},                              # the remote names another owner now
        "sh-cwds": {**OWNER, "cwd": gone, "cwds": [gone, str(later)]},         # cwd gone, a later cwd still there
        "sh-gone": {**OWNER, "cwd": gone, "cwds": [gone]},                     # nothing left on disk
        "sh-has": {**OWNER, "remote_host": "github.com", "cwd": str(gl)},     # not a candidate: has a host
        "sh-team": {"team": True, "remote_owner": "acme", "cwd": str(gh)},     # not a candidate: team
        "sh-other": {"team": False, "remote_owner": "old-handle", "cwd": str(gh)},   # not an owner account
    }
    with psycopg.connect(pg_url, autocommit=True) as conn:
        for sid, meta in metas.items():
            conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env, meta) "
                         "values (%s, 'chat', '/sh-test', 1, 'prod', %s)", (sid, Jsonb(meta)))
    yield metas
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("delete from sessions where project_dir = '/sh-test'")


def _meta(pg_url: str) -> dict[str, dict[str, Any]]:
    with psycopg.connect(pg_url) as conn:
        return {sid: m for sid, m in conn.execute("select session_id, meta from sessions where project_dir = '/sh-test'")}


def _mine(p: session_hosts.Plan) -> dict[str, tuple[str, str | None]]:
    return {r.session_id: (r.outcome, r.host) for r in p.rows if r.session_id.startswith("sh-")}


def test_rows_without_a_host_are_private_until_rederived(rows: dict[str, Any], pg_url: str) -> None:
    before = _meta(pg_url)
    assert {sid: egress.classify(m, ("vilmerfrost",)) for sid, m in before.items()} == {
        "sh-gh": "private", "sh-gl": "private", "sh-moved": "private", "sh-cwds": "private", "sh-gone": "private",
        "sh-has": "own", "sh-team": "team", "sh-other": "private"}
    ro = pg_url + ("&" if "?" in pg_url else "?") + "options=-c%20default_transaction_read_only%3Don"
    full = session_hosts.plan(db_url=ro, owners=("vilmerfrost",))                 # a dry run in a read-only session
    plan = session_hosts.Plan([r for r in full.rows if r.session_id.startswith("sh-")], full.owners)
    assert plan.counts() == {"candidates": 5, "rederived": 3, "owner-changed": 1, "no-remote": 1, "own_after": 2}
    assert _mine(plan) == {"sh-gh": ("rederived", "github.com"), "sh-gl": ("rederived", "gitlab.example.com"),
                           "sh-moved": ("owner-changed", None), "sh-cwds": ("rederived", "github.com"),
                           "sh-gone": ("no-remote", None)}
    assert _meta(pg_url) == before                                                  # nothing written
    assert session_hosts.apply(plan, db_url=pg_url) == 3
    after = _meta(pg_url)
    assert {sid: egress.classify(m, ("vilmerfrost",)) for sid, m in after.items()} == {
        "sh-gh": "own", "sh-gl": "private", "sh-moved": "private", "sh-cwds": "own", "sh-gone": "private",
        "sh-has": "own", "sh-team": "team", "sh-other": "private"}
    assert after["sh-gh"]["remote_host_source"] == "rederived" and "remote_host_source" not in after["sh-has"]
    assert _mine(session_hosts.plan(db_url=pg_url, owners=("vilmerfrost",))) == {
        "sh-moved": ("owner-changed", None), "sh-gone": ("no-remote", None)}         # applied rows are done
    assert session_hosts.apply(plan, db_url=pg_url) == 0                            # idempotent
    assert session_hosts.revert(db_url=pg_url) == 3
    assert _meta(pg_url) == before


def test_sessions_hosts_cli(rows: dict[str, Any], pg_url: str, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    assert cli.main(["sessions", "hosts"]) == 0
    out = capsys.readouterr().out
    assert "rederived" in out and "dry run: nothing written" in out
    assert cli.main(["sessions", "hosts", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True and doc["applied"] is None and doc["own_hosts"] == ["github.com"]
    assert {r["session_id"] for r in doc["rows"]} >= {"sh-gh", "sh-gl", "sh-moved", "sh-cwds", "sh-gone"}
    assert cli.main(["sessions", "hosts", "--apply", "--revert"]) == 2
    capsys.readouterr()
    assert cli.main(["sessions", "hosts", "--apply"]) == 0
    assert "applied: " in capsys.readouterr().out
    assert _meta(pg_url)["sh-gh"]["remote_host"] == "github.com"
    res = run_amem("sessions", "hosts", "--revert", "--json", env=base_env(AMEM_DB_URL=pg_url))
    assert res.returncode == 0 and json.loads(res.stdout)["reverted"] >= 3
    assert "remote_host" not in _meta(pg_url)["sh-gh"]
