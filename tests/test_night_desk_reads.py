"""Read receipts sync through owner-only RLS without changing night reports."""
from pathlib import Path

import psycopg
import pytest

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase/migrations"
OWNER = "00000000-0000-0000-0000-000000006101"
OTHER = "00000000-0000-0000-0000-000000006102"


def identity(c, uid=OWNER, role="authenticated"):
    c.execute("reset role")
    c.execute("select set_config('request.jwt.claim.sub',%s,true)", (uid,))
    c.execute(f"set local role {role}")


@pytest.fixture
def receipts(pg_url):
    with psycopg.connect(pg_url) as c:
        for role in ("authenticated", "anon"):
            c.execute(f"do $$ begin if not exists (select 1 from pg_roles where rolname='{role}') then create role {role}; end if; end $$")
        c.execute("create schema if not exists auth")
        c.execute("create or replace function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$")
        c.execute("grant usage on schema auth,public to authenticated,anon")
        c.execute(next(MIGRATIONS.glob("*_night_desk.sql")).read_text())
        # pg_url runs migrations without hosted Auth roles; install the policies
        # with Auth present, just as on Supabase, inside this rolled-back fixture.
        c.execute("drop table public.night_reads")
        c.execute("drop function public.amem_mark_nights_read(text[])")
        c.execute(next(MIGRATIONS.glob("*_night_desk_reads.sql")).read_text())
        c.execute("insert into amem_private.dashboard_owner values (true,%s) on conflict (singleton) do update set uid=excluded.uid", (OWNER,))
        for night in ("2026-10-24", "2026-10-25"):
            c.execute("insert into rsi_nights(night,status,headline,brief,report,costs,checkpoint) values (%s,'partial','Original report','{}','{}','{}','{}')", (night,))
        yield c
        c.rollback()


def test_explicit_read_unread_and_idempotence(receipts):
    c = receipts
    identity(c)
    assert c.execute("select * from night_reads").fetchall() == []
    assert c.execute("select amem_mark_nights_read(array['2026-10-24'])").fetchone() == (1,)
    at = c.execute("select read_at from night_reads where night='2026-10-24'").fetchone()
    assert c.execute("select amem_mark_nights_read(array['2026-10-24'])").fetchone() == (0,)
    assert c.execute("select read_at from night_reads where night='2026-10-24'").fetchone() == at
    c.execute("delete from night_reads where night='2026-10-24'")
    assert c.execute("select * from night_reads").fetchall() == []
    assert c.execute("select status,headline from rsi_nights order by night").fetchall() == [("partial", "Original report")] * 2


def test_bulk_covers_backlog_but_not_future_nights(receipts):
    c = receipts
    identity(c)
    assert c.execute("select amem_mark_nights_read()").fetchone() == (2,)
    assert c.execute("select amem_mark_nights_read()").fetchone() == (0,)
    assert c.execute("select amem_mark_nights_read(array[]::text[])").fetchone() == (0,)
    c.execute("reset role")
    c.execute("insert into rsi_nights(night,status,headline,brief,report,costs,checkpoint) values ('2026-10-26','ok','New','{}','{}','{}','{}')")
    identity(c)
    assert c.execute("select night from night_reads order by night").fetchall() == [("2026-10-24",), ("2026-10-25",)]
    assert c.execute("select amem_mark_nights_read(array['2026-10-26','missing'])").fetchone() == (1,)


@pytest.mark.parametrize("uid", [OTHER, ""])
def test_nonowner_cannot_read_insert_or_delete_receipts(receipts, uid):
    c = receipts
    identity(c)
    c.execute("select amem_mark_nights_read(array['2026-10-24'])")
    identity(c, uid)
    assert c.execute("select * from night_reads").fetchall() == []
    assert c.execute("select amem_mark_nights_read()").fetchone() == (0,)
    assert c.execute("delete from night_reads returning night").fetchall() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with c.transaction():
            c.execute("insert into night_reads(night) values ('2026-10-25')")
    identity(c)
    assert c.execute("select night from night_reads").fetchall() == [("2026-10-24",)]


def test_anon_denied_and_owner_cannot_write_reports_or_update_receipts(receipts):
    c = receipts
    identity(c, "", "anon")
    for sql in ("select * from night_reads", "select amem_mark_nights_read()"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction():
                c.execute(sql)
    identity(c)
    for sql in ("update night_reads set read_at=now()", "update rsi_nights set status='ok'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction():
                c.execute(sql)


def test_receipts_fail_closed_without_configured_owner(receipts):
    c = receipts
    c.execute("delete from amem_private.dashboard_owner")
    identity(c)
    assert c.execute("select amem_mark_nights_read()").fetchone() == (0,)
    assert c.execute("select * from night_reads").fetchall() == []
