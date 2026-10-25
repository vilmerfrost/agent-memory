"""The 5-minute promotion worker `amem rsi decide-job` (docs/rsi/activation.md step 10b): the launchd plist, the
least-privilege login check, decide-poll and the edit undo-poll as amem_app, the routines.log line, and what Night
Desk reads back through owner RLS (rsi_decision_status, edit_undo_status, rsi_decision_results).

A throwaway Postgres, a fake improve and a tmp HOME / sandbox. Nothing outside tmp_path is read or written.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import (edits_push, routines_log as RL, rsi_approval as AP, rsi_artifact as RA, rsi_decide_job as J,
                          rsi_targets as T)
from conftest import AMEM, base_env
from test_least_privilege import APP_PW, as_app, sandbox  # noqa: F401  (autouse: tmp RSI sandbox, no agent markers)
from test_rsi_approval import make_artifact

REPO = Path(__file__).resolve().parents[1]
LAUNCHD = REPO / "deploy" / "launchd"
MIGRATIONS = REPO / "supabase" / "migrations"
OWNER = str(uuid.UUID(int=7101))
OTHER = str(uuid.UUID(int=7102))

FAKE = '''#!{py}
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\\n")
mode = json.load(open(os.environ["FAKE_MODE"]))
if a[0] == "recover":
    print(json.dumps({{"cmd": "recover", "status": "ok", "outcomes": []}})); sys.exit(0)
if a[0] == "promote":
    d = json.load(open(a[a.index("--decision") + 1]))
    r = mode["promote"].get(d["artifact_hash"], mode["promote"]["*"])
    print(json.dumps(r["out"])); sys.exit(r.get("exit", 0))
if a[0] == "revert-change":
    print(json.dumps(mode["revert_change"])); sys.exit(mode.get("revert_change_exit", 0))
if a[0] == "revert":
    print("reverted " + a[1]); sys.exit(mode.get("revert_exit", 0))
sys.exit(2)
'''


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def world(pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Clean RSI + edit tables, amem_app with a password, a fake improve, a tmp HOME whose live files match."""
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        conn.execute("set session_replication_role = replica")
        for t in ("rsi_totp_failures", "rsi_totp_used", "rsi_decision_results", "rsi_decisions", "rsi_decision_tokens",
                  "rsi_eval_results", "rsi_proposals", "rsi_artifacts", "undo_requests", "edits"):
            conn.execute(f"delete from {t}")
    home = tmp_path / "home"
    home.mkdir()
    for name in ("a.md", "b.md", "c.md", "d.md"):
        (home / name).write_bytes(b"old\n")
    monkeypatch.setenv("HOME", str(home))
    script, log, mode = tmp_path / "improve", tmp_path / "improve.log", tmp_path / "mode.json"
    script.write_text(FAKE.format(py=sys.executable))
    script.chmod(0o755)
    mode.write_text(json.dumps({"promote": {"*": {"out": {"status": "promoted", "id": "prm-20261025-1"}}},
                                "revert_change": {"status": "reverted", "id": "prm-20261025-1"}}))
    for k, v in (("AMEM_IMPROVE_BIN", script), ("FAKE_LOG", log), ("FAKE_MODE", mode)):
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("AMEM_DB_URL", as_app(pg_url))
    return {"owner": pg_url, "app": as_app(pg_url), "home": home, "log": log, "mode": mode,
            "routines": tmp_path / "routines.log"}


def calls(log: Path) -> list[list[str]]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def stage(w: dict[str, Any], sandbox: Path, name: str, title: str) -> tuple[str, str]:  # noqa: F811
    h, d = make_artifact(sandbox, [(f"~/{name}", b"old\n", b"new\n")], exp=f"exp-{name}")
    with psycopg.connect(w["app"]) as conn:                                # staging runs as amem_app (rsi_writer)
        pid = AP.create_proposal(conn, d, title=title)
    return pid, h


def record(owner: str, pid: str, h: str, action: str, *, web: bool = True) -> str:
    """A recorded Night Desk decision, inserted as the superuser (the web edge function's SQL is the sibling's)."""
    with psycopg.connect(owner) as conn:                               # channel is generated from source
        names = ["proposal_id", "action", "artifact_hash", "source", "verified", "reason"]
        vals = [pid, action, h, "web" if web else "phone", "edge-token", "night desk test"]
        did = conn.execute(f"insert into rsi_decisions ({', '.join(names)}, expires_at) values "
                           f"({', '.join(['%s'] * len(names))}, now() + interval '24 hours') returning id", vals).fetchone()[0]
        new = {"apply": "approved", "reject": "rejected"}.get(action)
        if new:
            conn.execute("update rsi_proposals set status = %s, decided_at = now() where id = %s", (new, pid))
    return str(did)


