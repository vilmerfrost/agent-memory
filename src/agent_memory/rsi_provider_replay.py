"""Compare shared rules separately per provider; never pool A/B metrics across models."""
from __future__ import annotations
from datetime import UTC, datetime
import os


def required_providers(files):
    providers = set()
    for item in files:
        path = item.get("path", "")
        if path.startswith("~/.claude/"):
            providers.add("claude")
        elif path.startswith("~/.codex/"):
            providers.add("codex")
        else:
            providers.update(("claude", "codex"))
    return sorted(providers)


def combined_verdict(outcomes):
    """One verdict from each required provider's own verdict; scores are never pooled across providers.

    Any INCONCLUSIVE (or unknown) -> INCONCLUSIVE; else any WORSE -> WORSE; IMPROVES only when every provider
    IMPROVES; else NO_EFFECT. A provider's WORSE already needs rsi_verdict's score margin (policy/2: 2.0 over
    5 pairs), so one flipped run on one provider no longer sinks the change."""
    verdicts = [r.verdict for r in outcomes]
    if not verdicts or any(v == "INCONCLUSIVE" or v not in ("IMPROVES", "NO_EFFECT", "WORSE") for v in verdicts):
        return "INCONCLUSIVE"
    if "WORSE" in verdicts:
        return "WORSE"
    return "IMPROVES" if all(v == "IMPROVES" for v in verdicts) else "NO_EFFECT"


def replay(stages, settings, *, artifact_hash, experiment_id, baseline_id, budget):
    from agent_memory import rsi_night as N, rsi_replay as R, rsi_artifact as RA
    rs = R.sandbox_module()
    box = rs.Sandbox(RA.sandbox_dir())
    manifest, _, _ = rs.load_artifact(box.artifacts / artifact_hash)
    required = required_providers(manifest["files"])
    profiles = settings["provider_profiles"]
    if not required or any(p not in profiles for p in required):
        raise N.ExperimentFailed("missing required provider profile")
    if settings.get("cases") is None:
        # No explicit case list: the target's own cases, repeated up to the verdict's minimum pairs, within each
        # provider's share of the night's A/B pairs. Every provider runs the same plan.
        plan = R.auto_cases(artifact_hash, R.load_cases(), root=RA.sandbox_dir(),
                            reps=int(settings.get("reps") or 1),
                            max_pairs=max(1, budget.cfg.max_ab_pairs // len(required)))
        if plan is not None:
            settings = {**settings, "cases": [c.id for c in plan[0]], "reps": plan[1]}
    pairs = len(R.load_cases(settings.get("cases"))) * int(settings.get("reps") or 1) * len(required)
    if pairs > budget.cfg.max_ab_pairs:
        raise N.BudgetExhausted("ab_pairs", f"{pairs} provider-specific pairs exceed {budget.cfg.max_ab_pairs}")
    old = box.artifacts / artifact_hash / "evals" / "sandbox-replay.json"
    if old.is_file():
        os.replace(old, old.with_name(old.name + ".superseded-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")))
    results, views = [], []
    for provider in required:
        cfg = {k: v for k, v in settings.items() if k != "provider_profiles"}
        cfg.update(profiles[provider], profile=provider)
        outcome = N.SandboxReplayer({**stages, "replay_agent": provider}, cfg).replay(
            artifact_hash=artifact_hash, experiment_id=experiment_id, baseline_id=baseline_id, budget=budget)
        results.append(outcome)
        views.append({"provider": provider, "verdict": outcome.verdict, "reasons": outcome.reasons,
                      "arms": outcome.arms, "test_setup": outcome.setup, "relevance": outcome.relevance,
                      "judge_pairs": outcome.judge_pairs, "eval_path": outcome.eval_path,
                      "judge": outcome.judge, "jev": outcome.jev})
        if outcome.budget_stop:
            break
    verdict = combined_verdict(results) if len(results) == len(required) else "INCONCLUSIVE"
    reasons = [f"{p}: {r.verdict}" for p, r in zip(required, results)]
    if len(results) != len(required):
        reasons.append("provider coverage incomplete")
    if verdict == "IMPROVES" and any(not r.eval_path for r in results):
        verdict = "INCONCLUSIVE"
        reasons.append("provider evaluation record missing")
    eval_path = None
    try:
        eval_path = box.record_eval(artifact=artifact_hash, experiment=experiment_id, arm="B", suite="sandbox-replay",
            suite_version=R.SUITE_VERSION, verdict={"IMPROVES":"improves","WORSE":"worse","NO_EFFECT":"no-effect"}.get(verdict,"inconclusive"),
            results={"required_providers": required, "provider_results": views, "verdict": verdict, "reasons": reasons},
            model="provider-specific", effort="high", control_arm="A", replace=True)
    except (rs.SandboxError, OSError, ValueError):
        if verdict == "IMPROVES":
            verdict = "INCONCLUSIVE"
        reasons.append("combined evaluation record unavailable")
    single = results[0] if len(results) == 1 else None
    return N.ReplayOutcome(verdict=verdict, reasons=reasons, arms=single.arms if single else {},
        artifact_hash=artifact_hash, baseline_id=baseline_id, eval_path=str(eval_path) if eval_path else None,
        budget_stop=next((r.budget_stop for r in results if r.budget_stop), None), runs=sum(r.runs for r in results),
        judge=single.judge if single else None, jev=single.jev if single else None,
        setup=single.setup if single else {"agent":"separate provider comparisons","auth_mode":"subscription","effort":"high"},
        relevance=single.relevance if single else {}, judge_pairs=single.judge_pairs if single else [], provider_results=views,
        attempts=[a for r in results for a in getattr(r, "attempts", [])])
