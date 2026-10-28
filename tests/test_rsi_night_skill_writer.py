"""S6's section writer for candidates without a rule section of their own (2026-10-26).

The 2026-10-25 night selected three skill_scores candidates (skill:pepe, skill:meltdown, skill:anatomy). Their rule
key names the skill, not a section, so `_section_for` found none and every experiment fell back to the stub before
any writer call. Now the section comes from the hypothesis (`section_for_hypothesis`), and a writer-mode night whose
experiments all fall back is loud: report.writer, a first note, a brief warning, a degraded status and a routines.log
item. The fixtures are trimmed copies of the real SKILL.md files (frontmatter, every heading, one line per section)
and the structured fields of that night's candidates. A fake stands in for `claude -p`: no model, no paid call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_memory import (config, distill, edits_sections, rsi_artifact as RA, rsi_brief as BR, rsi_night as N,
                          rsi_schedule as S)
from agent_memory.rsi_budget import Budget, BudgetConfig, Usage
from rsi_night_support import (CLAUDE_MD, RULE_A, FakeEvidence, FakeReplayer, FakeStager, adapters, clean_db,
                               make_home, record_refresh, rows, seed_rule, strip_agent_env)

NIGHT = "2026-10-25"
PROFILE = config.REPO_ROOT / "config" / "rsi-night.subscription.json"
OPUS = "claude-opus-5-5"
FIX = Path(__file__).parent / "fixtures" / "rsi-night-skills"
REAL = json.loads((FIX / "night-2026-10-25.json").read_text())
CANDS = {c["key"]: c for c in REAL["candidates"]}
ADDED = "- Keep this part short: one line per item, then stop."
ROUTINE_MD = ("---\nname: config-check\n---\n\n# Config check\n\nRuns every morning.\n\n## Steps\n\n1. Read the config.\n\n"
              "## Report\n\n- One line per problem.\n")
_SECTION_RE = re.compile(r"^=== Rule (\S+) — [^\n]* ===\n(.*?)\n\n=== The rule before it ===", re.S | re.M)


class SectionWriter:
    """`claude -p` stand-in: rewrites whatever section the prompt carries (its text plus one added line), or
    answers `answer` (a string, or an exception to raise)."""

    def __init__(self, answer: Any = None, added: str = ADDED) -> None:
        self.answer, self.added, self.prompts = answer, added, []
        self.sections: list[str] = []

    def __call__(self, prompt: str) -> tuple[str, Usage]:
        self.prompts.append(prompt)
        if isinstance(self.answer, BaseException):
            raise self.answer
        if self.answer is not None:
            return self.answer, Usage(tokens_in=1000, tokens_out=200)
        m = _SECTION_RE.search(prompt)
        assert m, "the prompt carries no section"
        self.sections.append(m.group(1))
        new = m.group(2).rstrip() + "\n\n" + self.added
        return json.dumps({"new_section": new, "topic": "short checkable steps",
                           "why": "a fixed short shape is easy to follow and to check",
                           "summary": "adds one short rule line to this section", "kind": "prose",
                           "kind_reason": "it adds one plain rule line"}), Usage(tokens_in=1000, tokens_out=200)


def _budget() -> Budget:
    return Budget(BudgetConfig.from_dict({"max_usd": 0, "model_calls": {OPUS: 3}, "subscription_models": [OPUS],
                                          "max_subscription_tokens": 100_000}))


def _hyp(what: str, target: str) -> N.Hypothesis:
    return N.Hypothesis(what=what, why="the evidence gives only an aggregate score", target=target)


def _sections(text: str) -> dict[str, str]:
    return {s.id: edits_sections.section_text(text, s.id) for s in edits_sections.sections(text)}


def _one_section_changed(old: str, new: str) -> list[str]:
    before, after = _sections(old), _sections(new)
    assert list(before) == list(after)                                   # same headings, same order
    return [k for k in before if before[k] != after[k]]


def _frontmatter(text: str) -> str:
    return text[: text.index("\n---\n", 4) + 5]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    strip_agent_env(monkeypatch)
    return make_home(tmp_path, monkeypatch)


# --------------------------------------------------------------------------- the three real candidates


@pytest.mark.parametrize("key", list(CANDS))
def test_real_skill_candidates_get_a_writer_call_and_a_one_section_change(home: Path, key: str) -> None:
    c = CANDS[key]
    old = (FIX / c["fixture"]).read_text(encoding="utf-8")
    w = SectionWriter()
    impl = N.WriterImplementer({"model": OPUS}, call=w)
    got = impl.implement(_hyp(c["hypothesis"]["what"], c["target"]), old_text=old, experiment_id="exp-1",
                         baseline_id="bl-1", budget=_budget(),
                         context={"rule_key": c["rule_key"], "dedupe_key": c["key"], "evidence_text": ""})
    assert impl.calls == 1 and len(w.prompts) == 1
    assert (got.implementer, got.fallback_reason) == ("writer", None)
    assert (got.section_id, got.section_from) == (c["section"], "hypothesis")
    assert _one_section_changed(old, got.text) == [c["section"]]
    assert ADDED in edits_sections.section_text(got.text, c["section"])
    assert _frontmatter(got.text) == _frontmatter(old)                   # the frontmatter is untouched
    assert got.text != N.stub_change(old, _hyp(c["hypothesis"]["what"], c["target"]))
    rec = got.record()["implementation"]
    assert rec["section_id"] == c["section"] and rec["section_from"] == "hypothesis" and rec["diff"]


@pytest.mark.parametrize("h", REAL["second_hypotheses"], ids=lambda h: h["key"])
def test_the_pending_hypotheses_pick_the_section_they_name(h: dict[str, Any]) -> None:
    text = (FIX / CANDS[h["key"]]["fixture"]).read_text(encoding="utf-8")
    got = N.section_for_hypothesis(text, h["what"])
    assert (got.id if got else None) == h["section"]


def test_the_fixtures_keep_the_real_files_heading_structure() -> None:
    # Section ids of the real files on 2026-10-26 (edits_sections.sections, frontmatter skipped).
    assert list(_sections((FIX / "pepe.SKILL.md").read_text())) == [
        "intro", "hard-rules", "modes", "phase-0-read-the-room-silent", "phase-1-pitch", "phase-2-riff", "phase-3-lock",
        "phase-4-hyper-focus", "phase-4-hyper-focus/a-parallel-research", "phase-4-hyper-focus/b-one-idea-at-a-time",
        "briefs", "wide", "return-mode", "tone"]
    assert list(_sections((FIX / "meltdown.SKILL.md").read_text()))[-2:] == ["gate-review-loop-render",
                                                                           "report-and-lessons"]
    assert "workflow" in _sections((FIX / "anatomy.SKILL.md").read_text())


# --------------------------------------------------------------------------- every source kind the night produces


def _sighting_rule_keys(home: Path) -> dict[str, Any]:
    """The rule keys the night's own sighting code gives a routine failure and a provider finding."""
    night = N.Night.__new__(N.Night)
    night.cfg = N.load_config(PROFILE)
    night.allowlist = []
    delta = SimpleNamespace(routine_failures=[{"routine": "amem-daily", "run_id": "r1", "ref": "amem:run/r1"}],
                            findings=[SimpleNamespace(status="ok", n=9, source="clickhouse", ref="ch:q.rule_gap/x",
                                                      measurements={"gap": 0.4})])
    (routine,) = night._routine_sightings(delta)
    (finding,) = night._finding_sightings(delta)
    return {"routine_failures": routine.rule_key, "finding": finding.rule_key}


