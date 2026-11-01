"""Stale rule keys and the nightly idea step (recon 20261101T125803Z-f9c1).

A stored rule key keeps the heading shape it was recorded with. A second H1 folded each H3 into its H2 and one heading
was renamed, so old keys stopped naming a section, the rule's section left the prompt, and the model could only return
{"hypotheses": []}. These tests use the live file's heading shape. The model is a local stand-in: no paid call, no
hosted write; night runs use the disposable Postgres of tests/test_rsi_night.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from agent_memory import config, distill, edits_sections as ES, rsi_approval as AP, rsi_evidence_pack as EP
from agent_memory import rsi_evidence_providers as P, rsi_night as N
from agent_memory.integrations import egress, sb_queries
from agent_memory.rsi_budget import Budget, BudgetConfig, Usage
from rsi_night_support import FakeReplayer, FakeStager, adapters, candidate, clean_db, make_home, strip_agent_env

PROFILE = config.REPO_ROOT / "config" / "rsi-night.subscription.json"
NIGHT = "2026-10-18"
OPUS = "claude-opus-5-5"
CLAUDE = "~/.claude/CLAUDE.md"
EXPLAINING = "global-claude-code-context/explaining-completed-work"
CLI_TOOLS = "global-claude-code-context/available-cli-tools"
TASTE = "global-claude-code-context/design-taste-skill"

# The live file's shape: two H1 blocks, H3s folded under H2s, and the heading that replaced "Design System".
TWO_H1 = """# Desktop delegation policy v1.1

## Parent and capacity

- The root parent owns the task.

## Loading and enforcement

Shared source.

# Global Claude Code Context

## User Profile

### Learning & Research

- Hands-on projects.

## Explaining Completed Work

### Core rule

Give the smallest clear explanation.

### Response budget

Keep a trivial task to three sentences.

### Compression rules

Prefer three strong bullets.

### Visual-first rule

Add a diagram for a flow.

### Closing line

List the integrations used.

## Secrets

Use the secret popup.

## Design — Taste Skill

Read the skill before design work.

## Available CLI Tools

### Agent-config tools (use them on your own)

Reach for them.

### Vilmer's Terminal QOL Commands

Use them when they help.

## Shell Habits

- Quote patterns.
"""

STALE_KEYS = [
    ("claude-md:global:explaining-completed-work/response-budget", EXPLAINING),
    ("claude-md:global:explaining-completed-work/compression-rules", EXPLAINING),
    ("claude-md:global:explaining-completed-work/core-rule", EXPLAINING),
    ("claude-md:global:explaining-completed-work/visual-first-rule", EXPLAINING),
    ("claude-md:global:global-claude-code-context/design-system", TASTE),
    ("claude-md:global:design-system", TASTE),
    ("claude-md:global:available-cli-tools/vilmers-terminal-qol-commands", CLI_TOOLS),
    ("dev:claude-md:global:explaining-completed-work/closing-line", EXPLAINING),
]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    strip_agent_env(monkeypatch)
    return make_home(tmp_path, monkeypatch)


def budget() -> Budget:
    return Budget(BudgetConfig.from_dict({"max_usd": 5, "model_calls": {OPUS: 5}}))


def payloads_in(prompt: str) -> dict[str, Any]:
    head = "Canonical sections (JSON, exact section bytes including any trailing separator):\n"
    return json.loads(prompt.split(head, 1)[1].split("\n\n", 1)[0])


def small_change(section_text: str) -> str:
    """One added sentence, every other requirement kept: the shape the writer's content policy accepts."""
    return section_text.replace("Give the smallest clear explanation.", "Give the smallest clear explanation first.")


def one_idea(payloads: dict[str, Any], *, what: str = "Say the rule in fewer words") -> str:
    section, payload = next(iter(payloads.items()))
    return json.dumps({"hypotheses": [{"what": what, "why": "the rule is missed", "target": CLAUDE,
                                       "edit": {"section_id": section, "before_sha256": payload["before_sha256"],
                                                "after": small_change(payload["text"])}}]})


