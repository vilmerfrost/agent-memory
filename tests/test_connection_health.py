"""The service-only readiness RPC returns booleans and never mutates decision state."""
import uuid
import psycopg


def test_readiness_is_read_only_and_not_executable_by_app_roles(pg_url):
    with psycopg.connect(pg_url) as conn:
        tables = ("rsi_passkeys", "rsi_web_challenges", "rsi_decisions")
        before = [conn.execute(f"select count(*) from public.{name}").fetchone()[0] for name in tables]
        result = conn.execute("select public.amem_rsi_web_health()").fetchone()[0]
        assert set(result) == {"result", "passkey_enrolled"}
        assert result["result"] == "ok"
        assert isinstance(result["passkey_enrolled"], bool)
        assert before == [conn.execute(f"select count(*) from public.{name}").fetchone()[0] for name in tables]
        for role in ("amem_app", "rsi_reader", "rsi_writer"):
            assert not conn.execute("select has_function_privilege(%s,'public.amem_rsi_web_health()','execute')", (role,)).fetchone()[0]
        conn.execute("insert into public.rsi_passkeys(credential_id,public_key) values (%s,%s)", (uuid.uuid4().hex, "b" * 32))
        assert conn.execute("select public.amem_rsi_web_health()").fetchone()[0]["passkey_enrolled"] is True
        conn.rollback()
