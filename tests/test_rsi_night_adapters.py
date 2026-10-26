"""The default adapters of `amem rsi night` against stand-ins of their sibling modules (Mission 005): PackEvidence
(rsi_evidence_pack, contract 2.3), SandboxReplayer (rsi_replay, contract 2.2), SandboxImplementer (the live
Claude / Codex implementer inside sandbox/rsi_sandbox.py: unverified live, its wiring is checked here with a fake
sandbox) and the stage selection from the config. No docker, no model, no database."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_memory
from agent_memory import rsi_night as N
from agent_memory.rsi_budget import Budget, BudgetConfig


def budget(**kw: Any) -> Budget:
    return Budget(BudgetConfig.from_dict({"max_usd": 10.0, "model_calls": {"opus": 2, "codex": 2}, **kw}))


def test_pack_evidence_delegates_to_rsi_evidence_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    fake = SimpleNamespace(
        delta_since=lambda since, *, conn, **kw: seen.setdefault("delta", (since, conn, kw)) and "DELTA",
        default_retrievers=lambda connect=None: {"supabase": "SB"},
        build_pack=lambda cand, **kw: seen.setdefault("pack", (dict(cand), kw)) and "PACK",
        render_for_model=lambda pack: f"rendered:{pack}")
    monkeypatch.setitem(__import__("sys").modules, "agent_memory.rsi_evidence_pack", fake)
    monkeypatch.setattr(agent_memory, "rsi_evidence_pack", fake, raising=False)
    ev = N.PackEvidence()
    assert ev.delta("2026-10-18T00:00:00+00:00", conn="CONN") == "DELTA"
    assert seen["delta"] == ("2026-10-18T00:00:00+00:00", "CONN", {"retrievers": {"supabase": "SB"}})
    assert ev.pack({"dedupe_key": "k"}) == "PACK" and ev.render("PACK") == "rendered:PACK"
    assert seen["pack"] == ({"dedupe_key": "k"}, {"current_instruction_sha256": None})


@dataclass
class FakeVerdict:
    verdict: str
    reasons: tuple[str, ...]
    inputs: dict = field(default_factory=dict)


@dataclass
class FakeReplayConfig:
    agent: str = "stub"
    model: str | None = None
    effort: str | None = None
    reps: int = 1
    judge: str = "stub"
    jev: str = "fixture"
    credentials: str = "none"
    auth_mode: str = "api"
    judge_auth_mode: str = "api"
    judge_model: str | None = None
    judge_effort: str | None = None


def test_sandbox_replayer_maps_the_replay_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path))
    calls: dict[str, Any] = {}

    def run_replay(h: str, cases: list, cfg: Any, *, root: Path, experiment_id: str, budget: Any,
                   baseline_id: str | None = None) -> Any:
        calls.update(h=h, cases=cases, cfg=cfg, root=root, experiment_id=experiment_id, budget=budget,
                     baseline_id=baseline_id)
        return SimpleNamespace(verdict=FakeVerdict("NO_EFFECT", ("delta 0.00 below 0.10",)),
                               arms={"A": {"pass": 2}, "B": {"pass": 2}}, runs=[1, 2, 3, 4], artifact_hash=h,
                               baseline_id=baseline_id, eval_path=tmp_path / "e.json", budget_stop=None)
    cases = [SimpleNamespace(id=f"case-{i}", title=f"Task {i}") for i in range(1, 4)]
    fake = SimpleNamespace(load_cases=lambda ids=None: cases, ReplayConfig=FakeReplayConfig,
                           run_replay=run_replay)
    monkeypatch.setitem(__import__("sys").modules, "agent_memory.rsi_replay", fake)
    monkeypatch.setattr(agent_memory, "rsi_replay", fake, raising=False)
    cfg = N.load_config()
    b = budget()
    out = N.SandboxReplayer(cfg.stages, cfg.replay).replay(artifact_hash="a" * 64, experiment_id="exp-20261018-1",
                                                          baseline_id="bl-1", budget=b)
    assert out == N.ReplayOutcome("NO_EFFECT", ["delta 0.00 below 0.10"], {"A": {"pass": 2}, "B": {"pass": 2}},
                                  "a" * 64, "bl-1", str(tmp_path / "e.json"), None, 4,
                                  setup={"agent": "stub", "model": None, "effort": None, "auth_mode": "api",
                                         "judge": "stub", "judge_model": None, "judge_effort": None,
                                         "judge_auth_mode": "api", "tasks": [{"id": c.id, "title": c.title} for c in cases]})
    assert calls["cases"] == cases and calls["budget"] is b and calls["root"] == tmp_path
    assert calls["cfg"] == FakeReplayConfig()                                      # stub agent, stub judge, fixture Jev
    assert (calls["experiment_id"], calls["baseline_id"]) == ("exp-20261018-1", "bl-1")


class FakeBox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.prepared: list[tuple] = []
        self.ran: list[dict[str, Any]] = []

    def prepare_arm(self, experiment: str, baseline: str, arm: str, *, force: bool = False) -> dict:
        self.prepared.append((experiment, baseline, arm, force))
        p = self.arm_home(experiment, arm) / ".claude" / "CLAUDE.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# old\n")
        return {}

    def arm_home(self, experiment: str, arm: str) -> Path:
        return self.root / "experiments" / experiment / f"home-{arm}"

    def run(self, experiment: str, arm: str, command: list[str], **kw: Any) -> Any:
        self.ran.append({"command": command, **kw})
        (self.arm_home(experiment, arm) / ".claude" / "CLAUDE.md").write_text("# old\n\n- new rule.\n")
        out = json.dumps({"type": "result", "result": "done", "usage": {"input_tokens": 50, "output_tokens": 9},
                          "total_cost_usd": 0.04})
        return SimpleNamespace(exit_code=0, timed_out=False, stdout=out)


@pytest.mark.parametrize("agent,model,creds", [("claude", "opus", "keychain:rsi-sandbox/anthropic"),
                                               ("codex", "codex", "keychain:rsi-sandbox/openai")])
def test_sandbox_implementer_runs_the_agent_inside_the_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                               agent: str, model: str, creds: str) -> None:
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path))
    box = FakeBox(tmp_path)
    real = N.sandbox_module()
    monkeypatch.setattr(N, "sandbox_module", lambda: SimpleNamespace(Sandbox=lambda root: box, _home_rel=real._home_rel))
    h = N.Hypothesis("State the closing line as a checklist item", "agents skip it", "~/.claude/CLAUDE.md")
    b = budget()
    new = N.SandboxImplementer(agent, {"timeout_s": 120}).implement(h, old_text="# old\n", experiment_id="exp-20261018-1",
                                                                    baseline_id="bl-1", budget=b)
    assert new == "# old\n\n- new rule.\n"
    assert box.prepared == [("exp-20261018-1", "bl-1", "I", True)]
    (ran,) = box.ran
    assert ran["credentials"] == creds and ran["timeout_s"] == 120 and ran["run_id"] == "exp-20261018-1-implement"
    assert ran["command"][0] == agent and any("State the closing line" in a for a in ran["command"])
    assert b.costs()["by_model"][model]["calls"] == 1
    if agent == "claude":
        assert b.costs()["by_model"]["opus"]["usd"] == 0.04 and "--allowedTools" in ran["command"]
        assert b.costs()["model_bases"]["opus"] == {"reported": 0.04}
    else:                    # Codex reports no cost: booked as an estimate, never as provider-reported
        assert "reported" not in b.costs()["model_bases"]["codex"] and b.costs()["reported_usd"] == 0.0


def test_default_adapters_follow_the_config(tmp_path: Path) -> None:
    cfg = N.load_config()
    ad = N.default_adapters(cfg)
    assert isinstance(ad.hypothesizer, N.FixtureHypothesizer) and isinstance(ad.implementer, N.StubImplementer)
    assert isinstance(ad.triage, N.JevTriage) and ad.triage.mode == "fixture"
    assert isinstance(ad.stager, N.ImproveStager) and isinstance(ad.replayer, N.SandboxReplayer)
    d = json.loads(N.CONFIG_PATH.read_text())
    d["stages"].update(hypothesizer="claude-cli", implementer="codex", triage="live")
    p = tmp_path / "live.json"
    p.write_text(json.dumps(d))
    ad = N.default_adapters(N.load_config(p))
    assert isinstance(ad.hypothesizer, N.ClaudeCliHypothesizer) and isinstance(ad.implementer, N.SandboxImplementer)
    assert ad.implementer.agent == "codex" and ad.triage.mode == "live"
    d["stages"]["implementer"] = "gpt"
    p.write_text(json.dumps(d))
    with pytest.raises(N.ConfigError):
        N.load_config(p)