def test_the_writer_runs_for_every_source_kind(home: Path) -> None:
    keys = _sighting_rule_keys(home)
    assert keys == {"routine_failures": "routine:amem-daily", "finding": None}
    pepe = CANDS["skill:pepe"]
    cases = [  # (source, rule key, file text, hypothesis, expected section, how it was picked)
        ("deviations", f"claude-md:global:{RULE_A[0]}", CLAUDE_MD, "Make the closing line a fixed two-line summary",
         "closing-line", "rule_key"),
        ("skill_scores", pepe["rule_key"], (FIX / pepe["fixture"]).read_text(), pepe["hypothesis"]["what"],
         "return-mode", "hypothesis"),
        ("routine_failures", keys["routine_failures"], ROUTINE_MD, "In the report step, name the failing file first",
         "report", "hypothesis"),
        ("finding", keys["finding"], ROUTINE_MD, "Add to the steps: read the config twice before reporting",
         "steps", "hypothesis")]
    for source, rule_key, text, what, section, how in cases:
        w = SectionWriter()
        got = N.WriterImplementer({"model": OPUS}, call=w).implement(
            _hyp(what, "~/.agents/skills/demo/SKILL.md"), old_text=text, experiment_id="exp-1", baseline_id="bl-1",
            budget=_budget(), context={"rule_key": rule_key, "evidence_text": ""})
        assert (got.implementer, got.section_id, got.section_from) == ("writer", section, how), source
        assert _one_section_changed(text, got.text) == [section], source


