-- Expose readiness booleans only. The edge must authenticate the owner before calling.
create or replace function public.amem_rsi_web_health() returns jsonb
language plpgsql stable security definer set search_path = pg_catalog, public, pg_temp as $$
begin
  perform 1 from public.rsi_web_challenges limit 0;
  perform 1 from public.rsi_decisions limit 0;
  return jsonb_build_object('result', 'ok', 'passkey_enrolled',
    exists(select 1 from public.rsi_passkeys where revoked_at is null));
end $$;
revoke all on function public.amem_rsi_web_health() from public;
do $$
declare r text;
begin
  foreach r in array array['anon', 'authenticated', 'amem_app', 'rsi_reader', 'rsi_writer'] loop
    if exists(select 1 from pg_roles where rolname=r) then
      execute format('revoke all on function public.amem_rsi_web_health() from %I',r);
    end if;
  end loop;
  if exists(select 1 from pg_roles where rolname='service_role') then
    grant execute on function public.amem_rsi_web_health() to service_role;
  end if;
end $$;
notify pgrst, 'reload schema';
