-- One bounded, nonce-matched ClickHouse request. No user SQL or model prompts.
create or replace function public.amem_connection_probe_start() returns jsonb
language plpgsql security definer set search_path=pg_catalog,public,pg_temp as $$
declare r public.rsi_checkpoints%rowtype; nonce uuid; requested timestamptz;
begin
  perform pg_advisory_xact_lock(723914210);
  select * into r from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live' for update;
  if found and r.status='running' and r.started_at > now()-interval '90 seconds' then
    return jsonb_build_object('request_id',r.meta->>'request_id','requested_at',r.started_at);
  end if;
  nonce:=gen_random_uuid(); requested:=clock_timestamp();
  insert into public.rsi_checkpoints(job,run_key,status,started_at,finished_at,meta)
    values('connections:clickhouse-probe','live','running',requested,null,jsonb_build_object('request_id',nonce))
    on conflict(job,run_key) do update set status='running',started_at=excluded.started_at,finished_at=null,meta=excluded.meta;
  return jsonb_build_object('request_id',nonce,'requested_at',requested);
end $$;

create or replace function public.amem_connection_probe_result(p_request_id uuid) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog,public,pg_temp as $$
declare r public.rsi_checkpoints%rowtype; pending boolean:=false; state text:='unknown'; msg text:='No matching live query result is available.'; checked timestamptz:=now(); duration integer:=0;
begin
  select * into r from public.rsi_checkpoints where job='connections:clickhouse-probe' and run_key='live' and meta->>'request_id'=p_request_id::text;
  if found then
    if r.status='running' and r.started_at > now()-interval '90 seconds' then
      pending:=true; msg:='Waiting for the Mac to execute a fresh authenticated query.';
    elsif r.status='running' then
      msg:='The Mac did not complete this query within 90 seconds. Check its connection.';
    elsif r.finished_at is not null and r.finished_at >= r.started_at and r.finished_at > now()-interval '90 seconds' then
      checked:=r.finished_at;
      duration:=least(120000,greatest(0,coalesce((r.meta->>'latency_ms')::integer,0)));
      if r.status='done' and r.meta->'healthy'='true'::jsonb then
        state:='healthy'; msg:='A new authenticated SELECT 1 returned 1 from your Mac.';
      else
        state:='unhealthy'; msg:='The Mac could not complete its authenticated ClickHouse query.';
      end if;
    else
      state:='stale'; checked:=coalesce(r.finished_at,r.started_at); msg:='The live query result is over 90 seconds old.';
    end if;
  end if;
  return jsonb_build_object('request_id',p_request_id,'pending',pending,'check',jsonb_build_object('id','clickhouse','status',state,'checked_at',checked,'latency_ms',duration,'message',msg));
end $$;

create or replace function public.amem_connection_probe_pending() returns uuid
language sql stable security definer set search_path=pg_catalog,public,pg_temp as $$
  select (meta->>'request_id')::uuid from public.rsi_checkpoints
  where job='connections:clickhouse-probe' and run_key='live' and status='running'
    and started_at > now()-interval '90 seconds' limit 1;
$$;

create or replace function public.amem_connection_probe_finish(p_request_id uuid,p_healthy boolean,p_latency_ms integer,p_failure_code text) returns boolean
language plpgsql security definer set search_path=pg_catalog,public,pg_temp as $$
declare changed integer;
begin
  if p_request_id is null or p_healthy is null or p_latency_ms is null or p_latency_ms not between 0 and 120000
    or p_failure_code is null or p_failure_code not in ('ok','auth','http','connection','configuration','unexpected','unknown') then return false; end if;
  update public.rsi_checkpoints set status=case when p_healthy then 'done' else 'failed' end,
    finished_at=clock_timestamp(),meta=meta||jsonb_build_object('healthy',p_healthy,'latency_ms',p_latency_ms,'failure_code',p_failure_code)
    where job='connections:clickhouse-probe' and run_key='live' and status='running'
      and meta->>'request_id'=p_request_id::text and started_at > now()-interval '90 seconds';
  get diagnostics changed=row_count;
  return changed=1;
end $$;

revoke all on function public.amem_connection_probe_pending() from public;
revoke all on function public.amem_connection_probe_finish(uuid,boolean,integer,text) from public;
revoke all on function public.amem_connection_probe_start() from public;
revoke all on function public.amem_connection_probe_result(uuid) from public;
do $$ declare r text; begin
  foreach r in array array['anon','authenticated','amem_app','rsi_reader','rsi_writer','service_role'] loop
    if exists(select 1 from pg_roles where rolname=r) then
      execute format('revoke all on function public.amem_connection_probe_start() from %I',r);
      execute format('revoke all on function public.amem_connection_probe_result(uuid) from %I',r);
      execute format('revoke all on function public.amem_connection_probe_pending() from %I',r);
      execute format('revoke all on function public.amem_connection_probe_finish(uuid,boolean,integer,text) from %I',r);
    end if;
  end loop;
  if exists(select 1 from pg_roles where rolname='service_role') then
    grant execute on function public.amem_connection_probe_start() to service_role;
    grant execute on function public.amem_connection_probe_result(uuid) to service_role;
  end if;
  if exists(select 1 from pg_roles where rolname='amem_app') then
    grant execute on function public.amem_connection_probe_pending() to amem_app;
    grant execute on function public.amem_connection_probe_finish(uuid,boolean,integer,text) to amem_app;
  end if;
end $$;
notify pgrst,'reload schema';
