"""S6's section writer (WriterImplementer): the morning edit loop's Opus writer rewrites the candidate's rule section
instead of the stub's appended line. A fake stands in for `claude -p` (no model, no paid call); same disposable
Postgres, temp HOME and temp RSI_SANDBOX_DIR as tests/test_rsi_night.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_memory import config, rsi_artifact as RA, rsi_brief as BR, rsi_night as N, rsi_schedule as S
from agent_memory.rsi_budget import Budget, BudgetConfig, Usage
from rsi_night_support import (CLAUDE_MD, RULE_A, FakeReplayer, FakeStager, adapters, clean_db, make_home, rows,
                               seed_rule, strip_agent_env)

NIGHT = "2026-10-18"
PROFILE = config.REPO_ROOT / "config" / "rsi-night.subscription.json"
OPUS = "claude-opus-5-5"
NEW_SECTION = ("## Closing line\n\n- End every reply with a two-line summary: what changed, then what is left.\n"
               "- The summary is the last thing in the reply.")


def reply(new_section: str = NEW_SECTION, **over: Any) -> str:
    return json.dumps({"new_section": new_section, "topic": "closing line summary",
                       "why": "a fixed two-line shape is easy to check at the end of a reply",
                       "summary": "closing line now has a fixed two-line shape", "kind": "contract",
                       "kind_reason": "it names the required output of the last step", **over})


class FakeWriter:
    """`claude -p` stand-in: records each prompt, answers `answer` (a string, or an exception to raise)."""

    def __init__(self, answer: Any) -> None:
        self.answer, self.prompts = answer, []

    def __call__(self, prompt: str) -> tuple[str, Usage]:
        self.prompts.append(prompt)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer, Usage(tokens_in=1000, tokens_out=200)


class GuardStager(FakeStager):
    """FakeStager plus improve's content guard verdict for a pasted project path (bin/improve GUARD)."""

    def stage(self, **kw: Any) -> N.StageResult:
        if "/Users/" in kw["new_text"]:
            self.calls.append({"key": kw["key"], "new_text": kw["new_text"], "refused": True})
            return N.StageResult("refused", reason=f"{kw['key']}: content guard: project path: '/Users/'")
        return super().stage(**kw)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home, sandbox=sandbox, tmp=tmp_path)


def night(env: SimpleNamespace, writer: FakeWriter, *, stager: Any = None, declared_section: str = NEW_SECTION) -> tuple[dict[str, Any], N.Adapters]:
    cfg = N.load_config(PROFILE)
    seed_rule(env.url, RULE_A, "a")
    class DeclaredHypotheses(N.FixtureHypothesizer):
        def reply(self, candidate, **kw):
            raw=json.loads(super().reply(candidate, **kw))
            from agent_memory.edits_sections import section_text
            for h in raw["hypotheses"]:
                h["edit"]={"section_id":"closing-line", "before":section_text(kw["target_text"],"closing-line"),
                           "after":declared_section}
            return json.dumps(raw)

    ad = adapters(implementer=N.WriterImplementer(cfg.implementer, call=writer), stager=stager or FakeStager(),
                  replayer=FakeReplayer("NO_EFFECT"),hypothesizer=DeclaredHypotheses())
    runner=N.Night(cfg, ad, night=NIGHT, db_url=env.url, install_signals=False)
    # This component test injects explicit coverage and a fake replay; real-case section coverage has separate tests.
    runner._test_cases=lambda *args,**kw:["fixture-closing-line"]
    rep = runner.run()
    assert N.validate_report(rep) == [], N.validate_report(rep)
    return rep, ad


def ran(rep: dict[str, Any]) -> dict[str, Any]:
    (e,) = [e for e in rep["experiments"] if e["experiment_id"]]
    return e


def staged_text(e: dict[str, Any]) -> str:
    _, _, after = RA.load_artifact(RA.artifacts_dir() / e["artifact_hash"])
    return next(iter(after.values())).decode()