# --------------------------------------------------------------------------- the fallbacks still hold


def _impl_pepe(w: SectionWriter, what: str | None = None) -> N.Implementation:
    c = CANDS["skill:pepe"]
    return N.WriterImplementer({"model": OPUS}, call=w).implement(
        _hyp(what or c["hypothesis"]["what"], c["target"]), old_text=(FIX / c["fixture"]).read_text(),
        experiment_id="exp-1", baseline_id="bl-1", budget=_budget(), context={"rule_key": c["rule_key"]})


def test_rule_key_less_writer_error_falls_back(home: Path) -> None:
    w = SectionWriter(distill.WriterError("claude exited 1"))
    got = _impl_pepe(w)
    c = CANDS["skill:pepe"]
    assert w.prompts and got.implementer == "stub-fallback"
    # A failed subscription call has no usage record: the budget fails closed (as for a deviation candidate).
    assert got.fallback_reason == "budget: subscription_usage_unknown: no writer call"
    assert got.text == N.stub_change((FIX / c["fixture"]).read_text(), _hyp(c["hypothesis"]["what"], c["target"]))


def test_rule_key_less_proposal_refusal_falls_back(home: Path) -> None:
    got = _impl_pepe(SectionWriter("I would rather explain in prose."))
    assert got.implementer == "stub-fallback" and got.fallback_reason.startswith("writer proposal refused")


def test_rule_key_less_pinned_file_and_pinned_rule_get_no_writer_call(home: Path) -> None:
    pins = home / "agent-config" / "improve" / "pins.json"
    pins.write_text(json.dumps({"files": {"~/.agents/skills/pepe/SKILL.md": {"reason": "edited by hand"}}, "rules": {}}))
    w = SectionWriter()
    got = _impl_pepe(w)
    assert w.prompts == [] and got.fallback_reason == "target file is pinned: no writer call"
    pins.write_text(json.dumps({"files": {}, "rules": {"rsi-night-return-mode": {"reason": "judge prompt"}}}))
    got = _impl_pepe(w)
    assert w.prompts == [] and got.fallback_reason == "the rule is pinned: no writer call"


def test_no_section_named_falls_back_without_a_call(home: Path) -> None:
    w = SectionWriter()
    got = _impl_pepe(w, "Rewrite the rule as one short explicit checklist item")
    assert w.prompts == [] and got.implementer == "stub-fallback"
    assert got.fallback_reason == "no section matches the candidate or the hypothesis: no writer call"


def test_a_section_too_large_for_the_writer_is_never_picked() -> None:
    big = "# Doc\n\n## Return mode\n\n" + ("- a long rule line about return mode.\n" * 400) + "\n## Tone\n\n- Short.\n"
    assert N.section_for_hypothesis(big, "Finish the Return mode section") is None


# --------------------------------------------------------------------------- full nights


class GoTriage:
    health = "fixture"

    def assess(self, c: Any, *, state: str, budget: Budget) -> N.TriageAnswers:
        return N.TriageAnswers("experiment", 1.0, 2.0, 0.0, 2.0, "fixture")