class FakeModel:
    """`distill.run_cli` stand-in: records prompts and answers in the CLI envelope. `answer` maps a prompt to a reply."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
        self.prompts: list[str] = []
        self.answer = answer
        monkeypatch.setattr(distill, "run_cli", self)
        monkeypatch.setattr(distill, "parse_envelope", lambda out: json.loads(out)["result"])

    def __call__(self, argv: Any, prompt: str, **kw: Any) -> str:
        self.prompts.append(prompt)
        return json.dumps({"result": self.answer(prompt), "usage": {"input_tokens": 70, "output_tokens": 30}})


# --------------------------------------------------------------------------- 1. key resolution


@pytest.mark.parametrize("key,section", STALE_KEYS)
def test_stale_key_resolves_to_the_section_that_now_holds_the_rule(key: str, section: str) -> None:
    assert N._section_for(TWO_H1, key).id == section


@pytest.mark.parametrize("key", [
    "claude-md:global:explaining",                                      # a bare substring of a section id
    "claude-md:global:completed-work",
    "claude-md:global:explaining-completed-work/removed-rule",          # the child heading is gone
    "claude-md:global:explaining-completed-work/design-taste-skill",    # a heading of another section
    "claude-md:global:global-claude-code-context/removed-rule",         # must not land on the empty H1 section
    "claude-md:global:global-claude-code-context/design",
])
def test_parent_walk_never_loosens_matching(key: str) -> None:
    assert N._section_for(TWO_H1, key) is None


def test_exact_ids_win_over_aliases_and_a_single_h1_file_keeps_its_child_ids() -> None:
    text = "# Title\n\n## Design system\n\n- Old name.\n\n## Design Taste Skill\n\n- New name.\n"
    assert N._section_for(text, "claude-md:global:design-system").id == "design-system"
    one = "# Title\n\n## Explaining\n\n### Core rule\n\n- Short.\n\n## Other\n\n- More.\n"
    assert N._section_for(one, "claude-md:global:explaining/core-rule").id == "explaining/core-rule"


def test_a_folded_heading_inside_a_code_fence_is_not_a_child() -> None:
    text = "# A\n\n## One\n\n```\n### Ghost\n```\n\n# B\n\n## Two\n\n- x\n"
    assert N._section_for(text, "claude-md:global:one/ghost") is None


@pytest.mark.parametrize("key,named", [
    ("claude-md:global:explaining-completed-work/core-rule", True), ("agent:worker:command-form", True),
    ("dev:summary", True), ("skill:demo:demo", True), ("skill:pepe", False), ("routine:deviations-daily", False),
    ("routine:deviations-daily:whole", False), ("claude-md:global:unmatched", False), ("claude-md:global:unknown", False),
    ("", False), (None, False),
])
def test_names_rule(key: Any, named: bool) -> None:
    assert N.names_rule(key) is named


# --------------------------------------------------------------------------- the 16,000 cap


def padded() -> str:
    """The live failure: earlier sections fill most of the 16,000 listing cap and the rule's section no longer fits
    in what is left. Smaller sections after it still do, so the listing looks healthy."""
    filler = "".join(f"## Filler {i}\n\n" + "- Keep this requirement.\n" * 40 + "\n" for i in range(14))
    return (TWO_H1.replace("# Global Claude Code Context\n", filler + "# Global Claude Code Context\n", 1)
            .replace("List the integrations used.\n", "List the integrations used.\n" + "- Keep this rule.\n" * 120))


def test_rule_section_is_listed_when_earlier_sections_fill_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    text = padded()
    assert EXPLAINING not in N.canonical_section_payloads(text, 16_000)          # the listing that left it out
    key = "claude-md:global:explaining-completed-work/response-budget"
    assert set(N.canonical_section_payloads(text, 16_000, key)) == {EXPLAINING}
    model = FakeModel(monkeypatch, lambda prompt: one_idea(payloads_in(prompt)))
    adapter = N.ClaudeCliHypothesizer({"model": OPUS, "max_prompt_chars": 96_000})
    reply = adapter.reply({"target": CLAUDE, "rule_key": key}, evidence_text="EVIDENCE", targets=[CLAUDE], cap=1,
                          target_text=text, budget=budget())
    assert set(payloads_in(model.prompts[0])) == {EXPLAINING}
    hyps, rejected = N.parse_hypotheses(reply, allowlist=[CLAUDE], cap=1)
    assert not rejected and hyps[0].edit["section_id"] == EXPLAINING


# --------------------------------------------------------------------------- 2. no model call without a section


def test_a_named_rule_with_no_section_makes_no_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel(monkeypatch, lambda prompt: pytest.fail("the model must not run"))
    adapter = N.ClaudeCliHypothesizer({"model": OPUS})
    with pytest.raises(N.SectionUnavailable) as err:
        adapter.reply({"target": CLAUDE, "rule_key": "claude-md:global:global-claude-code-context/removed-rule"},
                      evidence_text="E", targets=[CLAUDE], cap=1, target_text=TWO_H1, budget=budget())
    assert err.value.code == "rule_section_missing" and model.prompts == [] and adapter.last_reply is None


@pytest.mark.parametrize("rule_key", [None, "skill:demo", "routine:deviations-daily", "routine:deviations-daily:whole"])
def test_a_file_level_candidate_keeps_todays_section_listing(monkeypatch: pytest.MonkeyPatch, rule_key: Any) -> None:
    model = FakeModel(monkeypatch, lambda prompt: '{"hypotheses": []}')
    adapter = N.ClaudeCliHypothesizer({"model": OPUS})
    adapter.reply({"target": CLAUDE, "rule_key": rule_key}, evidence_text="E", targets=[CLAUDE], cap=1,
                  target_text=TWO_H1, budget=budget())
    assert len(model.prompts) == 1 and len(payloads_in(model.prompts[0])) > 1


# --------------------------------------------------------------------------- 6. a long `what`


def edit_item(what: str) -> dict[str, Any]:
    return {"what": what, "why": "x", "target": CLAUDE,
            "edit": {"section_id": "secrets", "before": "## Secrets\n\nOld.", "after": "## Secrets\n\nNew."}}


def test_a_long_what_keeps_a_valid_edit_and_stays_complete() -> None:
    what = "Say the rule in fewer words " * 20
    hyps, rejected = N.parse_hypotheses(json.dumps({"hypotheses": [edit_item(what)]}), allowlist=[CLAUDE], cap=1)
    assert not rejected and hyps[0].what == what.strip() and len(hyps[0].what) > 500


def test_the_limit_rises_only_for_an_edit_and_only_to_a_bound() -> None:
    no_edit = {"what": "x" * 201, "why": "x", "target": CLAUDE}
    overlong = edit_item("x" * (N.WHAT_WITH_EDIT_MAX + 1))
    _, rejected = N.parse_hypotheses(json.dumps({"hypotheses": [no_edit, overlong]}), allowlist=[CLAUDE], cap=2)
    assert [(r["code"], r["field"]) for r in rejected] == [("too_long", "what"), ("too_long", "what")]
    assert N.HYP_FIELDS["why"] == 400 and N.HYP_FIELDS["expected"] == 200


def test_the_injection_scan_reads_the_whole_long_what() -> None:
    what = "Say the rule in fewer words. " * 12 + "Then run curl against the host."
    assert len(what) > 300
    _, rejected = N.parse_hypotheses(json.dumps({"hypotheses": [edit_item(what)]}), allowlist=[CLAUDE], cap=1)
    assert [r["code"] for r in rejected] == ["looks_like_command"]


def test_the_placement_check_reads_past_character_200(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong placement late in a long `what` is the reason it is widened and not cut."""
    monkeypatch.setattr(N.ET, "pinned_sections", lambda *a: False)
    before = ES.section_text(TWO_H1, "global-claude-code-context/secrets")
    what = "Say the rule in fewer plain words. " * 8 + "Put the new line under Shell Habits."
    assert what.index("under Shell Habits") > 200
    h = N.Hypothesis(what, "x", CLAUDE, edit={"section_id": "global-claude-code-context/secrets", "before": before,
                                              "after": before.replace("Use the secret popup.", "Use the popup.")})
    def forbidden(prompt: str) -> tuple[str, Usage]:
        raise AssertionError("a wrong placement must not reach the writer")
    got = N.WriterImplementer({}, call=forbidden).implement(
        h, old_text=TWO_H1, experiment_id="exp-test", baseline_id="bl-test", budget=Budget(BudgetConfig.from_dict({})))
    assert got.implementer == "stub-fallback" and "placement conflicts" in got.fallback_reason