def edit_with_undo(owner: str, improve_id: str, status: str = "kept") -> str:
    with psycopg.connect(owner) as conn:
        eid = conn.execute("insert into edits (instruction_source, rule_id, rule_title, status, improve_id, target_path) "
                           "values ('skill:dispatch', 'spawn', '4. Spawn', %s, %s, '~/c.md') returning id",
                           (status, improve_id)).fetchone()[0]
        assert conn.execute("select amem_request_undo(%s, 'phone')", (eid,)).fetchone()[0] in ("queued", "nothing")
    return str(eid)


@pytest.fixture
def night_desk(world: dict[str, Any]):
    """An owner-JWT connection the way PostgREST runs it: the Supabase API roles, auth.uid(), the Night Desk
    migration and this one applied inside one transaction that is rolled back. Set up on first use only: the
    migrations lock the tables until the rollback, so the job must have run before."""
    conn = psycopg.connect(world["owner"])
    ready: list[bool] = []

    def setup() -> None:
        for role in ("anon", "authenticated"):
            conn.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{role}') then create role {role}; "
                         "end if; end $$")
        conn.execute("create schema if not exists auth")
        conn.execute("create or replace function auth.uid() returns uuid language sql stable as "
                     "$$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$")
        conn.execute("grant usage on schema auth, public to authenticated, anon")
        for pat in ("*_night_desk.sql", "*_rsi_decision_status.sql"):
            conn.execute(next(MIGRATIONS.glob(pat)).read_text())
        conn.execute("insert into amem_private.dashboard_owner values (true, %s) on conflict (singleton) do update "
                     "set uid = excluded.uid", (OWNER,))
        ready.append(True)

    def as_user(uid: str, role: str = "authenticated") -> psycopg.Connection:
        if not ready:
            setup()
        conn.execute("reset role")
        conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (uid,))
        conn.execute(f"set local role {role}")
        return conn
    try:
        yield as_user
    finally:
        conn.rollback()
        conn.close()


def rows(conn: psycopg.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    cur = conn.execute(sql, args)
    return [dict(zip([c.name for c in cur.description], r)) for r in cur.fetchall()]


# --------------------------------------------------------------------------- the plist (files only)


def load(name: str) -> dict[str, Any]:
    return plistlib.loads((LAUNCHD / name).read_bytes())


def test_plist_lints_and_matches_the_other_routines() -> None:
    path = LAUNCHD / "com.vilmerfrost.amem-rsi-decide.plist"
    if shutil.which("plutil"):
        r = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.strip().endswith(": OK"), r.stdout + r.stderr
    p, night, brief = load(path.name), load("com.vilmerfrost.amem-rsi-night.plist"), load("com.vilmerfrost.amem-rsi-brief.plist")
    assert p["Label"] == "com.vilmerfrost.amem-rsi-decide" and p["RunAtLoad"] is False and "KeepAlive" not in p
    assert p["StartInterval"] == 300 and "StartCalendarInterval" not in p             # every 5 minutes
    args = p["ProgramArguments"]
    assert args[:6] == night["ProgramArguments"][:6] == brief["ProgramArguments"][:6]  # uv run --directory <repo> amem
    assert args[6:] == ["rsi", "decide-job"]                                          # the wrapper, never bare decide-poll
    for k in ("StandardOutPath", "StandardErrorPath", "StandardInPath"):
        assert p[k] == night[k] == brief[k], k                                        # launchd.log; stdin /dev/null
    env = p["EnvironmentVariables"]
    assert env == {k: v for k, v in brief["EnvironmentVariables"].items()}             # same env as the brief job
    assert env["AMEM_REQUIRE_REMOTE"] == "1" and env["AMEM_HOOK_OFF"] == "1"
    for k in env:                                     # credentials come from .env.local like every routine, never here
        assert not any(s in k for s in ("KEY", "TOKEN", "SECRET", "PASSWORD", "DB_URL", "CLAUDE", "CODEX")), k


# --------------------------------------------------------------------------- the login check


def test_login_check_accepts_amem_app_and_refuses_decision_capable_logins(world: dict[str, Any]) -> None:
    with psycopg.connect(world["app"]) as conn:
        assert AP.login_problems(conn) == ("amem_app", [])
    with psycopg.connect(world["owner"]) as conn:
        login, why = AP.login_problems(conn)
    assert login == "postgres" and any("superuser" in x for x in why) and any("insert into rsi_decisions" in x for x in why)
    # A decision function added later (the web path, say) is caught by its body, not by a name list.
    fn = "public.lp_probe_record_web_decision(uuid)"
    with psycopg.connect(world["owner"], autocommit=True) as conn:
        conn.execute("create or replace function public.lp_probe_record_web_decision(p uuid) returns text "
                     "language plpgsql security definer set search_path = public as $$ declare pr rsi_proposals; begin "
                     "select * into pr from rsi_proposals where id = p; "
                     "return public.rsi_insert_decision(pr, 'apply', 'phone', 'edge-token', null); end $$")
        conn.execute(f"grant execute on function {fn} to amem_app")
    try:
        with psycopg.connect(world["app"]) as conn:
            login, why = AP.login_problems(conn)
        assert why == [f"amem_app can execute a decision function: {fn.removeprefix('public.')}"]
        res = J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])
        assert res is not None and res.status == "FAIL" and "decisions" not in res.fields
        assert "refusing to run unattended" in world["routines"].read_text()
        assert calls(world["log"]) == []                                              # improve never ran
    finally:
        with psycopg.connect(world["owner"], autocommit=True) as conn:
            conn.execute(f"drop function {fn}")


