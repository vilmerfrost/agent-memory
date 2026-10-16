-- Reverse 20261023020000_rsi_totp.sql and 20261023010000_amem_app_role.sql (Mission 003.5, Phase E).
-- Manual only; never in migrations/. Afterwards the database is in the state 20261022010000_rsi_core.sql
-- left it (docs/rsi/verify_rsi_core.sql passes again).
-- First point AMEM_DB_URL back at the postgres connection string (`amem admin restore-credentials`):
-- amem_app stops working here.
begin;

-- TOTP (20261023020000)
drop function if exists public.amem_rsi_record_phone_decision(uuid, text, text, text, bigint);
drop function if exists public.amem_rsi_phone_bad_code(uuid);
drop table if exists public.rsi_totp_used, public.rsi_totp_failures;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.amem_rsi_record_decision(uuid, text, text, text) to service_role;
  end if;
end $$;

-- amem_app (20261023010000)
drop function if exists public.amem_rsi_arm_token(uuid, text, text, interval);
drop policy if exists rsi_write_tok_sel on public.rsi_decision_tokens;
revoke select (proposal_id, action, expires_at, used_at) on public.rsi_decision_tokens from rsi_writer;
grant insert on public.rsi_decision_tokens to rsi_writer;
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'public' loop
    execute format('drop policy if exists amem_app_all on public.%I', t);
  end loop;
end $$;
drop policy if exists amem_app_read on public.schema_migrations;
delete from public.schema_migrations
 where filename in ('20261023020000_rsi_totp.sql', '20261023010000_amem_app_role.sql');

-- The role is cluster-wide: revoke everything here, then drop it unless another database still uses it.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'amem_app') then
    execute 'revoke all on all tables in schema public from amem_app';
    execute 'revoke all on all functions in schema public from amem_app';
    execute 'revoke all on schema public from amem_app';
    execute 'revoke rsi_writer from amem_app';
    begin
      execute 'drop role amem_app';
    exception when dependent_objects_still_exist then
      raise notice 'kept role amem_app: still used in another database';
    end;
  end if;
end $$;

commit;