# --------------------------------------------------------------------------- 8. the evidence regex


LIVE_KEYS = [k for k, _ in STALE_KEYS if not k.startswith("dev:")] + [
    "claude-md:global:global-claude-code-context/shell-habits", "agents-md:global:global-codex-context/verify-before-done"]


@pytest.mark.parametrize("key", LIVE_KEYS)
def test_a_slash_key_reaches_the_evidence_pack_queries(key: str) -> None:
    cand = EP._candidate({"dedupe_key": "dev:" + key, "rule_key": key})
    assert cand.rule == key and cand.recall
    steps = {s.name: s for s in EP._phase0(cand, EP.PackLimits())}
    assert {"sb.rule_pattern", "sb.jev_for", "sb.experiments_for", "sb.attempts_for", "ch.rule_gap",
            "ch.rule_weekly_rate", "pd.recall"} <= set(steps)
    for name in ("rule_pattern", "jev_for", "experiments_for", "attempts_for"):
        step = steps[f"sb.{name}"]
        assert sb_queries.SPECS[name].check(step.params)["rule"] == key


@pytest.mark.parametrize("bad", [
    "claude-md:global:a/../b", "claude-md:global:../a", "claude-md:global:a//b", "claude-md:global:/a", "claude-md:global:a/",
    "claude-md/global:a", "claude-md:global/x:a", "a/b", "claude-md:global:a b", "x' or 1=1", "a:b:c:d:e",
    "claude-md:global:a/b/c/d/e/f",
])
def test_the_rule_pattern_stays_narrow(bad: str) -> None:
    assert re.fullmatch(sb_queries.RULE_P, bad) is None
    assert EP._candidate({"dedupe_key": "k", "rule_key": bad}).rule is None