def test_amem_app_cannot_record_a_decision_or_touch_the_results_views(world: dict[str, Any], sandbox: Path) -> None:  # noqa: F811
    pid, h = stage(world, sandbox, "a.md", "Probe")
    for sql in (f"insert into rsi_decisions (proposal_id, action, artifact_hash, source, verified, expires_at) "
                f"values ('{pid}', 'apply', '{h}', 'phone', 'edge-token', now() + interval '1 h')",
                f"select amem_rsi_record_decision('{pid}', 'apply', '{h}', '{'x' * 30}')",
                f"select amem_rsi_record_cli_decision('{pid}', 'apply', '{h}', null)",
                f"select rsi_insert_decision(p, 'apply', 'phone', 'edge-token', null) from rsi_proposals p where id = '{pid}'",
                "insert into rsi_decision_status (decision_id) values (gen_random_uuid())",
                "delete from rsi_decision_results"):
        with psycopg.connect(world["app"]) as conn:
            with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ObjectNotInPrerequisiteState)):
                conn.execute(sql)
    with psycopg.connect(world["owner"]) as conn:
        assert conn.execute("select count(*) from rsi_decisions").fetchone() == (0,)


# --------------------------------------------------------------------------- the job end to end, as amem_app


def test_job_applies_rejects_and_undoes_and_night_desk_reads_every_result(world: dict[str, Any], sandbox: Path,  # noqa: F811
                                                                          night_desk: Any) -> None:
    applied, ha = stage(world, sandbox, "a.md", "Shorter dispatch brief")
    denied, hb = stage(world, sandbox, "b.md", "Drop the closing line")
    d_apply = record(world["owner"], applied, ha, "apply")
    d_reject = record(world["owner"], denied, hb, "reject")
    e_undo = edit_with_undo(world["owner"], "imp-20261025-7")
    e_nothing = edit_with_undo(world["owner"], "imp-20261025-8", status="auto-reverted")

    res = J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])
    assert res is not None and res.status == "PASS", (res.items, res.lines)
    assert res.fields["login"] == "amem_app" and res.fields["decisions"] == 2
    assert res.fields["d_promoted"] == 1 and res.fields["d_rejected"] == 1
    assert res.fields["undo_requests"] == 2 and res.fields["u_reverted"] == 1 and res.fields["u_nothing"] == 1
    made = [c for c in calls(world["log"]) if c[0] in ("promote", "revert")]
    assert [c[0] for c in made] == ["promote", "revert"] and made[1][1] == "imp-20261025-7"

    # routines.log: the house format, parsed by the same parser routine-audit / ingest use
    (run,) = RL.parse_lines(world["routines"].read_text().splitlines()).runs
    assert run.routine == "amem-rsi-decide" and run.status == "pass"
    assert "login=amem_app" in world["routines"].read_text()

    # Undo of the applied change, recorded from Night Desk five minutes later
    d_undo = record(world["owner"], applied, ha, "undo")
    res2 = J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])
    assert res2 is not None and res2.status == "PASS" and res2.fields["d_reverted"] == 1

    conn = night_desk(OWNER)
    st = {r["decision_id"]: r for r in rows(conn, "select * from rsi_decision_status")}
    assert {str(k): (v["action"], v["state"], v["reason"]) for k, v in st.items()} == {
        d_apply: ("apply", "applied", None), d_reject: ("reject", "rejected", None), d_undo: ("undo", "undone", None)}
    assert all(v["state_at"] is not None and v["attempts"] == 1 for v in st.values())
    assert st[uuid.UUID(d_apply)]["change_id"] == "prm-20261025-1"
    assert {str(r["proposal_id"]): r["proposal_status"] for r in st.values()} == {applied: "reverted", denied: "rejected"}
    res_rows = rows(conn, "select decision_id, result, detail, handled_at from rsi_decision_results")
    assert sorted(r["result"] for r in res_rows) == ["promoted", "rejected", "reverted"]
    assert all(r["handled_at"] is not None for r in res_rows)
    undo = {str(r["edit_id"]): r for r in rows(conn, "select * from edit_undo_status")}
    assert (undo[e_undo]["state"], undo[e_undo]["reason"]) == ("undone", None)
    assert (undo[e_nothing]["state"], undo[e_nothing]["reason"]) == ("refused", "nothing-to-undo")

    for uid, role in ((OTHER, "authenticated"), ("", "authenticated")):            # another user, or no user
        conn = night_desk(uid, role)
        for v in ("rsi_decision_status", "edit_undo_status", "rsi_decision_results"):
            assert rows(conn, f"select * from {v}") == [], (uid, v)
    conn = night_desk("", "anon")
    for v in ("rsi_decision_status", "edit_undo_status", "rsi_decision_results"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(f"select * from {v}")
    conn = night_desk(OWNER)                                                        # the owner reads, never writes
    for sql in ("update rsi_decision_results set result = 'promoted'", "delete from rsi_decision_results",
                "select token_hash from rsi_decision_tokens"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(sql)


def test_refusals_and_retries_show_a_fixed_reason(world: dict[str, Any], sandbox: Path, night_desk: Any) -> None:  # noqa: F811
    """Two inputs beyond the brief's list: a live file that moved after staging (stale), and improve's write lock
    held (busy: still waiting, then retried). Plus an undo whose file changed after promotion (conflict)."""
    stale, hs = stage(world, sandbox, "a.md", "Stale target")
    busy, hb = stage(world, sandbox, "b.md", "Busy target")
    conflict, hc = stage(world, sandbox, "c.md", "Conflicting undo")
    world["mode"].write_text(json.dumps({
        "promote": {hs: {"out": {"status": "refused", "reason": "stale baseline or conflicting production change: "
                                                                 "~/a.md"}, "exit": 2},
                    hb: {"out": {"status": "refused", "reason": "busy: write lock held"}, "exit": 75},
                    "*": {"out": {"status": "promoted", "id": "prm-20261025-9"}}},
        "revert_change": {"status": "conflict", "conflicts": ["~/c.md changed after promotion"]}}))
    d_stale, d_busy = record(world["owner"], stale, hs, "apply"), record(world["owner"], busy, hb, "apply")
    d_c = record(world["owner"], conflict, hc, "apply")
    res = J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])
    assert res is not None and res.status == "PASS"
    assert (res.fields["d_refused"], res.fields["d_retry"], res.fields["d_promoted"]) == (1, 1, 1)
    assert any("refused" in w and "stale baseline" in w for w in res.warnings)       # a -warn line, not a FAIL
    d_undo = record(world["owner"], conflict, hc, "undo")
    J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])

    conn = night_desk(OWNER)
    st = {str(r["decision_id"]): (r["state"], r["reason"]) for r in rows(conn, "select * from rsi_decision_status")}
    assert st[d_stale] == ("refused", "live-file-changed")
    assert st[d_busy] == ("waiting", "retrying")                                    # retried on the next run
    assert st[d_c] == ("applied", None) and st[d_undo] == ("refused", "undo-conflict")
    conn.execute("reset role")
    detail = conn.execute("select detail from rsi_decision_results where decision_id = %s", (d_stale,)).fetchone()[0]
    assert "stale baseline" in detail                                               # the full text stays for the owner


