"""Mission 005 wave 2 (w-e2e): the wiring between the night and its real pieces, without the Docker sandbox.

- PackEvidence reads the night's own database (the default adapters got no db_url before: a night run with
  `run_night(db_url=...)` built its evidence pack from the process default store).
- The live hypothesizer's `claude -p` child gets no store or provider credential (model_cli_env, K4 accounting).
- `amem synth seed --rsi-demo` (synth.build_m005 / seed_m005): Claude and Codex sessions that repeat one instruction
  failure, deterministic, local-only, idempotent; deviations.candidates groups them per instruction file.
- The demo's pieces: the production fingerprint, the isolated env, the work-dir guard, the provider stand-ins and the
  per-scenario hypothesis fixtures (every one goes through the night's strict parser).
- The Jev fixtures the demo needs: each one's documented state hashes to its file name.

The Docker scenarios (success, no-effect, regression, outage, interruption, stale baseline, budget, injection) are in
sandbox/tests/test_m005_e2e_docker.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agent_memory import (cli, config, deviations as DV, jev, questions as Q, rsi_demo as D, rsi_night as N,
                          rsi_replay as R, synth)
from agent_memory.rsi_budget import Budget, BudgetConfig
from synth_support import fresh_db

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
KEYS = synth.m005_candidate_keys()


# --------------------------------------------------------------------------- the night's evidence pack and its database


def test_pack_evidence_reads_the_nights_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import rsi_evidence_pack as P, store
    seen: dict[str, Any] = {}
    monkeypatch.setattr(P, "build_pack", lambda cand, **kw: seen.update(kw) or "PACK")
    monkeypatch.setattr(store, "connect", lambda url=None, **kw: ("CONN", url))
    ev = N.PackEvidence("postgresql://u@127.0.0.1:5999/night")
    assert ev.pack({"dedupe_key": "k"}) == "PACK"
    assert "retrievers" not in seen and seen["connect"]() == ("CONN", "postgresql://u@127.0.0.1:5999/night")
    stand = {"ch": object()}
    N.PackEvidence("postgresql://u@127.0.0.1:5999/night", retrievers=stand).pack({"dedupe_key": "k"})
    assert seen["retrievers"]["ch"] is stand["ch"]
    assert seen["retrievers"]["sb"].connect() == ("CONN", "postgresql://u@127.0.0.1:5999/night")


def test_default_adapters_and_run_night_pass_the_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = N.load_config()
    assert N.default_adapters(cfg, "postgresql://x@127.0.0.1/a").evidence.db_url == "postgresql://x@127.0.0.1/a"
    got: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_night(cfg: Any, ad: Any, **kw: Any) -> Any:
        got["ad"] = ad
        raise Stop
    monkeypatch.setattr(N, "Night", fake_night)
    with pytest.raises(Stop):
        N.run_night(night="2026-10-18", db_url="postgresql://y@127.0.0.1/b")
    assert got["ad"].evidence.db_url == "postgresql://y@127.0.0.1/b"


# --------------------------------------------------------------------------- reaping an aborted night's containers


class StopsDuringImplement:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def implement(self, hyp: Any, **kw: Any) -> str:
        raise self.exc


@pytest.mark.parametrize("exc,status,reaped", [
    (N.NightAborted("SIGINT"), "aborted", True),
    (N.BudgetExhausted("wall_clock", "hard timeout"), "budget_exhausted", True),
    (N.BudgetExhausted("sandbox_runs", "cap 16"), "budget_exhausted", False)])
def test_a_stop_that_can_cut_a_container_run_reaps_tonights_experiments(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pg_url: str, exc: BaseException, status: str,
        reaped: bool) -> None:
    from rsi_night_support import RULE_A, FakeSandboxOps, adapters, clean_db, make_home, seed_rule, strip_agent_env
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(tmp_path / "sandbox"))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    strip_agent_env(monkeypatch)
    make_home(tmp_path, monkeypatch)
    clean_db(pg_url)
    seed_rule(pg_url, RULE_A, "a")
    box = FakeSandboxOps()
    rep = N.run_night(night="2026-10-18", db_url=pg_url,
                      adapters=adapters(implementer=StopsDuringImplement(exc), sandbox=box))
    assert rep["status"] == status and not rep["checkpoint"]["advanced"]
    exp = rep["experiments"][0]["experiment_id"]
    assert box.reaped == ([exp] if reaped else [])
    assert any(n.startswith("sandbox reap: ") for n in rep["notes"]) is reaped


# --------------------------------------------------------------------------- the live hypothesizer's environment (K4)


def test_model_cli_env_drops_stores_providers_and_secrets() -> None:
    src = {"PATH": "/usr/bin", "HOME": "/h", "LANG": "C", "AMEM_DB_URL": "postgresql://prod", "AMEM_HOOK_OFF": "1",
           "AMEM_SUPABASE_KEY": "x", "PGPASSWORD": "x", "SUPABASE_SERVICE_ROLE_KEY": "x", "TYPESAFE_API_KEY": "x",
           "AMEM_CLICKHOUSE_PASSWORD": "x", "LANGFUSE_SECRET_KEY": "x", "AMEM_PASTDEV_KEY": "x", "NTFY_TOKEN": "x",
           "OPENAI_API_KEY": "x", "GITHUB_TOKEN": "x", "MY_SERVICE_TOKEN": "x", "DATABASE_URL": "x", "RSI_SANDBOX_DIR": "/s",
           "CLAUDE_CODE_OAUTH_TOKEN": "keep", "ANTHROPIC_API_KEY": "keep", "CLAUDE_CONFIG_DIR": "/c", "FAKE_FLAG": "1"}
    env = N.model_cli_env(src)
    assert set(env) == {"PATH", "HOME", "LANG", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CONFIG_DIR",
                        "FAKE_FLAG"}


FAKE_CLAUDE = """#!{py}
import json, os, sys
sys.stdin.read()
open(os.environ["FAKE_ENV_LOG"], "w").write(json.dumps(sorted(os.environ)))
reply = {{"hypotheses": [{{"what": "State the closing line as one checklist item", "why": "agents skip it",
          "target": "~/.claude/CLAUDE.md", "risk": "low", "confidence": 0.6}}]}}
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(reply),
                  "usage": {{"input_tokens": 10, "output_tokens": 5}}, "total_cost_usd": 0.01}}))