class RealHypotheses:
    """S5 stand-in: the 2026-10-25 hypothesis for the candidate's key (the template's otherwise)."""

    def __init__(self, added=ADDED):
        self.added=added

    def reply(self, candidate: Any, *, evidence_text: str, targets: Any, cap: int, target_text: str,
              budget: Budget) -> str:
        c = CANDS.get(str(candidate.get("dedupe_key")))
        hyps = ([{**c["hypothesis"], "target": c["target"], "risk": "low", "confidence": 0.5}] if c
                else N.template_hypotheses(candidate)["hypotheses"])
        for h in hyps:
            sec=N._section_for(target_text,candidate.get("rule_key")) or N.section_for_hypothesis(target_text,h["what"])
            if sec:
                before=edits_sections.section_text(target_text,sec.id)
                h["edit"]={"section_id":sec.id,"before":before,"after":before.rstrip()+"\n\n"+self.added}
        return budget.call("hypothesizer-fixture", "hypothesize", lambda: (json.dumps({"hypotheses": hyps}), Usage()))


class GuardStager(FakeStager):
    """FakeStager plus improve's content guard verdict for a pasted project path (bin/improve GUARD)."""

    def stage(self, **kw: Any) -> N.StageResult:
        if "/Users/" in kw["new_text"]:
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


def seed_skill(env: SimpleNamespace, key: str = "skill:pepe") -> dict[str, Any]:
    c = CANDS[key]
    name = key.split(":", 1)[1]
    d = env.home / ".agents" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text((FIX / c["fixture"]).read_text())
    import psycopg
    with psycopg.connect(env.url) as conn:
        conn.execute("insert into skill_scores (skill, bucket, n_calls, n_sessions, n_scored, score) "
                     "values (%s, '*', %s, %s, %s, %s)", (name, c["metrics"]["n_scored"], c["metrics"]["sessions"],
                                                         c["metrics"]["n_scored"], c["metrics"]["score"]))
    record_refresh(env.url)          # skill candidates need a fresh recorded refresh (skill_refresh prerequisite)
    return c


def night(env: SimpleNamespace, writer: SectionWriter, *, stager: Any = None, evidence: Any = None
          ) -> dict[str, Any]:
    cfg = N.load_config(PROFILE)
    ad = adapters(implementer=N.WriterImplementer(cfg.implementer, call=writer), stager=stager or FakeStager(),
                  replayer=FakeReplayer("NO_EFFECT"), triage=GoTriage(), hypothesizer=RealHypotheses(writer.added),
                  evidence=evidence)
    runner=N.Night(cfg, ad, night=NIGHT, db_url=env.url, install_signals=False)
    # Fake replay component test; actual section declarations are checked independently.
    runner._test_cases=lambda *args,**kw:["fixture-exact-section"]
    rep=runner.run()
    assert N.validate_report(rep) == [], N.validate_report(rep)
    assert BR.validate_report(rep) == [], BR.validate_report(rep)
    return rep


def test_a_skill_scores_night_runs_the_writer_end_to_end(env: SimpleNamespace) -> None:
    c = seed_skill(env)
    w = SectionWriter()
    rep = night(env, w)
    (e,) = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["candidate_key"] == "skill:pepe" and e["hypothesis"]["what"] == c["hypothesis"]["what"]
    assert (e["implementer"], e["implementer_fallback_reason"]) == ("writer", None)
    assert e["implementation"]["section_id"] == "return-mode" and e["implementation"]["section_from"] == "declared_edit"
    _, _, after = RA.load_artifact(RA.artifacts_dir() / e["artifact_hash"])
    staged = next(iter(after.values())).decode()
    assert _one_section_changed((FIX / c["fixture"]).read_text(), staged) == ["return-mode"]
    assert rep["writer"] == {"mode": "writer", "experiments": 1, "wrote": 1, "fell_back": 0, "all_fell_back": False,
                             "reasons": []}
    assert rep["status"] == "ok" and not any(n.startswith(N.WRITER_NEVER_RAN) for n in rep["notes"])
    assert S.classify(rep) == ("PASS", [])
    (meta,) = rows(env.url, "select meta from rsi_experiments")
    assert meta[0]["implementation"]["section_from"] == "declared_edit" and meta[0]["new_text"] == staged


