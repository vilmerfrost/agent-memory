"""Night Desk fields in the night report (2026-10-28): partial-credit arm totals (score_sum, scenarios_passed,
scenarios_total), the rule the judge saw (judge_rule) with its prompt version, and the night's sandbox runs used and
released (costs.sandbox_runs_used / sandbox_runs_released).

The night tests use the same disposable Postgres, temp HOME and fakes as test_rsi_night.py. The replayer fake builds
its arms with rsi_replay.arm_scores and its rule with rsi_replay.judge_rule, and shapes provider views exactly as
rsi_provider_replay does. tests/data/rsi_night_report_fields/report.json is one such night, kept for Night Desk.
Regenerate it by deleting it (or with AMEM_WRITE_REPORT_FIXTURE=1) and running this file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rsi_night_support import (RULE_A, RULE_B, FakeReplayer, adapters, clean_db, make_home, rows, seed_rule,
                               strip_agent_env, write_config)
from rsi_replay_support import setup_root
from test_rsi_replay import replay as stub_replay

from agent_memory import rsi_brief as B
from agent_memory import rsi_night as N
from agent_memory import rsi_provider_replay as PR
from agent_memory import rsi_replay as R
from agent_memory import rsi_verdict as V
from agent_memory.rsi_budget import Budget, BudgetConfig

NIGHT = "2026-10-28"
FIXTURE = Path(__file__).parent / "data" / "rsi_night_report_fields" / "report.json"
SCORE_KEYS = ("score_sum", "scenarios_passed", "scenarios_total")
EDIT = {"path": "~/.claude/CLAUDE.md", "section_id": "closing-line", "heading": "Closing line",
        "before": "## Closing line\n\n- End with a short summary.",
        "after": "## Closing line\n\n- End every non-trivial task with a section named Skills, Plugins or MCPs used.\n"
                 "- List only integrations actually used. If none were used, write: None"}


def run(arm: str, rep: int, passed: bool | None, *, score: float | None = None,
        scenarios: dict | None = None) -> R.RunRecord:
    return R.RunRecord(run_id=f"r-{arm}{rep}", arm=arm, case="c", rep=rep, check_passed=passed, check_score=score,
                       check_scenarios=scenarios or {})


# ---------------------------------------------------------------- arm totals


def test_arm_scores_sum_partial_credit_and_scenarios():
    runs = [run("B", 0, False, score=0.5, scenarios={"passed": 2, "total": 4}),
            run("B", 1, True, score=1.0, scenarios={"passed": 4, "total": 4}),
            run("B", 2, False, scenarios={"passed": 1, "total": 3}),          # score from scenarios: 1/3
            run("B", 3, None)]                                                # not checked: left out
    got = R.arm_scores(runs)
    assert got == {"score_sum": round(0.5 + 1.0 + 1 / 3, 3), "scenarios_passed": 7, "scenarios_total": 11}
    assert got["score_sum"] == round(sum(V.run_check_score(r.check_passed, r.check_score, r.check_scenarios or None)
                                         for r in runs[:3]), 3)


def test_arm_scores_count_a_binary_check_as_one_scenario():
    got = R.arm_scores([run("A", 0, True), run("A", 1, False), run("A", 2, False, scenarios={"passed": 0, "total": 0})])
    assert got == {"score_sum": 1.0, "scenarios_passed": 1, "scenarios_total": 3}


def test_arm_scores_are_null_without_checked_runs():
    assert R.arm_scores([]) == R.arm_scores([run("A", 0, None)]) == dict.fromkeys(SCORE_KEYS)


def test_a_real_stub_replay_reports_arm_totals_and_no_rule_for_a_legacy_artifact(tmp_path, monkeypatch):
    root, _, h = setup_root(tmp_path, monkeypatch, "closing-line")
    res, _ = stub_replay(root, h, ["report-todos"])
    assert res.verdict.verdict == "IMPROVES"
    a, b = res.arms["A"], res.arms["B"]
    assert (a["checked"], b["checked"]) == (3, 3)
    assert (a["score_sum"], a["scenarios_passed"], a["scenarios_total"]) == (0.0, 0, 3)
    assert (b["score_sum"], b["scenarios_passed"], b["scenarios_total"]) == (3.0, 3, 3)
    # The synthetic candidate has no declared section edit: the judge saw no rule.
    assert res.judge_rule is None and res.judge_prompt_version == R.JUDGE_PROMPT_VERSION
    rec = json.loads((root / "artifacts" / h / "evals" / "sandbox-replay.json").read_text())["results"]
    assert rec["judge_rule"] is None and rec["arms"]["B"]["scenarios_total"] == 3
    out = json.loads((res.evidence_dir / "replay.json").read_text())
    assert out["judge_rule"] is None and out["judge_prompt_version"] == "2" and out["arms"]["A"]["score_sum"] == 0.0


# ---------------------------------------------------------------- the rule the judge saw


def test_judge_rule_is_exactly_the_text_in_the_judge_prompt():
    rule = R.judge_rule(EDIT)
    assert rule == {"section_id": "closing-line", "heading": "Closing line", "text": EDIT["after"]}
    p = R.judge_prompt("Fix it", {"check_passed": True, "final": "", "diff": ""},
                       {"check_passed": False, "final": "", "diff": ""}, EDIT)
    assert f"Section: {rule['heading']}\n{rule['text']}\n" in p


def test_judge_rule_is_capped_like_the_prompt_and_null_for_legacy():
    long = {**EDIT, "after": "x" * (R.MAX_PROMPT_RULE + 50)}
    assert len(R.judge_rule(long)["text"]) == R.MAX_PROMPT_RULE
    assert R.judge_rule(None) is None and R.judge_rule({"section_id": "s", "after": "  "}) is None
    assert R.judge_rule({"section_id": "s", "after": "rule"})["heading"] == "s"     # heading falls back to the id


def test_report_judge_rule_redacts_and_rejects_bad_records():
    rule = N.report_judge_rule({"section_id": "a", "heading": "A", "text": "use key sk-abcdefghijklmnopqrstuvwx now"})
    assert rule["section_id"] == "a" and "sk-abcdefghijklmnop" not in rule["text"] and "[redacted]" in rule["text"]
    assert N.report_judge_rule(None) is None and N.report_judge_rule({"text": ""}) is None
    assert N.report_judge_rule("rule") is None


def test_report_provider_results_fill_legacy_views_with_nulls():
    legacy = {"provider": "claude", "verdict": "WORSE", "arms": {"A": {"checked": 3, "check_pass_rate": 0.0},
                                                                 "B": {"checked": 3, "check_pass_rate": 0.33}}}
    (v,) = N.report_provider_results([legacy])
    assert v["judge_rule"] is None and v["judge_prompt_version"] is None
    assert all(v["arms"][arm][k] is None for arm in "AB" for k in SCORE_KEYS)
    assert v["arms"]["A"]["checked"] == 3                                         # nothing else changes
    assert N.report_arms({}) == {"A": dict.fromkeys(SCORE_KEYS), "B": dict.fromkeys(SCORE_KEYS)}
    assert N.report_arms({"A": {"score_sum": 2.5}})["A"]["score_sum"] == 2.5


# ---------------------------------------------------------------- sandbox runs used and released


def test_costs_count_used_and_released_runs_once():
    b = Budget(BudgetConfig(max_sandbox_runs=20))
    ids = [f"run-{i}" for i in range(6)]
    for rid in ids:
        b.take_sandbox_run(rid)
    for rid in ids[:3]:
        b.record_run(rid, duration_s=1.0, model="stub")
    b.release_sandbox_runs(ids[3:5], reason="subscription agent used fewer tool containers")
    b.release_sandbox_runs(ids[5:], reason="judge skipped")
    c = b.costs()
    assert (c["sandbox_runs_used"], c["sandbox_runs_released"], c["sandbox_runs"]) == (3, 3, 3)
    assert c["sandbox_releases"]["released_runs"] == c["accounting"]["sandbox_runs_released"] == 3
    fresh = Budget(BudgetConfig()).costs()
    assert (fresh["sandbox_runs_used"], fresh["sandbox_runs_released"]) == (0, 0)


def test_preflight_failed_report_has_zero_used_and_released(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    rep = N.run_night(night=NIGHT, config_path=bad, dry_run=True)
    assert rep["status"] == "preflight_failed"
    assert (rep["costs"]["sandbox_runs_used"], rep["costs"]["sandbox_runs_released"]) == (0, 0)


# ---------------------------------------------------------------- a stub night


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    home = make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    seed_rule(pg_url, RULE_A, "a")
    seed_rule(pg_url, RULE_B, "b")
    cfg = write_config(tmp_path, max_experiments=2, max_sandbox_runs=200)
    return SimpleNamespace(url=pg_url, home=home, sandbox=sandbox, tmp=tmp_path, cfg=cfg)


def night(env: SimpleNamespace, replayer: Any) -> dict[str, Any]:
    return N.run_night(night=NIGHT, db_url=env.url, config_path=env.cfg, adapters=adapters(replayer=replayer))


# Per provider, per arm: (check_passed, check_score, scenarios) of five checked runs, as a 5+5 subscription night
# records them from check_entry.py.
RUNS = {
    "claude": {"A": [(False, 0.5, (2, 4)), (False, 0.25, (1, 4)), (True, 1.0, (4, 4)), (False, 0.5, (2, 4)),
                     (False, 0.0, (0, 4))],
               "B": [(True, 1.0, (4, 4)), (False, 0.75, (3, 4)), (True, 1.0, (4, 4)), (False, 0.75, (3, 4)),
                     (True, 1.0, (4, 4))]},
    "codex": {"A": [(True, 1.0, (3, 3)), (False, 2 / 3, (2, 3)), (True, 1.0, (3, 3)), (False, 1 / 3, (1, 3)),
                    (True, 1.0, (3, 3))],
              "B": [(True, 1.0, (3, 3)), (True, 1.0, (3, 3)), (False, 2 / 3, (2, 3)), (True, 1.0, (3, 3)),
                    (True, 1.0, (3, 3))]},
}


class ScoredReplayer(FakeReplayer):
    """Experiment 1 tests a Claude-only file (one provider: the experiment carries that provider's arms), experiment
    2 a shared rule (claude and codex: never pooled, so the experiment's own arms stay empty). Each provider takes
    its 10 agent runs plus 4 tool containers per agent run on the budget, runs one tool container per agent run and
    gives the other 3 back, as a subscription agent does."""

    def replay(self, *, artifact_hash: str, experiment_id: str, baseline_id: str, budget: Budget) -> N.ReplayOutcome:
        out = super().replay(artifact_hash=artifact_hash, experiment_id=experiment_id, baseline_id=baseline_id,
                             budget=budget)
        providers = ["claude"] if len(self.calls) == 1 else ["claude", "codex"]
        views = []
        for p in providers:
            runs = []
            for arm in "AB":
                for rep, (passed, score, (sp, st)) in enumerate(RUNS[p][arm]):
                    rid = f"{experiment_id}-{p}-{arm}-r{rep}"
                    tools = [f"{rid}-t{i}" for i in range(4)]
                    for x in (rid, *tools):
                        budget.take_sandbox_run(x)
                    budget.record_run(rid, duration_s=30.0, model="stub", exit_code=0)
                    budget.record_run(tools[0], duration_s=2.0)
                    budget.release_sandbox_runs(tools[1:], reason="subscription agent: unused tool containers")
                    runs.append(R.RunRecord(run_id=rid, arm=arm, case="worker-final-report", rep=rep,
                                            check_passed=passed, check_score=score,
                                            check_scenarios={"passed": sp, "total": st}))
            arms = {arm: {"runs": 5, "checked": 5,
                          "check_pass_rate": round(sum(r.check_passed for r in runs if r.arm == arm) / 5, 4),
                          "errors": 0, "timeouts": 0, **R.arm_scores([r for r in runs if r.arm == arm]),
                          "home": "baseline" if arm == "A" else f"baseline + artifact {artifact_hash[:12]}"}
                    for arm in "AB"}
            views.append({"provider": p, "verdict": out.verdict, "reasons": [f"{p}: fake replay"], "arms": arms,
                          "test_setup": {"agent": p, "tasks": [{"id": "worker-final-report",
                                                                "title": "Write the worker's final report"}]},
                          "relevance": {}, "judge_pairs": [], "eval_path": out.eval_path, "judge": None, "jev": None,
                          "judge_rule": R.judge_rule(EDIT), "judge_prompt_version": R.JUDGE_PROMPT_VERSION,
                          # the replay record's own count: the report must not add it to the ledger's
                          "sandbox_reservations_released": 30})
        out.provider_results = views
        out.runs = 10 * len(views)
        out.arms = views[0]["arms"] if len(views) == 1 else {}
        out.judge_rule = views[0]["judge_rule"]
        out.judge_prompt_version = views[0]["judge_prompt_version"]
        return out


def test_stub_night_report_carries_every_new_field(env: SimpleNamespace) -> None:
    rep = night(env, ScoredReplayer("NO_EFFECT", runs=0))
    assert N.validate_report(rep) == [], N.validate_report(rep)
    assert B.validate_report(json.loads(json.dumps(rep, default=str))) == []
    exps = [e for e in rep["experiments"] if e.get("replay_runs") is not None]
    assert len(exps) == 2
    single, shared = exps
    claude_b = single["provider_results"][0]["arms"]["B"]
    assert (claude_b["score_sum"], claude_b["scenarios_passed"], claude_b["scenarios_total"]) == (4.5, 18, 20)
    assert single["arms"]["B"]["score_sum"] == 4.5 and single["arms"]["A"]["scenarios_passed"] == 9
    assert all(shared["arms"][a][k] is None for a in "AB" for k in SCORE_KEYS)     # providers are never pooled
    codex = next(v for v in shared["provider_results"] if v["provider"] == "codex")
    assert (codex["arms"]["A"]["score_sum"], codex["arms"]["A"]["scenarios_passed"]) == (4.0, 12)
    for e in exps:
        assert e["judge_rule"] == R.judge_rule(EDIT) and e["judge_prompt_version"] == "2"
        assert all(v["judge_rule"] == e["judge_rule"] for v in e["provider_results"])
    # 3 provider replays x 10 agent runs x 5 containers: 60 ran (agent + one tool), 90 given back, each once.
    co = rep["costs"]
    assert (co["sandbox_runs_used"], co["sandbox_runs_released"]) == (60, 90)
    assert co["sandbox_runs_released"] == co["sandbox_releases"]["released_runs"] == co["accounting"]["sandbox_runs_released"]
    ((meta,),) = rows(env.url, "select meta from rsi_experiments where id = %s", single["experiment_id"])
    assert meta["judge_rule"] == single["judge_rule"] and meta["arms"]["B"]["scenarios_total"] == 20
    md = B.render_markdown(B.make_brief(night=NIGHT, root=env.sandbox, db_url=env.url))
    assert "(5 checked, score 9 of 20 scenarios)" in md and "(5 checked, score 18 of 20 scenarios)" in md
    assert "score 12 of 15 scenarios" in md
    if os.environ.get("AMEM_WRITE_REPORT_FIXTURE") == "1" or not FIXTURE.exists():
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(rep, indent=1, sort_keys=True, default=str)
        for tmp in {os.path.realpath(env.sandbox), str(env.sandbox)}:      # no pytest temp paths in the fixture
            text = text.replace(tmp, "/sandbox")
        FIXTURE.write_text(text + "\n")


def test_legacy_replay_outcome_gives_nulls_in_the_night_report(env: SimpleNamespace) -> None:
    rep = night(env, FakeReplayer("NO_EFFECT"))
    assert N.validate_report(rep) == []
    e = next(x for x in rep["experiments"] if x.get("replay_runs") is not None)
    assert e["judge_rule"] is None and e["judge_prompt_version"] is None
    assert all(e["arms"][a][k] is None for a in "AB" for k in SCORE_KEYS)
    assert e["arms"]["A"]["pass_rate"] == 0.5                                      # the old fields stay
    assert (rep["costs"]["sandbox_runs_used"], rep["costs"]["sandbox_runs_released"]) == (8, 0)
    md = B.render_markdown(B.make_brief(night=NIGHT, root=env.sandbox, db_url=env.url))
    assert "scenarios" not in md


def test_provider_replay_carries_rule_and_version_per_provider(tmp_path, monkeypatch):
    outcomes = {p: N.ReplayOutcome("NO_EFFECT", [], {"A": {"score_sum": 1.0}, "B": {"score_sum": 2.0}}, "h", "bl",
                                   f"/e/{p}.json", runs=10, judge_rule=R.judge_rule(EDIT) if p == "codex" else None,
                                   judge_prompt_version="2")
                for p in ("claude", "codex")}
    recorded = {}

    class Box:
        def __init__(self, root):
            self.artifacts = Path(root) / "artifacts"

        def record_eval(self, **kw):
            recorded.update(kw)
            return Path("/e/combined.json")

    class Stage:
        def __init__(self, stages, cfg):
            self.provider = cfg["profile"]

        def replay(self, **kw):
            return outcomes[self.provider]

    rs = SimpleNamespace(Sandbox=Box, load_artifact=lambda d: ({"files": [{"path": "~/agent-config/x.md"}]}, "h", None),
                         SandboxError=RuntimeError)
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path))
    monkeypatch.setattr(R, "sandbox_module", lambda: rs)
    monkeypatch.setattr(PR, "_replayer", lambda N_: Stage)
    budget = SimpleNamespace(cfg=SimpleNamespace(max_ab_pairs=10))
    out = PR.replay({}, {"provider_profiles": {"claude": {}, "codex": {}}, "cases": ["fix-add"], "reps": 1},
                    artifact_hash="h", experiment_id="exp-1", baseline_id="bl", budget=budget)
    views = {v["provider"]: v for v in out.provider_results}
    assert views["claude"]["judge_rule"] is None and views["codex"]["judge_rule"]["section_id"] == "closing-line"
    assert {v["judge_prompt_version"] for v in views.values()} == {"2"}
    assert out.judge_rule == R.judge_rule(EDIT) and out.judge_prompt_version == "2"
    assert recorded["results"]["provider_results"][1]["judge_rule"]["heading"] == "Closing line"


def test_saved_fixture_is_a_valid_report_with_every_field_filled():
    rep = json.loads(FIXTURE.read_text())
    assert N.validate_report(rep) == [] and B.validate_report(rep) == []
    assert rep["costs"]["sandbox_runs_used"] > 0 and rep["costs"]["sandbox_runs_released"] > 0
    exps = [e for e in rep["experiments"] if e.get("judge_rule")]
    assert exps and all(e["judge_prompt_version"] and e["judge_rule"]["text"] for e in exps)
    assert any(all(e["arms"][a][k] is not None for a in "AB" for k in SCORE_KEYS) for e in exps)
    assert all(v["arms"][a][k] is not None and v["judge_rule"] for e in exps for v in e["provider_results"]
               for a in "AB" for k in SCORE_KEYS)