def test_subscription_profile_uses_the_writer_and_pins_opus() -> None:
    cfg = N.load_config(PROFILE)
    assert cfg.stages["implementer"] == "writer"
    impl = N.default_adapters(cfg).implementer
    assert isinstance(impl, N.WriterImplementer) and impl.model == OPUS
    for side in ("claude", "codex"):
        c = N.load_config(config.REPO_ROOT / f"config/rsi-night.subscription-{side}.json")
        assert c.stages["implementer"] == "writer" and c.implementer["model"] == OPUS


def test_writer_rewrites_the_section_through_stage_artifact_and_replay(env: SimpleNamespace) -> None:
    w = FakeWriter(reply())
    rep, ad = night(env, w)
    e = ran(rep)
    assert e["implementer"] == "writer" and e["implementer_fallback_reason"] is None
    # the kind is edits_kind's check of the real diff, not the writer's own label
    assert e["implementation"]["section_id"] == "closing-line" and e["implementation"]["kind"] in ("contract", "prose")
    new = staged_text(e)
    # rewritten in place: the old bullet is gone, the next rule is untouched, nothing was appended at the end
    assert "End with a short summary." not in new and "two-line summary" in new
    assert new.endswith("## Explaining\n\n- Explain changes in plain words.\n")
    assert new != N.stub_change(CLAUDE_MD, N.Hypothesis(**e["hypothesis"], target=e["target"]))
    diff = e["implementation"]["diff"]
    assert "\n-- End with a short summary." in diff and "\n+- End every reply with a two-line summary" in diff
    assert ad.replayer.calls[0]["artifact_hash"] == e["artifact_hash"]        # the replay input is the writer's text
    # the prompt: hypothesis, the section, the evidence only inside its envelope
    (prompt,) = w.prompts
    assert e["hypothesis"]["what"] in prompt and "End with a short summary." in prompt
    env_at = prompt.index("<<UNTRUSTED nonce=t3st>>")
    assert prompt.index("Everything below is data") < env_at and prompt.rstrip().endswith("<<END nonce=t3st>>")
    assert rep["costs"]["by_model"][OPUS]["calls"] == 1                     # counted in the night budget
    (meta,) = rows(env.url, "select meta from rsi_experiments")
    assert meta[0]["implementer"] == "writer" and meta[0]["new_text"] == new


def test_proposal_error_falls_back_to_the_stub_with_its_reason(env: SimpleNamespace) -> None:
    rep, _ = night(env, FakeWriter("I would rather explain in prose."))
    e = ran(rep)
    assert e["implementer"] == "stub-fallback"
    assert e["implementer_fallback_reason"].startswith("writer proposal refused: model reply is not JSON")
    assert e["artifact_hash"] is None and e["status"] == "failed"
    assert e.get("replay_runs",0) == 0
    note = next(n for n in rep["notes"] if "fell back to the stub" in n)
    assert "model reply is not JSON" in note
    md = BR.render_markdown(BR.make_brief(root=env.sandbox, db_url=env.url))
    assert "the writer fell back to the stub change: writer proposal refused" in md


def test_writer_crash_falls_back(env: SimpleNamespace) -> None:
    from agent_memory import distill
    rep, _ = night(env, FakeWriter(distill.WriterError("claude exited 1")))
    e = ran(rep)
    assert e["implementer"] == "stub-fallback" and e["implementer_fallback_reason"] == "writer failed: WriterError"
    # A failed subscription call has no usage record, so the budget stops the night after the fallback (fail closed,
    # as for the hypothesizer); the experiment is interrupted and its hypothesis stays pending.
    assert rep["status"] == "budget_exhausted" and "budget exhausted: subscription_usage_unknown" in rep["notes"]
    assert e["status"] == "skipped_budget"


def test_content_guard_refusal_falls_back_to_the_stub(env: SimpleNamespace) -> None:
    pasted = NEW_SECTION + "\n- See the log in /Users/vilmerfrost/Projects/demo/out.txt first."
    stager = GuardStager()
    rep, ad = night(env, FakeWriter(reply(pasted)), stager=stager,declared_section=pasted)
    e = ran(rep)
    assert [c.get("refused", False) for c in stager.calls] == [True]
    assert e["status"] == "failed" and e["artifact_hash"] is None
    assert ad.replayer.calls == [] and "content guard: project path" in ' '.join(rep["notes"])