INTENDED_LINES = {
    "skill:pepe": "- Return five ranked ideas with name, pitch, slot, score and reason, followed by the pick.",
    "skill:meltdown": "- If the review loop stops below eight, still render and report each lower criterion and its remaining fix.",
    "skill:anatomy": "- Before geometry, name the closest shipped example and reuse its kit and structure.",
}


@pytest.mark.parametrize("key", list(CANDS))
def test_intended_skill_intervention_reaches_exact_bound_artifact(env: SimpleNamespace, key: str) -> None:
    """Known intended writer output tests matcher→section edit→artifact bytes, not model quality or fallback."""
    c = seed_skill(env, key)
    old = (FIX / c["fixture"]).read_bytes()
    source = env.home / c["target"][2:]
    before_live = source.read_bytes()
    w = SectionWriter(added=INTENDED_LINES[key])
    rep = night(env, w)
    (e,) = [e for e in rep["experiments"] if e["experiment_id"]]
    manifest, digest, after_files = RA.load_artifact(RA.artifacts_dir() / e["artifact_hash"])
    (file,) = manifest["files"]
    after = after_files[file["after_sha256"]]
    assert source.read_bytes() == before_live == old  # baseline/live source remains A
    assert e["implementer"] == "writer" and e["implementer_fallback_reason"] is None
    assert e["implementation"]["intended_intervention"] is True
    assert file["path"] == c["target"] and file["before_sha256"] == RA.sha256_bytes(old)
    assert file["after_sha256"] == RA.sha256_bytes(after) != file["before_sha256"]
    assert digest == e["artifact_hash"] == RA.artifact_hash(manifest)
    assert manifest["experiment_id"] == e["experiment_id"] and manifest["baseline_id"] == e["baseline_id"]
    # The staging identity is synthetic; implementation.section_id names the actual source rule section.
    assert file["rule_id"] == "rsi-section-" + e["implementation"]["section_id"].replace("/", "--")
    assert e["implementation"]["section_id"] == c["section"]
    assert _one_section_changed(old.decode(), after.decode()) == [c["section"]]
    assert INTENDED_LINES[key] in edits_sections.section_text(after.decode(), c["section"])
    assert after.decode() != N.stub_change(old.decode(), _hyp(c["hypothesis"]["what"], c["target"]))


def test_a_skill_scores_guard_refusal_falls_back_and_the_night_says_so(env: SimpleNamespace) -> None:
    seed_skill(env)
    w = SectionWriter(added="- See the log in /Users/vilmerfrost/Projects/demo/out.txt first.")
    rep = night(env, w, stager=GuardStager())
    (e,) = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["implementer"] == "writer" and e["status"] == "failed"
    assert e["artifact_hash"] is None and e.get("replay_runs", 0) == 0
    assert "content guard: project path" in ' '.join(rep["notes"])
    assert w.sections == ["return-mode"]
    assert rep["status"] == "partial"


@pytest.mark.parametrize("replay_verdict", ["IMPROVES", "NO_EFFECT", "WORSE"])
def test_fallback_is_never_evidence_for_the_intended_intervention(env: SimpleNamespace,
                                                                replay_verdict: str) -> None:
    seed_skill(env)
    cfg = N.load_config(PROFILE)
    ad = adapters(implementer=N.WriterImplementer(cfg.implementer, call=SectionWriter("not JSON")),
                  stager=FakeStager(), replayer=FakeReplayer(replay_verdict), triage=GoTriage(),
                  hypothesizer=RealHypotheses())
    runner=N.Night(cfg, ad, night=NIGHT, db_url=env.url, install_signals=False)
    # Fake replay component test; actual section declarations are checked independently.
    runner._test_cases=lambda *args,**kw:["fixture-exact-section"]
    rep=runner.run()
    (e,) = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["implementer"] == "stub-fallback" and e["verdict"] is None
    assert not e["implementation"]["contract_verified"]
    assert e["status"] == "failed" and e["proposal_id"] is None
    assert e["artifact_hash"] is None and ad.replayer.calls == []
    assert rows(env.url, "select count(*) from rsi_proposals") == [(0,)]
    (meta,) = rows(env.url, "select meta from rsi_experiments")
    assert not meta[0]["implementation"]["contract_verified"]
    assert "replay_verdict" not in meta[0]


