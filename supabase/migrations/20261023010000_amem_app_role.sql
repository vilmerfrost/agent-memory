-- amem_app: the login role for every routine and the RSI promotion worker (docs/rsi/least-privilege.md, A1).
-- It can do what the routines do (DML on the existing tables, the rsi_writer rights) and nothing that
-- creates a human decision: no insert on rsi_decisions, no execute on either decision function, no DDL.
-- No password here. `amem admin split-credentials` sets one; until then the role cannot log in.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'amem_app') then
    create role amem_app login;
  end if;
end $$;

grant rsi_writer to amem_app;
grant usage on schema public to amem_app;

-- Every existing non-RSI table: full DML plus an RLS policy (RLS is on and these tables have no other
-- policy, so without one amem_app would see nothing). schema_migrations: read only.
do $$
declare t text;
begin
  for t in select tablename from pg_tables
           where schemaname = 'public' and tablename not like 'rsi\_%' and tablename <> 'schema_migrations' loop
    execute format('grant select, insert, update, delete on public.%I to amem_app', t);
    execute format('drop policy if exists amem_app_all on public.%I', t);
    execute format('create policy amem_app_all on public.%I for all to amem_app using (true) with check (true)', t);
  end loop;
end $$;
-- Views (session_groups is security_invoker: the table policies above still apply).
do $$
declare v text;
begin
  for v in select viewname from pg_views where schemaname = 'public' loop
    execute format('grant select on public.%I to amem_app', v);
  end loop;
end $$;
grant select on public.schema_migrations to amem_app;
drop policy if exists amem_app_read on public.schema_migrations;
create policy amem_app_read on public.schema_migrations for select to amem_app using (true);

-- The terminal undo of a legacy edit (amem edits undo) calls this invoker-rights function.
grant execute on function public.amem_request_undo(uuid, text) to amem_app;

-- Decision tokens: the push arms them through one narrow function (insert, or re-arm while unused) instead of
-- table writes; ON CONFLICT DO UPDATE would need SELECT on token_hash. rsi_writer reads only the columns the
-- push checks, never token_hash, and cannot write the table directly.
create or replace function public.amem_rsi_arm_token(p_proposal uuid, p_action text, p_token_hash text, p_ttl interval)
returns boolean
language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if p_action not in ('apply', 'reject', 'undo') or p_token_hash !~ '^[0-9a-f]{64}$'
     or p_ttl is null or p_ttl > interval '7 days' then
    raise exception 'amem_rsi_arm_token: invalid arguments';
  end if;
  insert into public.rsi_decision_tokens (proposal_id, action, token_hash, expires_at)
  values (p_proposal, p_action, p_token_hash, now() + p_ttl)
  on conflict (proposal_id, action) do update set token_hash = excluded.token_hash, expires_at = excluded.expires_at
  where public.rsi_decision_tokens.used_at is null;
  return found;
end $$;
revoke all on function public.amem_rsi_arm_token(uuid, text, text, interval) from public;
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'service_role'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on function public.amem_rsi_arm_token(uuid, text, text, interval) from %I', r);
    end if;
  end loop;
end $$;
grant execute on function public.amem_rsi_arm_token(uuid, text, text, interval) to rsi_writer;
revoke insert, update, delete on public.rsi_decision_tokens from rsi_writer;
grant select (proposal_id, action, expires_at, used_at) on public.rsi_decision_tokens to rsi_writer;
drop policy if exists rsi_write_tok_sel on public.rsi_decision_tokens;
create policy rsi_write_tok_sel on public.rsi_decision_tokens for select to rsi_writer using (true);

-- Belt and braces: amem_app never gets a decision path, even if a later grant to PUBLIC slips in.
revoke all on function public.amem_rsi_record_decision(uuid, text, text, text) from amem_app;
revoke all on function public.amem_rsi_record_cli_decision(uuid, text, text, text) from amem_app;
revoke all on function public.rsi_insert_decision(public.rsi_proposals, text, text, text, text) from amem_app;
revoke insert, update, delete, truncate on public.rsi_decisions from amem_app;

notify pgrst, 'reload schema';
