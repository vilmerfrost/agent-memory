"""Mission 005, section 10: safety requirements proven on the real pieces (Docker sandbox, a night-staged proposal).

    push / merge     a sandbox worker cannot push or merge: no host repo is visible, no git credential, no route
                     (stub path, --network none) and the egress proxy denies GitHub (live path, rsi-internal)
    credentials      a worker's env is the allowlist; no store URL, provider key, Keychain or host secret file
    no self-approval the investigator's code never calls a decision function; a non-TTY / agent process is refused;
                     the writer role cannot record decisions; a guessed token is denied
    exact artifact   Vilmer's (simulated) approval of the night-staged proposal is refused by the promotion worker
                     after one byte of the artifact changed; agent-config's own loader refuses it too
    writes disabled  the lean config refuses every paid model; the night only ever runs `improve stage`

Run:  env AMEM_NO_DOTENV=1 uv run --quiet pytest sandbox/tests/test_m005_safety_docker.py -s -rA -p no:cacheprovider
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ["AMEM_NO_DOTENV"] = "1"
# RSI_SANDBOX_DIR stays: test_isolation.py (same session) needs the caller's temp root. Every sandbox here is explicit.
for _k in ("AMEM_JEV_LIVE", "TYPESAFE_API_KEY", "AMEM_JEV_FIXTURES_DIR", "AMEM_QUESTIONS_DIR", "AMEM_DB_URL"):
    os.environ.pop(_k, None)

import psycopg  # noqa: E402
import rsi_sandbox as rs  # noqa: E402

from agent_memory import config, rsi_approval as AP, rsi_artifact as RA, rsi_demo as D, rsi_night as N  # noqa: E402
from agent_memory.rsi_budget import Budget, BudgetConfig, BudgetExhausted  # noqa: E402

SENSITIVE_NAME = re.compile(r"^(AMEM_|SUPABASE|PG|TYPESAFE|CLICKHOUSE|LANGFUSE|PASTDEV|ANTHROPIC|OPENAI|CODEX_API|GH_|"
                            r"GITHUB|AWS_)|KEY|TOKEN|SECRET|PASSW|DB_URL|CREDENTIAL", re.I)
DECISION_SQL = re.compile(r"insert\s+into\s+rsi_decisions|update\s+rsi_decisions|amem_rsi_record_\w*\s*\(|"
                          r"amem_rsi_arm_token\s*\(", re.I)
DOCKER_OK = rs.docker_available() and rs.load_lock() is not None
IMPROVE = D.REAL_HOME / "agent-config" / "bin" / "improve"
STAGE_OK = IMPROVE.is_file() and "def cmd_stage" in IMPROVE.read_text(errors="replace")
needs_docker = pytest.mark.skipif(not DOCKER_OK, reason="docker or images/lock.json unavailable")
needs_stage = pytest.mark.skipif(not (DOCKER_OK and STAGE_OK), reason="needs docker and ~/agent-config `improve stage`")

GIT_PROBE = r'''
import json, os, socket, subprocess, sys
out = {"env": sorted(os.environ)}
host = sys.argv[1]
out["host_paths"] = {p: os.path.exists(p) for p in (host, host + "/Projects/agent-memory/.git", host + "/agent-config/.git",
                                                   host + "/.ssh", host + "/.git-credentials", "/Users")}
out["home"] = sorted(os.listdir(os.environ["HOME"]))
out["secret_files"] = [p for p in ("/home/rsi/.git-credentials", "/home/rsi/.ssh", "/home/rsi/.netrc",
                                   "/home/rsi/.config/gh/hosts.yml", "/home/rsi/.codex/auth.json") if os.path.exists(p)]
env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "GIT_CONFIG_NOSYSTEM": "1"}
def git(*a, cwd="/work/repo"):
    p = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=env, timeout=60)
    return {"rc": p.returncode, "err": (p.stderr or p.stdout)[-300:]}
os.makedirs("/work/repo")
git("init", "-q", "-b", "main")
git("-c", "user.email=w@x", "-c", "user.name=w", "commit", "-q", "--allow-empty", "-m", "sandbox commit")
out["push_github"] = git("push", "https://github.com/vilmerfrost/agent-memory.git", "HEAD:refs/heads/main")
out["push_host_repo"] = git("push", host + "/Projects/agent-memory", "HEAD:refs/heads/main")
out["fetch_host_repo"] = git("fetch", host + "/agent-config", "main")
out["merge_into_host_repo"] = git("-C", host + "/agent-config", "merge", "--no-edit", "HEAD", cwd="/work")
try:
    socket.create_connection(("github.com", 443), timeout=5).close(); out["tcp_github"] = "CONNECTED"
except OSError as e:
    out["tcp_github"] = f"{type(e).__name__}: {e}"
print(json.dumps(out))
'''


@pytest.fixture
def box(tmp_path: Path) -> Any:
    root = tmp_path / "sandbox"
    sb = rs.Sandbox(root)
    sb.init()
    exp, base = f"t-safety-{secrets.token_hex(3)}", f"t-base-{secrets.token_hex(3)}"
    (sb.baseline_dir(base) / ".claude").mkdir(parents=True)
    (sb.baseline_dir(base) / ".claude" / "CLAUDE.md").write_text("# synthetic\n")
    sb.prepare_arm(exp, base, "A")
    yield sb, exp
    shutil.rmtree(sb.experiment_dir(exp), ignore_errors=True)


def probe(sb: Any, exp: str, network: str) -> dict[str, Any]:
    r = sb.run(exp, "A", ["python3", "-c", GIT_PROBE, str(D.REAL_HOME)], timeout_s=180, network=network,
               run_id=f"{exp}-git-{network}")
    assert r.exit_code == 0 and not r.timed_out, r.stderr[-1500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def image_env() -> set[str]:
    """The env names the worker image itself sets (Dockerfile ENV): no secret can come from there."""
    raw = rs.docker("image", "inspect", rs.image_id("worker"), "--format", "{{json .Config.Env}}").stdout
    return {e.split("=", 1)[0] for e in json.loads(raw)}


def assert_no_push_or_merge(out: dict[str, Any]) -> None:
    assert not any(out["host_paths"].values()), out["host_paths"]
    for k in ("push_github", "push_host_repo", "fetch_host_repo", "merge_into_host_repo"):
        assert out[k]["rc"] != 0, (k, out[k])
    assert out["tcp_github"] != "CONNECTED" and out["secret_files"] == []
    baked = image_env()
    assert not any(SENSITIVE_NAME.search(n) for n in baked), baked
    # HOSTNAME: docker; LC_CTYPE: python3's own locale coercion (PEP 538) in the probe process
    assert set(out["env"]) <= set(rs.ENV_ALLOWLIST) | baked | {"HOSTNAME", "PWD", "SHLVL", "_", "LC_CTYPE"}, \
        set(out["env"]) - set(rs.ENV_ALLOWLIST) - baked
    assert not any(SENSITIVE_NAME.search(n) for n in out["env"]), out["env"]


@needs_docker
def test_sandbox_worker_cannot_push_or_merge_without_network(box: Any, evidence: Any) -> None:
    sb, exp = box
    out = probe(sb, exp, "none")
    assert_no_push_or_merge(out)
    assert "Could not resolve host" in out["push_github"]["err"] or "resolve" in out["push_github"]["err"].lower()
    evidence(f"--network none: push github rc {out['push_github']['rc']} ({out['push_github']['err'].strip()[-70:]}); "
             f"host repos visible {out['host_paths']}; tcp github {out['tcp_github'][:40]}; env {out['env']}")


@needs_docker
def test_sandbox_worker_cannot_push_through_the_egress_proxy(box: Any, evidence: Any) -> None:
    """The live-agent network (rsi-internal): only the egress proxy, which allowlists the model APIs only."""
    sb, exp = box
    st = sb.net_status()
    was_up = bool(st.get("internal")) and st.get("proxy_running")
    sb.net_up()
    try:
        out = probe(sb, exp, rs.NET_INTERNAL)
    finally:
        if not was_up:
            sb.net_down()
    assert_no_push_or_merge(out)
    assert "403" in out["push_github"]["err"] or "CONNECT" in out["push_github"]["err"], out["push_github"]
    evidence(f"rsi-internal: push github rc {out['push_github']['rc']} ({out['push_github']['err'].strip()[-80:]}); "
             f"tcp github {out['tcp_github'][:50]}")


# --------------------------------------------------------------------------- no self-approval, exact artifact


FORBIDDEN = set(N.FORBIDDEN_CALLS) | {"decide_poll", "record_cli_decision", "record_phone_decision", "issue_tokens"}
INVESTIGATOR = ("rsi_night", "rsi_replay", "rsi_verdict", "rsi_evidence_pack", "rsi_budget", "rsi_brief", "rsi_demo")


def test_investigator_code_never_calls_a_decision_function() -> None:
    """No call of a decision / token function and no SQL that writes a decision or arms a token, in any module of
    the investigator (the night, replay, verdict, evidence, budget, brief) or the demo."""
    for name in INVESTIGATOR:
        tree = ast.parse((config.REPO_ROOT / "src" / "agent_memory" / f"{name}.py").read_text())
        called = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert not (called & FORBIDDEN), (name, called & FORBIDDEN)
        sql = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and DECISION_SQL.search(n.value)]
        assert sql == [], (name, sql)


def test_lean_config_refuses_paid_models() -> None:
    cfg = N.load_config()
    assert cfg.budget.max_usd == 0.0 and cfg.stages == {"triage": "fixture", "hypothesizer": "fixture",
                                                         "implementer": "stub", "replay_agent": "stub", "judge": "stub",
                                                         "replay_jev": "fixture"}
    b = Budget(cfg.budget)
    for model in ("opus", "sonnet", "codex", "jev-1.14.0"):
        with pytest.raises(BudgetExhausted):
            b.take_call(model, stage="test")
    b2 = Budget(cfg.budget)
    b2.take_call("hypothesizer-fixture", stage="test")                        # the free fixture stand-ins run


FAKE_IMPROVE = """#!{py}
import json, os, sys
open(os.environ["FAKE_IMPROVE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "recover":
    print(json.dumps({{"cmd": "recover", "status": "ok", "outcomes": []}})); sys.exit(0)
print(json.dumps({{"status": "promoted", "id": "prm-fake"}}))
"""


@needs_stage
def test_night_cannot_approve_and_approval_binds_the_exact_artifact(tmp_path: Path, evidence: Any) -> None:
    with D.DisposablePostgres() as db:
        world = D.build_world(tmp_path / "world")
        world.db_url = D.fresh_database(db.url)
        res = D.run_scenario(world, D.SCENARIOS["success"])
        assert res["ok"], res["problems"]
        e = res["experiment"]
        pid, h = e["proposal_id"], e["artifact_hash"]
        env = D.demo_env(world)
        # the night's own process context: no TTY, so a CLI decision is refused; an agent marker is refused too
        with pytest.raises(AP.NotHuman):
            AP.record_cli_decision(None, pid, "apply", h, stdin_isatty=False, environ={})    # type: ignore[arg-type]
        with pytest.raises(AP.NotHuman):
            AP.record_cli_decision(None, pid, "apply", h, stdin_isatty=True, environ={"RSI_SANDBOX": "1"})  # type: ignore[arg-type]
        with psycopg.connect(world.db_url) as conn:
            conn.execute("set role rsi_writer")                               # the role routines write with
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select amem_rsi_record_cli_decision(%s, 'apply', %s, null)", (pid, h))
        with psycopg.connect(world.db_url) as conn:
            conn.execute("set role rsi_writer")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("insert into rsi_decisions (proposal_id, action, artifact_hash, source, verified, expires_at) "
                             "values (%s, 'apply', %s, 'cli', 'cli-tty', now() + interval '1 h')", (pid, h))
        with psycopg.connect(world.db_url) as conn:
            assert AP.record_phone_decision(conn, pid, "apply", h, secrets.token_urlsafe(24)) == "denied"
            assert conn.execute("select count(*) from rsi_decisions where proposal_id = %s", (pid,)).fetchone() == (0,)
            # Vilmer approves (simulated in this throwaway DB): the token is bound to the proposal, hash and action
            tok = AP.issue_tokens(conn, pid, h)
            assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "recorded"
        adir = world.sandbox / "artifacts" / h
        f = next((adir / "files").iterdir())
        os.chmod(adir / "files", 0o755)
        os.chmod(f, 0o644)
        data = bytearray(f.read_bytes())
        data[-2] ^= 0x01                                                       # one byte
        f.write_bytes(bytes(data))
        with pytest.raises(RA.ArtifactError, match="changed since staging"):
            RA.load_artifact(adir)
        lib = D.REAL_HOME / "agent-config" / "lib" / "rsi_artifact.py"
        spec = importlib.util.spec_from_file_location("ac_rsi_artifact_safety", lib)
        acmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(acmod)
        with pytest.raises(Exception):                                         # agent-config's own loader refuses
            acmod.load_artifact(adir)
        fake = tmp_path / "improve-fake"
        fake.write_text(FAKE_IMPROVE.format(py=sys.executable))
        fake.chmod(0o755)
        log = tmp_path / "improve-fake.log"
        with D.environ({**env, "AMEM_IMPROVE_BIN": str(fake), "FAKE_IMPROVE_LOG": str(log)}):
            rep = AP.decide_poll(db_url=world.db_url, push=False)
        with psycopg.connect(world.db_url) as conn:
            (did,) = conn.execute("select id::text from rsi_decisions where proposal_id = %s", (pid,)).fetchone()
        mine = [x for x in rep.handled if x["decision"] == did]              # (the base story's old reject is handled too)
        assert [x["result"] for x in mine] == ["refused"], rep.handled
        calls = [json.loads(x)[0] for x in log.read_text().splitlines()] if log.exists() else []
        assert "promote" not in calls, calls
        with psycopg.connect(world.db_url) as conn:
            st, why = conn.execute("select status, result from rsi_proposals where id = %s", (pid,)).fetchone()
        assert st == "failed" and ("changed since staging" in why or "differ" in why), why
    evidence(f"proposal {pid}: CLI decision refused (no TTY / agent marker); rsi_writer cannot record or insert a "
             f"decision; a guessed token -> denied; after a simulated phone approval one byte of {h[:12]} changed -> "
             f"agent-memory and agent-config loaders refuse, promotion worker {mine[0]['result']}, improve calls "
             f"{calls} (no promote), proposal {st}: {why}")