def test_job_refuses_agent_sessions_and_overlaps(world: dict[str, Any]) -> None:
    assert J.run_decide_job(db_url=world["app"], routines_log=world["routines"], environ={"CLAUDECODE": "1"}) is None
    out = subprocess.run([AMEM, "rsi", "decide-job", "--routines-log", str(world["routines"])],
                         env=base_env(CLAUDECODE="1", AMEM_DB_URL=world["app"]), capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 2 and "refused inside an agent session" in out.stderr
    assert not world["routines"].exists()                                            # nothing ran, nothing logged
    with J.job_lock() as got:
        assert got
        res = J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])
    assert res is not None and res.status == "SKIP" and "overlap" in res.lines[1]
    assert calls(world["log"]) == []


def test_cli_runs_as_amem_app_from_the_env_like_launchd(world: dict[str, Any], sandbox: Path) -> None:  # noqa: F811
    pid, h = stage(world, sandbox, "a.md", "CLI path")
    did = record(world["owner"], pid, h, "reject")
    env = base_env(AMEM_DB_URL=world["app"], RSI_SANDBOX_DIR=str(sandbox), HOME=str(world["home"]),
                   AMEM_IMPROVE_BIN=str(Path(world["log"]).parent / "improve"), FAKE_LOG=str(world["log"]),
                   FAKE_MODE=str(world["mode"]))
    out = subprocess.run([AMEM, "rsi", "decide-job", "--no-push", "--json", "--routines-log", str(world["routines"])],
                         env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    rep = json.loads(out.stdout)
    assert rep["status"] == "PASS" and rep["fields"]["login"] == "amem_app" and rep["fields"]["d_rejected"] == 1
    assert rep["detail"]["decide"]["handled"] == [{"decision": did, "result": "rejected"}]


# --------------------------------------------------------------------------- the real improve (opt-in)

IMPROVE_REPO = os.environ.get("RSI_TEST_IMPROVE_REPO")


@pytest.mark.skipif(not IMPROVE_REPO, reason="set RSI_TEST_IMPROVE_REPO to an agent-config checkout (read only)")
def test_job_with_the_real_improve_promotes_and_undoes_in_a_sandbox_home(world: dict[str, Any], sandbox: Path,  # noqa: F811
                                                                         tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                         night_desk: Any) -> None:
    """Real improve (stage, promote, revert-change, verify-decision through amem) in a fake HOME, decide-job as
    amem_app, a web Apply and a web Undo recorded as the superuser. The live setup is never touched."""
    repo = Path(IMPROVE_REPO or "")
    home = tmp_path / "rhome"
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
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(ac), *args], check=True)
    env = base_env(AMEM_DB_URL=world["app"], HOME=str(home), IMPROVE_AC=str(ac), RSI_SANDBOX_DIR=str(sandbox),
                   ROUTINE_STATE=str(home / ".claude" / "parent"), PYTHONDONTWRITEBYTECODE="1",
                   IMPROVE_AMEM=shlex.quote(AMEM), AMEM_AGENT_CONFIG_DIR=str(ac))
    env = {k: v for k, v in env.items() if k not in AP.AGENT_ENV and not (k.startswith("CODEX_") and k != "CODEX_HOME")}
    for k in ("HOME", "IMPROVE_AC", "ROUTINE_STATE", "IMPROVE_AMEM", "AMEM_AGENT_CONFIG_DIR"):
        monkeypatch.setenv(k, env[k])
    monkeypatch.setenv("AMEM_IMPROVE_BIN", str(repo / "bin" / "improve"))
    _, bdir = T.create_baseline([claude_md])
    new = tmp_path / "new.md"
    new.write_text(original.replace("a short summary", "a section that lists the tools you used"))
    r = subprocess.run([sys.executable, str(repo / "bin" / "improve"), "stage", "--json", "--experiment",
                        "exp-20261025-01", "--baseline", str(bdir), "--change", f"~/.claude/CLAUDE.md={new}",
                        "--rule-id", "~/.claude/CLAUDE.md=claude-md-global--closing-line"],
                       capture_output=True, text=True, env=env)
    st = json.loads(r.stdout.strip().splitlines()[-1])
    assert st["status"] == "staged", r.stdout + r.stderr
    h, adir = st["artifact_hash"], Path(st["artifact_dir"])
    m = json.loads((adir / "manifest.json").read_text())
    rec = {"format": RA.EVAL_FORMAT, "artifact_hash": h, "experiment_id": m["experiment_id"], "baseline_id": m["baseline_id"],
           "suite": "sandbox-replay", "suite_version": "1", "model": "none", "effort": "none", "verdict": "pass",
           "started_at": "2026-10-25T00:00:00+00:00", "finished_at": "2026-10-25T00:00:01+00:00", "runner": "test",
           "results": {}}
    (adir / "evals" / "sandbox-replay.json").write_bytes(RA.canonical(rec) + b"\n")
    with psycopg.connect(world["app"]) as conn:
        pid = AP.create_proposal(conn, adir, title="Closing line as a contract")
    d_apply = record(world["owner"], pid, h, "apply")
    res = J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])
    assert res is not None and res.status == "PASS" and res.fields["d_promoted"] == 1, (res.items, res.detail)
    assert claude_md.read_text() == new.read_text()                                # exactly the approved bytes
    d_undo = record(world["owner"], pid, h, "undo")
    res = J.run_decide_job(db_url=world["app"], push=False, routines_log=world["routines"])
    assert res is not None and res.status == "PASS" and res.fields["d_reverted"] == 1, (res.items, res.detail)
    assert claude_md.read_text() == original                                       # undo restored the exact bytes
    conn = night_desk(OWNER)
    st2 = {str(r["decision_id"]): (r["state"], r["change_id"]) for r in rows(conn, "select * from rsi_decision_status")}
    assert st2[d_apply][0] == "applied" and st2[d_apply][1].startswith("prm-") and st2[d_undo][0] == "undone"


