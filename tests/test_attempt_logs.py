"""Subscription attempt runs leave a sandbox run log and sidecar, so the Langfuse sink traces attempts too.

A subscription replay runs the official client on the host (rsi_subscription.run), not in a container, so
rsi_sandbox writes no run log for it. rsi_replay.write_attempt_log writes `logs/<run_id>.json` itself
(langfuse.SUBSCRIPTION_LOG_FORMAT) and _write_evidence adds the `.rsi.json` sidecar. Fixture mode only:
rsi_subscription.run is a stub, checks run on the host (HostSandbox), Langfuse is the local stand-in.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import attempt_links as AL, rsi_replay as R, rsi_subscription as SUB
from agent_memory import rsi_subscription_config as SC
from agent_memory.integrations import langfuse as L, sync
from agent_memory.rsi_budget import Budget, BudgetConfig
from rsi_replay_support import HostSandbox, rs, setup_root

EXP = "exp-20261017-01"
MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
SECRET = "sk-ant-oat01-ATTEMPTLOGSECRET0123456789abcdef"    # what the fake auth home holds


def _tool(call: int, tool: str, *, exit_code: int | None = 0, error: str | None = None,
          read: dict | None = None) -> dict[str, Any]:
    """One bridge tool-evidence record, shaped like sandbox/subscription_tools.py writes it (night 2026-10-25)."""
    rec = {"call": call, "tool": tool, "command_sha256": hashlib.sha256(f"c{call}".encode()).hexdigest(),
           "exit_code": exit_code, "duration_s": 0.19, "network": "none", "credentials": "none", "error": error,
           "output_sha256": hashlib.sha256(f"o{call}".encode()).hexdigest()}
    if read is not None:
        rec["verified_read"] = read
    return rec


class FakeClient:
    """rsi_subscription.run in fixture mode: reads the arm's CLAUDE.md through the 'bridge', fixes the typo, and
    returns a result shaped like the real one. `fail` maps an arm to a SubscriptionError code, `leak` puts the
    auth secret into the text and the runtime evidence (the worst case the log must survive)."""

    def __init__(self, fail: dict[str, str] | None = None, leak: bool = False, usage: dict | None = None):
        self.fail, self.leak, self.usage = fail or {}, leak, usage
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, provider: str, prompt: str, **kw: Any) -> SUB.SubscriptionResult:
        tools = kw.get("tools_allowed", True)
        self.calls.append((provider, tools))
        if not tools:                                   # the subscription judge
            return SUB.SubscriptionResult('{"winner":"tie","reason":"same checked result"}',
                                          {"input_tokens": 30, "output_tokens": 10}, {}, [], .01)
        arm = kw["workspace"].name.rsplit("-", 2)[-2]
        if arm in self.fail:
            raise SUB.SubscriptionError(self.fail[arm])
        path = kw["arm_home"] / ".claude/CLAUDE.md"
        read = {"path": "~/.claude/CLAUDE.md", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "success": True, "full_read": True, "source": "sandbox-bridge"}
        for f in kw["workspace"].glob("*.md"):
            f.write_text(f.read_text().replace("recieve", "receive"))
        calls = [_tool(1, "task_read_arm", read=read), _tool(2, "task_shell"), _tool(3, "task_shell", exit_code=1),
                 _tool(4, "task_shell", exit_code=None, error="tool_timeout")]
        text = "Fixed the typo in `/work/README.md`."
        evidence = {"verified_reads": [read], "billing_source": "subscription", "auth_method": "claude.ai",
                    "subscription_type": "max", "transport": "official-subscription-client"}
        if self.leak:
            token = kw["auth"].claude_token()
            text += f"\nCLAUDE_CODE_OAUTH_TOKEN={token} from {kw['auth'].home}/.env\nauth {token}"
            evidence["token"] = token
        usage = self.usage if self.usage is not None else {
            "input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 50}
        return SUB.SubscriptionResult(text, usage, evidence, calls, .01)


def _auth_home(tmp_path: Path) -> Path:
    home = tmp_path / "auth-home"
    (home / ".claude").mkdir(parents=True, mode=0o700)
    (home / ".env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={SECRET}\n")
    (home / ".claude" / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": SECRET}}))
    return home


def _replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: FakeClient, profiles=("claude", "codex"),
            reps: int = 1, cases=("docs-typo",)) -> tuple[Path, list[R.ReplayResult]]:
    root, bid, h = setup_root(tmp_path, monkeypatch, "closing-line", EXP)
    home = _auth_home(tmp_path)
    monkeypatch.setattr(SUB, "run", client)
    monkeypatch.setattr(SC, "auth_for", lambda provider, settings, tools: SUB.SubscriptionAuth(
        home=home, client=Path("/usr/bin/true"), claude_token=lambda: SECRET))
    caps = {"claude-opus-5-5": 40, "gpt-6.1-sol": 40}
    budget = Budget(BudgetConfig.from_dict({"max_usd": 0, "model_calls": caps, "subscription_models": list(caps),
                                            "max_subscription_tokens": 1_000_000, "max_sandbox_runs": 400}))
    out = []
    for p in profiles:                               # rsi_provider_replay: one replay per profile, one experiment
        cfg = R.ReplayConfig(agent=p, model=MODELS[p], effort="high", reps=reps, profile=p, auth_mode="subscription",
                             auth={"home": str(home)}, judge="codex", judge_model="gpt-6.1-sol", judge_effort="high",
                             judge_auth_mode="subscription", judge_auth={"home": str(home)}, jev="none")
        out.append(R.run_replay(h, R.load_cases(list(cases)), cfg, root=root, experiment_id=EXP, baseline_id=bid,
                                budget=budget, sandbox=HostSandbox(root)))
    return root, out


def _logs(root: Path) -> Path:
    return rs.Sandbox(root).experiment_dir(EXP) / "logs"


def test_every_subscription_attempt_writes_a_log_and_sidecar_the_sink_parses(tmp_path, monkeypatch):
    root, results = _replay(tmp_path, monkeypatch, FakeClient())
    logs = _logs(root)
    attempts = [run for res in results for run in res.runs]
    assert len(attempts) == 4 and {r.profile for r in attempts} == {"claude", "codex"}
    for res in results:
        assert not json.loads((res.evidence_dir / "replay.json").read_text())["errors"]
    for run in attempts:
        log, side = logs / f"{run.run_id}.json", logs / f"{run.run_id}.rsi.json"
        assert log.is_file() and side.is_file(), run.run_id
        meta = json.loads(log.read_text())
        assert meta["format"] == L.SUBSCRIPTION_LOG_FORMAT and meta["experiment"] == EXP and meta["arm"] == run.arm
        assert meta["timing"] == "host-measured" and meta["exit_code"] == 0 and meta["error"] is None
        assert not log.with_name(f"{run.run_id}.stdout").exists()    # no client stdout is ever stored
        parsed = L.load_run_log(log)
        L.check(parsed)                                             # a record the sink may send
        want = AL.session_id_for(EXP, run.arm, run.case, run.rep, run.profile)
        assert (parsed.experiment_id, parsed.arm, parsed.case, parsed.rep) == \
            (EXP, run.arm, AL.case_key(run.case, run.profile), run.rep)
        assert parsed.session_id == run.session_id == want                  # rsi_attempts.attempt_session_id
        assert parsed.agent == run.profile and parsed.timing == "host-measured" and not parsed.failed
        assert parsed.check_passed is run.check_passed is True
        (gen,) = parsed.generations
        assert gen.model == MODELS[run.profile] and gen.cost_usd is None
        assert gen.usage == {"input": 100, "output": 10, "cache_read_input_tokens": 2000,
                             "cache_creation_input_tokens": 50, "total": 2160}
        assert parsed.tokens() == (run.tokens_in + run.tokens_out, run.tokens_out)
        assert [(t.name, t.status) for t in parsed.tools] == [("task_read_arm", "completed"), ("task_shell", "completed"),
                                                              ("task_shell", "failed"), ("task_shell", "failed")]
        assert parsed.result == "Fixed the typo in `/work/README.md`."
        assert abs((parsed.ended_at - parsed.started_at).total_seconds() - run.duration_s) < 1e-6
        root_span = L.build_spans(parsed)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"].get("stringValue") for a in root_span["attributes"]}
        assert attrs["langfuse.observation.metadata.timing"] == "host-measured"
        assert attrs["langfuse.observation.metadata.tool_timing"] == "ordinal"
        assert attrs["langfuse.observation.metadata.amem_session_id"] == want


def test_check_and_judge_runs_keep_their_logs(tmp_path, monkeypatch):
    root, results = _replay(tmp_path, monkeypatch, FakeClient())
    logs = _logs(root)
    run_result_keys = set(rs.RunResult("e", "K", "r", "c", 0, False, 0.0, "", "", "", "", []).to_json())
    chk = sorted(logs.glob("*-chk.json"))
    assert [p.stem for p in chk] == sorted(r.check_run_id for res in results for r in res.runs)
    for p in chk:
        meta = json.loads(p.read_text())
        assert set(meta) == run_result_keys and meta["arm"] == "K"           # rsi_sandbox's RunResult, unchanged
        assert p.with_name(f"{p.stem}.stdout").is_file()
        assert not p.with_name(f"{p.stem}.rsi.json").exists()               # checks get no sidecar, as before
        parsed = L.load_run_log(p)
        assert (parsed.case, parsed.rep, parsed.timing) == (p.stem, 0, "reconstructed")
    assert not list(logs.glob("*-J-*"))           # a subscription judge runs on the host and leaves no run log
    files = {p.name for p in logs.iterdir()}
    attempts = {r.run_id for res in results for r in res.runs}
    assert {n.split(".")[0] for n in files} == attempts | {p.stem for p in chk}


def test_the_sink_lists_attempts_and_checks_once(tmp_path, monkeypatch, pg_url):
    import test_integrations_langfuse as TL
    from integrations_mock import MockService
    root, results = _replay(tmp_path, monkeypatch, FakeClient(), reps=2)
    attempts = {r.run_id for res in results for r in res.runs}
    checks = {r.check_run_id for res in results for r in res.runs}
    assert len(attempts) == 8 and len(checks) == 8
    with MockService() as svc, psycopg.connect(pg_url) as conn:
        for q in ("delete from rsi_checkpoints where job = 'sync:langfuse'",
                  "delete from rsi_evidence where system = 'langfuse'"):
            conn.execute(q)
        conn.commit()
        try:
            lf = TL.FakeLangfuse(svc)
            sink = L.LangfuseSink(TL.client_for(svc), root)
            assert {p.stem for _, p in sink._logs()} == attempts | checks
            plan = sync.plan(sink, conn, environ={})
            assert (plan["rows"], plan["requests"], plan.get("refused")) == (16, 16, 0), sink.refused
            rep = sync.run(sink, conn, environ={})
            assert (rep.status, rep.batches, rep.rows, rep.failed) == ("ok", 16, 16, 0)
            rows = conn.execute("select metrics from rsi_evidence where system = 'langfuse'").fetchall()
            by_run = {m["run_id"]: m for (m,) in rows}
            assert set(by_run) == attempts | checks
            for res in results:
                for run in res.runs:
                    m = by_run[run.run_id]
                    assert (m["amem_session_id"], m["case"], m["rep"], m["agent"]) == \
                        (run.session_id, AL.case_key(run.case, run.profile), run.rep, run.profile)
                    assert m["tokens_total"] == run.tokens_in + run.tokens_out and m["tools"] == 4
            roots = [o for o in lf.observations if not o["parentObservationId"]]
            assert len(roots) == 16 and len({o["traceId"] for o in roots}) == 16
            n_obs, n_scores = len(lf.observations), len(lf.scores)
            conn.commit()
            again = sync.plan(sink, conn, environ={})                    # idempotent: nothing new to send
            assert (again["rows"], again["requests"]) == (0, 0)
            rep2 = sync.run(sink, conn, environ={})
            assert (rep2.status, rep2.batches) == ("ok", 0)
            assert (len(lf.observations), len(lf.scores)) == (n_obs, n_scores)
            assert SECRET not in lf.bodies()
        finally:
            conn.rollback()
            for q in ("delete from rsi_checkpoints where job = 'sync:langfuse'",
                      "delete from rsi_evidence where system = 'langfuse'"):
                conn.execute(q)
            conn.commit()


@pytest.mark.parametrize("code,timed_out", [("client_timeout", True), ("client_run_failed", False)])
def test_a_failed_or_timed_out_attempt_still_writes_its_log(tmp_path, monkeypatch, code, timed_out):
    root, (res,) = _replay(tmp_path, monkeypatch, FakeClient(fail={"A": code}), profiles=("claude",))
    logs = _logs(root)
    bad = next(r for r in res.runs if r.arm == "A")
    assert bad.error == code and res.verdict.verdict == "INCONCLUSIVE"
    meta = json.loads((logs / f"{bad.run_id}.json").read_text())
    assert (meta["exit_code"], meta["error"], meta["timed_out"]) == (1, code, timed_out)
    assert meta["usage"] == {} and meta["tools"] == [] and meta["result"] == ""
    assert (logs / f"{bad.run_id}.rsi.json").is_file()
    parsed = L.load_run_log(logs / f"{bad.run_id}.json")
    assert parsed.failed and parsed.error_code == code and parsed.generations == [] and parsed.check_passed is None
    assert parsed.session_id == bad.session_id
    root_span = L.build_spans(parsed)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert root_span["status"]["code"] == 2
    assert {"key": "langfuse.observation.metadata.error_code", "value": {"stringValue": code}} in root_span["attributes"]
    assert not (logs / f"{bad.run_id}-chk.json").exists()          # no check for a failed attempt, as before


def test_an_attempt_that_crashes_after_the_client_still_writes_its_log(tmp_path, monkeypatch):
    # Zero usage: usage_fields raises ValueError after the client returned; the replay stops with a runner error.
    root, (res,) = _replay(tmp_path, monkeypatch, FakeClient(usage={}), profiles=("codex",))
    logs = _logs(root)
    (first,) = res.runs
    errors = json.loads((res.evidence_dir / "replay.json").read_text())["errors"]
    assert any(e.startswith("runner: ValueError") for e in errors), errors
    meta = json.loads((logs / f"{first.run_id}.json").read_text())
    assert (meta["exit_code"], meta["error"]) == (None, "not finished: ValueError")
    parsed = L.load_run_log(logs / f"{first.run_id}.json")
    assert parsed.is_error and parsed.failed and parsed.error_code is None
    assert (logs / f"{first.run_id}.rsi.json").is_file()


def test_no_secret_from_the_auth_home_reaches_a_log(tmp_path, monkeypatch):
    root, results = _replay(tmp_path, monkeypatch, FakeClient(leak=True))
    logs = _logs(root)
    home = str(tmp_path / "auth-home")
    for res in results:
        for run in res.runs:
            assert SECRET in run.final                    # the stub did leak it into the client result
            for p in (logs / f"{run.run_id}.json", logs / f"{run.run_id}.rsi.json"):
                text = p.read_text()
                assert SECRET not in text and "ATTEMPTLOGSECRET" not in text, p.name
                assert home not in text and "auth-home" not in text, p.name
                assert "subscription_type" not in text and "auth_method" not in text and "argv" not in text
            meta = json.loads((logs / f"{run.run_id}.json").read_text())
            assert "[redacted]" in meta["result"]
            assert L.load_run_log(logs / f"{run.run_id}.json").result.count(SECRET) == 0
