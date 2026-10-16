"""RSI V1 approval-first (docs/rsi/approval.md): artifacts, proposals, decision tokens, the SQL decision functions,
RLS and immutability, the promotion worker (with a fake improve), verify-decision, durable candidates, shared
evidence, checkpoints, sandbox-session exclusion, baselines, the tracking plan, the freeze check, and the
`edits run` stage mode. A throwaway Postgres; nothing outside tmp_path is read or written, except the
read-only cross-repo test at the end (gated on RSI_TEST_IMPROVE_REPO), which runs the real improve in a
fake HOME.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import deviations, rsi_approval as AP, rsi_artifact as RA, rsi_targets as T, sessions_ingest
from conftest import AMEM, base_env


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(root))
    monkeypatch.delenv("RSI_ARTIFACTS_DIR", raising=False)
    for k in [k for k in os.environ if k in AP.AGENT_ENV or (k.startswith("CODEX_") and k != "CODEX_HOME")]:
        monkeypatch.delenv(k)                        # this suite runs inside an agent session; the code checks these
    return root


def make_artifact(root: Path, files: list[tuple[str, bytes, bytes]], *, exp: str = "exp-20261016-01",
                  baseline: str = "bl-000000000001", suites: tuple[str, ...] = ("static", "sandbox-replay"),
                  verdict: str = "pass") -> tuple[str, Path]:
    """The same bytes improve stage writes (agent-config lib/rsi_artifact.py)."""
    entries = sorted(({"path": k, "kind": "instruction", "rule_id": "demo-rule", "mode": "0644",
                       "before_sha256": RA.sha256_bytes(b), "after_sha256": RA.sha256_bytes(a)} for k, b, a in files),
                     key=lambda f: f["path"])
    m = {"format": RA.ARTIFACT_FORMAT, "experiment_id": exp, "baseline_id": baseline, "proposal_kind": "edit_instruction",
         "activation": "none", "files": entries}
    h = RA.artifact_hash(m)
    d = root / "artifacts" / h
    (d / "files").mkdir(parents=True)
    (d / "before").mkdir()
    (d / "evals").mkdir()
    (d / "manifest.json").write_bytes(RA.canonical(m) + b"\n")
    for _, b, a in files:
        (d / "files" / RA.sha256_bytes(a)).write_bytes(a)
        (d / "before" / RA.sha256_bytes(b)).write_bytes(b)
    for s in suites:
        rec = {"format": RA.EVAL_FORMAT, "artifact_hash": h, "experiment_id": exp, "baseline_id": baseline, "suite": s,
               "suite_version": "1", "model": "none", "effort": "none", "verdict": verdict,
               "started_at": "2026-10-16T00:00:00+00:00", "finished_at": "2026-10-16T00:00:01+00:00", "runner": "test",
               "results": {}}
        (d / "evals" / f"{s}.json").write_bytes(RA.canonical(rec) + b"\n")
    return h, d


@pytest.fixture
def rdb(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("set session_replication_role = replica")   # superuser-only: skips the append-only triggers
        for t in ("rsi_decision_results", "rsi_decisions", "rsi_decision_tokens", "rsi_eval_results", "rsi_proposals",
                  "rsi_artifacts", "rsi_candidates", "rsi_evidence", "rsi_checkpoints", "rsi_experiments", "deviations",
                  "edits", "sessions"):
            conn.execute(f"delete from {t}")
    monkeypatch.setenv("AMEM_DB_URL", pg_url)
    return pg_url


def proposal(url: str, adir: Path, **kw: Any) -> str:
    with psycopg.connect(url) as conn:
        return AP.create_proposal(conn, adir, title=kw.pop("title", "Demo"), **kw)


# --------------------------------------------------------------------------- formats, markers


def test_hash_vector_matches_agent_config() -> None:
    assert RA.artifact_hash(RA.EXAMPLE_MANIFEST) == RA.EXAMPLE_HASH
    repo = os.environ.get("RSI_TEST_IMPROVE_REPO")
    if repo:
        out = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import rsi_artifact as r; "
                              "print(r.artifact_hash(r.EXAMPLE_MANIFEST), r.EXAMPLE_HASH)", str(Path(repo) / "lib")],
                             capture_output=True, text=True, check=True).stdout.split()
        assert out == [RA.EXAMPLE_HASH, RA.EXAMPLE_HASH]


def test_agent_markers() -> None:
    assert AP.agent_session_reason({}) is None
    assert "CLAUDECODE" in (AP.agent_session_reason({"CLAUDECODE": "1"}) or "")
    assert AP.agent_session_reason({"CODEX_HOME": "/x"}) is None
    assert "CODEX_SANDBOX" in (AP.agent_session_reason({"CODEX_SANDBOX": "seatbelt"}) or "")


def test_cli_decision_refuses_agents_and_non_tty() -> None:
    for env, tty in (({"CLAUDECODE": "1"}, True), ({}, False)):
        with pytest.raises(AP.NotHuman):
            AP.record_cli_decision(None, "p", "apply", "h", stdin_isatty=tty, environ=env)  # type: ignore[arg-type]


def test_load_artifact_detects_tampering(tmp_path: Path) -> None:
    h, d = make_artifact(tmp_path, [("~/x.md", b"a\n", b"b\n")])
    assert RA.load_artifact(d)[1] == h
    f = d / "files" / RA.sha256_bytes(b"b\n")
    f.write_bytes(b"b\nsneaked\n")
    with pytest.raises(RA.ArtifactError, match="changed since staging"):
        RA.load_artifact(d)


# --------------------------------------------------------------------------- schema: RLS, immutability, roles


def test_rls_immutability_and_roles(rdb: str, sandbox: Path) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d)
    with psycopg.connect(rdb) as conn:
        rls = dict(conn.execute("select relname, relrowsecurity from pg_class where relname like 'rsi\\_%%' and relkind = 'r'").fetchall())
        assert rls and all(rls.values()), rls
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("update rsi_artifacts set baseline_id = 'x' where hash = %s", (h,))
    for t in ("rsi_artifacts", "rsi_decisions", "rsi_eval_results"):
        with psycopg.connect(rdb) as conn:
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                conn.execute(f"truncate {t} cascade")
    with psycopg.connect(rdb) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into rsi_artifacts (hash, manifest_canonical, manifest, files, baseline_id) "
                         "values (%s, '{}', '{\"format\": \"rsi-artifact/1\"}', '{}', 'b')", ("0" * 64,))
    for sql, err in (("insert into rsi_decisions (proposal_id, action, artifact_hash, source, verified, expires_at) "
                      f"values ('{pid}', 'apply', '{h}', 'cli', 'cli-tty', now() + interval '1 h')", psycopg.errors.InsufficientPrivilege),
                     (f"update rsi_artifacts set baseline_id = 'x' where hash = '{h}'", psycopg.errors.InsufficientPrivilege),
                     ("select * from rsi_decision_tokens", psycopg.errors.InsufficientPrivilege),
                     (f"select amem_rsi_record_cli_decision('{pid}', 'apply', '{h}', null)", psycopg.errors.InsufficientPrivilege)):
        with psycopg.connect(rdb) as conn:
            conn.execute("set role rsi_writer")
            with pytest.raises(err):
                conn.execute(sql)
    with psycopg.connect(rdb) as conn:
        conn.execute("set role rsi_reader")
        assert conn.execute("select count(*) from rsi_proposals").fetchone() == (1,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"update rsi_proposals set status = 'approved' where id = '{pid}'")


# --------------------------------------------------------------------------- phone decisions


def test_phone_decision_tokens_binding_and_replay(rdb: str, sandbox: Path) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d)
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h)
        assert set(tok) == {"apply", "reject"}
        stored = [r[0] for r in conn.execute("select token_hash from rsi_decision_tokens").fetchall()]
        assert tok["apply"] not in json.dumps(stored)                           # only bound hashes are stored
        assert AP.record_phone_decision(conn, pid, "apply", h, "x" * 30) == "denied"
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["reject"]) == "denied"   # bound to the action
        assert AP.record_phone_decision(conn, pid, "apply", "f" * 64, tok["apply"]) == "hash-mismatch"
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "recorded"
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "already"   # repeated tap
        assert AP.record_phone_decision(conn, pid, "reject", h, tok["reject"]) == "already"  # apply consumed reject
        assert conn.execute("select count(*) from rsi_decisions").fetchone() == (1,)
        assert conn.execute("select status from rsi_proposals where id = %s", (pid,)).fetchone() == ("approved",)
        assert AP.issue_tokens(conn, pid, h, ("apply",)) == {}                 # a used token is never re-armed
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("delete from rsi_decisions")


def test_expired_tokens_and_proposals(rdb: str, sandbox: Path) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"c\n")])
    pid = proposal(rdb, d)
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h, ttl=timedelta(seconds=-1))
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "expired"
    h2, d2 = make_artifact(sandbox, [("~/y.md", b"a\n", b"c\n")])
    pid2 = proposal(rdb, d2, ttl=timedelta(seconds=-1))
    with psycopg.connect(rdb) as conn:
        tok2 = AP.issue_tokens(conn, pid2, h2)
        assert AP.record_phone_decision(conn, pid2, "apply", h2, tok2["apply"]) == "expired"
        assert conn.execute("select status from rsi_proposals where id = %s", (pid2,)).fetchone() == ("expired",)


def test_verify_decision(rdb: str, sandbox: Path) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d)
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h)
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "recorded"
        row = conn.execute("select id, decided_at, expires_at from rsi_decisions").fetchone()
        good = {"format": RA.DECISION_FORMAT, "decision_id": str(row[0]), "proposal_id": pid, "action": "apply",
                "artifact_hash": h, "decided_at": row[1].isoformat(), "expires_at": row[2].isoformat(),
                "source": "phone", "verified": "edge-token"}
        assert AP.verify_decision(conn, good) == (True, "ok")
        for bad, why in (({**good, "decision_id": "00000000-0000-0000-0000-000000000000"}, "no such decision"),
                         ({**good, "artifact_hash": "f" * 64}, "artifact hash differs"),
                         ({**good, "verified": "cli-tty"}, "source differs"),
                         ({**good, "action": "undo"}, "not an apply")):
            ok, msg = AP.verify_decision(conn, bad)
            assert not ok and why in msg, msg
        conn.execute("update rsi_proposals set status = 'rejected' where id = %s", (pid,))
        assert AP.verify_decision(conn, good)[0] is False


# --------------------------------------------------------------------------- the promotion worker (fake improve)

FAKE = '''#!{py}
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\\n")
mode = json.load(open(os.environ["FAKE_MODE"]))
if a[0] == "recover":
    print(json.dumps({{"cmd": "recover", "status": "ok", "outcomes": []}})); sys.exit(0)
if a[0] == "promote":
    d = json.load(open(a[a.index("--decision") + 1]))
    open(os.environ["FAKE_LOG"], "a").write(json.dumps({{"decision": d}}) + "\\n")
    print(json.dumps(mode["promote"])); sys.exit(mode.get("promote_exit", 0))
if a[0] == "revert-change":
    print(json.dumps(mode["revert"])); sys.exit(mode.get("revert_exit", 0))
sys.exit(2)
'''


@pytest.fixture
def fake_improve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"                      # live files equal the artifacts' before bytes, so nothing is stale
    home.mkdir(exist_ok=True)
    for name in ("x.md", "y.md"):
        (home / name).write_bytes(b"a\n")
    monkeypatch.setenv("HOME", str(home))
    script, log, mode = tmp_path / "improve", tmp_path / "improve.log", tmp_path / "mode.json"
    script.write_text(FAKE.format(py=sys.executable))
    script.chmod(0o755)
    mode.write_text(json.dumps({"promote": {"status": "promoted", "id": "prm-20261016-1"},
                                "revert": {"status": "reverted", "id": "prm-20261016-1"}}))
    monkeypatch.setenv("AMEM_IMPROVE_BIN", str(script))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_MODE", str(mode))
    return {"log": log, "mode": mode}


def calls(log: Path) -> list[Any]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def test_worker_promotes_only_approved_and_then_undoes(rdb: str, sandbox: Path, fake_improve: dict[str, Path]) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d)
    rep = AP.decide_poll(db_url=rdb, push=False)                      # no decision yet: nothing promoted
    assert rep.handled == [] and not any(c[0] == "promote" for c in calls(fake_improve["log"]) if isinstance(c, list))
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h)
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "recorded"
    rep = AP.decide_poll(db_url=rdb, push=False)
    assert [x["result"] for x in rep.handled] == ["promoted"]
    promote = [c for c in calls(fake_improve["log"]) if isinstance(c, list) and c[0] == "promote"]
    assert promote == [["promote", "--json", "--artifact", h, "--expect-hash", h, "--decision", promote[0][-1]]]
    dec = next(c["decision"] for c in calls(fake_improve["log"]) if isinstance(c, dict))
    assert dec["artifact_hash"] == h and dec["action"] == "apply" and dec["verified"] == "edge-token"
    assert not Path(promote[0][-1]).exists()                          # the decision file is removed afterwards
    with psycopg.connect(rdb) as conn:
        assert conn.execute("select status, change_id from rsi_proposals where id = %s", (pid,)).fetchone() == ("promoted", "prm-20261016-1")
    assert AP.decide_poll(db_url=rdb, push=False).handled == []        # handled once
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h, ("undo",))
        assert AP.record_phone_decision(conn, pid, "undo", h, tok["undo"]) == "recorded"
    rep = AP.decide_poll(db_url=rdb, push=False)
    assert [x["result"] for x in rep.handled] == ["reverted"]
    with psycopg.connect(rdb) as conn:
        assert conn.execute("select status from rsi_proposals where id = %s", (pid,)).fetchone() == ("reverted",)


def test_worker_refuses_when_local_bytes_differ_from_the_stored_artifact(rdb: str, sandbox: Path,
                                                                        fake_improve: dict[str, Path]) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d)
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h)
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "recorded"
    f = d / "files" / RA.sha256_bytes(b"b\n")
    f.write_bytes(b"b\nsneaked\n")
    rep = AP.decide_poll(db_url=rdb, push=False)
    assert [x["result"] for x in rep.handled] == ["refused"]
    assert not any(isinstance(c, list) and c[0] == "promote" for c in calls(fake_improve["log"]))
    with psycopg.connect(rdb) as conn:
        assert conn.execute("select status from rsi_proposals where id = %s", (pid,)).fetchone()[0] == "failed"


def test_worker_stale_refusal_and_busy_retry(rdb: str, sandbox: Path, fake_improve: dict[str, Path]) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d)
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h)
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "recorded"
    fake_improve["mode"].write_text(json.dumps({"promote": {"status": "refused", "reason": "busy: write lock held"},
                                                "promote_exit": 75, "revert": {}}))
    assert [x["result"] for x in AP.decide_poll(db_url=rdb, push=False).handled] == ["retry"]
    with psycopg.connect(rdb) as conn:
        assert conn.execute("select status from rsi_proposals where id = %s", (pid,)).fetchone() == ("approved",)
    fake_improve["mode"].write_text(json.dumps({"promote": {"status": "refused", "reason": "stale baseline or conflicting "
                                                            "production change: ~/x.md"}, "promote_exit": 2, "revert": {}}))
    assert [x["result"] for x in AP.decide_poll(db_url=rdb, push=False).handled] == ["refused"]
    with psycopg.connect(rdb) as conn:
        assert conn.execute("select status from rsi_proposals where id = %s", (pid,)).fetchone() == ("stale",)


def test_single_worker_lock(rdb: str, sandbox: Path, fake_improve: dict[str, Path]) -> None:
    with AP.worker_lock() as got:
        assert got
        assert AP.decide_poll(db_url=rdb, push=False).busy


def test_housekeeping_marks_stale_proposals(rdb: str, sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (home / "x.md").write_text("a\n")
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d)
    with psycopg.connect(rdb) as conn:
        assert AP.housekeeping(conn) == (0, 0)
        (home / "x.md").write_text("a hand edit\n")
        assert AP.housekeeping(conn) == (0, 1)
        assert conn.execute("select status from rsi_proposals where id = %s", (pid,)).fetchone() == ("stale",)


def test_push_payload_has_bound_actions_and_no_secrets(rdb: str, sandbox: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h, d = make_artifact(sandbox, [("~/x.md", b"a\n", b"b\n")])
    pid = proposal(rdb, d, title="Edit closing line", summary="Make it a contract.", rationale={"why": "8 deviations"},
                   risk={"level": "low"}, confidence=0.8, expected={"effect": "fewer deviations"})
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h)
        p = conn.execute("select * from rsi_proposals where id = %s", (pid,)).fetchone()
        cols = [c.name for c in conn.execute("select * from rsi_proposals limit 0").description]
    pay = AP.proposal_payload("topic-x", dict(zip(cols, p)), tok, "https://fn.example.test/functions/v1/amem-decide")
    labels = [a["label"] for a in pay["actions"]]
    assert labels == ["Apply", "Reject"]
    bodies = [json.loads(a["body"]) for a in pay["actions"]]
    assert all(b["proposal_id"] == pid and b["artifact_hash"] == h for b in bodies)
    assert bodies[0]["token"] == tok["apply"] and bodies[1]["token"] == tok["reject"]
    assert "Why: 8 deviations" in pay["message"] and "Files: x.md" in pay["message"] and "Risk: low" in pay["message"]


# --------------------------------------------------------------------------- candidates, evidence, checkpoints


def test_durable_candidates_evidence_and_checkpoints(rdb: str) -> None:
    with psycopg.connect(rdb) as conn:
        c1 = AP.upsert_candidate(conn, dedupe_key="dev:claude-md:global/closing-line", source="deviations",
                                 kind="edit_instruction", evidence_refs=["amem:deviation/1"])
        conn.execute("update rsi_candidates set status = 'deferred' where id = %s", (c1,))
        c2 = AP.upsert_candidate(conn, dedupe_key="dev:claude-md:global/closing-line", source="deviations",
                                 kind="edit_instruction", evidence_refs=["amem:deviation/2"])
        assert c1 == c2
        row = conn.execute("select status, seen_count, evidence_refs from rsi_candidates where id = %s", (c1,)).fetchone()
        assert row[0] == "deferred" and row[1] == 2 and sorted(row[2]) == ["amem:deviation/1", "amem:deviation/2"]
        e = AP.add_evidence(conn, system="clickhouse", kind="trend", subject_refs=["amem:rule/claude-md:global/closing-line"],
                            summary="weekly rate fell", metrics={"rate": 0.2}, external_ref="ch:q1:2026-W42")
        assert AP.add_evidence(conn, system="clickhouse", kind="trend", subject_refs=[], metrics={"rate": 0.1},
                               external_ref="ch:q1:2026-W42") == e
        assert not AP.upstream_done(conn, "rsi-nightly", "2026-10-16")
        AP.checkpoint(conn, "rsi-nightly", "2026-10-16", "done")
        assert AP.upstream_done(conn, "rsi-nightly", "2026-10-16")
    assert AP.night_key(datetime(2026, 10, 17, 1, 30, tzinfo=UTC).astimezone()) <= "2026-10-17"


# --------------------------------------------------------------------------- sandbox sessions stay out of prod evaluation


def test_sandbox_sessions_are_tagged_and_excluded(rdb: str, sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def transcript(root: Path, sid: str, cwd: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(30):
            lines.append({"type": "user", "uuid": f"{sid}-u{i}", "sessionId": sid, "cwd": cwd, "timestamp": f"2026-10-16T10:{i:02d}:00Z",
                          "message": {"role": "user", "content": f"please do step {i} " + "x" * 400}})
            lines.append({"type": "assistant", "uuid": f"{sid}-a{i}", "sessionId": sid, "cwd": cwd,
                          "timestamp": f"2026-10-16T10:{i:02d}:30Z",
                          "message": {"id": f"m{i}", "role": "assistant", "content": [
                              {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "ls"}}]}})
        (root / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    prod_root = tmp_path / "projects"
    exp_root = sandbox / "experiments" / "exp-20261016-01" / "claude-home" / "projects"
    transcript(prod_root / "-Users-v-work", "11111111-1111-1111-1111-111111111111", str(tmp_path / "work"))
    transcript(exp_root / "-home-rsi-work", "22222222-2222-2222-2222-222222222222", "/home/rsi/work")
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    for root in (prod_root, exp_root):
        sessions_ingest.ingest(root, days=7, now=now, db_url=rdb, tools=("claude",))
    with psycopg.connect(rdb) as conn:
        env = dict(conn.execute("select session_id, env || ':' || coalesce(experiment_id, '-') from sessions").fetchall())
    assert env == {"11111111-1111-1111-1111-111111111111": "prod:-",
                   "22222222-2222-2222-2222-222222222222": "sandbox:exp-20261016-01"}
    picked = [r.session_id for r in deviations.pick_sessions(since=now - timedelta(days=7), limit=10, db_url=rdb)]
    assert picked == ["11111111-1111-1111-1111-111111111111"]


# --------------------------------------------------------------------------- baselines, tracking plan, freeze


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    for d in (".claude/scheduled-tasks/demo", ".codex", "agent-config/improve", "agent-config/agents", "agent-config/parent",
              "agent-config/launchd", ".agents/skills/demo", "Library/LaunchAgents"):
        (home / d).mkdir(parents=True, exist_ok=True)
    (home / ".claude/CLAUDE.md").write_text("# Global\n\n## Closing line\n\n- End with the tools you used.\n")
    (home / ".codex/AGENTS.md").write_text("# Codex\n")
    (home / ".claude/scheduled-tasks/demo/SKILL.md").write_text("---\nname: demo\n---\nRun the demo.\n")
    (home / "agent-config/parent/PARENT.md").write_text("# Parent\n")
    (home / "agent-config/agents/worker.md").write_text("# Worker\n")
    (home / ".agents/skills/demo/SKILL.md").write_text("---\nname: demo\n---\n# Demo\n")
    (home / "agent-config/improve/criteria.json").write_text(json.dumps({"max_changes_per_week": None, "allowed_targets": []}))
    (home / "agent-config/links.tsv").write_text("# links\nsettings.json\t~/.claude/settings.json\tac\tall\n")
    (home / "agent-config/settings.json").write_text("{}\n")
    subprocess.run(["git", "-C", str(home / "agent-config"), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(home / "agent-config"), "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(home / "agent-config"), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], check=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AMEM_AGENT_CONFIG_DIR", str(home / "agent-config"))
    return home


def _tree(root: Path) -> dict[str, tuple[str, int]]:
    return {str(p.relative_to(root)): (RA.sha256_bytes(p.read_bytes()), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file() and ".git/" not in str(p) and "sandbox" not in p.parts}


def test_baseline_snapshot_leaves_production_untouched_and_detects_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                         sandbox: Path) -> None:
    home = _home(tmp_path, monkeypatch)
    before = _tree(home)
    bid, bdir = T.create_baseline()
    assert _tree(home) == before                                        # bytes and mtimes unchanged
    assert bid.startswith("bl-") and bdir.parent == sandbox / "baselines" and not os.access(bdir / "manifest.json", os.W_OK)
    m = T.load_baseline(bdir)
    assert {f["path"] for f in m["files"]} >= {"~/.claude/CLAUDE.md", "~/.codex/AGENTS.md", "~/.claude/scheduled-tasks/demo/SKILL.md"}
    assert T.check_baseline(bdir)["drift"] is False
    (home / ".codex/AGENTS.md").write_text("# Codex\n- edited\n")
    r = T.check_baseline(bdir)
    assert r["drift"] and r["changed"] == ["~/.codex/AGENTS.md"]
    (home / ".claude/CLAUDE.md").write_text("api_key = sk-ant-api03-" + "A" * 40 + "\n")
    with pytest.raises(T.BaselineError, match="secret-shaped"):
        T.create_baseline()
    (tmp_path / "outside.md").write_text("# not under HOME\n")
    with pytest.raises(T.BaselineError, match="outside HOME"):
        T.create_baseline([tmp_path / "outside.md"])


def test_tracking_plan_apply_and_inverse_are_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    orig = {k: (home / k).read_bytes() for k in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".claude/scheduled-tasks/demo/SKILL.md")}
    links0 = (home / "agent-config/links.tsv").read_bytes()
    plan = T.tracking_plan()
    (tmp_path / "apply.sh").write_text(plan["apply"])
    (tmp_path / "inverse.sh").write_text(plan["inverse"])
    env = {**os.environ, "HOME": str(home), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    r = subprocess.run(["bash", str(tmp_path / "apply.sh")], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert (home / ".claude/CLAUDE.md").is_symlink() and (home / ".claude/CLAUDE.md").read_bytes() == orig[".claude/CLAUDE.md"]
    assert (home / "agent-config/home/claude/CLAUDE.md").read_bytes() == orig[".claude/CLAUDE.md"]
    assert not (home / ".claude/scheduled-tasks/demo/SKILL.md").is_symlink()      # mirrored, not linked
    assert "home/claude/CLAUDE.md\t~/.claude/CLAUDE.md\tac\tall" in (home / "agent-config/links.tsv").read_text()
    backup = r.stdout.strip().rsplit(" ", 1)[-1]
    r2 = subprocess.run(["bash", str(tmp_path / "inverse.sh"), backup], capture_output=True, text=True, env=env)
    assert r2.returncode == 0, r2.stderr
    for k, b in orig.items():
        assert not (home / k).is_symlink() and (home / k).read_bytes() == b, k
    assert (home / "agent-config/links.tsv").read_bytes() == links0


def test_freeze_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    plist = {"Label": "com.vilmerfrost.amem-daily", "EnvironmentVariables": {"AMEM_EDITS_LIVE": "1"}}
    for p in (home / "Library/LaunchAgents", home / "agent-config/launchd"):
        (p / "com.vilmerfrost.amem-daily.plist").write_bytes(plistlib.dumps(plist))
    tasks = home / "Library/Application Support/Claude/claude-code-sessions/a/b"
    tasks.mkdir(parents=True)
    (tasks / "scheduled-tasks.json").write_text(json.dumps({"scheduledTasks": [{"id": "ab-lessons-monthly", "enabled": True}]}))
    st = T.freeze_status()
    assert not st["frozen"] and len(st["reasons"]) == 4
    plist["EnvironmentVariables"] = {}
    for p in (home / "Library/LaunchAgents", home / "agent-config/launchd"):
        (p / "com.vilmerfrost.amem-daily.plist").write_bytes(plistlib.dumps(plist))
    (home / "agent-config/improve/criteria.json").write_text(json.dumps({"max_changes_per_week": 0}))
    (tasks / "scheduled-tasks.json").write_text(json.dumps({"scheduledTasks": [{"id": "ab-lessons-monthly", "enabled": False}]}))
    st = T.freeze_status()
    assert st["frozen"], st["reasons"]


# --------------------------------------------------------------------------- the real improve, end to end (opt-in)

REPO = os.environ.get("RSI_TEST_IMPROVE_REPO")


@pytest.mark.skipif(not REPO, reason="set RSI_TEST_IMPROVE_REPO to an agent-config checkout with improve stage/promote")
def test_cross_repo_stage_approve_promote_undo(rdb: str, sandbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real improve (stage, promote, revert-change) + real Postgres + real `amem rsi verify-decision`, in a fake HOME."""
    repo = Path(REPO or "")
    home = tmp_path / "home"
    ac = home / "agent-config"
    for d in (ac / "lib", ac / "improve", ac / "agents", home / ".claude" / "parent", home / ".claude" / "projects"):
        d.mkdir(parents=True)
    for f in ("sessions.py", "routine.py", "fence.py", "rsi_artifact.py"):
        shutil.copy(repo / "lib" / f, ac / "lib" / f)
    crit = json.loads((repo / "improve" / "criteria.json").read_text())
    crit.update(apply_mode="stage", promotion={"required_suites": ["static", "sandbox-replay"]})
    (ac / "improve" / "criteria.json").write_text(json.dumps(crit))
    shutil.copy(repo / "improve" / "fence.txt", ac / "improve" / "fence.txt")
    (ac / "improve" / "pins.json").write_text(json.dumps({"files": {}, "rules": {}}))
    claude_md = home / ".claude" / "CLAUDE.md"
    original = "# Global\n\n## Closing line\n\n- End with a short summary.\n"
    claude_md.write_text(original)
    subprocess.run(["git", "-C", str(ac), "init", "-q"], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(ac), "config", k, v], check=True)
    subprocess.run(["git", "-C", str(ac), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(ac), "commit", "-qm", "base"], check=True)
    env = base_env(AMEM_DB_URL=rdb, HOME=str(home), IMPROVE_AC=str(ac), ROUTINE_STATE=str(home / ".claude" / "parent"),
                   RSI_SANDBOX_DIR=str(sandbox), PYTHONDONTWRITEBYTECODE="1", IMPROVE_AMEM=shlex.quote(AMEM),
                   AMEM_AGENT_CONFIG_DIR=str(ac))
    env = {k: v for k, v in env.items() if k not in AP.AGENT_ENV and not (k.startswith("CODEX_") and k != "CODEX_HOME")}
    for k in ("HOME", "IMPROVE_AC", "ROUTINE_STATE", "IMPROVE_AMEM", "AMEM_AGENT_CONFIG_DIR"):
        monkeypatch.setenv(k, env[k])
    monkeypatch.setenv("AMEM_IMPROVE_BIN", str(repo / "bin" / "improve"))
    bid, bdir = T.create_baseline([claude_md])
    new = tmp_path / "new.md"
    new.write_text(original.replace("a short summary", "a section that lists the tools you used"))
    imp = lambda *a: subprocess.run([sys.executable, str(repo / "bin" / "improve"), *a], capture_output=True, text=True, env=env)  # noqa: E731
    r = imp("stage", "--json", "--experiment", "exp-20261016-01", "--baseline", str(bdir),
            "--change", f"~/.claude/CLAUDE.md={new}", "--rule-id", "~/.claude/CLAUDE.md=claude-md-global--closing-line")
    st = json.loads(r.stdout.strip().splitlines()[-1])
    assert st["status"] == "staged", r.stdout + r.stderr
    assert claude_md.read_text() == original                                       # staging wrote nothing
    h, adir = st["artifact_hash"], Path(st["artifact_dir"])
    m = json.loads((adir / "manifest.json").read_text())
    rec = {"format": RA.EVAL_FORMAT, "artifact_hash": h, "experiment_id": m["experiment_id"], "baseline_id": m["baseline_id"],
           "suite": "sandbox-replay", "suite_version": "1", "model": "none", "effort": "none", "verdict": "pass",
           "started_at": "2026-10-16T00:00:00+00:00", "finished_at": "2026-10-16T00:00:01+00:00", "runner": "test", "results": {}}
    (adir / "evals" / "sandbox-replay.json").write_bytes(RA.canonical(rec) + b"\n")
    pid = proposal(rdb, adir, title="Closing line as a contract")
    assert AP.decide_poll(db_url=rdb, push=False).handled == [] and claude_md.read_text() == original   # no decision: no change
    forged = tmp_path / "forged.json"                                              # well-formed, but not in the DB
    forged.write_text(json.dumps({"format": RA.DECISION_FORMAT, "decision_id": "00000000-0000-0000-0000-000000000001",
                                  "proposal_id": pid, "action": "apply", "artifact_hash": h,
                                  "decided_at": datetime.now(UTC).isoformat(),
                                  "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                                  "source": "phone", "verified": "edge-token"}))
    r = imp("promote", "--json", "--artifact", h, "--expect-hash", h, "--decision", str(forged))
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert r.returncode != 0 and res["status"] == "refused" and "decision" in res.get("reason", ""), r.stdout + r.stderr
    assert claude_md.read_text() == original                                       # a forged approval changes nothing
    with psycopg.connect(rdb) as conn:
        tok = AP.issue_tokens(conn, pid, h, ("apply", "reject", "undo"))
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"]) == "recorded"
    rep = AP.decide_poll(db_url=rdb, push=False)
    assert [x["result"] for x in rep.handled] == ["promoted"], rep.handled
    assert claude_md.read_text() == new.read_text()                                # exactly the approved bytes
    with psycopg.connect(rdb) as conn:
        assert AP.record_phone_decision(conn, pid, "undo", h, tok["undo"]) == "recorded"
    rep = AP.decide_poll(db_url=rdb, push=False)
    assert [x["result"] for x in rep.handled] == ["reverted"], rep.handled
    assert claude_md.read_text() == original                                       # undo restored the exact bytes
    ledger = (ac / "improve" / "ledger.jsonl").read_text()
    assert '"event": "promoted"' in ledger and '"event": "reverted"' in ledger and h in ledger


# --------------------------------------------------------------------------- rollback


def test_rollback_sql_reverses_the_migration_and_it_reapplies(pg_url: str) -> None:
    """docs/rsi/rollback_rsi_core.sql on a scratch database: RSI objects gone, the rest intact, migrate again works."""
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("drop database if exists rsi_rollback")
        conn.execute("create database rsi_rollback")
    url = pg_url.rsplit("/", 1)[0] + "/rsi_rollback"
    try:
        assert subprocess.run([AMEM, "db", "migrate"], env=base_env(AMEM_DB_URL=url), capture_output=True).returncode == 0
        sql = (Path(__file__).resolve().parents[1] / "docs" / "rsi" / "rollback_rsi_core.sql").read_text()
        with psycopg.connect(url) as conn:
            conn.execute("insert into edits (instruction_source, rule_id, status) values ('t', 'r', 'staged')")
        with psycopg.connect(url) as conn:
            with pytest.raises(psycopg.errors.RaiseException, match="staged"):
                conn.execute(sql)
        with psycopg.connect(url) as conn:
            conn.execute("update edits set status = 'refused'")
            conn.execute(sql)
        with psycopg.connect(url) as conn:
            assert conn.execute("select count(*) from pg_tables where tablename like 'rsi\\_%%'").fetchone() == (0,)
            cols = {r[0] for r in conn.execute("select column_name from information_schema.columns where table_name = 'sessions'")}
            assert "env" not in cols and "experiment_id" not in cols and "session_id" in cols
            assert conn.execute("select count(*) from edits").fetchone() == (1,)
        assert subprocess.run([AMEM, "db", "migrate"], env=base_env(AMEM_DB_URL=url), capture_output=True).returncode == 0
        with psycopg.connect(url) as conn:
            assert conn.execute("select count(*) from pg_tables where tablename like 'rsi\\_%%'").fetchone()[0] == 10
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("drop database if exists rsi_rollback with (force)")