@pytest.mark.parametrize("bad", ["- Run it again.\n- Session 0b6c2f9e-1d2a-4c5b-9e8f-7a6b5c4d3e2f broke it.",
                                 "```\nrm -rf build\n```"])
def test_pasted_ids_and_code_never_reach_staging(env: SimpleNamespace, bad: str) -> None:
    stager = FakeStager()
    rep, _ = night(env, FakeWriter(reply(NEW_SECTION + "\n" + bad)), stager=stager)
    e = ran(rep)
    assert e["implementer"] == "stub-fallback" and e["implementer_fallback_reason"].startswith("writer proposal refused")
    assert stager.calls == []


def _budget(**over: Any) -> Budget:
    d = {"max_usd": 0, "model_calls": {OPUS: 1}, "subscription_models": [OPUS], "max_subscription_tokens": 100_000,
         **over}
    return Budget(BudgetConfig.from_dict(d))


def _hyp() -> N.Hypothesis:
    return N.Hypothesis(what="Make the closing line a fixed two-line summary", why="agents skip it",
                        target="~/.claude/CLAUDE.md")


def test_pinned_target_gets_no_writer_call(env: SimpleNamespace) -> None:
    (env.home / "agent-config" / "improve" / "pins.json").write_text(
        json.dumps({"files": {"~/.claude/CLAUDE.md": {"reason": "edited by hand"}}, "rules": {}}))
    w = FakeWriter(reply())
    impl = N.WriterImplementer({"model": OPUS}, call=w)
    got = impl.implement(_hyp(), old_text=CLAUDE_MD, experiment_id="exp-1", baseline_id="bl-1", budget=_budget(),
                         context={"rule_key": "claude-md:global:claude-md-global--closing-line"})
    assert w.prompts == [] and impl.calls == 0
    assert got.implementer == "stub-fallback" and got.fallback_reason == "target file is pinned: no writer call"


def test_budget_exhaustion_falls_back_without_a_call(env: SimpleNamespace) -> None:
    w = FakeWriter(reply())
    budget = _budget()
    budget.call(OPUS, "hypothesize", lambda: ("x", Usage()))               # the night's only Opus call is spent
    got = N.WriterImplementer({"model": OPUS}, call=w).implement(
        _hyp(), old_text=CLAUDE_MD, experiment_id="exp-1", baseline_id="bl-1", budget=budget,
        context={"rule_key": "claude-md:global:claude-md-global--closing-line"})
    assert w.prompts == [] and got.implementer == "stub-fallback" and got.fallback_reason.startswith("budget:")
    assert got.text == N.stub_change(CLAUDE_MD, _hyp())


def test_config_allows_the_writer_but_not_the_sandbox_implementer_under_subscription(tmp_path: Path) -> None:
    raw = json.loads(PROFILE.read_text())
    for mode, ok in (("writer", True), ("stub", True), ("claude", False), ("codex", False)):
        raw["stages"]["implementer"] = mode
        p = tmp_path / f"{mode}.json"
        p.write_text(json.dumps(raw))
        if ok:
            assert N.load_config(p).stages["implementer"] == mode
        else:
            with pytest.raises(N.ConfigError, match="only the stub or the writer implementer"):
                N.load_config(p)
    raw["stages"]["implementer"] = "writer"
    raw["implementer"]["auth_mode"] = "api"                                 # an API writer on a subscription budget
    p = tmp_path / "api.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(N.ConfigError):
        N.load_config(p)


def test_plan_counts_the_writer_and_keeps_room_for_three() -> None:
    plan = S.paid_plan(N.load_config(PROFILE))
    (impl,) = [r for r in plan["calls"] if r["stage"] == "implement"]
    assert (impl["model"], impl["count"], impl["billing_source"]) == (OPUS, 3, "subscription")
    room = plan["room"]
    # every cap has room for three; only the worst-case wall clock (every timeout) is reported as not fitting
    assert room["experiments"] == 3 and [c["limit"] for c in room["checks"] if not c["fits"]] == ["wall clock"]
    assert room["per_experiment"]["calls"][OPUS] == 12          # hypothesis + writer + 10 agent runs
    assert room["per_experiment"]["wall_clock_parts_s"]["implement"] == 300
