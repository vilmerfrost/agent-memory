"""`amem rsi night` with the REAL rsi_evidence_pack (w-evidence, contract 2.3): delta_since on the disposable
Postgres, build_pack / render_for_model for the selected candidate. Skips until rsi_evidence_pack is on the branch
(it lands in the rsi/m005 merge). Providers are not configured in tests (every AMEM_* variable is scrubbed), so
ClickHouse / Langfuse / past.dev answer unavailable and the night continues on Supabase evidence alone.
Replay and staging are the fakes of tests/rsi_night_support.py."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_memory import rsi_night as N
from rsi_night_support import (KEY_A, KEY_B, RULE_A, RULE_B, adapters, candidate, clean_db, make_home, seed_rule,
                               strip_agent_env)

EP = pytest.importorskip("agent_memory.rsi_evidence_pack")


class SettledEvidence(N.PackEvidence):
    """The real module; only the 60 s settle window is off, so rows written by the test count at once."""

    def __init__(self) -> None:
        self.since_calls: list[str | None] = []
        self.rendered: list[str] = []

    def delta(self, since: str | None, *, conn: Any) -> Any:
        self.since_calls.append(since)
        return EP.delta_since(since, conn=conn, limits=EP.DeltaLimits(settle_s=0))

    def render(self, pack: Any) -> str:
        text = super().render(pack)
        self.rendered.append(text)
        return text


class RecordingHypothesizer(N.FixtureHypothesizer):
    def __init__(self) -> None:
        super().__init__()
        self.evidence: list[str] = []

    def reply(self, candidate: Any, *, evidence_text: str, **kw: Any) -> str:
        self.evidence.append(evidence_text)
        return super().reply(candidate, evidence_text=evidence_text, **kw)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.setenv("AMEM_DB_URL", pg_url)          # build_pack's Supabase retriever connects through store
    strip_agent_env(monkeypatch)
    make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, sandbox=sandbox, tmp=tmp_path)


def test_two_nights_with_the_real_delta_and_evidence_pack(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    seed_rule(env.url, RULE_B, "b")
    ev, hyp = SettledEvidence(), RecordingHypothesizer()
    r1 = N.run_night(night="2026-10-18", db_url=env.url, adapters=adapters(evidence=ev, hypothesizer=hyp))
    assert N.validate_report(r1) == [], N.validate_report(r1)
    assert r1["status"] in ("ok", "partial") and r1["checkpoint"]["advanced"], r1["notes"]
    assert (r1["delta"]["sessions"], r1["delta"]["deviations"]) == (5, 5)
    assert {p["key"] for p in r1["patterns"]} == {KEY_A, KEY_B}
    assert r1["experiments"][0]["status"] == "staged" and r1["health"]["supabase"] == "ok"
    assert hyp.evidence and EP.BEGIN in hyp.evidence[0] and hyp.evidence[0] == ev.rendered[0]
    seed_rule(env.url, RULE_B, "b2", sessions=1)
    r2 = N.run_night(night="2026-10-19", db_url=env.url, adapters=adapters(evidence=ev, hypothesizer=hyp))
    assert N.validate_report(r2) == [] and ev.since_calls == [None, r1["checkpoint"]["after"]]
    assert (r2["delta"]["sessions"], r2["delta"]["deviations"]) == (1, 1)
    assert [p["key"] for p in r2["patterns"]] == [KEY_B]
    assert candidate(env.url, KEY_B)["seen_count"] == 2 and candidate(env.url, KEY_A)["seen_count"] == 1