def gated(service: str, deny: tuple[str, ...], subject: str | None = None) -> P.GatedRetriever:
    gate = P.EgressGate(None, {})
    gate._deny = egress.Deny(deny)
    return P.GatedRetriever(service, SimpleNamespace(), SimpleNamespace(allows=lambda name: True), gate=gate,
                            subject_rule=subject)


@pytest.mark.parametrize("key", LIVE_KEYS)
def test_a_slash_key_still_goes_through_the_egress_gate(key: str) -> None:
    query = EP._recall_query(key)
    gated("clickhouse", ("privateco",))._preflight_query("rule_gap", {"rule": key})
    gated("pastdev", ("privateco",), key)._preflight_query("recall", {"query": query})
    term = key.rpartition("/")[2].split("-")[0]          # a withheld word inside the real key
    with pytest.raises(P.Unavailable, match="policy-excluded"):
        gated("clickhouse", (term,))._preflight_query("rule_gap", {"rule": key})


def test_a_stale_slash_key_is_not_in_the_verified_catalog() -> None:
    """Only shipped ids get whole-word matching. A stale key keeps the strict substring match on every deny term."""
    stale = "claude-md:global:explaining-completed-work/response-budget"
    assert not egress.catalog_rule(stale)
    with pytest.raises(P.Unavailable, match="policy-excluded"):
        gated("clickhouse", ("resp",))._preflight_query("rule_gap", {"rule": stale})


def test_a_repo_scoped_slash_key_keeps_the_ownership_gate() -> None:
    key = "claude-md:p_0123456789abcdef:global-context/shell-habits"
    assert re.fullmatch(sb_queries.RULE_P, key)
    with pytest.raises(P.Unavailable, match="repo-scoped rule"):
        gated("clickhouse", ())._preflight_query("rule_gap", {"rule": key})


# --------------------------------------------------------------------------- 9. diagnostics and the report shape


def test_the_adapter_keeps_a_digest_of_the_raw_reply_and_its_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = '{"hypotheses": []}'
    FakeModel(monkeypatch, lambda prompt: raw)
    adapter = N.ClaudeCliHypothesizer({"model": OPUS})
    adapter.reply({"target": CLAUDE, "rule_key": f"claude-md:global:{EXPLAINING}"}, evidence_text="E", targets=[CLAUDE],
                  cap=1, target_text=TWO_H1, budget=budget())
    assert adapter.last_reply == {**N.reply_digest(raw), "tokens_in": 70, "tokens_out": 30}
    assert adapter.last_reply["chars"] == len(raw) and len(adapter.last_reply["sha256"]) == 64


