"""Web Apply / Reject / Undo from Night Desk (docs/rsi/approval.md "Web decisions").

SQL: migration 20261025210000_rsi_web_decide.sql on the throwaway Postgres (the edge function's checks of the
login and the WebAuthn signature are trusted here; supabase/functions/amem-decide/web_test.ts covers them).
Grants: the Supabase API roles recreated with default privileges, plus the go-live revoke of
amem_request_undo_owner (supabase/migrations-golive/). CLI: `amem rsi passkey-*`. Deno: the edge tests and an
end-to-end run (real handler + real SQL + a software passkey) in the pinned Deno image, skipped without it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from agent_memory import rsi_approval as AP, rsi_passkey as PK
from conftest import AMEM, base_env
from test_least_privilege import as_app, sandbox, staged  # noqa: F401  (sandbox: autouse fixture)
from test_rsi_approval import FAKE, calls

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import edge_test  # noqa: E402

GOLIVE = REPO / "supabase" / "migrations-golive" / "20261025220000_night_desk_web_golive.sql"
WEB_TABLES = ("rsi_passkeys", "rsi_passkey_enroll_codes", "rsi_web_challenges")
WEB_FUNCS = ("amem_rsi_web_challenge(text,uuid,text,text)", "amem_rsi_web_context(uuid,text)",
             "amem_rsi_web_decide(uuid,text,text,bigint)", "amem_rsi_web_enroll_start(text,text)",
             "amem_rsi_web_enroll_finish(uuid,text,text,text,bigint,text[],text,boolean)")
N = iter(range(1, 10_000))


def nonce() -> str:
    return hashlib.sha256(f"nonce-{next(N)}-{id(object())}".encode()).hexdigest()


@pytest.fixture
def db(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("alter role amem_app with login password 'app-test-password-0123456789'")
        conn.execute("set session_replication_role = replica")
        for t in ("rsi_web_challenges", "rsi_passkey_enroll_codes", "rsi_decision_results", "rsi_decisions",
                  "rsi_passkeys", "rsi_decision_tokens", "rsi_eval_results", "rsi_proposals", "rsi_artifacts",
                  "undo_requests"):
            conn.execute(f"delete from {t}")
        conn.execute("delete from edits where rule_id like 'web-%'")
    monkeypatch.setenv("AMEM_DB_URL", as_app(pg_url))
    return {"owner": pg_url, "app": as_app(pg_url)}


def q(conn: psycopg.Connection, sql: str, *args: object) -> object:
    return conn.execute(sql, args).fetchone()[0]


def add_passkey(conn: psycopg.Connection, cred: str = "cred-AAAAAAAAAAAAAAAAAAAA", count: int = 0) -> str:
    return str(q(conn, "insert into rsi_passkeys (credential_id, public_key, sign_count) values (%s, %s, %s) returning id",
                 cred, "pk-" + "B" * 40, count))


def challenge(conn: psycopg.Connection, kind: str, ident: str, action: str, nh: str | None = None) -> dict:
    return q(conn, "select amem_rsi_web_challenge(%s, %s, %s, %s)", kind, ident, action, nh or nonce())


def decide(conn: psycopg.Connection, cid: str, nh: str | None, cred: str | None, count: int | None) -> str:
    return str(q(conn, "select amem_rsi_web_decide(%s, %s, %s, %s)", cid, nh, cred, count))


def kept_edit(conn: psycopg.Connection, status: str = "kept") -> str:
    return str(q(conn, "insert into edits (instruction_source, rule_id, status, improve_id, target_path) "
                       "values ('~/.claude/CLAUDE.md', 'web-rule', %s, 'imp-20261025-1', '~/.claude/CLAUDE.md') "
                       "returning id", status))


# --------------------------------------------------------------------------------------------- challenges


def test_challenge_refuses_early_and_binds_the_artifact(db: dict[str, str], sandbox: Path) -> None:  # noqa: F811
    pid, h, _ = staged(db["owner"], sandbox, "web-a.md")
    with psycopg.connect(db["owner"]) as conn:
        assert challenge(conn, "proposal", pid, "apply") == {"result": "not-enrolled"}       # no passkey yet
        ok = challenge(conn, "proposal", pid, "reject")                                     # reject needs none
        assert (ok["result"], ok["needs_passkey"], ok["credentials"]) == ("ok", False, [])
        add_passkey(conn)
        ap = challenge(conn, "proposal", pid, "apply")
        assert (ap["result"], ap["needs_passkey"], ap["summary"]["artifact_hash"], ap["summary"]["state"],
                ap["summary"]["target"]) == ("ok", True, h, "staged", "~/web-a.md")
        assert [c["id"] for c in ap["credentials"]] == ["cred-AAAAAAAAAAAAAAAAAAAA"]
        assert q(conn, "select artifact_hash from rsi_web_challenges where id = %s", ap["challenge_id"]) == h
        assert q(conn, "select expires_at - created_at from rsi_web_challenges where id = %s",
                 ap["challenge_id"]).total_seconds() == 120
        assert challenge(conn, "proposal", pid, "undo") == {"result": "not-promoted"}
        assert challenge(conn, "edit", pid, "apply") == {"result": "invalid"}                # edit: undo only
        assert challenge(conn, "proposal", "00000000-0000-4000-8000-000000000000", "apply") == {"result": "missing"}
        assert challenge(conn, "proposal", pid, "apply", "not-a-hash") == {"result": "invalid"}
        conn.execute("update rsi_proposals set expires_at = now() - interval '1 minute' where id = %s", (pid,))
        assert challenge(conn, "proposal", pid, "apply") == {"result": "expired"}


# --------------------------------------------------------------------------------------------- decisions


def test_web_apply_records_once_with_channel_web_and_the_passkey(db: dict[str, str], sandbox: Path) -> None:  # noqa: F811
    pid, h, _ = staged(db["owner"], sandbox, "web-b.md")
    with psycopg.connect(db["owner"]) as conn:
        kid = add_passkey(conn)
        nh = nonce()
        cid = challenge(conn, "proposal", pid, "apply", nh)["challenge_id"]
        assert decide(conn, cid, None, None, None) == "passkey-required"                  # nothing consumed
        assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", 1) == "recorded"
        assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", 2) == "already"          # second tap
        row = conn.execute("select action, source, channel, verified, passkey_id::text, artifact_hash from rsi_decisions "
                           "where proposal_id = %s", (pid,)).fetchall()
        assert row == [("apply", "web", "web", "edge-token", kid, h)]
        assert q(conn, "select status from rsi_proposals where id = %s", pid) == "approved"
        assert q(conn, "select sign_count from rsi_passkeys where id = %s", kid) == 1
        assert challenge(conn, "proposal", pid, "apply") == {"result": "not-staged"}
        assert challenge(conn, "proposal", pid, "reject") == {"result": "not-staged"}
    with psycopg.connect(db["owner"]) as conn, pytest.raises(psycopg.errors.RaiseException):   # still append-only
        conn.execute("update rsi_decisions set source = 'cli' where proposal_id = %s", (pid,))


def test_wrong_nonce_count_or_credential_burns_the_challenge(db: dict[str, str], sandbox: Path) -> None:  # noqa: F811
    pid, _, _ = staged(db["owner"], sandbox, "web-c.md")
    with psycopg.connect(db["owner"]) as conn:
        add_passkey(conn, count=5)
        nh = nonce()
        cid = challenge(conn, "proposal", pid, "apply", nh)["challenge_id"]
        assert decide(conn, cid, nonce(), "cred-AAAAAAAAAAAAAAAAAAAA", 6) == "passkey-invalid"   # signed another one
        assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", 6) == "expired"                # burned: one use
        for count in (5, 4, 0):                                                                    # cloned / replayed
            nh = nonce()
            cid = challenge(conn, "proposal", pid, "apply", nh)["challenge_id"]
            assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", count) == "passkey-invalid", count
        nh = nonce()
        cid = challenge(conn, "proposal", pid, "apply", nh)["challenge_id"]
        assert decide(conn, cid, nh, "cred-unknown-AAAAAAAAAAAA", 9) == "not-enrolled"
        conn.execute("update rsi_passkeys set revoked_at = now()")
        assert challenge(conn, "proposal", pid, "apply") == {"result": "not-enrolled"}
        assert q(conn, "select count(*) from rsi_decisions") == 0
        assert q(conn, "select status from rsi_proposals where id = %s", pid) == "staged"


def test_expired_challenge_and_moved_artifact_record_nothing(db: dict[str, str], sandbox: Path) -> None:  # noqa: F811
    pid, _, _ = staged(db["owner"], sandbox, "web-d.md")
    with psycopg.connect(db["owner"]) as conn:
        add_passkey(conn)
        nh = nonce()
        cid = challenge(conn, "proposal", pid, "apply", nh)["challenge_id"]
        conn.execute("update rsi_web_challenges set expires_at = now() - interval '1 second' where id = %s", (cid,))
        assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", 1) == "expired"
        nh = nonce()
        cid = challenge(conn, "proposal", pid, "apply", nh)["challenge_id"]
        conn.execute("update rsi_web_challenges set artifact_hash = %s where id = %s", ("e" * 64, cid))
        assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", 2) == "hash-mismatch"
        assert decide(conn, "00000000-0000-4000-8000-00000000abcd", nh, "x", 3) == "invalid"
        assert q(conn, "select count(*) from rsi_decisions") == 0


def test_reject_needs_only_the_login_and_blocks_a_later_apply(db: dict[str, str], sandbox: Path) -> None:  # noqa: F811
    pid, _, _ = staged(db["owner"], sandbox, "web-e.md")
    with psycopg.connect(db["owner"]) as conn:
        cid = challenge(conn, "proposal", pid, "reject")["challenge_id"]
        assert decide(conn, cid, None, None, None) == "recorded"
        assert decide(conn, cid, None, None, None) == "already"
        assert conn.execute("select action, channel, passkey_id from rsi_decisions where proposal_id = %s",
                            (pid,)).fetchall() == [("reject", "web", None)]
        add_passkey(conn)
        assert challenge(conn, "proposal", pid, "apply") == {"result": "not-staged"}


def test_web_undo_of_an_edit_uses_the_undo_request_path(db: dict[str, str]) -> None:
    with psycopg.connect(db["owner"]) as conn:
        add_passkey(conn)
        eid = kept_edit(conn)
        nh = nonce()
        ch = challenge(conn, "edit", eid, "undo", nh)
        assert (ch["result"], ch["needs_passkey"], ch["summary"]["artifact_hash"], ch["summary"]["state"]) == \
            ("ok", True, None, "kept")
        assert decide(conn, ch["challenge_id"], None, None, None) == "passkey-required"
        assert decide(conn, ch["challenge_id"], nh, "cred-AAAAAAAAAAAAAAAAAAAA", 1) == "recorded"
        assert conn.execute("select source, handled_at from undo_requests where edit_id = %s", (eid,)).fetchall() == \
            [("web", None)]
        nh = nonce()
        cid = challenge(conn, "edit", eid, "undo", nh)["challenge_id"]                     # pending: no second request
        assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", 2) == "already"
        conn.execute("update edits set status = 'reverted' where id = %s", (eid,))
        assert challenge(conn, "edit", eid, "undo") == {"result": "not-promoted"}
        assert challenge(conn, "edit", kept_edit(conn, "proposed"), "undo") == {"result": "not-promoted"}
        assert q(conn, "select count(*) from rsi_decisions") == 0                           # edits: no RSI row


def test_promotion_worker_promotes_a_web_apply(db: dict[str, str], sandbox: Path, tmp_path: Path,  # noqa: F811
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """decide-poll (as amem_app) and verify_decision accept a web decision exactly like a phone one."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "web-f.md").write_bytes(b"a\n")
    monkeypatch.setenv("HOME", str(home))
    script, log, mode = tmp_path / "improve", tmp_path / "improve.log", tmp_path / "mode.json"
    script.write_text(FAKE.format(py=sys.executable))
    script.chmod(0o755)
    mode.write_text(json.dumps({"promote": {"status": "promoted", "id": "prm-20261025-9"}}))
    for k, v in (("AMEM_IMPROVE_BIN", script), ("FAKE_LOG", log), ("FAKE_MODE", mode)):
        monkeypatch.setenv(k, str(v))
    pid, h, _ = staged(db["app"], sandbox, "web-f.md")
    with psycopg.connect(db["owner"]) as conn:
        add_passkey(conn)
        nh = nonce()
        cid = challenge(conn, "proposal", pid, "apply", nh)["challenge_id"]
        assert decide(conn, cid, nh, "cred-AAAAAAAAAAAAAAAAAAAA", 1) == "recorded"
    rep = AP.decide_poll(db_url=db["app"], push=False)
    assert [x["result"] for x in rep.handled] == ["promoted"]
    promote = [c for c in calls(log) if isinstance(c, list) and c[0] == "promote"]
    assert promote and promote[0][promote[0].index("--expect-hash") + 1] == h
    d = [c["decision"] for c in calls(log) if isinstance(c, dict)][0]       # the rsi-decision/1 file improve reads
    assert (d["source"], d["verified"], d["action"], d["artifact_hash"]) == ("web", "edge-token", "apply", h)


