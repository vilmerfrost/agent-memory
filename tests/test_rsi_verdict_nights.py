"""The real 2026-10-25..27 provider verdict inputs, replayed through today's decide() and combined_verdict().

Fixture: tests/fixtures/rsi-verdict-nights-1025-1027.json, copied read-only from each night's report.json
provider_results and the eval records they point to (results.cases / judge / jev / verdict / reasons). Those
records predate scenario scores, so each case's scores are its pass counts (CaseStats default).

Each night ran 3 pairs per provider. Under the shipped policy/2 (min_pairs 5) every one of them is too thin to
decide anything. To see what the new rules change, the second table replays them with the pair minimum the night
had (3) and every other policy/2 rule unchanged."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_memory import rsi_provider_replay as P
from agent_memory import rsi_verdict as V

FIXTURE = Path(__file__).parent / "fixtures" / "rsi-verdict-nights-1025-1027.json"
ROWS = json.loads(FIXTURE.read_text())["rows"]
RECORDED = dataclasses.replace(V.load_policy(), min_pairs=3)

# (experiment, provider): (old verdict, new verdict under policy/2 rules with the night's 3 pairs)
EXPECTED = {
    ("exp-20261025-95f15ef6", "claude"): ("NO_EFFECT", "NO_EFFECT"),
    ("exp-20261025-95f15ef6", "codex"): ("WORSE", "NO_EFFECT"),          # A 3/3 -> B 2/3: one flip
    ("exp-20261025-63a57bf4", "claude"): ("NO_EFFECT", "NO_EFFECT"),
    ("exp-20261025-63a57bf4", "codex"): ("INCONCLUSIVE", "INCONCLUSIVE"),  # now harness-dead (A 0/3, B 0/3)
    ("exp-20261025-93b69041", "claude"): ("NO_EFFECT", "NO_EFFECT"),
    ("exp-20261025-93b69041", "codex"): ("NO_EFFECT", "NO_EFFECT"),
    ("exp-20261026-63a57bf4", "claude"): ("NO_EFFECT", "NO_EFFECT"),
    ("exp-20261026-63a57bf4", "codex"): ("INCONCLUSIVE", "INCONCLUSIVE"),  # now harness-dead
    ("exp-20261027-5a8bda33", "claude"): ("IMPROVES", "NO_EFFECT"),      # A 0/3 -> B 1/3: one flip
    ("exp-20261027-5a8bda33", "codex"): ("WORSE", "NO_EFFECT"),          # A 3/3 -> B 2/3: one flip
    ("exp-20261027-7c731284", "claude"): ("WORSE", "INCONCLUSIVE"),      # checks equal, judge 0/3 for B
    ("exp-20261027-7c731284", "codex"): ("NO_EFFECT", "NO_EFFECT"),
    ("exp-20261027-90b819e4", "claude"): ("INCONCLUSIVE", "NO_EFFECT"),  # A 0/3 -> B 1/3: one flip
    ("exp-20261027-90b819e4", "codex"): ("NO_EFFECT", "NO_EFFECT"),
}

# experiment: (old combined verdict, new combined verdict)
EXPECTED_COMBINED = {
    "exp-20261025-95f15ef6": ("WORSE", "NO_EFFECT"),
    "exp-20261025-63a57bf4": ("INCONCLUSIVE", "INCONCLUSIVE"),
    "exp-20261025-93b69041": ("NO_EFFECT", "NO_EFFECT"),
    "exp-20261026-63a57bf4": ("INCONCLUSIVE", "INCONCLUSIVE"),
    "exp-20261027-5a8bda33": ("WORSE", "NO_EFFECT"),
    "exp-20261027-7c731284": ("WORSE", "INCONCLUSIVE"),
    "exp-20261027-90b819e4": ("INCONCLUSIVE", "NO_EFFECT"),
}


def inputs(row: dict) -> tuple[V.ArmPair, V.JudgeResult, V.JevResult]:
    machine = V.ArmPair(cases=tuple(V.CaseStats(**c) for c in row["cases"]), runs_expected=row["runs_expected"],
                        runs_done=row["runs_expected"], budget_stop=row["budget_stop"])
    return machine, V.JudgeResult(**row["judge"]), V.JevResult(**row["jev"])


def new_verdict(row: dict, policy: V.VerdictPolicy) -> V.Verdict:
    return V.decide(*inputs(row), policy)


def test_fixture_covers_every_provider_verdict_of_the_three_nights():
    assert {(r["experiment"], r["provider"]) for r in ROWS} == set(EXPECTED)
    assert len(ROWS) == 14
    for r in ROWS:
        assert r["old_verdict"] == r["report_verdict"] == EXPECTED[(r["experiment"], r["provider"])][0]
        assert r["old_policy"]["version"] == "rsi-verdict-policy/1"
        assert all(c["pairs"] == 3 for c in r["cases"])


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r['experiment']}-{r['provider']}")
def test_shipped_policy_wants_more_pairs_than_the_nights_ran(row):
    v = new_verdict(row, V.load_policy())
    assert v.verdict == "INCONCLUSIVE" and v.reasons == ("too-few-pairs:3<5",)


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"{r['experiment']}-{r['provider']}")
def test_old_vs_new_verdict_per_provider(row):
    old, new = EXPECTED[(row["experiment"], row["provider"])]
    v = new_verdict(row, RECORDED)
    assert (row["old_verdict"], v.verdict) == (old, new), v.reasons
    # nothing in these three nights is a real regression or improvement by two runs' worth
    assert v.verdict not in ("WORSE", "IMPROVES")


def test_old_vs_new_combined_verdict_per_experiment():
    by_exp: dict[str, list[dict]] = {}
    for r in ROWS:
        by_exp.setdefault(r["experiment"], []).append(r)
    got = {}
    for exp, rows in by_exp.items():
        old = {r["experiment_verdict"] for r in rows}
        assert len(old) == 1
        new = P.combined_verdict([SimpleNamespace(verdict=new_verdict(r, RECORDED).verdict) for r in rows])
        got[exp] = (old.pop(), new)
    assert got == EXPECTED_COMBINED


def test_the_flips_now_read_as_noise_with_their_numbers():
    row = next(r for r in ROWS if (r["experiment"], r["provider"]) == ("exp-20261027-5a8bda33", "codex"))
    assert row["old_reasons"] == ["regressed:worker-final-report A 3/3 -> B 2/3"]
    v = new_verdict(row, RECORDED)
    assert v.reasons == ("checks-equal, judge b_winrate 0.50", "within-noise:worker-final-report A 3/3 -> B 2/3",
                         "score-margin:2")
    row = next(r for r in ROWS if (r["experiment"], r["provider"]) == ("exp-20261027-7c731284", "claude"))
    assert row["old_reasons"] == ["checks-equal", "judge-prefers-baseline:b_winrate 0.00"]
    assert new_verdict(row, RECORDED).reasons == ("checks-equal", "judge-only-preference:b_winrate 0.00")
