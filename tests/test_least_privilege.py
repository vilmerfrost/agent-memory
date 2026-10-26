"""Least-privilege promotion (docs/rsi/least-privilege.md): amem_app cannot create a human decision but can
run the routines and the promotion worker; phone Apply / Undo need a TOTP step, each step works once, wrong
codes lock; the API roles reach only the TOTP-gated function; the admin credential sits behind a Keychain
item that needs a human (a fake `security` here: the real login Keychain is never touched); the split of
credentials rewrites one env line and is undone exactly. A throwaway Postgres; nothing outside tmp_path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from agent_memory import credentials as C, edits_push, rsi_approval as AP, rsi_totp as TT
from conftest import AMEM, base_env
from test_rsi_approval import FAKE, calls, make_artifact

APP_PW = "app-test-password-0123456789"


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setenv("RSI_SANDBOX_DIR", str(root))
    for k in [k for k in os.environ if k in AP.AGENT_ENV or (k.startswith("CODEX_") and k != "CODEX_HOME")]:
        monkeypatch.delenv(k)
    for k in ("AMEM_RSI_DECIDE_PAGE", "AMEM_RSI_DECIDE_URL", C.ADMIN_ENV):
        monkeypatch.delenv(k, raising=False)
    return root                                  # C.SECURITY_ENV stays: the conftest tripwire, or `keychain`'s fake


def test_tests_never_reach_the_real_keychain() -> None:
    """After the split the real item holds the production DSN: no test, in-process or subprocess, may open it."""
    from conftest import KEYCHAIN_TRIPWIRE
    assert C._security() == KEYCHAIN_TRIPWIRE != C.DEFAULT_SECURITY
    assert base_env()[C.SECURITY_ENV] == KEYCHAIN_TRIPWIRE
    assert base_env(AMEM_ADMIN_SECURITY="/usr/bin/security")[C.SECURITY_ENV] == KEYCHAIN_TRIPWIRE
    assert not C.admin_present()                 # the tripwire exits 44: reads as "no item"
    out = subprocess.run([AMEM, "admin", "credentials-status"], env=base_env(), capture_output=True, text=True)
    assert "admin Keychain item: absent" in out.stdout


def as_app(pg_url: str) -> str:
    return pg_url.replace("postgres:postgres@", f"amem_app:{APP_PW}@", 1)


@pytest.fixture
def db(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The owner URL and the amem_app URL, with clean RSI tables."""
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute(f"alter role amem_app with login password '{APP_PW}'")
        conn.execute("set session_replication_role = replica")
        for t in ("rsi_totp_failures", "rsi_totp_used", "rsi_decision_results", "rsi_decisions", "rsi_decision_tokens",
                  "rsi_eval_results", "rsi_proposals", "rsi_artifacts", "rsi_candidates", "rsi_evidence",
                  "rsi_checkpoints", "rsi_experiments"):
            conn.execute(f"delete from {t}")
    monkeypatch.setenv("AMEM_DB_URL", as_app(pg_url))
    return {"owner": pg_url, "app": as_app(pg_url)}


def staged(url: str, root: Path, name: str = "x.md") -> tuple[str, str, Path]:
    h, d = make_artifact(root, [(f"~/{name}", b"a\n", b"b\n")])
    with psycopg.connect(url) as conn:
        pid = AP.create_proposal(conn, d, title=f"Demo {name}")
    return pid, h, d


# --------------------------------------------------------------------------- amem_app: no decision path