# --------------------------------------------------------------------------------------------- enrollment


def test_enroll_code_is_one_use_hashed_and_short_lived(db: dict[str, str]) -> None:
    with psycopg.connect(db["owner"]) as conn:
        old = PK.create_enroll_code(conn)
        code = PK.create_enroll_code(conn)                                                  # ends the old one
        assert len(PK.normalize_code(code)) == 24 and code.count("-") == 5
        assert q(conn, "select count(*) from rsi_passkey_enroll_codes where code_hash = %s", PK.normalize_code(code)) == 0
        start = lambda c, nh: q(conn, "select amem_rsi_web_enroll_start(%s, %s)", PK.code_hash(c), nh)  # noqa: E731
        assert start(old, nonce()) == {"result": "expired"}
        assert start("WRONG-CODE-AAAA-BBBB-CCCC-DDDD", nonce()) == {"result": "invalid"}
        nh = nonce()
        s = start(code.lower().replace("-", " "), nh)                                       # normalized
        assert (s["result"], s["exclude"]) == ("ok", [])
        fin = lambda cid, nh2, cred: q(conn, "select amem_rsi_web_enroll_finish(%s, %s, %s, %s, 0, %s, 'multiDevice', true)",  # noqa: E731
                                       cid, nh2, cred, "pk-" + "C" * 40, ["internal", "hybrid"])
        assert fin(s["challenge_id"], nonce(), "cred-NEW-AAAAAAAAAAAAAAA") == "invalid"     # other challenge
        assert fin(s["challenge_id"], nh, "cred-NEW-AAAAAAAAAAAAAAA") == "enrolled"
        assert fin(s["challenge_id"], nh, "cred-NEW2-AAAAAAAAAAAAAA") == "invalid"          # challenge used
        assert start(code, nonce()) == {"result": "invalid"}                                # code used
        assert [r["credential"] for r in PK.list_passkeys(conn)] == ["cred-NEW-AAA"]
        code2 = PK.create_enroll_code(conn)
        nh = nonce()
        s2 = start(code2, nh)
        assert s2["exclude"] == ["cred-NEW-AAAAAAAAAAAAAAA"]
        assert fin(s2["challenge_id"], nh, "cred-NEW-AAAAAAAAAAAAAAA") == "invalid"         # same credential twice
        assert q(conn, "select amem_rsi_web_decide(%s, %s, null, null)", s2["challenge_id"], nh) == "invalid"
        kid = PK.list_passkeys(conn)[0]["id"]
        assert PK.revoke_passkey(conn, "short") == 0
        assert PK.revoke_passkey(conn, str(kid)) == 1
        assert PK.list_passkeys(conn)[0]["revoked_at"] is not None


