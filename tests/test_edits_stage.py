"""`amem edits run` in RSI V1 stage mode (AMEM_EDITS_MODE=stage): a baseline, `improve stage`, a proposal row,
never `improve apply`, and the production file stays byte-identical. Fake improve; throwaway database."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import edits
from test_daily_support import DailyIO, Push, daily_io  # noqa: F401
from test_edits_support import (  # noqa: F401
    SKILL_MD,
    SPAWN_NEW,
    EHome,
    edit_rows,
    ehome,
    envelope,
    ewriter,
    proposal,
    seed,
    sha_of,
)
from test_rsi_approval import make_artifact
from test_scoring_support import ScoringDb, sdb  # noqa: F401

NEW_SKILL = SKILL_MD.replace("Each brief names the branch.\n",
                             "Each brief names the branch on its first line, before the task text.\n")
KEY = "~/.agents/skills/dispatch/SKILL.md"


@pytest.fixture
def staged(sdb: ScoringDb, ehome: EHome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:  # noqa: F811
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("HOME", str(ehome.root))
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.setenv(edits.LIVE_ENV, "1")
    monkeypatch.setenv(edits.STAGE_ENV, "stage")
    with psycopg.connect(sdb.url) as conn:
        conn.execute("set session_replication_role = replica")
        for t in ("rsi_decisions", "rsi_decision_tokens", "rsi_eval_results", "rsi_proposals", "rsi_artifacts"):
            conn.execute(f"delete from {t}")
    return sandbox


def test_stage_mode_stages_a_proposal_and_never_applies(sdb: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                        daily_io: DailyIO, staged: Path) -> None:  # noqa: F811
    daily_io.use_push(Push(status=200))
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", sha=sha_of(ehome.skill), n=3)
    before = ehome.skill.read_bytes()
    h, adir = make_artifact(staged, [(KEY, before, NEW_SKILL.encode())], exp="exp-20261016-edit-00000000",
                            suites=("static",))
    ehome.improve.queue("stage", {"stdout": "improve stage: static checks\n" + json.dumps(
        {"cmd": "stage", "status": "staged", "artifact_hash": h, "artifact_dir": str(adir)}) + "\n", "exit": 0})
    ewriter(envelope(proposal(SPAWN_NEW)))
    r = edits.run(db_url=sdb.url, notify={"NTFY_TOPIC": "t", "NTFY_SERVER": "https://ntfy.example.test"})
    (o,) = r.outcomes
    assert o.status == "staged" and "awaiting a human decision" in o.reason, o.line()
    assert "staged=1" in r.summary()
    assert ehome.skill.read_bytes() == before                         # production untouched
    (call,) = ehome.improve.calls()                                   # stage only: never apply
    assert call[0] == "stage"
    opt = {call[i]: call[i + 1] for i in range(1, len(call) - 1) if call[i].startswith("--") and not call[i + 1].startswith("--")}
    assert opt["--change"].startswith(KEY + "=") and opt["--rule-id"] == f"{KEY}=skill-dispatch--spawn"
    bdir = Path(opt["--baseline"])
    assert bdir.parent == staged / "baselines" and (bdir / "files" / KEY[2:]).read_bytes() == before
    (row,) = edit_rows(sdb.url)
    assert row["status"] == "staged" and row["meta"]["artifact_hash"] == h and row["meta"]["baseline_id"] == bdir.name
    with psycopg.connect(sdb.url) as conn:
        p = conn.execute("select id::text, status, targets from rsi_proposals").fetchall()
    assert p == [(row["meta"]["proposal_id"], "staged", [KEY])]


def test_stage_refusal_records_refused_and_no_proposal(sdb: ScoringDb, ehome: EHome, ewriter: Any,  # noqa: F811
                                                        daily_io: DailyIO, staged: Path) -> None:  # noqa: F811
    daily_io.use_push(Push(status=200))
    seed(sdb.url, "skill:dispatch", "spawn", "4. Spawn", sha=sha_of(ehome.skill), n=3)
    before = ehome.skill.read_bytes()
    ehome.improve.queue("stage", {"stdout": json.dumps({"cmd": "stage", "status": "refused",
                                                        "reason": "outside allowed_targets"}) + "\n", "exit": 2})
    ewriter(envelope(proposal(SPAWN_NEW)))
    r = edits.run(db_url=sdb.url)
    (o,) = r.outcomes
    assert o.status == "refused" and "outside allowed_targets" in o.reason
    assert ehome.skill.read_bytes() == before and [c[0] for c in ehome.improve.calls()] == ["stage"]
    assert [x["status"] for x in edit_rows(sdb.url)] == ["refused"]
    with psycopg.connect(sdb.url) as conn:
        assert conn.execute("select count(*) from rsi_proposals").fetchone() == (0,)