def test_amem_app_cannot_create_a_human_decision(db: dict[str, str], sandbox: Path) -> None:
    pid, h, _ = staged(db["app"], sandbox)                          # amem_app may stage (rsi_writer)
    attempts = [
        ("insert into rsi_decisions (proposal_id, action, artifact_hash, source, verified, expires_at) "
         f"values ('{pid}', 'apply', '{h}', 'phone', 'edge-token', now() + interval '1 h')"),
        f"select amem_rsi_record_decision('{pid}', 'apply', '{h}', '{'x' * 30}')",
        f"select amem_rsi_record_phone_decision('{pid}', 'apply', '{h}', '{'x' * 30}', 1)",
        f"select amem_rsi_record_cli_decision('{pid}', 'apply', '{h}', null)",
        f"select amem_rsi_phone_bad_code('{pid}')",
        "select * from rsi_totp_used",
        f"update rsi_artifacts set baseline_id = 'x' where hash = '{h}'",
        "truncate sessions",
        "create table forged (x int)",
        "set role postgres",
        f"update rsi_decision_tokens set used_at = null where proposal_id = '{pid}'",
    ]
    for sql in attempts:
        with psycopg.connect(db["app"]) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
    with psycopg.connect(db["owner"]) as conn:
        assert conn.execute("select count(*) from rsi_decisions").fetchone() == (0,)


def test_amem_app_runs_the_worker_end_to_end(db: dict[str, str], sandbox: Path, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "x.md").write_bytes(b"a\n")
    monkeypatch.setenv("HOME", str(home))
    script, log, mode = tmp_path / "improve", tmp_path / "improve.log", tmp_path / "mode.json"
    script.write_text(FAKE.format(py=sys.executable))
    script.chmod(0o755)
    mode.write_text(json.dumps({"promote": {"status": "promoted", "id": "prm-20261023-1"},
                                "revert": {"status": "reverted", "id": "prm-20261023-1"}}))
    for k, v in (("AMEM_IMPROVE_BIN", script), ("FAKE_LOG", log), ("FAKE_MODE", mode)):
        monkeypatch.setenv(k, str(v))
    pid, h, _ = staged(db["app"], sandbox)
    with psycopg.connect(db["app"]) as conn:
        tok = AP.issue_tokens(conn, pid, h)                          # the push, as amem_app
        assert set(tok) == {"apply", "reject"}
        assert AP.issue_tokens(conn, pid, h) .keys() == {"apply", "reject"}   # re-arm unused tokens (ON CONFLICT)
        tok = AP.issue_tokens(conn, pid, h)
    with psycopg.connect(db["owner"]) as conn:                       # what the edge function does, TOTP verified
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"], TT.step_of(), phone=True) == "recorded"
    with psycopg.connect(db["app"]) as conn:
        row = conn.execute("select id, decided_at, expires_at from rsi_decisions").fetchone()
        d = {"format": "rsi-decision/1", "decision_id": str(row[0]), "proposal_id": pid, "action": "apply",
             "artifact_hash": h, "decided_at": row[1].isoformat(), "expires_at": row[2].isoformat(),
             "source": "phone", "verified": "edge-token"}
        assert AP.verify_decision(conn, d) == (True, "ok")           # what improve promote asks, as amem_app
    rep = AP.decide_poll(db_url=db["app"], push=False)
    assert [x["result"] for x in rep.handled] == ["promoted"]
    with psycopg.connect(db["app"]) as conn:
        assert conn.execute("select status from rsi_proposals where id = %s", (pid,)).fetchone() == ("promoted",)
    assert any(isinstance(c, list) and c[0] == "promote" for c in calls(log))


def test_amem_app_runs_the_ingestion_paths(db: dict[str, str]) -> None:
    from agent_memory import store
    with psycopg.connect(db["app"]) as conn:
        conn.execute("insert into sessions (session_id, kind, project_dir, transcript_bytes, env) "
                     "values ('lp-1', 'chat', '/x', 10, 'prod') on conflict (session_id) do update set env = excluded.env")
        eid = conn.execute("insert into edits (instruction_source, rule_id, status) values ('t', 'r', 'kept') "
                           "returning id").fetchone()[0]
        assert conn.execute("select amem_request_undo(%s, 'cli')", (eid,)).fetchone()[0]
        assert conn.execute("select count(*) from schema_migrations").fetchone()[0] > 20
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("delete from schema_migrations")
    store.insert_run("lp-test", "pass", db_url=db["app"])
    with psycopg.connect(db["owner"]) as conn:
        conn.execute("delete from edits where id = %s", (eid,))
        conn.execute("delete from sessions where session_id = 'lp-1'")