def test_passkey_commands_refuse_agents_and_list_works(db: dict[str, str]) -> None:
    env = base_env(AMEM_DB_URL=db["owner"])
    for args in (["passkey-enroll"], ["passkey-revoke", "0" * 36]):
        for extra in ({"CLAUDECODE": "1"}, {}):                                             # agent, or no TTY
            r = subprocess.run([AMEM, "rsi", *args], env={**env, **extra}, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
            assert r.returncode == 2 and "refused outside Vilmer's own terminal" in r.stderr, (args, extra)
    with psycopg.connect(db["owner"]) as conn:
        add_passkey(conn, "cred-LIST-AAAAAAAAAAAAAAA")
        assert q(conn, "select count(*) from rsi_passkey_enroll_codes") == 0
    r = subprocess.run([AMEM, "rsi", "passkey-list", "--json"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert [x["credential"] for x in json.loads(r.stdout)] == ["cred-LIST-AA"]
    assert "pk-" not in r.stdout                                                             # no keys printed


# --------------------------------------------------------------------------------------------- grants


def test_api_roles_reach_only_the_web_functions_and_golive_revokes_owner_undo(pg_url: str) -> None:
    roles = ("anon", "authenticated", "service_role")
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("drop database if exists rsi_web")
        conn.execute("create database rsi_web")
        for r in roles:
            conn.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{r}') then create role {r} nologin; end if; end $$")
    url = pg_url.rsplit("/", 1)[0] + "/rsi_web"
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("create schema auth")                                               # Supabase's auth.uid()
            conn.execute("create function auth.uid() returns uuid language sql stable as "
                         "$$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$")
            conn.execute("grant usage on schema auth to anon, authenticated, service_role")
            conn.execute("grant execute on function auth.uid() to anon, authenticated, service_role")
            for kind in ("tables", "functions", "sequences"):
                conn.execute(f"alter default privileges in schema public grant all on {kind} to anon, authenticated, service_role")
        res = subprocess.run([AMEM, "db", "migrate"], env=base_env(AMEM_DB_URL=url), capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        with psycopg.connect(url) as conn:
            assert q(conn, "select count(*) from schema_migrations where filename like '%%golive%%'") == 0   # never automatic
            for fn in WEB_FUNCS:
                for r in (*roles, "amem_app", "rsi_writer", "rsi_reader"):
                    can = q(conn, "select has_function_privilege(%s, %s, 'execute')", r, f"public.{fn}")
                    assert can == (r == "service_role"), (r, fn)
            for r in (*roles, "amem_app", "rsi_writer", "rsi_reader"):
                assert not q(conn, "select has_function_privilege(%s, %s, 'execute')", r,
                             "public.rsi_insert_decision(public.rsi_proposals,text,text,text,text,uuid)"), r
                for t in WEB_TABLES:
                    for priv in ("select", "insert", "update", "delete"):
                        assert not q(conn, "select has_table_privilege(%s, %s, %s)", r, f"public.{t}", priv), (r, t, priv)
            assert q(conn, "select has_function_privilege('authenticated', 'public.amem_request_undo_owner(uuid)', 'execute')")
        with psycopg.connect(url, autocommit=True) as conn:                                 # the go-live step
            conn.execute(GOLIVE.read_text())
        with psycopg.connect(url) as conn:
            assert not q(conn, "select has_function_privilege('authenticated', 'public.amem_request_undo_owner(uuid)', 'execute')")
            assert q(conn, "select has_function_privilege('service_role', 'public.amem_request_undo(uuid,text)', 'execute')")
            conn.execute("set role authenticated")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select public.amem_request_undo_owner('00000000-0000-4000-8000-000000000000')")
    finally:
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute("drop database if exists rsi_web with (force)")
            for r in roles:
                conn.execute(f"drop role if exists {r}")


def test_no_code_path_applies_the_golive_folder() -> None:
    from agent_memory import config
    assert config.MIGRATIONS_DIR.resolve() == (REPO / "supabase" / "migrations").resolve()
    hits = sorted(str(p.relative_to(REPO)) for d in ("src", "scripts", "tests") for p in (REPO / d).rglob("*.py")
                  if "migrations-golive" in p.read_text(errors="replace"))
    assert hits == ["tests/test_web_decide.py"], hits


# --------------------------------------------------------------------------------------------- Deno / node


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="node not installed")
def test_phone_handler_tests_still_pass_under_node() -> None:
    """handler.ts never imports the WebAuthn library, so the phone tests keep running without Deno."""
    r = subprocess.run(["node", "--test", str(REPO / "supabase" / "functions" / "amem-decide" / "handler_test.ts")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "# fail 0" in r.stdout or "fail 0" in r.stdout


needs_deno = pytest.mark.skipif(not edge_test.image_present(),
                                reason=f"Docker or the pinned Deno image {edge_test.DENO_IMAGE} is missing")


@needs_deno
def test_amem_decide_deno_tests_pass() -> None:
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "edge_test.py"), "amem-decide"],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-4000:]
    assert " 0 failed" in r.stdout


@needs_deno
def test_web_end_to_end_with_a_software_passkey(db: dict[str, str], sandbox: Path) -> None:  # noqa: F811
    apply_id, apply_h, _ = staged(db["owner"], sandbox, "web-e2e-apply.md")
    reject_id, _, _ = staged(db["owner"], sandbox, "web-e2e-reject.md")
    cross_id, _, _ = staged(db["owner"], sandbox, "web-e2e-cross.md")
    promoted_id, _, _ = staged(db["owner"], sandbox, "web-e2e-promoted.md")
    with psycopg.connect(db["owner"]) as conn:
        conn.execute("update rsi_proposals set status = 'promoted', change_id = 'prm-20261025-7' where id = %s", (promoted_id,))
        edit_id = kept_edit(conn)
        code = PK.create_enroll_code(conn)                       # what `amem rsi passkey-enroll` stores and prints
    port = db["owner"].rsplit(":", 1)[1].split("/")[0]
    env = {"AMEM_E2E_PG_URL": f"postgres://postgres:postgres@host.docker.internal:{port}/postgres",
           "AMEM_E2E_CODE": code, "AMEM_E2E_APPLY": apply_id, "AMEM_E2E_REJECT": reject_id,
           "AMEM_E2E_CROSS": cross_id, "AMEM_E2E_PROMOTED": promoted_id, "AMEM_E2E_EDIT": edit_id}
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "edge_test.py"), "amem-decide", "--e2e",
                        *[f"--env={k}={v}" for k, v in env.items()]], capture_output=True, text=True, timeout=600)
    steps = [json.loads(line.split("E2E ", 1)[1]) for line in r.stdout.splitlines() if "E2E {" in line]
    print("\n".join(json.dumps(s) for s in steps))
    assert r.returncode == 0, r.stdout[-6000:] + r.stderr[-3000:]
    assert " 0 failed" in r.stdout and steps[-1]["step"] == "done"
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select p.title, d.action, d.channel, d.verified, d.passkey_id is not null "
                            "from rsi_decisions d join rsi_proposals p on p.id = d.proposal_id order by d.decided_at").fetchall()
        print(rows)
        assert rows == [("Demo web-e2e-apply.md", "apply", "web", "edge-token", True),
                        ("Demo web-e2e-reject.md", "reject", "web", "edge-token", False),
                        ("Demo web-e2e-promoted.md", "undo", "web", "edge-token", True)]
        assert q(conn, "select artifact_hash from rsi_decisions where proposal_id = %s", apply_id) == apply_h
        assert conn.execute("select status from rsi_proposals where id = any(%s) order by title",
                            ([apply_id, cross_id, reject_id],)).fetchall() == [("approved",), ("staged",), ("rejected",)]
        assert conn.execute("select source, handled_at from undo_requests where edit_id = %s", (edit_id,)).fetchall() == \
            [("web", None)]
        assert conn.execute("select count(*), max(sign_count), bool_and(revoked_at is null) from rsi_passkeys").fetchone() == \
            (1, steps[-1]["passkey_count"], True)
        assert q(conn, "select used_at is not null from rsi_passkey_enroll_codes where code_hash = %s", PK.code_hash(code))
