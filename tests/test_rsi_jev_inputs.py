"""What Jev sees and what code decides without Jev (jev-night): the triage state's true recurrence, result grade,
covering replay cases and their last baseline pass rate; the harness-dead verdict rule; the replay state's derived
`checks:` lines; and the Jev model and token usage kept from the response. No live Jev call: the SDK client is a
fake, and the fixtures are recorded files.

The realistic inputs follow the 2026-10-25 / 2026-10-26 nights (~/sandbox/nights/*/report.json): skill candidates
`skill:meltdown` (case meltdown-brief) and `skill:pepe` (case pepe-lock), replayed per provider profile, where the
codex arm failed every check in both arms (A 0/3, B 0/3) and Jev still said improves 0.71, harness_problem 0.22."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
import typesafe_sdk
from psycopg.types.json import Jsonb

from agent_memory import jev, rsi_night as N, rsi_replay as R, rsi_verdict as V
from agent_memory.rsi_budget import Budget, BudgetConfig
from rsi_night_support import clean_db

MELTDOWN = {"source": "skill_scores", "kind": "edit_skill", "target": "~/.agents/skills/meltdown/SKILL.md",
            "rule_key": "skill:meltdown", "seen_count": 4, "evidence_refs": ["amem:skill/meltdown"],
            "metrics": {"score": 0.41, "n_scored": 6, "sessions": 6}}
DEVIATION = {"source": "deviations", "kind": "edit_instruction", "target": "~/.agents/skills/pepe/SKILL.md",
             "rule_key": "skill:pepe:phase-3-lock", "seen_count": 9,
             "evidence_refs": [f"amem:deviation/d{i}" for i in range(3)] + ["amem:experiment/exp-20261025-95f15ef6"],
             "metrics": {"count": 3, "sessions": 2, "followed_grade_mean": 0.35, "result_grade_mean": 0.6,
                         "objective_result_mean": None}}


def lines(state: str) -> dict[str, str]:
    return dict(x.split(": ", 1) for x in state.splitlines()[1:] if ": " in x and not x.startswith("- "))


# --------------------------------------------------------------------------- triage state (pure)


def test_triage_state_shows_true_recurrence_not_nights_seen() -> None:
    # seen_count 9 (nights that sighted it) used to read "5-9"; the problem happened in 3 deviations (2 sessions).
    # The experiment ref is history, not an occurrence.
    st = N.triage_state(DEVIATION, ["WORSE"], True)
    assert N.recurrence(DEVIATION) == 3
    assert lines(st)["recurrence"] == "2-4"
    assert lines(st)["sessions"] == "2-4"
    assert lines(st)["result_grade"] == "0.6" and lines(st)["followed_grade"] == "0.3"   # round(0.35, 1) = 0.3
    assert lines(st)["objective_result"] == "unknown" and lines(st)["prior_experiments"] == "WORSE"
    # A skill candidate: one evidence ref, 6 scored sessions -> recurrence 6, not seen_count 4.
    assert lines(N.triage_state(MELTDOWN, [], True))["recurrence"] == "5-9"


def test_triage_state_lists_covering_cases_with_baseline_or_unknown() -> None:
    base = {"meltdown-brief": {"claude": (3, 3), "codex": (0, 3)}}
    st = N.triage_state(MELTDOWN, ["INCONCLUSIVE"], True, ["meltdown-brief"], base)
    assert st.splitlines()[-2:] == ["test_cases: 1", "- meltdown-brief: baseline_passes claude=all codex=none"]
    # No replay history for the case: unknown, never a guess.
    st = N.triage_state(DEVIATION, [], True, ["pepe-lock"], {})
    assert st.splitlines()[-2:] == ["test_cases: 1", "- pepe-lock: baseline_passes unknown"]
    # Buckets: some (< half), most, and cases sorted; no cases at all says 0.
    st = N.triage_state(MELTDOWN, [], True, ["b-case", "a-case"], {"a-case": {"default": (1, 3)},
                                                                    "b-case": {"default": (2, 3)}})
    assert st.splitlines()[-3:] == ["test_cases: 2", "- a-case: baseline_passes default=some",
                                    "- b-case: baseline_passes default=most"]
    assert N.triage_state(MELTDOWN, [], True).splitlines()[-1] == "test_cases: 0"


def test_triage_state_is_stable_and_capped() -> None:
    cases = [f"case-{i:02d}" for i in range(N.TRIAGE_CASES_MAX + 3)]
    st = N.triage_state(MELTDOWN, [], True, cases, None)
    assert st == N.triage_state(MELTDOWN, [], True, list(reversed(cases)), {})
    assert st.splitlines()[-1] == "- and 3 more"
    assert f"test_cases: {len(cases)}" in st


# --------------------------------------------------------------------------- baseline history (database)


ART = ("insert into rsi_artifacts (hash, manifest_canonical, manifest, files, baseline_id) "
       "select encode(sha256(convert_to(m, 'UTF8')), 'hex'), m, m::jsonb, '{}', 'bl-1' "
       "from (select %s::text as m) x returning hash")


def _eval(conn: psycopg.Connection, h: str, suite: str, cases: list[dict[str, Any]], ago_h: float,
          digests: dict[str, str] | None = None) -> None:
    rec = {"format": "rsi-eval/1", "suite": suite,
           "results": {"cases": cases, "case_digests": digests or {}, "candidate_arm": "B", "control_arm": "A"}}
    conn.execute("insert into rsi_eval_results (artifact_hash, experiment_id, baseline_id, suite, suite_version, "
                 "verdict, record, record_sha, created_at) values (%s, 'exp-x', 'bl-1', %s, '2', 'inconclusive', %s, "
                 "%s, %s)", (h, suite, Jsonb(rec), hashlib.sha256(json.dumps([suite, ago_h, cases]).encode()).hexdigest(),
                             datetime.now(UTC) - timedelta(hours=ago_h)))


@pytest.fixture
def night(pg_url: str) -> Iterator[SimpleNamespace]:
    clean_db(pg_url)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        h = conn.execute(ART, ('{"format":"rsi-artifact/1","jev-night":1}',)).fetchone()[0]
        mk = lambda a, b, n=3: {"case": "meltdown-brief", "pairs": n, "a_pass": a, "b_pass": b}  # noqa: E731
        _eval(conn, h, "sandbox-replay-codex", [mk(2, 2)], 50)                        # older: superseded
        _eval(conn, h, "sandbox-replay-codex", [mk(0, 0)], 26, {"meltdown-brief": "d-now"})   # 2026-10-26 codex
        _eval(conn, h, "sandbox-replay-claude", [mk(3, 3)], 26, {"meltdown-brief": "d-now"})  # 2026-10-26 claude
        _eval(conn, h, "sandbox-replay-claude", [mk(0, 3)], 1, {"meltdown-brief": "d-old"})  # an older case version
        _eval(conn, h, "sandbox-replay", [{"case": "anatomy-plan", "pairs": 0, "a_pass": 0, "b_pass": 0}], 2)
        n = N.Night.__new__(N.Night)
        n.conn, n.notes, n._case_digests = conn, [], {"meltdown-brief": "d-now", "pepe-lock": "d-p"}
        yield SimpleNamespace(n=n, h=h)
    clean_db(pg_url)


def test_baselines_read_the_newest_record_per_provider_suite(night: SimpleNamespace) -> None:
    got = night.n._baselines(["meltdown-brief", "pepe-lock", "anatomy-plan"])
    # newest codex record wins over the older one; the claude record of another case version is skipped; a record
    # without complete pairs and a case never replayed are absent (triage_state says unknown)
    assert got == {"meltdown-brief": {"claude": (3, 3), "codex": (0, 3)}}
    st = N.triage_state(MELTDOWN, [], True, ["meltdown-brief", "pepe-lock"], got)
    assert "- meltdown-brief: baseline_passes claude=all codex=none" in st
    assert "- pepe-lock: baseline_passes unknown" in st
    assert night.n._baselines([]) == {} and night.n.notes == []


# --------------------------------------------------------------------------- harness-dead verdict


def _machine(*cases: tuple[str, int, int, int]) -> V.ArmPair:
    stats = tuple(V.CaseStats(c, p, a, b) for c, p, a, b in cases)
    return V.ArmPair(cases=stats, runs_expected=2 * sum(p for _, p, _, _ in cases),
                     runs_done=2 * sum(p for _, p, _, _ in cases))


CODEX_1026 = _machine(("meltdown-brief", 3, 0, 0))
# The recorded nights ran 3 pairs (policy/1 min_pairs). The shipped policy/2 asks for 5; these tests replay the
# recorded inputs through the shipped rules with only the pair minimum the night had.
RECORDED = dataclasses.replace(V.load_policy(), min_pairs=3)
JUDGE_1026 = V.JudgeResult(mode="codex", judged=3, wins_a=0, wins_b=2, ties=1)
JEV_1026 = V.JevResult(mode="live", status="ok", effect="improves", confidence=0.71, harness_problem=0.22,
                       evidence_strength=0.64)


def test_both_arms_failing_every_check_is_harness_dead() -> None:
    policy = RECORDED
    assert policy.harness_dead_inconclusive is True
    v = V.decide(CODEX_1026, JUDGE_1026, JEV_1026, policy)
    assert v.verdict == "INCONCLUSIVE" and v.reasons == ("harness-dead:meltdown-brief",)
    two = _machine(("meltdown-brief", 3, 0, 0), ("pepe-lock", 3, 0, 0))
    assert V.decide(two, JUDGE_1026, JEV_1026, policy).reasons == ("harness-dead:meltdown-brief,pepe-lock",)
    # Before the rule, the judge's preference alone decided (b_winrate 0.83 >= 0.75: judge-only-preference).
    off = dataclasses.replace(RECORDED, harness_dead_inconclusive=False)
    assert V.decide(CODEX_1026, JUDGE_1026, JEV_1026, off).reasons == (
        "checks-equal", "judge-only-preference:b_winrate 0.83")


def test_mixed_cases_keep_their_verdicts() -> None:
    policy = RECORDED
    # claude 2026-10-26: both arms pass everything -> NO_EFFECT, as before
    claude = _machine(("meltdown-brief", 3, 3, 3))
    judge = V.JudgeResult(mode="codex", judged=3, wins_a=1, wins_b=2)
    assert V.decide(claude, judge, JEV_1026, policy).verdict == "NO_EFFECT"
    # one dead case next to an improving one: the improving case decides (IMPROVES with judge and Jev agreeing)
    mixed = _machine(("meltdown-brief", 3, 0, 0), ("pepe-lock", 3, 1, 3))
    good = V.JevResult(mode="fixture", status="ok", effect="improves", confidence=0.8, harness_problem=0.1)
    v = V.decide(mixed, V.JudgeResult(mode="stub", judged=6, wins_b=4, ties=2), good, policy)
    assert v.verdict == "IMPROVES" and v.reasons[0] == "improved:pepe-lock A 1/3 -> B 3/3"
    # and a regression next to a dead case is still WORSE
    worse = _machine(("meltdown-brief", 3, 0, 0), ("pepe-lock", 3, 3, 1))
    assert V.decide(worse, judge, good, policy).verdict == "WORSE"
    # earlier rules still come first: missing runs beat harness-dead
    short = V.ArmPair(cases=CODEX_1026.cases, runs_expected=6, runs_done=5)
    assert V.decide(short, JUDGE_1026, JEV_1026, policy).reasons == ("missing-runs:5/6",)


# --------------------------------------------------------------------------- replay Jev state


def test_replay_state_has_plain_checks_lines() -> None:
    cfg = R.ReplayConfig(agent="codex", model="gpt-6.1-sol", reps=3)
    st = R.jev_state(CODEX_1026, JUDGE_1026, cfg)
    assert "- meltdown-brief: pairs=3 A_pass=0 B_pass=0 checks: both arms failed every check" in st
    assert "checks overall: both arms failed every check" in st.splitlines()
    claude = R.jev_state(_machine(("meltdown-brief", 3, 3, 3)), JUDGE_1026, cfg)
    assert claude.splitlines()[5:7] == ["- meltdown-brief: pairs=3 A_pass=3 B_pass=3 checks: both arms passed every check",
                                        "checks overall: both arms passed every check"]
    mixed = R.jev_state(_machine(("a", 3, 1, 3), ("b", 3, 2, 0), ("c", 3, 1, 1)), None, cfg)
    assert [x.rsplit("checks: ", 1)[1] for x in mixed.splitlines() if x.startswith("- ")] == [
        "B passed more", "A passed more", "both arms passed the same number"]
    assert "checks overall: mixed: B passed more on some cases, A on others" in mixed
    assert "checks overall: B passed more" in R.jev_state(_machine(("a", 3, 0, 0), ("b", 3, 1, 2)), None, cfg)
    assert R.checks_line(0, 0, 0) == "no complete pairs"


# --------------------------------------------------------------------------- Jev model and usage


RESPONSES = {
    "replay": {"model": "jev-1.14.2", "usage": {"input_tokens": 431, "output_tokens": 9}, "answers": {
        "effect": {"type": "choice", "choice": "unclear", "confidence": 0.62,
                   "probabilities": {"improves": 0.1, "no_effect": 0.2, "worse": 0.08, "unclear": 0.62}},
        "harness_problem": {"type": "noul", "noul": 0.81},
        "evidence_strength": {"type": "score", "score": 0.4, "confidence": 0.6, "legend": {"0": "W", "1": "M", "2": "S"},
                              "probabilities": {"0": 0.6, "1": 0.4, "2": 0.0}}}},
    "triage": {"model": "jev-1.14.2", "usage": {"input_tokens": 612, "output_tokens": 14}, "answers": {
        "action": {"type": "choice", "choice": "wait", "confidence": 0.7,
                   "probabilities": {"experiment": 0.3, "wait": 0.7}},
        "impact": {"type": "score", "score": 1.0, "confidence": 0.6, "legend": {"0": "L", "1": "M", "2": "H"},
                   "probabilities": {"0": 0.2, "1": 0.6, "2": 0.2}},
        "risk": {"type": "score", "score": 0.5, "confidence": 0.6, "legend": {"0": "L", "1": "M", "2": "H"},
                 "probabilities": {"0": 0.5, "1": 0.5, "2": 0.0}},
        "testability": {"type": "score", "score": 0.4, "confidence": 0.6, "legend": {"0": "P", "1": "F", "2": "G"},
                        "probabilities": {"0": 0.6, "1": 0.4, "2": 0.0}},
        "reason": {"type": "choice", "choice": "hard_to_test", "confidence": 0.7,
                   "probabilities": {"hard_to_test": 0.7, "worth_testing": 0.3}}}},
}


class FakeJev:
    """typesafe_sdk.TypeSafeClient without a network: answers from RESPONSES by the asked questions."""
    calls: list[dict[str, Any]] = []

    def __init__(self, api_key: str) -> None:
        pass

    def __enter__(self) -> FakeJev:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def system_one(self, state: Any, questions: dict[str, Any], model: str | None = None) -> Any:
        FakeJev.calls.append({"state": state, "model": model})
        body = RESPONSES["replay" if "effect" in questions else "triage"]
        return typesafe_sdk.SystemOneResponse.model_validate_json(json.dumps(body))


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> type[FakeJev]:
    monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-typesafe-key-never-print")
    monkeypatch.setenv("AMEM_JEV_LIVE", "1")
    FakeJev.calls = []
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", FakeJev)
    return FakeJev


def _budget() -> Budget:
    return Budget(BudgetConfig.from_dict({"max_usd": 1.0, "model_calls": {jev.MODEL: 2}}))


def test_ask_keeps_the_response_model_and_usage(live: type[FakeJev]) -> None:
    qs = [jev.choice("effect", "x?", {"improves": None, "no_effect": None, "worse": None, "unclear": None}),
          jev.noul("harness_problem", "y?"), jev.score("evidence_strength", "z?", ["W", "M", "S"])]
    got = jev.ask("suite: sandbox-replay/2", qs)
    assert isinstance(got, dict) and list(got) == ["effect", "harness_problem", "evidence_strength"]
    assert got["harness_problem"].value == pytest.approx(0.81)
    assert got.model == "jev-1.14.2" and got.usage == jev.JevUsage(431, 9)   # served model differs from MODEL
    assert live.calls[0]["model"] == jev.MODEL


def test_codex_1026_fixture_reads_through_to_harness_dead() -> None:
    """The recorded 2026-10-26 codex replay (fixture: Jev's live answers to the state before the checks lines).
    Jev still reads improves; code says harness-dead, so the misread cannot matter."""
    from agent_memory import questions as Q
    qs = Q.load_file(R.QUESTIONS_FILE, "rsi-replay").jev_questions()
    st = R.jev_state(CODEX_1026, JUDGE_1026, R.ReplayConfig(agent="codex", model="gpt-6.1-sol", reps=3))
    got = jev._ask_fixture(st, qs)
    assert got.model == "jev-1.14.0" and got.usage == jev.JevUsage(0, 0)      # the recording's, not a live call
    res = R.run_jev(st, "fixture")
    assert (res.status, res.effect, res.harness_problem) == ("ok", "improves", pytest.approx(0.22))
    assert res.model == "jev-1.14.0" and res.tokens_in == 0
    v = V.decide(CODEX_1026, JUDGE_1026, res, RECORDED)
    assert v.verdict == "INCONCLUSIVE" and v.reasons == ("harness-dead:meltdown-brief",)
    assert v.inputs["jev"]["model"] == "jev-1.14.0"


def test_live_replay_jev_records_model_and_tokens(live: type[FakeJev]) -> None:
    b = _budget()
    st = R.jev_state(CODEX_1026, JUDGE_1026, R.ReplayConfig(agent="codex", model="gpt-6.1-sol", reps=3))
    res = R.run_jev(st, "live", b)
    assert res.status == "ok" and res.effect == "unclear" and res.harness_problem == pytest.approx(0.81)
    assert (res.model, res.tokens_in, res.tokens_out) == ("jev-1.14.2", 431, 9)
    assert res.summary()["model"] == "jev-1.14.2"
    call = b.calls[-1]
    assert (call["model"], call["stage"], call["tokens_in"], call["tokens_out"]) == (jev.MODEL, "replay-jev", 431, 9)
    assert call["price_basis"] == "call" and call["usd"] == pytest.approx(0.01)   # still the flat per-call price
    assert b.costs()["by_model"][jev.MODEL]["tokens_in"] == 431
    # the night report keeps the scalar fields
    assert N._summary(res, N.JEV_KEYS)["tokens_out"] == 9


def test_live_triage_books_jev_tokens_and_keeps_the_model(live: type[FakeJev]) -> None:
    b = _budget()
    st = N.triage_state(MELTDOWN, [], True, ["meltdown-brief"], {"meltdown-brief": {"claude": (3, 3)}})
    ans = N.JevTriage("live").assess(MELTDOWN, state=st, budget=b)
    assert ans is not None and ans.mode == "live" and ans.action == "wait" and ans.reason == "hard_to_test"
    assert ans.model == "jev-1.14.2" and ans.as_dict()["model"] == "jev-1.14.2"
    assert live.calls[0]["state"] == st
    call = b.calls[-1]
    assert (call["stage"], call["tokens_in"], call["tokens_out"], call["price_basis"]) == ("triage", 612, 14, "call")
    assert b.costs()["by_model"][jev.MODEL] == {"calls": 1, "tokens_in": 612, "tokens_out": 14, "usd": 0.01}