def test_amem_app_fires_the_cloud_digest_triggers(db: dict[str, str]) -> None:
    """amem_app has no EXECUTE on the two cloud_digests trigger functions (revoked from PUBLIC on purpose).
    Triggers still fire: sessions_ingest marks digests ingested as amem_app after the split (E3 preflight)."""
    key = "cloud:lp-trigger-test"
    with psycopg.connect(db["owner"]) as conn:
        conn.execute("delete from cloud_digests where source_key = %s", (key,))
        assert not conn.execute("select has_function_privilege('amem_app', 'public.cloud_digests_keep_routine()', "
                                "'execute')").fetchone()[0]
    try:
        with psycopg.connect(db["app"]) as conn:
            conn.execute("insert into cloud_digests (source_key, kind, session_id, digest) values (%s, 'chat', 's', 'c')",
                         (key,))
            conn.execute("insert into cloud_digests (source_key, kind, routine, session_id) values (%s, 'routine', 'r1', 's')",
                         (key,))                                     # claim_chat deletes the chat row first
            conn.execute("update cloud_digests set kind = 'chat', routine = null, ingested_at = now() "
                         "where source_key = %s", (key,))           # keep_routine keeps it a routine row
            kind, routine, ingested = conn.execute("select kind, routine, ingested_at from cloud_digests "
                                                   "where source_key = %s", (key,)).fetchone()
        assert (kind, routine) == ("routine", "r1") and ingested is not None
    finally:
        with psycopg.connect(db["owner"]) as conn:
            conn.execute("delete from cloud_digests where source_key = %s", (key,))