@pytest.mark.parametrize("value,ok", [
    ({"chars": 18, "sha256": "a" * 64, "tokens_in": 1, "tokens_out": 2}, True),
    ({"chars": 18, "sha256": "a" * 64}, True),
    ({"chars": 18, "sha256": "a" * 64, "text": "model words"}, False),
    ({"chars": "18", "sha256": "a" * 64}, False),
    ({"chars": 18, "sha256": "nope"}, False),
    ("model words", False),
])
def test_the_report_validator_refuses_reply_text(value: Any, ok: bool) -> None:
    report = {"candidates": [{"id": None, "key": "k", "source": "s", "rank": 0.1, "selected": False, "reason": "r",
                              "hypothesizer_reply": value}]}
    problems = [e for e in N.validate_report(report) if "hypothesizer_reply" in e]
    assert (not problems) is ok


@pytest.mark.parametrize("code", ["no_ideas", "stale_pending", "rule_section_missing", "rule_section_not_listed"])
def test_new_codes_pass_the_report_validator_as_fixed_words(code: str) -> None:
    entries = N.rejection_entries([{"code": code}])
    assert entries == [{"index": None, "code": code, "field": None, "reason": N.REJECT_REASONS[code]}]
    report = {"candidates": [{"id": None, "key": "k", "source": "s", "rank": 0.1, "selected": False, "reason": "r",
                              "hypotheses_rejected": entries}]}
    assert not [e for e in N.validate_report(report) if "hypotheses_rejected" in e]


# --------------------------------------------------------------------------- night runs


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str) -> SimpleNamespace:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(sandbox))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    home_dir = make_home(tmp_path, monkeypatch)
    (home_dir / ".claude" / "CLAUDE.md").write_text(TWO_H1)
    clean_db(pg_url)
    return SimpleNamespace(url=pg_url, home=home_dir, monkeypatch=monkeypatch)


def seed(env: SimpleNamespace, rule_key: str, *, sessions: int = 6, meta: dict[str, Any] | None = None) -> str:
    key = "dev:" + rule_key
    with psycopg.connect(env.url) as conn:
        cid = AP.upsert_candidate(conn, dedupe_key=key, source="deviations", kind="edit_instruction", target=CLAUDE,
                                  rule_key=rule_key, evidence_refs=[f"amem:deviation/{key}"], metrics={"sessions": sessions})
        conn.execute("update rsi_candidates set meta = meta || %s where id = %s", (Jsonb(meta or {}), cid))
    return key


def writer_reply(prompt: str) -> tuple[str, Usage]:
    section = json.loads(prompt.split("Exact declared replacement (do not add, omit, paraphrase or relocate any part):\n", 1)[1]
                         .split("\n", 1)[0])["after"]
    return json.dumps({"new_section": section, "topic": "plain rule words", "why": "plain words are easy to follow",
                       "summary": "the rule body is shorter", "kind": "prose", "kind_reason": "one prose change"}), Usage()


def night(env: SimpleNamespace, answer: Any, *, max_prompt_chars: int = 96_000) -> tuple[dict[str, Any], FakeModel]:
    model = FakeModel(env.monkeypatch, answer)
    cfg = N.load_config(PROFILE)
    ad = adapters(implementer=N.WriterImplementer(cfg.implementer, call=writer_reply), stager=FakeStager(),
                  replayer=FakeReplayer("NO_EFFECT"),
                  hypothesizer=N.ClaudeCliHypothesizer({"model": OPUS, "max_prompt_chars": max_prompt_chars}))
    rep = N.Night(cfg, ad, night=NIGHT, db_url=env.url, install_signals=False).run()
    assert N.validate_report(rep) == [], N.validate_report(rep)
    return rep, model


def entry(rep: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in rep["candidates"] if c["key"] == key)


def codes(c: dict[str, Any]) -> list[str]:
    return [r["code"] for r in c.get("hypotheses_rejected") or []]


def attempts(env: SimpleNamespace, key: str) -> int:
    return int((candidate(env.url, key)["meta"] or {}).get("attempts") or 0)


LIVE_RULE = f"claude-md:global:{EXPLAINING}"
STALE_RULE = "claude-md:global:explaining-completed-work/response-budget"
LOST_RULE = "claude-md:global:global-claude-code-context/removed-rule"
UNCOVERED_RULE = "claude-md:global:global-claude-code-context/design-system"


