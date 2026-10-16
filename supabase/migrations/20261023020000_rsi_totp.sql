-- Phone Apply / Undo need a TOTP code (docs/rsi/least-privilege.md, A3).
-- The edge function amem-decide verifies the 6-digit code against RSI_TOTP_SECRET (a function secret,
-- never on the Mac or in the database) and passes the matched 30-second step here. Each step is used once.
-- Reject needs no code: it can only stop a change.
-- After this migration service_role can no longer call the token-only function, so the edge function
-- cannot skip the code for Apply or Undo.

create table if not exists public.rsi_totp_used (
    step        bigint primary key,
    proposal_id uuid not null,
    action      text not null,
    used_at     timestamptz not null default now()
);
alter table public.rsi_totp_used enable row level security;

-- Wrong codes, per proposal. Five wrong codes lock the proposal's phone path (brute force: a token
-- holder could otherwise try all 10^6 codes against the public endpoint).
create table if not exists public.rsi_totp_failures (
    id          uuid primary key default gen_random_uuid(),
    proposal_id uuid not null,
    at          timestamptz not null default now()
);
create index if not exists rsi_totp_failures_proposal_idx on public.rsi_totp_failures (proposal_id);
alter table public.rsi_totp_failures enable row level security;

create or replace function public.amem_rsi_phone_bad_code(p_proposal uuid) returns integer
language plpgsql security definer set search_path = public, pg_temp as $$
declare n integer;
begin
  insert into public.rsi_totp_failures (proposal_id) values (p_proposal);
  select count(*) into n from public.rsi_totp_failures where proposal_id = p_proposal;
  return n;
end $$;

create or replace function public.amem_rsi_record_phone_decision(p_proposal uuid, p_action text, p_artifact_hash text,
                                                                 p_token text, p_totp_step bigint) returns text
language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if (select count(*) from public.rsi_totp_failures where proposal_id = p_proposal) >= 5 then
    return 'locked';
  end if;
  if p_action in ('apply', 'undo') then
    if p_totp_step is null or p_totp_step <= 0 then
      return 'totp-required';
    end if;
    begin
      insert into public.rsi_totp_used (step, proposal_id, action) values (p_totp_step, p_proposal, p_action);
    exception when unique_violation then
      return 'totp-replayed';
    end;
  end if;
  return public.amem_rsi_record_decision(p_proposal, p_action, p_artifact_hash, p_token);
end $$;

revoke all on table public.rsi_totp_used, public.rsi_totp_failures from public;
revoke all on function public.amem_rsi_record_phone_decision(uuid, text, text, text, bigint) from public;
revoke all on function public.amem_rsi_phone_bad_code(uuid) from public;
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'service_role', 'amem_app', 'rsi_reader', 'rsi_writer'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('revoke all on table public.rsi_totp_used, public.rsi_totp_failures from %I', r);
      execute format('revoke all on function public.amem_rsi_phone_bad_code(uuid) from %I', r);
      execute format('revoke all on function public.amem_rsi_record_phone_decision(uuid, text, text, text, bigint) from %I', r);
      execute format('revoke all on function public.amem_rsi_record_decision(uuid, text, text, text) from %I', r);
    end if;
  end loop;
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.amem_rsi_record_phone_decision(uuid, text, text, text, bigint) to service_role;
    grant execute on function public.amem_rsi_phone_bad_code(uuid) to service_role;
  end if;
end $$;

notify pgrst, 'reload schema';
