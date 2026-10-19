"""Synthetic data and fakes for tests/test_rsi_night*.py (Mission 005, `amem rsi night`).

A temp HOME holds the synthetic instruction targets (never the real ~/.claude), a temp RSI_SANDBOX_DIR holds
baselines, artifacts, locks and the night report. The fakes stand in for w-evidence (delta_since, build_pack,
render_for_model), w-replay (run_replay), `improve stage` and the sandbox's docker recovery. They follow the
contracts in docs/rsi/m005-plan.md 2.2 / 2.3: the fake replayer writes the same rsi-eval/1 record the real one
writes, and the fake stager writes the same rsi-artifact/1 layout `improve stage` writes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from agent_memory import rsi_approval as AP, rsi_artifact as RA, rsi_night as N, rsi_targets as T
from agent_memory.rsi_budget import Budget, BudgetExhausted

TABLES = ("rsi_decision_results", "rsi_decisions", "rsi_decision_tokens", "rsi_eval_results", "rsi_proposals",
          "rsi_artifacts", "rsi_candidates", "rsi_evidence", "rsi_checkpoints", "rsi_experiments", "deviations",
          "edits", "skill_scores", "runs", "sessions")

CLAUDE_MD = ("# Global\n\n## Closing line\n\n- End with a short summary.\n\n## Explaining\n\n"
             "- Explain changes in plain words.\n")
WORKER_MD = "# Worker\n\n## Rules\n\n- First rule.\n"
SKILL_MD = "---\nname: demo\n---\n\n# Demo\n\n- Do the demo.\n"

RULE_A = ("claude-md-global--closing-line", "Closing line", 3, 0.2)     # rule id, ref, sessions, followed grade
RULE_B = ("claude-md-global--explaining", "Explaining", 2, 0.4)
KEY_A = f"dev:claude-md:global:{RULE_A[0]}"
KEY_B = f"dev:claude-md:global:{RULE_B[0]}"


def make_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    ac = home / "agent-config"
    for d in (home / ".claude", ac / "agents", ac / "improve", home / ".agents" / "skills" / "demo"):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "CLAUDE.md").write_text(CLAUDE_MD)
    (ac / "agents" / "worker.md").write_text(WORKER_MD)
    (home / ".agents" / "skills" / "demo" / "SKILL.md").write_text(SKILL_MD)
    (ac / "settings.json").write_text("{}\n")
    (ac / "improve" / "criteria.json").write_text(json.dumps({"allowed_targets": []}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AMEM_AGENT_CONFIG_DIR", str(ac))
    return home


def clean_db(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("set session_replication_role = replica")   # superuser-only: skips the append-only triggers
        for t in TABLES:
            conn.execute(f"delete from {t}")


def seed_rule(url: str, rule: tuple[str, str, int, float], tag: str, *, sessions: int | None = None) -> list[str]:
    """`sessions` synthetic sessions, each with one deviation from `rule` (claude-md:global)."""
    rid, ref, n, grade = rule
    ids = []
    with psycopg.connect(url) as conn:
        for i in range(sessions if sessions is not None else n):
            sid = f"sess-{tag}-{rid[-8:]}-{i}"
            conn.execute("insert into sessions (session_id, kind, project_dir, started_at, tool) "
                         "values (%s, 'chat', '/work/demo', now() - interval '1 hour', 'claude')", (sid,))
            conn.execute("insert into deviations (source_key, session_id, instruction_source, instruction_ref, turn, what, why, "
                         "how, followed_grade, result_grade, rule_id, rule_title) values (%s, %s, 'claude-md:global', %s, 1, "
                         "'skipped the rule', 'rule unclear', 'state it plainly', %s, 0.5, %s, %s)",
                         (f"dev:{sid}:{rid}", sid, ref, grade, rid, ref))
            ids.append(sid)
    return ids


def write_config(tmp_path: Path, name: str = "night.json", *, candidates: dict[str, Any] | None = None,
                 **budget: Any) -> Path:
    d = json.loads(N.CONFIG_PATH.read_text())
    d["budget"].update(budget)
    d["candidates"].update(candidates or {})
    p = tmp_path / name
    p.write_text(json.dumps(d))
    return p


def write_eval(adir: Path, suite: str, verdict: str) -> Path:
    m = json.loads((adir / "manifest.json").read_text())
    rec = {"format": RA.EVAL_FORMAT, "artifact_hash": adir.name, "experiment_id": m["experiment_id"],
           "baseline_id": m["baseline_id"], "suite": suite, "suite_version": "1", "model": "none", "effort": "none",
           "verdict": verdict, "started_at": "2026-10-18T01:00:00Z", "finished_at": "2026-10-18T01:00:01Z",
           "runner": "test", "results": {}}
    p = adir / "evals" / f"{suite}.json"
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(RA.canonical(rec) + b"\n")
    return p


# --------------------------------------------------------------------------- fakes


class FakeEvidence:
    """delta_since: rows created after `since` (the database clock), as w-evidence's Delta. build_pack /
    render_for_model: one finding per provider; `outage` marks providers down; `inject` puts hostile text in."""

    def __init__(self, *, outage: bool = False, inject: str | None = None) -> None:
        self.outage, self.inject = outage, inject
        self.since_calls: list[str | None] = []
        self.packs: list[dict[str, Any]] = []
        self.rendered: list[str] = []

    def delta(self, since: str | None, *, conn: psycopg.Connection) -> Any:
        self.since_calls.append(since)
        until = conn.execute("select now()").fetchone()[0]
        lo = N._parse_ts(since) if since else until - timedelta(days=7)
        q = lambda sql: [r[0] for r in conn.execute(sql, (lo, until)).fetchall()]  # noqa: E731
        sessions = q("select session_id from sessions where created_at > %s and created_at <= %s order by 1")
        devs = [str(x) for x in q("select id from deviations where created_at > %s and created_at <= %s order by 1")]
        runs = [{"ref": f"amem:run/{r[0]}", "run_id": str(r[0]), "routine": r[1], "status": r[2]} for r in conn.execute(
            "select id, routine, status from runs where status in ('fail', 'error') and created_at > %s and created_at <= %s",
            (lo, until)).fetchall()]
        health = {"supabase": "ok", **({"clickhouse": "unavailable"} if self.outage else {})}
        counts = {"sessions": len(sessions), "deviations": len(devs), "eval_failures": 0, "routine_failures": len(runs),
                  "experiments": 0, "findings": 0}
        return SimpleNamespace(since=lo.isoformat(), until=until.isoformat(), first=since is None, sessions=sessions,
                               deviations=devs, eval_failures=[], routine_failures=runs, experiments=[], findings=[],
                               health=health, truncated={}, next_checkpoint=until.isoformat(), counts=lambda: counts)

    def pack(self, candidate: Any) -> Any:
        self.packs.append(dict(candidate))
        st = "unavailable" if self.outage else "ok"
        findings = [SimpleNamespace(source="clickhouse", ref="ch:deviation-rate:2026-W42", ts=None,
                                    measurements={"rate": 0.3}, n=12, status=st),
                    SimpleNamespace(source="pastdev", ref="pastdev:decision/17", ts=None, measurements={}, n=None,
                                    status="ok")]
        return SimpleNamespace(candidate_key=candidate["dedupe_key"], findings=findings, truncated=False,
                               health={"supabase": "ok", "clickhouse": st, "langfuse": st, "pastdev": "ok",
                                       "jev": "ok", "experiments": "ok"})

    def render(self, pack: Any) -> str:
        body = "\n".join(f"{f.source} {f.ref} {f.status}" for f in pack.findings)
        text = f"<<UNTRUSTED nonce=t3st>>\n{body}\n{self.inject or ''}\n<<END nonce=t3st>>"
        self.rendered.append(text)
        return text


class FakeStager:
    """`improve stage` stand-in: the same artifact layout and the same stale-baseline refusal."""

    def __init__(self, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.calls: list[dict[str, Any]] = []

    def stage(self, *, experiment_id: str, baseline_dir: Path, key: str, new_text: str, rule_id: str,
              timeout: float) -> N.StageResult:
        self.calls.append({"experiment_id": experiment_id, "key": key, "rule_id": rule_id, "new_text": new_text})
        if self.refuse:
            return N.StageResult("refused", reason=self.refuse)
        bm = json.loads((baseline_dir / "manifest.json").read_text())
        before = (baseline_dir / "files" / key[2:]).read_bytes()
        if RA.sha256_bytes(T.untilde(key).read_bytes()) != RA.sha256_bytes(before):
            return N.StageResult("refused", reason=f"{key}: live file changed since baseline (stale baseline)")
        after = new_text.encode()
        files = [{"path": key, "kind": "instruction", "rule_id": rule_id, "mode": "0644",
                  "before_sha256": RA.sha256_bytes(before), "after_sha256": RA.sha256_bytes(after)}]
        m = {"format": RA.ARTIFACT_FORMAT, "experiment_id": experiment_id, "baseline_id": bm["baseline_id"],
             "proposal_kind": "edit_instruction", "activation": "none", "files": files}
        h = RA.artifact_hash(m)
        d = RA.artifacts_dir() / h
        for sub in ("files", "before", "evals"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_bytes(RA.canonical(m) + b"\n")
        (d / "files" / RA.sha256_bytes(after)).write_bytes(after)
        (d / "before" / RA.sha256_bytes(before)).write_bytes(before)
        write_eval(d, "static", "pass")
        return N.StageResult("staged", artifact_hash=h, artifact_dir=str(d))


EVAL_VERDICT = {"IMPROVES": "improves", "NO_EFFECT": "no-effect", "WORSE": "worse", "INCONCLUSIVE": "inconclusive"}


class FakeReplayer:
    """run_replay stand-in: charges `runs` container runs to the budget (as w-replay does: take_sandbox_run before,
    record_run after; BudgetExhausted caught and reported as budget_stop), writes the bound eval record."""

    def __init__(self, verdicts: str | list[str] = "IMPROVES", *, runs: int = 4,
                 on_replay: Callable[[Budget], None] | None = None, health: str = "ok",
                 judge: dict[str, Any] | None = None, jev: dict[str, Any] | None = None) -> None:
        self.verdicts = [verdicts] if isinstance(verdicts, str) else list(verdicts)
        self.runs, self.on_replay, self.health = runs, on_replay, health
        self.judge, self.jev = judge, jev            # the replay's judge / Jev summaries (rsi_verdict *.summary())
        self.calls: list[dict[str, Any]] = []

    def preflight(self) -> str:
        return self.health

    def replay(self, *, artifact_hash: str, experiment_id: str, baseline_id: str, budget: Budget) -> N.ReplayOutcome:
        verdict = self.verdicts[min(len(self.calls), len(self.verdicts) - 1)]
        self.calls.append({"artifact_hash": artifact_hash, "experiment_id": experiment_id, "baseline_id": baseline_id})
        adir = RA.artifacts_dir() / artifact_hash
        if self.on_replay:
            self.on_replay(budget)
        done = 0
        try:
            for i in range(self.runs):
                rid = f"{experiment_id}-c{i // 2}-{'AB'[i % 2]}-r1"
                budget.take_sandbox_run(rid)
                budget.record_run(rid, duration_s=1.5, model="stub", exit_code=0)
                done += 1
        except BudgetExhausted as exc:
            p = write_eval(adir, "sandbox-replay", "inconclusive")
            return N.ReplayOutcome("INCONCLUSIVE", [f"budget-exhausted:{exc.kind}"], {"A": {}, "B": {}}, artifact_hash,
                                   baseline_id, str(p), budget_stop=exc.kind, runs=done)
        p = write_eval(adir, "sandbox-replay", EVAL_VERDICT[verdict])
        arms = {"A": {"pass_rate": 0.5, "n": 3}, "B": {"pass_rate": 0.9 if verdict == "IMPROVES" else 0.5, "n": 3}}
        return N.ReplayOutcome(verdict, [f"fake replay: {verdict}"], arms, artifact_hash, baseline_id, str(p), runs=done,
                               judge=self.judge, jev=self.jev)


class FakeSandboxOps(N.SandboxOps):
    """The real lock (rsi_sandbox.Sandbox.acquire_lock in the temp RSI_SANDBOX_DIR); recovery without docker."""

    def __init__(self) -> None:
        self.recovered = 0
        self.reaped: list[str] = []

    def recover(self) -> dict[str, Any]:
        self.recovered += 1
        return {"killed_containers": [], "released_locks": [], "removed_records": []}

    def reap(self, experiments: Any) -> dict[str, Any]:
        self.reaped += list(experiments)
        return {e: [] for e in experiments}


def adapters(**kw: Any) -> N.Adapters:
    return N.Adapters(evidence=kw.get("evidence") or FakeEvidence(),
                      triage=kw.get("triage") or N.JevTriage("fixture"),
                      hypothesizer=kw.get("hypothesizer") or N.FixtureHypothesizer(),
                      implementer=kw.get("implementer") or N.StubImplementer(),
                      stager=kw.get("stager") or FakeStager(),
                      replayer=kw.get("replayer") or FakeReplayer(),
                      proposer=kw.get("proposer") or N.ApprovalProposer(),
                      sandbox=kw.get("sandbox") or FakeSandboxOps())


def rows(url: str, sql: str, *args: Any) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


def candidate(url: str, key: str) -> dict[str, Any]:
    with psycopg.connect(url) as conn:
        cur = conn.execute("select * from rsi_candidates where dedupe_key = %s", (key,))
        cols = [c.name for c in cur.description]
        r = cur.fetchone()
    return dict(zip(cols, r)) if r else {}


def read_report(night: str) -> dict[str, Any]:
    return json.loads(N.report_path(night).read_text())


def strip_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [k for k in os.environ if k in AP.AGENT_ENV or (k.startswith("CODEX_") and k != "CODEX_HOME")]:
        monkeypatch.delenv(k)