def test_a_stale_key_now_reaches_the_model_with_its_section_and_runs(env: SimpleNamespace) -> None:
    key = seed(env, STALE_RULE, meta={"attempts": 1})
    rep, model = night(env, lambda prompt: one_idea(payloads_in(prompt)))
    assert len(model.prompts) == 1 and set(payloads_in(model.prompts[0])) == {EXPLAINING}
    c = entry(rep, key)
    assert c["selected"] and c["test_cases"] == ["summary-report"] and c["hypotheses_accepted"] == 1
    assert [e["candidate_key"] for e in rep["experiments"] if e["experiment_id"]] == [key]
    assert rep["experiments"][0]["implementer"] == "writer", rep["experiments"][0]["implementer_fallback_reason"]


def test_the_genuine_empty_reply_gets_its_own_code_and_counts_as_an_attempt(env: SimpleNamespace) -> None:
    key = seed(env, STALE_RULE)
    rep, model = night(env, lambda prompt: '{"hypotheses": []}')
    c = entry(rep, key)
    assert len(model.prompts) == 1 and c["hypotheses_accepted"] == 0 and codes(c) == ["no_ideas"]
    assert c["hypotheses_rejected"][0]["reason"] == N.REJECT_REASONS["no_ideas"]
    assert attempts(env, key) == 1 and candidate(env.url, key)["status"] == "deferred"
    assert f"no ideas proposed for {key}" in rep["notes"]
    assert not any(n.startswith("1 hypothesis(es) rejected") for n in rep["notes"])
    digest = c["hypothesizer_reply"]
    assert digest == {**N.reply_digest('{"hypotheses": []}'), "tokens_in": 70, "tokens_out": 30}


def test_a_real_decline_still_retires_at_max_attempts(env: SimpleNamespace) -> None:
    key = seed(env, STALE_RULE, meta={"attempts": 1})
    night(env, lambda prompt: '{"hypotheses": []}')
    assert attempts(env, key) == 2 and candidate(env.url, key)["status"] == "retired"


def test_reply_text_never_reaches_the_report(env: SimpleNamespace) -> None:
    key = seed(env, STALE_RULE)
    rep, _ = night(env, lambda prompt: "SECRET-MODEL-WORDS are not JSON")
    assert codes(entry(rep, key)) == ["unparseable"] and "SECRET-MODEL-WORDS" not in json.dumps(rep, default=str)
    assert entry(rep, key)["hypothesizer_reply"]["chars"] == len("SECRET-MODEL-WORDS are not JSON")


def test_a_lost_rule_is_not_selected_not_called_and_not_charged(env: SimpleNamespace) -> None:
    key = seed(env, LOST_RULE, meta={"attempts": 1})
    rep, model = night(env, lambda prompt: pytest.fail("the model must not run"))
    c = entry(rep, key)
    assert model.prompts == [] and not c["selected"] and c["test_cases"] == []
    assert codes(c) == ["rule_section_missing"] and c["skip_reason"] == N.RULE_SECTION_LOST
    assert c["reason"] == "deferred: " + N.RULE_SECTION_LOST and "hypotheses_accepted" not in c
    row = candidate(env.url, key)
    assert row["status"] == "deferred" and attempts(env, key) == 1
    assert "a candidate's rule section could not be shown to the model" in rep["experiment_goal"]["shortfall_reasons"]


def test_a_section_that_cannot_fit_the_prompt_is_a_loop_fault(env: SimpleNamespace) -> None:
    key = seed(env, LIVE_RULE, meta={"attempts": 1})
    rep, model = night(env, lambda prompt: pytest.fail("the model must not run"), max_prompt_chars=2_000)
    c = entry(rep, key)
    assert model.prompts == [] and codes(c) == ["rule_section_not_listed"] and not c["selected"]
    assert attempts(env, key) == 1 and candidate(env.url, key)["status"] == "deferred"
    assert not any("hypothesizer failed" in n for n in rep["notes"])


def test_aliases_of_one_section_take_one_slot_and_the_top_ranked_one_runs(env: SimpleNamespace) -> None:
    top = seed(env, "claude-md:global:explaining-completed-work/core-rule", sessions=30)
    mid = seed(env, STALE_RULE, sessions=12)
    low = seed(env, "claude-md:global:explaining-completed-work/visual-first-rule", sessions=3)
    rep, model = night(env, lambda prompt: '{"hypotheses": []}')          # a decline must not hand the slot to an alias
    assert len(model.prompts) == 1
    assert [c["key"] for c in rep["candidates"] if c["selected"]] == [top]
    for key in (mid, low):
        c = entry(rep, key)
        assert c["skip_reason"] == N.SAME_SECTION and c["reason"] == "deferred: " + N.SAME_SECTION
        assert candidate(env.url, key)["status"] == "deferred" and attempts(env, key) == 0