# --------------------------------------------------------------------------- the documented rollback


def test_documented_rollback_removes_the_migration_and_it_reapplies(pg_url: str) -> None:
    doc = (REPO / "docs" / "rsi" / "least-privilege.md").read_text()
    block = doc.split("## Unattended decide-job", 1)[1].split("```sql\n", 1)[1].split("```", 1)[0]
    body = "\n".join(ln for ln in block.splitlines() if ln.strip() not in ("begin;", "commit;"))
    cols = ("select column_name from information_schema.columns where table_name = 'rsi_decision_results' "
            "and column_name in ('reason_code', 'updated_at')")
    with psycopg.connect(pg_url) as conn:                    # one transaction, rolled back: the session DB stays
        assert len(conn.execute(cols).fetchall()) == 2
        conn.execute(body)
        assert conn.execute(cols).fetchall() == []
        assert conn.execute("select to_regclass('public.rsi_decision_status')").fetchone() == (None,)
        conn.execute(next(MIGRATIONS.glob("*_rsi_decision_status.sql")).read_text())
        assert len(conn.execute(cols).fetchall()) == 2
        conn.rollback()


# --------------------------------------------------------------------------- two pollers, one revert


def test_two_undo_pollers_revert_once(world: dict[str, Any]) -> None:
    eid = edit_with_undo(world["owner"], "imp-20261025-3")
    with psycopg.connect(world["app"]) as holder:                                  # the other poller holds the claim
        holder.execute("select 1 from undo_requests where edit_id = %s for update", (eid,))
        assert edits_push.undo_poll(push=False, db_url=world["app"]).outcomes == []
        assert calls(world["log"]) == []
    (o,) = edits_push.undo_poll(push=False, db_url=world["app"]).outcomes        # released: handled once
    assert o.result == "reverted"
    assert edits_push.undo_poll(push=False, db_url=world["app"]).outcomes == []
    barrier, outs = threading.Barrier(2), []
    eid2 = edit_with_undo(world["owner"], "imp-20261025-4")

    def poll() -> None:
        barrier.wait()
        outs.extend(edits_push.undo_poll(push=False, db_url=world["app"]).outcomes)
    threads = [threading.Thread(target=poll) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert [o.result for o in outs] == ["reverted"] and str(outs[0].edit_id) == eid2
    assert [c for c in calls(world["log"]) if c[0] == "revert"] == [
        ["revert", "imp-20261025-3", "--reason", "phone undo"], ["revert", "imp-20261025-4", "--reason", "phone undo"]]