def test_every_table_is_reachable_by_amem_app_or_deliberately_not(db: dict[str, str]) -> None:
    """Guards later migrations: a new routine table without an amem_app grant and policy fails here."""
    secret = {"rsi_totp_used", "rsi_totp_failures",                       # owner-only (20261025210000_rsi_web_decide)
              "rsi_passkeys", "rsi_passkey_enroll_codes", "rsi_web_challenges",
              "night_reads"}  # user reading state is owner-only, never routine input
    with psycopg.connect(db["owner"]) as conn:
        tables = [r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")]
        pol = {r[0] for r in conn.execute("select tablename from pg_policies where schemaname = 'public' and "
                                          "('amem_app' = any(roles) or 'rsi_writer' = any(roles) or 'rsi_reader' = any(roles))")}
        for t in tables:
            can = conn.execute("select has_table_privilege('amem_app', %s, 'select')", (f"public.{t}",)).fetchone()[0]
            if t in secret or t == "rsi_decision_tokens":
                assert not can or t == "rsi_decision_tokens", t
                continue
            assert can and t in pol, f"{t}: amem_app needs a grant and an RLS policy"
        for (v,) in conn.execute("select viewname from pg_views where schemaname = 'public'").fetchall():
            assert conn.execute("select has_table_privilege('amem_app', %s, 'select')", (f"public.{v}",)).fetchone()[0], v
    with psycopg.connect(db["app"]) as conn:
        conn.execute("select count(*) from session_groups").fetchone()


# --------------------------------------------------------------------------- TOTP in SQL


def test_phone_apply_needs_a_step_and_each_step_works_once(db: dict[str, str], sandbox: Path) -> None:
    p1, h1, _ = staged(db["owner"], sandbox, "a.md")
    p2, h2, _ = staged(db["owner"], sandbox, "b.md")
    with psycopg.connect(db["owner"]) as conn:
        t1, t2 = AP.issue_tokens(conn, p1, h1), AP.issue_tokens(conn, p2, h2)
        rec = lambda pid, a, h, tok, step: AP.record_phone_decision(conn, pid, a, h, tok, step, phone=True)  # noqa: E731
        assert rec(p1, "apply", h1, t1["apply"], None) == "totp-required"
        assert rec(p1, "apply", h1, t1["apply"], 0) == "totp-required"
        assert rec(p1, "apply", h1, t1["apply"], 61_000_000) == "recorded"
        assert rec(p2, "apply", h2, t2["apply"], 61_000_000) == "totp-replayed"     # same step, other proposal
        assert rec(p2, "reject", h2, t2["reject"], None) == "recorded"              # reject needs no code
        assert conn.execute("select count(*) from rsi_decisions").fetchone() == (2,)


def test_five_wrong_codes_lock_the_phone_path(db: dict[str, str], sandbox: Path) -> None:
    pid, h, _ = staged(db["owner"], sandbox)
    with psycopg.connect(db["owner"]) as conn:
        tok = AP.issue_tokens(conn, pid, h)
        for i in range(1, 6):
            assert conn.execute("select amem_rsi_phone_bad_code(%s)", (pid,)).fetchone()[0] == i
        assert AP.record_phone_decision(conn, pid, "apply", h, tok["apply"], 61_000_001, phone=True) == "locked"
        assert AP.record_phone_decision(conn, pid, "reject", h, tok["reject"], None, phone=True) == "locked"


def test_totp_matches_the_rfc_vector_and_the_window() -> None:
    assert TT.code_at(TT.RFC_SECRET_B32, TT.RFC_VECTOR[0] // TT.STEP) == TT.RFC_VECTOR[1]
    s, now = TT.new_secret(), 1_800_000_000.0
    assert TT.verify(s, TT.code_at(s, TT.step_of(now) - 1), t=now) == TT.step_of(now) - 1
    assert TT.verify(s, TT.code_at(s, TT.step_of(now) - 3), t=now) is None
    assert TT.verify(s, "12345", t=now) is None
    assert TT.otpauth_uri(s).startswith("otpauth://totp/agent-memory:rsi-approvals?secret=" + s)


# --------------------------------------------------------------------------- hosted Supabase grants


def run_check(url: str, path: Path, ok: str) -> None:
    notices: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.add_notice_handler(lambda d: notices.append(d.message_primary))
        conn.execute(path.read_text())
    assert ok in notices, (path.name, notices)


def test_supabase_api_roles_reach_only_the_totp_gated_function(pg_url: str) -> None:
    roles = ("anon", "authenticated", "service_role")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("drop database if exists rsi_lp")
        conn.execute("create database rsi_lp")
        for r in roles:
            conn.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{r}') then create role {r} nologin; end if; end $$")
    url = pg_url.rsplit("/", 1)[0] + "/rsi_lp"
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for kind in ("tables", "functions", "sequences"):
                conn.execute(f"alter default privileges in schema public grant all on {kind} to anon, authenticated, service_role")
        assert subprocess.run([AMEM, "db", "migrate"], env=base_env(AMEM_DB_URL=url), capture_output=True).returncode == 0
        with psycopg.connect(url) as conn:
            allowed = {"amem_rsi_record_phone_decision(uuid,text,text,text,bigint)": {"service_role"},
                       "amem_rsi_phone_bad_code(uuid)": {"service_role"},
                       "amem_rsi_record_decision(uuid,text,text,text)": set(),
                       "amem_rsi_record_cli_decision(uuid,text,text,text)": set(),
                       "rsi_insert_decision(public.rsi_proposals,text,text,text,text)": set()}
            for fn, ok in allowed.items():
                for r in (*roles, "amem_app", "rsi_writer"):
                    can = conn.execute("select has_function_privilege(%s, %s, 'execute')", (r, f"public.{fn}")).fetchone()[0]
                    assert can == (r in ok), (r, fn)
            for r in roles:
                for t in [x[0] for x in conn.execute("select tablename from pg_tables where tablename like 'rsi\\_%%'")]:
                    assert not conn.execute("select has_table_privilege(%s, %s, 'select')", (r, f"public.{t}")).fetchone()[0], (r, t)
            assert conn.execute("select has_table_privilege('service_role', 'public.undo_requests', 'select')").fetchone()[0]
        docs = Path(__file__).resolve().parents[1] / "docs" / "rsi"
        run_check(url, docs / "verify_least_privilege.sql", "least_privilege verify: OK")
        with psycopg.connect(url, autocommit=True) as conn:                # the reviewed rollback ...
            conn.execute((docs / "rollback_least_privilege.sql").read_text())
        run_check(url, docs / "verify_rsi_core.sql", "rsi_core verify: OK")   # ... restores the Phase C state
        assert subprocess.run([AMEM, "db", "migrate"], env=base_env(AMEM_DB_URL=url), capture_output=True).returncode == 0
        run_check(url, docs / "verify_least_privilege.sql", "least_privilege verify: OK")   # and it applies again
        with psycopg.connect(url) as conn:
            assert conn.execute("select count(*) from rsi_artifacts").fetchone() == (0,)     # checks left nothing
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("drop database if exists rsi_lp with (force)")
            for r in roles:
                conn.execute(f"drop role if exists {r}")


# --------------------------------------------------------------------------- the push: no Apply without a code

def _proposal_row(url: str, pid: str) -> dict[str, Any]:
    with psycopg.connect(url) as conn:
        cur = conn.execute("select * from rsi_proposals where id = %s", (pid,))
        return dict(zip([c.name for c in cur.description], cur.fetchone()))


def test_push_without_a_decide_page_offers_reject_only(db: dict[str, str], sandbox: Path) -> None:
    pid, h, _ = staged(db["owner"], sandbox)
    with psycopg.connect(db["owner"]) as conn:
        tok = AP.issue_tokens(conn, pid, h)
    pay = AP.proposal_payload("topic", _proposal_row(db["owner"], pid), tok, "https://fn.example.test/functions/v1/amem-decide")
    assert [a["label"] for a in pay["actions"]] == ["Reject"]
    assert tok["apply"] not in json.dumps(pay) and f"amem rsi decide {pid} apply" in pay["message"]
    assert json.loads(pay["actions"][0]["body"])["token"] == tok["reject"]


def test_push_with_a_decide_page_keeps_the_token_in_the_fragment(db: dict[str, str], sandbox: Path) -> None:
    pid, h, _ = staged(db["owner"], sandbox)
    with psycopg.connect(db["owner"]) as conn:
        tok = AP.issue_tokens(conn, pid, h)
    page = "https://decide.example.test/rsi/"
    pay = AP.proposal_payload("topic", _proposal_row(db["owner"], pid), tok, "https://fn.example.test/x", page)
    view, reject = pay["actions"]
    assert view["action"] == "view" and view["url"].startswith(page + "#")
    base, frag = view["url"].split("#", 1)
    assert tok["apply"] not in base and f"t={tok['apply']}" in frag and f"h={h}" in frag and "a=apply" in frag
    assert reject["action"] == "http" and json.loads(reject["body"])["action"] == "reject"


def test_decide_page_has_one_endpoint_and_refuses_plain_http() -> None:
    html = AP.render_decide_page("https://abc.supabase.co/functions/v1/amem-decide")
    assert "connect-src https://abc.supabase.co;" in html and '"https://abc.supabase.co/functions/v1/amem-decide"' in html
    assert "__AMEM_" not in html and "<script src" not in html and "https://fonts" not in html
    for bad in ("http://abc.supabase.co/x", "https://abc.supabase.co/x?u=evil", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            AP.render_decide_page(bad)


# --------------------------------------------------------------------------- the admin credential (fake Keychain)

FAKE_SECURITY = r'''#!{py}
import json, os, shlex, sys, time
store = os.environ["FAKE_KC"]
items = json.load(open(store)) if os.path.exists(store) else {{}}
def save():
    json.dump(items, open(store, "w"))
args = sys.argv[1:]
if args == ["-i"]:
    args = shlex.split(sys.stdin.read())
def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None
key = f"{{opt('-s')}}/{{opt('-a')}}"
cmd = args[0]
if cmd == "add-generic-password":
    if key in items: sys.exit(45)
    value = bytes.fromhex(opt("-X")).decode() if opt("-X") else opt("-w")
    trusted = [] if (opt("-T") == "" and os.environ.get("FAKE_IGNORE_T") != "1") else ["/usr/bin/security"]
    items[key] = {{"value": value, "trusted": trusted}}; save(); sys.exit(0)
if cmd == "find-generic-password":
    if key not in items: sys.exit(44)
    if "-w" not in args: print("attributes"); sys.exit(0)
    it = items[key]
    if not it["trusted"]:
        answer = os.environ.get("FAKE_DIALOG", "wait")
        if answer == "wait": time.sleep(30)
        if answer == "deny": sys.exit(128)
    print(it["value"]); sys.exit(0)
if cmd == "delete-generic-password":
    if key not in items: sys.exit(44)
    del items[key]; save(); sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def keychain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe, store = tmp_path / "security", tmp_path / "kc.json"
    exe.write_text(FAKE_SECURITY.format(py=sys.executable))
    exe.chmod(0o755)
    monkeypatch.setenv(C.SECURITY_ENV, str(exe))
    monkeypatch.setenv("FAKE_KC", str(store))
    monkeypatch.setattr(C, "GATE_PROBE", 1.0)
    monkeypatch.setattr(C, "READ_TIMEOUT", 1.5)
    return store


def test_admin_item_is_gated_and_reads_fail_closed(keychain: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert not C.admin_present()
    with pytest.raises(C.AdminUnavailable, match="no admin credential"):
        C.admin_db_url()
    C.store_admin("postgresql://postgres:s3cr%40t@db.example.test:5432/postgres")
    assert C.admin_present() and C.gated()
    assert json.loads(keychain.read_text())[f"{C.ADMIN_SERVICE}/{C.ADMIN_ACCOUNT}"]["trusted"] == []
    with pytest.raises(C.AdminUnavailable, match="not answered"):            # nobody at the Mac: fail closed
        C.admin_db_url()
    monkeypatch.setenv("FAKE_DIALOG", "deny")
    with pytest.raises(C.AdminUnavailable, match="refused"):
        C.admin_db_url()
    monkeypatch.setenv("FAKE_DIALOG", "allow")
    assert C.admin_db_url() == "postgresql://postgres:s3cr%40t@db.example.test:5432/postgres"
    with pytest.raises(C.AdminUnavailable, match="already exists"):
        C.store_admin("postgresql://postgres:other@h/postgres")


def test_an_open_keychain_item_is_deleted_and_refused(keychain: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_IGNORE_T", "1")                               # as if -T "" were dropped by the parser
    with pytest.raises(C.AdminUnavailable, match="without a dialog"):
        C.store_admin("postgresql://postgres:pw@h/postgres")
    assert not C.admin_present()


def test_db_migrate_and_cli_decide_never_fall_back_when_the_item_exists(keychain: Path, monkeypatch: pytest.MonkeyPatch,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    from agent_memory import cli
    C.store_admin("postgresql://postgres:pw@127.0.0.1:1/postgres")
    monkeypatch.setenv("AMEM_DB_URL", "postgresql://amem_app:pw@127.0.0.1:1/postgres")
    assert cli.main(["db", "migrate"]) == 2
    assert "migrate refused" in capsys.readouterr().err
    assert cli.main(["rsi", "decide", "00000000-0000-0000-0000-000000000000", "apply"]) == 2


def test_app_dsn_maps_the_user_and_quotes_the_password() -> None:
    assert C.app_dsn("postgresql://postgres:old@db.abc.supabase.co:5432/postgres?sslmode=require", "p@ss/w:rd") == \
        "postgresql://amem_app:p%40ss%2Fw%3Ard@db.abc.supabase.co:5432/postgres?sslmode=require"
    assert C.app_dsn("postgresql://postgres.abc123:old@aws-0-eu-north-1.pooler.supabase.com:6543/postgres", "pw") == \
        "postgresql://amem_app.abc123:pw@aws-0-eu-north-1.pooler.supabase.com:6543/postgres"
    for bad in ("postgresql://someone:pw@h/db", "host=h user=postgres", "mysql://postgres:pw@h/db"):
        with pytest.raises(ValueError):
            C.app_dsn(bad, "pw")


def test_project_url_still_resolves_after_the_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMEM_SUPABASE_URL", raising=False)
    monkeypatch.setenv("AMEM_DB_URL", "postgresql://amem_app.abc123:pw@aws-0-eu-north-1.pooler.supabase.com:6543/postgres")
    assert edits_push.project_url() == "https://abc123.supabase.co"


def test_supabase_key_kind_never_shows_the_value() -> None:
    import base64 as b64
    jwt = lambda role: "x." + b64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=") + ".y"  # noqa: E731
    assert [C.supabase_key_kind(v) for v in ("", "sb_secret_abc", "sb_publishable_abc", jwt("anon"), jwt("service_role"), "zzz")] \
        == ["none", "secret", "publishable", "anon", "service_role", "unknown"]


def test_split_and_restore_credentials_round_trip(db: dict[str, str], keychain: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_memory import cli_admin
    envf = tmp_path / "env.local"
    original = f'TYPESAFE_API_KEY=keep-me\nAMEM_DB_URL="{db["owner"]}"\nAMEM_JEV_LIVE=1\n'
    envf.write_text(original)
    envf.chmod(0o600)
    monkeypatch.setenv("FAKE_DIALOG", "wait")                              # the probe sees a dialog: gated
    cli_admin.split(db["owner"], envf, say=lambda *_: None)
    text = envf.read_text()
    lines = text.splitlines()
    assert lines[0] == "TYPESAFE_API_KEY=keep-me" and lines[2] == "AMEM_JEV_LIVE=1"
    app = lines[1].split("=", 1)[1].strip('"')
    assert lines[1].startswith('AMEM_DB_URL="') and C.dsn_user(app) == "amem_app" and db["owner"] not in text
    assert oct(envf.stat().st_mode & 0o777) == "0o600"
    with psycopg.connect(app) as conn:
        assert conn.execute("select current_user").fetchone() == ("amem_app",)
    assert C.admin_present()
    monkeypatch.setenv("FAKE_DIALOG", "allow")
    cli_admin.restore(envf)
    assert envf.read_text() == original and not C.admin_present()
    with pytest.raises(ValueError, match="postgres user"):                 # a second split is refused
        cli_admin.split(app, envf, say=lambda *_: None)


def test_admin_commands_refuse_agents_and_missing_terminals() -> None:
    from agent_memory import cli_admin
    assert "agent session" in (cli_admin._human_only("x", stdin_isatty=True, environ={"CLAUDECODE": "1"}) or "")
    assert "terminal" in (cli_admin._human_only("x", stdin_isatty=False, environ={}) or "")
    assert cli_admin._human_only("x", stdin_isatty=True, environ={}) is None
    for cmd in (["admin", "split-credentials"], ["admin", "restore-credentials"], ["rsi", "totp-enroll"]):
        r = subprocess.run([AMEM, *cmd], env={**base_env(), "CLAUDECODE": "1"}, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        assert r.returncode == 2 and "refused" in r.stderr, (cmd, r.stderr)