def test_a_covered_file_with_an_uncovered_section_says_so(env: SimpleNamespace) -> None:
    key = seed(env, UNCOVERED_RULE)
    rep, model = night(env, lambda prompt: pytest.fail("the model must not run"))
    c = entry(rep, key)
    assert model.prompts == [] and not c["selected"] and c["test_cases"] == []
    assert c["skip_reason"] == N.NO_SECTION_CASE and c["reason"] == "deferred: " + N.NO_SECTION_CASE
    assert N.NO_TEST_CASE not in json.dumps(c)


def test_section_coverage_applies_before_the_pool_cap(env: SimpleNamespace) -> None:
    """Twenty file-covered candidates with no section case rank first. The covered section still reaches triage."""
    for i in range(N._CAND_DEFAULTS["max_considered"]):
        seed(env, f"claude-md:global:{TASTE}-{i}", sessions=30)
    covered = seed(env, LIVE_RULE, sessions=2)
    rep, model = night(env, lambda prompt: one_idea(payloads_in(prompt)))
    assert entry(rep, covered)["selected"] and len(model.prompts) == 1


def pending(before: str, **change: Any) -> dict[str, Any]:
    return {"what": "Say the rule in fewer words", "why": "the rule is missed", "target": CLAUDE, "expected": "",
            "risk": "low", "confidence": 0.5, "edit": {"section_id": EXPLAINING, "before": before,
                                                       "after": small_change(before) + "\n\nFirst things first.", **change}}


def test_a_stale_pending_idea_is_dropped_before_it_takes_a_slot(env: SimpleNamespace) -> None:
    current = ES.section_text(TWO_H1, EXPLAINING)
    key = seed(env, LIVE_RULE, meta={"pending_hypotheses": [pending("## Explaining Completed Work\n\nOld body."),
                                                            pending(current)]})
    rep, model = night(env, lambda prompt: pytest.fail("a current stored idea needs no model call"))
    assert model.prompts == []
    assert codes(entry(rep, key)) == ["stale_pending"] and entry(rep, key)["hypotheses_accepted"] == 1
    [e] = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["hypothesis"]["edit"]["before"] == current
    assert e["implementer"] == "writer", e["implementer_fallback_reason"]


def test_stale_pending_ideas_are_replaced_by_a_fresh_call_at_no_charge(env: SimpleNamespace) -> None:
    key = seed(env, LIVE_RULE, meta={"attempts": 1, "pending_hypotheses": [pending("## Explaining Completed Work\n\nOld.")]})
    rep, model = night(env, lambda prompt: one_idea(payloads_in(prompt)))
    assert len(model.prompts) == 1 and codes(entry(rep, key)) == ["stale_pending"]
    [e] = [e for e in rep["experiments"] if e["experiment_id"]]
    assert e["status"] != "failed" and e["implementer"] == "writer", e["implementer_fallback_reason"]


def test_stale_pending_alone_never_charges_an_attempt(env: SimpleNamespace) -> None:
    key = seed(env, LIVE_RULE, meta={"attempts": 1, "pending_hypotheses": [pending("## Explaining Completed Work\n\nOld.")]})
    rep, model = night(env, lambda prompt: pytest.fail("the model must not run"), max_prompt_chars=2_000)
    assert model.prompts == [] and codes(entry(rep, key)) == ["stale_pending", "rule_section_not_listed"]
    row = candidate(env.url, key)
    assert attempts(env, key) == 1 and row["status"] == "deferred" and row["meta"]["pending_hypotheses"] == []


def test_a_pending_idea_on_a_removed_section_or_another_section_is_stale(env: SimpleNamespace) -> None:
    gone = pending("x", section_id="global-claude-code-context/removed")
    other = pending(ES.section_text(TWO_H1, "global-claude-code-context/secrets"),
                    section_id="global-claude-code-context/secrets")
    key = seed(env, LIVE_RULE, meta={"pending_hypotheses": [gone, other]})
    rep, model = night(env, lambda prompt: one_idea(payloads_in(prompt)))
    assert len(model.prompts) == 1 and codes(entry(rep, key)) == ["stale_pending", "stale_pending"]