"""


def test_claude_cli_hypothesizer_child_gets_no_store_or_provider_credential(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    script, log = tmp_path / "claude", tmp_path / "env.json"
    script.write_text(FAKE_CLAUDE.format(py=sys.executable))
    script.chmod(0o755)
    for k, v in (("AMEM_EDITS_CLAUDE", str(script)), ("FAKE_ENV_LOG", str(log)),
                 ("AMEM_DB_URL", "postgresql://u:p@db.example.invalid/prod"), ("AMEM_LANGFUSE_SECRET_KEY", "sk-lf-x"),
                 ("SUPABASE_ACCESS_TOKEN", "sbp_x"), ("PGPASSWORD", "x")):
        monkeypatch.setenv(k, v)
    b = Budget(BudgetConfig.from_dict({"max_usd": 5.0, "model_calls": {"opus": 2}}))
    reply = N.ClaudeCliHypothesizer({}).reply({"dedupe_key": "k", "target": "~/.claude/CLAUDE.md"}, evidence_text="",
                                              targets=["~/.claude/CLAUDE.md"], cap=2, target_text="# x\n", budget=b)
    assert json.loads(reply)["hypotheses"][0]["target"] == "~/.claude/CLAUDE.md"
    names = set(json.loads(log.read_text()))
    assert {"AMEM_DB_URL", "AMEM_LANGFUSE_SECRET_KEY", "SUPABASE_ACCESS_TOKEN", "PGPASSWORD"}.isdisjoint(names)
    assert "AMEM_HOOK_OFF" in names and "PATH" in names and "FAKE_ENV_LOG" in names


# --------------------------------------------------------------------------- synth --rsi-demo


def test_build_m005_is_deterministic_and_repeats_one_failure_in_claude_and_codex() -> None:
    a, b = synth.build_m005(NOW), synth.build_m005(NOW)
    assert json.dumps(a, default=str, sort_keys=True) == json.dumps(b, default=str, sort_keys=True)
    tools = {s["session_id"]: s["tool"] for s in a["sessions"]}
    assert sorted(tools.values()).count("claude") == 4 and sorted(tools.values()).count("codex") == 3
    assert {d["rule_id"] for d in a["deviations"]} == {synth.M005_RULE} and len(a["deviations"]) == 8
    for d in a["deviations"]:
        assert d["instruction_source"] == synth.M005_SOURCES[tools[d["session_id"]]]
        assert d["created_at"] < NOW and (NOW - d["created_at"]).days < 7     # inside a first night's window
    assert all("SYNTHETIC" in s["title"] and s["meta"]["synthetic"] for s in a["sessions"])
    assert [e["status"] for e in a["edits"]] == ["reverted"]                    # the history: tried before
    assert KEYS == {"claude": f"dev:claude-md:global:{synth.M005_RULE}", "codex": f"dev:agents-md:global:{synth.M005_RULE}"}


def test_seed_m005_refuses_a_non_local_database() -> None:
    with pytest.raises(synth.UnsafeTarget):
        synth.seed_m005("postgresql://u:p@db.example.com:5432/postgres")


def test_seed_m005_seeds_once_and_groups_per_instruction_file(pg_url: str, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    for rel, src in ((".claude/CLAUDE.md", D.REPLAY_HOME / ".claude" / "CLAUDE.md"),
                     (".codex/AGENTS.md", D.BENCH / "home" / ".codex" / "AGENTS.md")):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_bytes(src.read_bytes())
    monkeypatch.setenv("HOME", str(home))
    with fresh_db(pg_url) as url:
        anchor = (datetime.now(UTC).date().replace(day=1))
        synth.seed(url, anchor.replace(year=anchor.year - 1), out=tmp_path / "gold")
        first = synth.seed_m005(url)
        again = synth.seed_m005(url)
        assert first["status"] == "seeded" and again["status"] == "reused" and again["counts"] == first["counts"]
        assert first["counts"] == {"sessions": 7, "deviations": 8, "edits": 1}
        groups = {(g["instruction_source"], g["rule_id"]): g for g in DV.candidates(days=8, db_url=url)
                  if g["rule_id"] == synth.M005_RULE}
        claude, codex = groups[("claude-md:global", synth.M005_RULE)], groups[("agents-md:global", synth.M005_RULE)]
        assert (claude["sessions"], claude["count"], claude["tools"]) == (4, 5, {"claude": 5})
        assert (codex["sessions"], codex["count"], codex["tools"]) == (3, 3, {"codex": 3})
        assert claude["target_paths"] == ["~/.claude/CLAUDE.md"] and codex["target_paths"] == ["~/.codex/AGENTS.md"]


def test_cli_synth_seed_rsi_demo(pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    with fresh_db(pg_url) as url:
        assert cli.main(["synth", "seed", "--db-url", url, "--anchor", "2026-01-01", "--out", str(tmp_path),
                         "--rsi-demo", "--json"]) == 0
        rep = json.loads(capsys.readouterr().out)
        assert rep["rsi_demo"]["status"] == "seeded" and rep["rsi_demo"]["candidate_keys"] == KEYS
        with psycopg.connect(url) as conn:
            n = conn.execute("select count(*) from deviations where rule_id = %s", (synth.M005_RULE,)).fetchone()[0]
        assert n == 8


# --------------------------------------------------------------------------- the demo's pieces


def test_production_fingerprint_is_stable_and_sees_a_change(tmp_path: Path) -> None:
    h = tmp_path / "fakehome"
    (h / ".claude").mkdir(parents=True)
    (h / ".agents" / "skills" / "x").mkdir(parents=True)
    (h / ".claude" / "CLAUDE.md").write_text("# rules\n")
    (h / ".agents" / "skills" / "x" / "SKILL.md").write_text("skill\n")
    (h / "sandbox").mkdir()
    a, b = D.production_fingerprint(h), D.production_fingerprint(h)
    assert a == b and D.fingerprint_diff(a, b) == []
    (h / ".agents" / "skills" / "x" / "SKILL.md").write_text("skill changed\n")
    (h / "sandbox" / "new").mkdir()
    c = D.production_fingerprint(h)
    assert D.fingerprint_diff(a, c) == ["listing:~/sandbox", "tree:~/.agents/skills"]


def test_demo_env_is_an_allowlist_with_local_store_and_tripwire(tmp_path: Path) -> None:
    w = D.World(work=tmp_path, home=tmp_path / "home", ac=tmp_path / "home" / "agent-config", sandbox=tmp_path / "sandbox",
                bin=tmp_path / "bin", tripwire_log=tmp_path / "trip.log", db_url="postgresql://postgres:postgres@127.0.0.1:5999/x")
    base = {"PATH": "/usr/bin", "HOME": str(D.REAL_HOME), "AMEM_DB_URL": "postgresql://prod", "AMEM_SUPABASE_URL": "https://x",
            "LANGFUSE_SECRET_KEY": "x", "AMEM_PASTDEV_KEY": "x", "CLAUDECODE": "1", "CODEX_SANDBOX": "seatbelt",
            "RSI_ARTIFACTS_DIR": str(D.REAL_HOME / "sandbox" / "artifacts"), "TYPESAFE_API_KEY": "x", "AMEM_JEV_LIVE": "1"}
    env = D.demo_env(w, hypotheses=tmp_path / "hyp", base=base)
    assert env["HOME"] == str(w.home) and env["RSI_SANDBOX_DIR"] == str(w.sandbox) and env["AMEM_NO_DOTENV"] == "1"
    assert env["AMEM_DB_URL"].startswith("postgresql://postgres:postgres@127.0.0.1:")
    assert env["AMEM_ADMIN_SECURITY"] == str(w.bin / "security") and env["AMEM_RSI_HYPOTHESES_DIR"] == str(tmp_path / "hyp")
    for k in ("AMEM_SUPABASE_URL", "LANGFUSE_SECRET_KEY", "AMEM_PASTDEV_KEY", "CLAUDECODE", "CODEX_SANDBOX",
              "RSI_ARTIFACTS_DIR", "TYPESAFE_API_KEY", "AMEM_JEV_LIVE"):
        assert k not in env


@pytest.mark.parametrize("rel", ["", "sandbox", "sandbox/x", ".claude", "agent-config/tmp", "Projects/agent-memory/x"])
def test_demo_refuses_work_dirs_on_protected_paths(rel: str) -> None:
    with pytest.raises(D.DemoError):
        D.check_isolated(D.REAL_HOME / rel if rel else D.REAL_HOME)


def test_environ_restores_the_callers_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("W_E2E_MARK", "1")
    with D.environ({"PATH": "/usr/bin", "HOME": "/nowhere"}):
        assert os.environ.get("W_E2E_MARK") is None and os.environ["HOME"] == "/nowhere"
    assert os.environ["W_E2E_MARK"] == "1"


def test_render_shows_the_ten_steps_and_the_decision_commands() -> None:
    acts = {"apply": "amem rsi decide P apply --hash H", "reject": "amem rsi decide P reject --hash H",
            "details": "amem rsi show P"}
    r = {"scenario": "success", "expect": "IMPROVES", "why": "w", "night": "2026-10-18", "status": "ok", "ok": True,
         "problems": [], "seconds": 6.0, "checkpoint": {"advanced": True}, "costs": {"total_usd": 0.0},
         "seed": {"base": {"anchor": "2026-09-18"}, "rsi_demo": {"status": "seeded", "dataset": "d", "rule": "r",
                                                                 "counts": {"sessions": 7, "deviations": 8}}},
         "candidates": [{"key": "k", "rank": 0.7, "selected": True, "reason": "selected"}],
         "evidence": {"findings": [{}], "health": {"supabase": "ok"}},
         "experiment": {"hypothesis": {"what": "x"}, "target": "~/.claude/CLAUDE.md", "artifact_hash": "H",
                        "baseline_id": "bl-1", "arms": {"A": {"check_pass_rate": 0.6, "runs": 3},
                                                         "B": {"check_pass_rate": 1.0, "runs": 3}},
                        "verdict": "IMPROVES", "verdict_reasons": ["improved"], "status": "staged", "proposal_id": "P"},
         "eval_problems": [], "brief": {"path": "/tmp/b.md", "pending": {"total": 1, "items": [{"actions": acts}]}},
         "checks": {"production_unchanged": True, "synthetic_production_unchanged": True,
                    "scratch_agent_config_unchanged": True, "keychain_tripwire_hits": 0, "containers_left": [],
                    "improve_calls": ["stage"], "db_host": "127.0.0.1"}}
    out = D.render([r])
    assert all(f"{n:>2}. " in out for n in range(1, 11)) and out.count("SYNTHETIC DATA") == 1
    assert all(a in out for a in acts.values()) and "-> OK" in out


@pytest.mark.parametrize("mode", ["healthy", "outage", "injection"])
def test_provider_stand_ins_use_the_real_query_specs(mode: str) -> None:
    from agent_memory.integrations import evidence as E
    ps = D.stand_in_providers(mode, canary="/tmp/x")
    rule = f"claude-md:global:{synth.M005_RULE}"
    res = {k: E.run_query(ps[k], q, p) for k, q, p in (
        ("ch", "rule_gap", {"rule": rule, "days": "30", "min_n": "1", "limit": "6"}),
        ("pd", "recall", {"query": "synth m005 closing line", "limit": "6"}),
        ("lf", "experiment_scores", {"experiment_id": "exp-20261018-abc"}))}
    if mode == "outage":
        assert res["ch"].status == "error" and res["pd"].status == "error" and res["lf"].status == "unavailable"
    else:
        assert res["ch"].status == "ok" and res["ch"].refs[0].metrics["gap"] == 0.6
        assert res["pd"].status == "ok" and res["pd"].refs[0].subject_refs[0].startswith("amem:edit/")
        assert ("IGNORE ALL PREVIOUS" in (res["pd"].refs[0].summary or "")) is (mode == "injection")


@pytest.mark.parametrize("name", list(D.SCENARIOS))
def test_scenario_hypothesis_fixtures_parse_strictly(name: str) -> None:
    s = D.SCENARIOS[name]
    d = D.BENCH / "hypotheses" / s.hypotheses
    allow = ["~/.claude/CLAUDE.md", "~/.codex/AGENTS.md"]
    for f in sorted(d.glob("*.json")):
        assert f.stem in {hashlib.sha256(k.encode()).hexdigest()[:16] for k in KEYS.values()}, f.name
        ok, rejected = N.parse_hypotheses(f.read_text(), allowlist=allow, cap=2)
        assert len(ok) == 1, (f, rejected)
        assert len(rejected) == (2 if name == "injection" else 0)
    if name in ("success", "injection"):     # the stub agent recognizes the appended line as its closing_line rule
        sa = _stub_agent()
        h = N.parse_hypotheses((d / "3231684c3adeef71.json").read_text(), allowlist=allow, cap=2)[0][0]
        line = N.stub_change("# x\n", h).splitlines()[-1]
        assert sa.RULES["closing_line"].search(line).group(2) == "Skills used: none"
    if name == "regression":
        h = N.parse_hypotheses((d / "3231684c3adeef71.json").read_text(), allowlist=allow, cap=2)[0][0]
        assert _stub_agent().RULES["no_edit_existing"].search(N.stub_change("# x\n", h))


def _stub_agent() -> Any:
    import importlib.util
    spec = importlib.util.spec_from_file_location("w_e2e_stub_agent", R.AGENT_SRC / "stub_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- the demo's Jev fixtures


def _demo_fixtures() -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for p in sorted(config.JEV_FIXTURES_DIR.glob("*.json")):
        body = json.loads(p.read_text())
        if "M005 demo" in str(body.get("_note", "")):
            out.append((p, body))
    return out


def test_demo_jev_fixtures_hash_to_their_state() -> None:
    fx = _demo_fixtures()
    assert len(fx) >= 5
    triage = Q.load_file(Q.questions_dir() / "rsi-triage.toml", "rsi-triage").jev_questions()
    replay = Q.load_file(R.QUESTIONS_FILE, "rsi-replay").jev_questions()
    for p, body in fx:
        if "_triage_state" in body:
            assert jev.fixture_key(body["_triage_state"], triage) == p.stem, p.name
            jev._collect(triage, {k: jev._answer_adapter().validate_json(json.dumps(v))
                                  for k, v in body["answers"].items()})
        else:
            assert jev.fixture_key(body["_replay_state"], replay) == p.stem, p.name
        assert "_state" not in body                     # tests/test_tasks_state.py reserves "_state" for routines


def test_model_cli_env_home_override_is_rehearsal_only() -> None:
    """AMEM_RSI_MODEL_HOME (set only by the paid rehearsal) gives the host `claude -p` the real HOME for its login;
    the variable itself never reaches the child."""
    env = N.model_cli_env({"PATH": "/usr/bin", "HOME": "/tmp/fake", "AMEM_RSI_MODEL_HOME": "/Users/real"})
    assert env == {"PATH": "/usr/bin", "HOME": "/Users/real"}
    assert N.model_cli_env({"PATH": "/usr/bin", "HOME": "/h"})["HOME"] == "/h"


def test_live_rehearsal_is_not_ok_unless_the_night_ran() -> None:
    """2026-10-18: the first paid rehearsal printed REHEARSAL OK although the night failed its credentials preflight."""
    from agent_memory import rsi_rehearsal as R
    failed = {"gate": {"status": "SKIP"}, "night_job": {"status": "FAIL", "items": ["credentials replay agent"],
              "fields": {"status": "preflight_failed"}}, "experiment": {}, "brief_job": {"status": "PASS"},
              "report": {"costs": {"total_usd": 0.0}}, "max_usd": 1.0,
              "fingerprint": {"before": "a", "after": "a"}, "checks": {"synthetic_production_unchanged": True,
              "scratch_agent_config_unchanged": True, "db_host": "127.0.0.1"}}
    probs = R.problems_of(failed, live=True)
    assert any("did not run to the end" in p for p in probs)
    assert any("no experiment to a verdict" in p for p in probs)
    ran = {**failed, "night_job": {"status": "PASS", "fields": {"status": "ok"}},
           "experiment": {"verdict": "NO_EFFECT", "status": "rejected"}, "report": {"costs": {"total_usd": 1.2}}}
    probs = R.problems_of(ran, live=True)
    assert not any("did not run" in p or "no experiment" in p for p in probs)
    assert any("over the approved $1.00" in p for p in probs)


def test_a_long_target_never_cuts_the_evidence_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-10-18 review: the prompt was cut at max_prompt_chars from the end, which could drop the evidence
    block's END line and the reply-shape instructions. The target text now gives way first."""
    script, seen = tmp_path / "claude", tmp_path / "prompt.txt"
    script.write_text(FAKE_CLAUDE.replace("sys.stdin.read()", f"open({str(seen)!r}, 'w').write(sys.stdin.read())")
                      .format(py=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("AMEM_EDITS_CLAUDE", str(script))
    monkeypatch.setenv("FAKE_ENV_LOG", str(tmp_path / "env.json"))
    evidence = "BEGIN-EVIDENCE nonce\n" + ("{\"i\": 1}\n" * 900) + "END-EVIDENCE nonce\n"
    b = Budget(BudgetConfig.from_dict({"max_usd": 5.0, "model_calls": {"opus": 2}}))
    N.ClaudeCliHypothesizer({"max_prompt_chars": 12_000}).reply(
        {"dedupe_key": "k", "target": "~/.claude/CLAUDE.md"}, evidence_text=evidence, targets=["~/.claude/CLAUDE.md"],
        cap=2, target_text="x" * 12_000, budget=b)
    prompt = seen.read_text()
    assert len(prompt) <= 12_000 and "END-EVIDENCE nonce" in prompt and evidence in prompt