def test_a_night_where_every_experiment_fell_back_is_loud(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    rep = night(env, SectionWriter("I would rather explain in prose."))
    w = rep["writer"]
    assert (w["experiments"], w["wrote"], w["fell_back"], w["all_fell_back"]) == (1, 0, 1, True)
    assert w["reasons"][0]["reason"].startswith("writer proposal refused: model reply is not JSON")
    # report: a degraded status and the first note, which survives the notes cut
    assert rep["status"] == "partial" and rep["checkpoint"]["advanced"] is True
    assert rep["notes"][0].startswith("writer never ran: 1/1 experiments fell back to the stub (writer proposal "
                                      "refused: model reply is not JSON")
    # routines.log: the FAIL line says why
    status, items = S.classify(rep)
    assert status == "FAIL" and any(i.startswith("writer never ran: 1/1 experiments fell back to the stub (writer "
                                                 "proposal refused") for i in items), items
    # the morning brief: one warning line (the note is not repeated) and the phone push says it too
    b = BR.make_brief(root=env.sandbox, db_url=env.url)
    md = BR.render_markdown(b)
    assert "- Warning: writer never ran: 1/1 experiments fell back to the stub change (writer proposal refused" in md
    assert "- Note: writer never ran" not in md
    _, body = BR.notification(b)
    assert "Writer never ran: every experiment used the stub change." in body
    assert b["health"]["warnings"][0].startswith("writer never ran: 1/1")      # the brief JSON keeps its keys


def test_a_stub_mode_night_has_no_writer_tally(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    rep = N.Night(N.load_config(PROFILE), adapters(replayer=FakeReplayer("NO_EFFECT")), night=NIGHT, db_url=env.url,
                  install_signals=False).run()
    assert "writer" not in rep and rep["status"] == "ok"


# --------------------------------------------------------------------------- provider health always has a reason


class SilentErrorEvidence(FakeEvidence):
    """S1's delta reports langfuse "error" with no step detail: the 2026-10-25 report's shape."""

    def delta(self, since: str | None, *, conn: Any) -> Any:
        d = super().delta(since, conn=conn)
        d.health = {**d.health, "langfuse": "error"}
        return d


def test_an_errored_provider_always_has_a_reason(env: SimpleNamespace) -> None:
    seed_rule(env.url, RULE_A, "a")
    rep = night(env, SectionWriter(), evidence=SilentErrorEvidence())
    assert rep["health"]["langfuse"] == "error"
    assert rep["health_reasons"]["langfuse"] == ["S1 delta: error, no step detail"]
    for k, v in rep["health"].items():
        if v in N.UNHEALTHY:
            assert rep["health_reasons"].get(k), k
    status, items = S.classify(rep)
    assert status == "FAIL" and "health langfuse: error (S1 delta: error, no step detail)" in items


def test_delta_steps_explain_the_provider_health() -> None:
    night = N.Night.__new__(N.Night)
    night.health = {k: "skipped" for k in N.PROVIDERS}
    night.health_reasons = {}
    night._merge_health({"supabase": "ok", "langfuse": "error"},
                        [{"step": "lf.experiment_scores", "status": "error", "found": 0, "detail": "timeout after 20s"}],
                        "S1 delta")
    assert night.health_reasons == {"langfuse": ["lf.experiment_scores: error (timeout after 20s)"]}
    assert night.health_where == {"langfuse": "S1 delta"}
